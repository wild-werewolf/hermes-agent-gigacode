"""Run admission queue, durable request identity (gateway/cron) and the deterministic context pack."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agent.gigacode import prompt_pack, request_identity
from agent.gigacode.errors import GigacodeError
from agent.gigacode.journal import cron_request_id
from agent.gigacode.scheduler import RunScheduler


def _hold(scheduler, key, release, entered, limit=1):
    with scheduler.slot(session_key=key, limit=limit, should_abort=lambda: False, deadline=time.monotonic() + 30):
        entered.set()
        release.wait(10)


def test_queue_overflow_is_runtime_busy():
    scheduler = RunScheduler(queue_limit=1)
    release, entered = threading.Event(), threading.Event()
    threading.Thread(target=_hold, args=(scheduler, "a", release, entered), daemon=True).start()
    entered.wait(5)
    waiter = threading.Thread(target=_hold, args=(scheduler, "b", threading.Event(), threading.Event()), daemon=True)
    waiter.start()
    deadline = time.monotonic() + 5
    while scheduler.waiting < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    with pytest.raises(GigacodeError) as exc:
        with scheduler.slot(session_key="c", limit=1, should_abort=lambda: False, deadline=time.monotonic() + 5):
            pass
    assert exc.value.kind == "runtime_busy"
    release.set()


def test_one_session_runs_sequentially_even_with_spare_capacity():
    scheduler = RunScheduler()
    release, entered = threading.Event(), threading.Event()
    threading.Thread(target=_hold, args=(scheduler, "same", release, entered, 4), daemon=True).start()
    entered.wait(5)
    second_entered = threading.Event()
    threading.Thread(target=_hold, args=(scheduler, "same", threading.Event(), second_entered, 4),
                     daemon=True).start()
    assert not second_entered.wait(0.5)
    release.set()
    assert second_entered.wait(5)


def test_waiting_request_honours_cancellation():
    scheduler = RunScheduler()
    release, entered = threading.Event(), threading.Event()
    threading.Thread(target=_hold, args=(scheduler, "s", release, entered), daemon=True).start()
    entered.wait(5)
    with pytest.raises(GigacodeError) as exc:
        with scheduler.slot(session_key="s", limit=1, should_abort=lambda: True, deadline=time.monotonic() + 5):
            pass
    assert exc.value.kind == "cancelled"
    release.set()


def test_gateway_identity_uses_bot_and_update_id():
    event = SimpleNamespace(platform_update_id=555, source=SimpleNamespace(platform=SimpleNamespace(value="telegram")))
    request_identity.bind_gateway_request(event, SimpleNamespace(_bot=SimpleNamespace(id=4242)))
    identity = request_identity.resolve(SimpleNamespace(platform="telegram", _user_id="7"))
    assert (identity.channel, identity.user_id, identity.request_id) == ("telegram:4242", "telegram:7", "555")
    request_identity.bind_gateway_request(SimpleNamespace(platform_update_id=None, source=None), None)
    fresh = request_identity.resolve(SimpleNamespace(platform="cli", _user_id=None))
    assert fresh.request_id.startswith("turn:") and fresh.channel == "cli:local"


def _cron(monkeypatch, origin, scheduled="2026-10-09T08:00:00+00:00", execution_id="ex1"):
    from cron import execution_identity

    execution = execution_identity.CronExecution(job_id="job1", job_name="n", execution_id=execution_id,
                                                 source="builtin", scheduled_instant=scheduled, started_at=None,
                                                 profile="default")
    monkeypatch.setattr(execution_identity, "current_cron_execution", lambda: execution)
    monkeypatch.setattr("cron.jobs.get_job", lambda job_id: {"id": job_id, "origin": origin})


def test_cron_identity_is_stable_and_owned(monkeypatch):
    _cron(monkeypatch, {"platform": "telegram", "user_id": "7", "chat_id": "42"})
    identity = request_identity.resolve(SimpleNamespace(platform="cron"))
    assert identity.channel == "cron" and identity.user_id == "telegram:7"
    assert identity.request_id == cron_request_id("job1", "2026-10-09T08:00:00+00:00")
    assert identity.source == "cron_job:job1"


def test_manual_cron_fire_reuses_its_durable_execution_id(monkeypatch):
    _cron(monkeypatch, {"platform": "telegram", "user_id": "7"}, scheduled=None, execution_id="exec-42")
    assert request_identity.resolve(SimpleNamespace(platform="cron")).request_id == "manual:exec-42"


def test_legacy_cron_job_without_provenance_is_refused(monkeypatch):
    _cron(monkeypatch, None)
    with pytest.raises(LookupError):
        request_identity.resolve(SimpleNamespace(platform="cron"))


def _history(turns):
    rows = []
    for n in range(turns):
        rows += [{"role": "user", "content": f"q{n}"},
                 {"role": "assistant", "content": "", "tool_calls": [{"id": f"t{n}", "function": {"name": "x",
                                                                                                   "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": f"t{n}", "content": "r" * 200},
                 {"role": "assistant", "content": f"a{n}"}]
    return rows


def test_pack_drops_only_whole_old_turns_and_says_so():
    full = prompt_pack.build(system_instructions="S", session_data={}, history=_history(10), request="now",
                             budget_tokens=100000)
    assert full.turns_dropped == 0 and '"history_truncated": false' in full.text
    small = prompt_pack.build(system_instructions="S", session_data={}, history=_history(10), request="now",
                              budget_tokens=full.size_tokens - 200)
    assert 0 < small.turns_dropped and small.size_tokens <= full.size_tokens - 200
    assert prompt_pack.TRUNCATION_NOTICE in small.text
    history = small.text.split("CONVERSATION HISTORY\n")[1].split("\n\nCURRENT USER REQUEST")[0]
    kept = [line for line in history.splitlines() if line]
    assert '"q9"' in kept[-1] and '"q0"' not in history
    for line in kept:  # every kept turn still pairs its tool call with the result
        assert ('"tool_calls"' in line) == ('"tool_call_id"' in line)


def test_mandatory_part_over_budget_is_context_too_large():
    with pytest.raises(GigacodeError) as exc:
        prompt_pack.build(system_instructions="S" * 5000, session_data={}, history=[], request="q",
                          budget_tokens=1024)
    assert exc.value.kind == "context_too_large"


def test_pack_counts_utf8_bytes_not_characters():
    assert prompt_pack.estimate_tokens("ж" * 10) == 20
