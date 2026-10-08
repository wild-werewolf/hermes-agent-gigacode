"""End-to-end ``api_mode: gigacode_cli`` turns: real AIAgent + SessionDB + bridge + simulated CLI process.

The simulator is injected through ``agent._gigacode_deps``; the manifest verifier is replaced only
where noted (``test_missing_manifest_never_starts_the_cli`` uses the real one).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from agent.gigacode import request_identity
from agent.gigacode.config import parse_settings
from agent.gigacode.journal import GigacodeJournal
from agent.gigacode.manifest import VerifiedManifest
from agent.gigacode.scheduler import RunScheduler
from agent.gigacode_runtime import GigacodeRuntimeDeps
from tests.agent.gigacode.conftest import direct_driver, success_steps

pytestmark = pytest.mark.platforms("linux")

MANIFEST = VerifiedManifest({"cli": {"version": "1.2.3"}, "rootfs": {}, "model": {"id": "fixture-model"},
                             "mcp": {"qualified_tool_prefix": "mcp__hermes__"}})


def _settings(**over):
    section = {"executable": "/opt/gigacode/bin/gigacode", "verified_version": "1.2.3",
               "prompt_budget_tokens": 400000, "runtime_rootfs": "/srv/rootfs", "bwrap_executable": "/usr/bin/bwrap",
               "verification_manifest": "/srv/manifest.json", "wall_timeout_seconds": 60,
               "termination_grace_seconds": 2, "skills_allowlist": ["demo-skill"], **over}
    return parse_settings(section)


class Harness:
    def __init__(self, tmp_path: Path, scenario):
        from hermes_state import SessionDB

        self.tmp, self.scenario = tmp_path, scenario
        self.db = SessionDB(db_path=tmp_path / "state.db")
        self.journal_path = tmp_path / "journal.db"
        self.records: list[Path] = []
        self.verify = lambda settings: MANIFEST
        self.settings = _settings()
        self.scheduler = RunScheduler()

    def agent(self, session_id="sess-e2e"):
        from run_agent import AIAgent

        agent = AIAgent(model="fixture-model", provider="gigacode-cli", api_mode="gigacode_cli", api_key="",
                        base_url="gigacode://local", quiet_mode=True, skip_context_files=True, skip_memory=True,
                        session_db=self.db, session_id=session_id, platform="telegram", user_id="7",
                        chat_id="42")
        self.current_scenario = None
        agent._gigacode_deps = GigacodeRuntimeDeps(
            load_settings=lambda: self.settings, verify=lambda s: self.verify(s),
            make_driver=lambda s, m: direct_driver(self.current_scenario),
            journal=lambda: GigacodeJournal(self.journal_path), scheduler=self.scheduler,
            runs_root=lambda: self.tmp / "runs")
        return agent

    def run(self, agent, text, *, update_id, history=None, **scenario_spec):
        record = self.tmp / f"record-{len(self.records)}.json"
        self.records.append(record)
        scenario_spec.setdefault("steps", success_steps())
        self.current_scenario = self.scenario(record=str(record), **scenario_spec)
        request_identity._GATEWAY_REQUEST.set(request_identity.GatewayRequest("telegram:4242", str(update_id)))
        return agent.run_conversation(text, conversation_history=history or [])

    def journal(self):
        return GigacodeJournal(self.journal_path)

    def db_rows(self, session_id="sess-e2e"):
        return [(m["role"], m.get("content")) for m in self.db.get_messages(session_id)]


@pytest.fixture
def harness(tmp_path, scenario):
    (tmp_path / "runs").mkdir()
    h = Harness(tmp_path, scenario)
    yield h
    h.db.close()


def test_simple_turn_runs_once_and_persists_once(harness):
    agent = harness.agent()
    result = harness.run(agent, "Привет", update_id=1, steps=success_steps("Здравствуйте!"))
    assert result["completed"] and result["final_response"] == "Здравствуйте!"
    assert result["api_calls"] == 0 and result["agent_runs"] == 1 and result["agent_api_calls"] is None
    assert result["agent_persisted"] is True and result["error"] is None and agent.client is None
    assert harness.db_rows() == [("user", "Привет"), ("assistant", "Здравствуйте!")]
    run = harness.journal().run(result["run_id"])
    assert run["state"] == "succeeded" and run["token_revoked"] == 1 and run["cleanup_state"] == "complete"
    pack = json.loads(harness.records[0].read_text())["stdin"]
    assert pack.startswith("HERMES EXECUTION CONTEXT") and '"Привет"' in pack.split("CURRENT USER REQUEST")[1]
    assert not list((harness.tmp / "runs").iterdir())  # sealed run directory removed


def test_second_turn_pack_contains_the_saved_first_answer(harness):
    agent = harness.agent()
    first = harness.run(agent, "Как дела?", update_id=10, steps=success_steps("Отлично, спасибо."))
    harness.run(agent, "А сейчас?", update_id=11, history=first["messages"])
    pack = json.loads(harness.records[1].read_text())["stdin"]
    history = pack.split("CONVERSATION HISTORY")[1].split("CURRENT USER REQUEST")[0]
    assert "Отлично, спасибо." in history and "Как дела?" in history


def test_tool_call_goes_through_the_bridge_and_pairs_are_persisted(harness):
    agent = harness.agent()
    steps = [
        {"event": {"type": "system", "subtype": "init", "model": "fixture-model", "session_id": "s"}},
        {"event": {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "c1", "name": "mcp__hermes__hermes_skills_list", "input": {}}]}}},
        {"event": {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "{{call:0}}"}]}}},
        *success_steps("Навыков нет.")[1:],
    ]
    result = harness.run(agent, "Какие навыки?", update_id=20, steps=steps,
                         mcp_calls=[{"tool": "hermes_skills_list", "arguments": {}}])
    assert result["completed"]
    seen = json.loads(harness.records[0].read_text())["mcp"]
    assert "hermes_skills_list" in seen["tools"] and "hermes_memory" not in seen["tools"]
    assert seen["results"][0]["isError"] is False
    roles = [r for r, _ in harness.db_rows()]
    assert roles == ["user", "assistant", "tool", "assistant"]
    [audit] = harness.journal().audit(result["run_id"])
    assert audit["wire_tool"] == "hermes_skills_list" and audit["outcome"] == "succeeded"


def test_duplicate_update_never_executes_twice(harness):
    agent = harness.agent()
    first = harness.run(agent, "раз", update_id=30)
    again = harness.run(agent, "раз", update_id=30)
    assert again["run_id"] == first["run_id"] and again["error"]["kind"] == "duplicate_request"
    assert not harness.records[1].exists()  # the CLI was not started for the redelivery


def test_missing_manifest_never_starts_the_cli(harness):
    from agent.gigacode.manifest import verify_activation

    harness.verify = verify_activation
    agent = harness.agent()
    result = harness.run(agent, "текст без инструментов", update_id=40)
    assert not result["completed"] and result["error"]["kind"] == "config_unverified"
    assert not harness.records[0].exists()
    assert harness.journal().run(result["run_id"])["state"] == "failed"


def test_cli_error_is_reported_without_fallback(harness):
    agent = harness.agent()
    steps = [{"event": {"type": "result", "subtype": "error", "is_error": True, "error": {"message": "no auth"}}}]
    result = harness.run(agent, "q", update_id=50, steps=steps, exit_code=1)
    assert not result["completed"] and result["error"]["kind"] == "cli_error"
    assert result["error"]["run_id"] == result["run_id"] and result["api_calls"] == 0
    assert agent.provider == "gigacode-cli" and agent.api_mode == "gigacode_cli"


def test_interrupt_cancels_the_process(harness):
    agent = harness.agent()
    threading.Timer(1.5, agent.interrupt).start()
    result = harness.run(agent, "долгая задача", update_id=60, steps=[{"sleep": 60}, *success_steps()])
    assert not result["completed"] and result["error"]["kind"] == "cancelled"
    assert harness.journal().run(result["run_id"])["state"] == "cancelled"


def test_persist_failure_blocks_the_session_until_reconciled(harness, monkeypatch):
    from agent.gigacode import reconcile
    from hermes_state import SessionDB

    agent = harness.agent()
    monkeypatch.setattr(agent, "_flush_messages_to_session_db", lambda *a, **k: False)
    failed = harness.run(agent, "сохрани", update_id=70, steps=success_steps("Ответ"))
    assert not failed["completed"] and failed["error"]["kind"] == "persist_failed"
    assert harness.journal().run(failed["run_id"])["state"] == "recovery_required"
    monkeypatch.undo()
    blocked = harness.run(agent, "дальше", update_id=71)
    assert blocked["error"]["kind"] == "session_recovery_required" and failed["run_id"] in blocked["final_response"]
    assert not harness.records[1].exists()

    journal = harness.journal()
    plan = reconcile.plan(journal, failed["run_id"], "succeeded")
    assert plan.writes_history and plan.message_count == 1
    for _ in range(2):  # repeating the same decision is a no-op
        reconcile.apply(journal, failed["run_id"], "succeeded", reason="checked", operator="op",
                        open_session_db=lambda: SessionDB(db_path=harness.tmp / "state.db"))
    assert [c for r, c in harness.db_rows() if r == "assistant"].count("Ответ") == 1
    assert journal.run(failed["run_id"])["state"] == "resolved_succeeded"
    resumed = harness.run(agent, "теперь можно", update_id=72)
    assert resumed["completed"]


def test_attachment_gets_a_clear_refusal(harness):
    agent = harness.agent()
    content = [{"type": "text", "text": "что на фото?"}, {"type": "image_url", "image_url": {"url": "data:,"}}]
    result = harness.run(agent, content, update_id=80)
    assert result["error"]["kind"] == "unsupported_attachment" and not harness.records[0].exists()


def test_context_too_large(harness):
    harness.settings = _settings(prompt_budget_tokens=1024)
    agent = harness.agent()
    result = harness.run(agent, "x" * 5000, update_id=90)
    assert result["error"]["kind"] == "context_too_large" and not harness.records[0].exists()


def test_owner_private_refuses_group_chats(harness):
    agent = harness.agent()
    agent._chat_type = "group"
    result = harness.run(agent, "всем привет", update_id=95)
    assert result["error"]["kind"] == "policy_scope_denied" and not harness.records[0].exists()


def test_voice_marker_from_the_gateway_is_refused(harness):
    from agent.gigacode.prompt_pack import UNSUPPORTED_ATTACHMENT

    agent = harness.agent()
    result = harness.run(agent, f"{UNSUPPORTED_ATTACHMENT}\n\n", update_id=96)
    assert result["error"]["kind"] == "unsupported_attachment" and not harness.records[0].exists()


def test_unexpected_failure_after_start_requires_recovery(harness, monkeypatch):
    from agent.gigacode_runtime import _TurnRun

    def boom(self, process, manifest):
        raise RuntimeError("projection bug")
    monkeypatch.setattr(_TurnRun, "_finish", boom)
    agent = harness.agent()
    result = harness.run(agent, "q", update_id=97)
    assert result["error"]["kind"] == "internal_error"
    assert harness.journal().run(result["run_id"])["state"] == "recovery_required"
