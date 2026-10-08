"""Durable journal: request keys, restart transitions, one-shot operator grants."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from agent.gigacode.journal import GigacodeJournal, JournalError, canonical_request_key, cron_request_id

SCOPE = dict(profile_id="default", channel="telegram:42", user_id="telegram:7")


@pytest.fixture
def journal(tmp_path):
    return GigacodeJournal(tmp_path / "journal.db")


def _admit(journal, request_id, session="s1"):
    return journal.admit(**SCOPE, request_id=request_id, session_id=session, source="owner_private")


def test_repeated_request_key_returns_the_existing_run(journal):
    first = _admit(journal, "update-100")
    again = _admit(journal, "update-100")
    assert not first.duplicate and again.duplicate and again.run_id == first.run_id
    assert again.row["request_key"] == canonical_request_key("default", "telegram:42", "telegram:7", "update-100")


def test_cron_request_id_uses_the_planned_fire_time():
    a = cron_request_id("job1", "2026-10-09T08:00:00Z")
    assert a == cron_request_id("job1", "2026-10-09T08:00:00Z") != cron_request_id("job1", "2026-10-09T09:00:00Z")
    assert len(a) == 64


def _dead_owner(journal, run_id):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    journal.update(run_id, owner_pid=proc.pid, owner_create=1.0)


def test_restart_turns_running_into_recovery_and_queued_into_failed(journal):
    running, queued, live = _admit(journal, "r"), _admit(journal, "q"), _admit(journal, "live")
    journal.mark_running(running.run_id, {"driver": "direct"})
    _dead_owner(journal, running.run_id)
    _dead_owner(journal, queued.run_id)
    assert set(journal.recover_orphans()) == {running.run_id, queued.run_id}
    assert journal.run(running.run_id)["state"] == "recovery_required"
    failed = journal.run(queued.run_id)
    assert failed["state"] == "failed" and failed["error_kind"] == "restart_before_start"
    assert journal.run(live.run_id)["state"] == "queued"  # our own live process: untouched
    assert _admit(journal, "q").row["state"] == "failed"  # a repeat reports the stored status
    assert journal.session_blocker("s1") == running.run_id


def _issue(journal, **kw):
    base = dict(**SCOPE, wire_tool="hermes_memory", actions=["add"], ttl_seconds=60, operator="op")
    return journal.issue_grant(**{**base, **kw})


def test_request_key_grant_matches_exactly_one_request(journal):
    target, other = _admit(journal, "u1"), _admit(journal, "u2")
    grant = _issue(journal, request_key=target.row["request_key"])
    assert journal.consume_grants(other.run_id) == []
    [won] = journal.consume_grants(target.run_id)
    assert won["grant_id"] == grant["grant_id"]
    assert journal.consume_grants(target.run_id) == []  # consumed once


def test_next_request_skips_previously_queued_and_binds_the_first_new_one(journal):
    earlier = _admit(journal, "old")
    time.sleep(0.01)
    grant = _issue(journal, next_request=True)
    time.sleep(0.01)
    first, second = _admit(journal, "new1"), _admit(journal, "new2")
    assert journal.consume_grants(earlier.run_id) == []
    assert journal.consume_grants(second.run_id) == []  # belongs to the FIRST new request
    assert [g["grant_id"] for g in journal.consume_grants(first.run_id)] == [grant["grant_id"]]


def test_competing_dequeues_have_a_single_winner(journal, tmp_path):
    grant = _issue(journal, next_request=True)
    time.sleep(0.01)
    run = _admit(journal, "race")
    results = []
    barrier = threading.Barrier(4)

    def consume():
        barrier.wait()
        results.append(GigacodeJournal(tmp_path / "journal.db").consume_grants(run.run_id))
    threads = [threading.Thread(target=consume) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(len(r) for r in results) == 1 and grant["grant_id"] in str(results)


def test_expiry_and_revoke(journal):
    run = _admit(journal, "e1")
    _issue(journal, request_key=run.row["request_key"], ttl_seconds=1)
    time.sleep(1.1)
    assert journal.consume_grants(run.run_id) == []
    run2 = _admit(journal, "e2")
    grant = _issue(journal, request_key=run2.row["request_key"])
    journal.revoke_grant(grant["grant_id"])
    assert journal.consume_grants(run2.run_id) == []
    run3 = _admit(journal, "e3")
    grant3 = _issue(journal, request_key=run3.row["request_key"])
    journal.consume_grants(run3.run_id)
    assert journal.grant_usable(grant3["grant_id"], run3.run_id)
    journal.revoke_grant(grant3["grant_id"])
    assert not journal.grant_usable(grant3["grant_id"], run3.run_id)


def test_issue_refusals(journal):
    with pytest.raises(JournalError):
        _issue(journal, next_request=True, user_id="*")
    with pytest.raises(JournalError):
        _issue(journal)  # no selector
    _issue(journal, next_request=True)
    with pytest.raises(JournalError, match="already exists"):
        _issue(journal, next_request=True)  # one active --next-request per scope/tool
    running = _admit(journal, "busy")
    journal.mark_running(running.run_id, {})
    with pytest.raises(JournalError, match="already running"):
        _issue(journal, request_key=running.row["request_key"])
    foreign = canonical_request_key("default", "telegram:42", "telegram:8", "x")
    with pytest.raises(JournalError, match="scope"):
        _issue(journal, request_key=foreign)
