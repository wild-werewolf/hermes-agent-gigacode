"""Context-bound MCP bridge over real loopback HTTP: auth, namespace, grants, audit, file confinement."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from agent.gigacode.journal import GigacodeJournal
from agent.gigacode.policy import NATIVE_DENY, SKILL_DENY
from agent.gigacode.tools import CATALOG, Scope, build_grants
from agent.transports.gigacode_tools_mcp_server import BridgeContext, GigacodeToolsBridge

SCOPE = Scope("default", "telegram:42", "telegram:7", "sess-1")


class StubStore:
    memory_entries = ["likes tea"]
    user_entries = ["name: Ann"]


class StubDb:
    def _read_all(self, query, params):
        return [{"id": "sess-1", "user_id": "7", "source": "telegram"},
                {"id": "sess-own-old", "user_id": "7", "source": "telegram"},
                {"id": "sess-other", "user_id": "99", "source": "telegram"},
                {"id": "sess-other-platform", "user_id": "7", "source": "discord"}]


class StubAgent:
    def __init__(self, slow: float = 0.0):
        self.calls: list[tuple[str, dict]] = []
        self._memory_store = StubStore()
        self.slow = slow
        self.tools = [{"type": "function", "function": {
            "name": "web_search", "description": "search",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
            {"type": "function", "function": {"name": "memory", "description": "mem", "parameters": {
                "type": "object", "properties": {"action": {"type": "string"}, "content": {"type": "string"},
                                                 "target": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "web_extract", "description": "x", "parameters": {
                "type": "object", "properties": {"urls": {"type": "array"}}, "required": ["urls"]}}},
            {"type": "function", "function": {"name": "skills_list", "description": "l", "parameters": {
                "type": "object", "properties": {"category": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "skill_view", "description": "v", "parameters": {
                "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}}},
            {"type": "function", "function": {"name": "session_search", "description": "s", "parameters": {
                "type": "object", "properties": {"query": {"type": "string"}, "session_id": {"type": "string"},
                                                 "profile": {"type": "string"}}}}}]

    def _get_session_db_for_recall(self):
        return StubDb()

    def _invoke_tool(self, name, args, task_id, tool_call_id=None):
        self.calls.append((name, dict(args)))
        if self.slow:
            time.sleep(self.slow)
        return json.dumps({"success": True, "tool": name})


ENABLED = {"web_search", "web_extract", "memory", "session_search", "skills_list", "skill_view"}


def _bridge(tmp_path, *, operator_rows=(), agent=None, expires_in=600.0, journal=None):
    journal = journal or GigacodeJournal(tmp_path / "journal.db")
    grants = build_grants(policy="owner_private", source="owner_private", scope=SCOPE,
                          expires_at=time.time() + expires_in, operator_rows=list(operator_rows), enabled_tools=ENABLED)
    ctx = BridgeContext(run_id="gc_run1", session_id="sess-1", profile_id="default", user_id="telegram:7",
                        task_id="task", grants=grants, cancellation=threading.Event(), agent=agent or StubAgent(),
                        journal=journal, expires_at=time.time() + expires_in, skills_allowlist=frozenset({"ok-skill"}))
    return GigacodeToolsBridge(ctx).start()


class Client:
    def __init__(self, bridge, token=None, host=None):
        self.url, self.token, self.session, self.n = bridge.url, token or bridge.token, None, 0
        self.host = host

    def post(self, method, params=None, notify=False, session=True):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            body["id"] = self.n
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        if self.session and session:
            headers["Mcp-Session-Id"] = self.session
        if self.host:
            headers["Host"] = self.host
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            return exc.code, None

    def initialize(self):
        status, reply = self.post("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})
        assert status == 200 and self.session
        assert self.post("notifications/initialized", notify=True)[0] == 202
        return reply

    def call(self, name, arguments):
        status, reply = self.post("tools/call", {"name": name, "arguments": arguments})
        assert status == 200
        result = reply["result"]
        return result["isError"], result["content"][0]["text"]


@pytest.fixture
def bridge(tmp_path):
    b = _bridge(tmp_path)
    yield b
    b.stop()


def test_initialize_list_and_call_roundtrip(bridge):
    client = Client(bridge)
    reply = client.initialize()
    assert reply["result"]["serverInfo"]["name"] == "hermes"
    names = [t["name"] for t in client.post("tools/list")[1]["result"]["tools"]]
    assert names == sorted(["hermes_memory_read", "hermes_session_search", "hermes_skill_view", "hermes_skills_list",
                            "hermes_web_extract", "hermes_web_search"])
    assert "hermes_memory" not in names  # memory mutation is never a policy default
    is_error, text = client.call("hermes_web_search", {"query": "q"})
    assert not is_error and json.loads(text)["tool"] == "web_search"
    assert bridge.context.agent.calls == [("web_search", {"query": "q"})]
    [audit] = bridge.context.journal.audit("gc_run1")
    assert audit["outcome"] == "succeeded" and audit["wire_tool"] == "hermes_web_search"
    assert audit["args_sha256"] and "q" not in json.dumps(audit)


def test_auth_host_and_session_checks(bridge):
    assert Client(bridge, token="wrong").post("initialize")[0] == 401
    assert Client(bridge, host="evil.example:80").post("initialize")[0] == 403
    client = Client(bridge)
    client.initialize()
    assert client.post("tools/list", session=False)[0] == 400
    bridge.revoke()
    assert client.post("tools/list")[0] == 401  # token dead after revocation


def test_ungranted_and_native_names_are_denied_and_audited(bridge):
    client = Client(bridge)
    client.initialize()
    for name in ("hermes_memory", "web_search", "run_shell_command", "hermes_write_file"):
        is_error, text = client.call(name, {"action": "add", "content": "x"} if name == "hermes_memory" else {})
        assert is_error and text.startswith("Denied by Hermes")
    assert bridge.context.agent.calls == []
    assert {a["outcome"] for a in bridge.context.journal.audit("gc_run1")} == {"denied"}


def test_wire_names_never_collide_with_native_deny():
    assert not set(CATALOG) & (set(NATIVE_DENY) | set(SKILL_DENY))
    assert all(name.startswith("hermes_") for name in CATALOG)


def test_invalid_arguments_rejected_before_dispatch(bridge):
    client = Client(bridge)
    client.initialize()
    is_error, text = client.call("hermes_web_search", {"query": 5})
    assert is_error and "wrong type" in text and bridge.context.agent.calls == []


def test_memory_read_is_scoped_and_session_search_excludes_other_users(bridge):
    client = Client(bridge)
    client.initialize()
    is_error, text = client.call("hermes_memory_read", {})
    assert not is_error and json.loads(text) == {"success": True, "memory": ["likes tea"], "user": ["name: Ann"]}
    client.call("hermes_session_search", {"query": "x", "profile": "other-profile"})
    name, args = bridge.context.agent.calls[-1]
    assert name == "session_search" and "profile" not in args
    assert args["exclude_session_ids"] == ["sess-other", "sess-other-platform"]
    is_error, text = client.call("hermes_session_search", {"session_id": "sess-other"})
    assert "not readable" in text


def test_skill_allowlist(bridge):
    client = Client(bridge)
    client.initialize()
    _, text = client.call("hermes_skill_view", {"name": "secret-skill"})
    assert "allowlist" in text and not bridge.context.agent.calls


def _operator_row(journal, run_id, tool, actions, roots=()):
    run = journal.admit(profile_id="default", channel="telegram:42", user_id="telegram:7", request_id=run_id,
                        session_id="sess-1", source="owner_private")
    journal.issue_grant(profile_id="default", channel="telegram:42", user_id="telegram:7", wire_tool=tool,
                        actions=actions, ttl_seconds=120, operator="op", request_key=run.row["request_key"],
                        path_roots=roots)
    return run.run_id, journal.consume_grants(run.run_id)


def test_operator_grant_allows_only_its_actions_and_flags_prior_untrusted_input(tmp_path):
    journal = GigacodeJournal(tmp_path / "journal.db")
    run_id, rows = _operator_row(journal, "u1", "hermes_memory", ["add"])
    b = _bridge(tmp_path, operator_rows=rows, journal=journal)
    b.context.run_id = run_id
    try:
        client = Client(b)
        client.initialize()
        assert not client.call("hermes_web_search", {"query": "q"})[0]  # untrusted web input first
        assert not client.call("hermes_memory", {"action": "add", "content": "fact"})[0]
        assert client.call("hermes_memory", {"action": "remove", "old_text": "fact"})[0]  # action not granted
        assert client.call("hermes_memory", {"action": "nuke"})[0]  # unknown action → denied
        mutation = [a for a in journal.audit(run_id) if a["wire_tool"] == "hermes_memory" and a["outcome"] == "succeeded"]
        assert len(mutation) == 1 and mutation[0]["untrusted_input_before"] == 1
        assert mutation[0]["grant_source"] == "operator_explicit"
        journal.revoke_grant(rows[0]["grant_id"])
        assert client.call("hermes_memory", {"action": "add", "content": "again"})[0]  # revoked mid-run
    finally:
        b.stop()


def test_file_grant_confines_paths(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    os.symlink(outside, root / "link")
    os.symlink(outside / "secret.txt", root / "sub" / "alias.txt")
    journal = GigacodeJournal(tmp_path / "journal.db")
    run_id, rows = _operator_row(journal, "f1", "hermes_write_file", ["write"], roots=[str(root)])
    b = _bridge(tmp_path, operator_rows=rows, journal=journal)
    b.context.run_id = run_id
    try:
        client = Client(b)
        client.initialize()
        assert not client.call("hermes_write_file", {"path": "sub/new.txt", "content": "hi"})[0]
        assert (root / "sub" / "new.txt").read_text() == "hi"
        for bad in ("../outside/secret.txt", "link/secret.txt", "sub/alias.txt", str(outside / "x.txt")):
            is_error, text = client.call("hermes_write_file", {"path": bad, "content": "pwned"})
            assert is_error, bad
        assert (outside / "secret.txt").read_text() == "secret" and not (outside / "x.txt").exists()
        assert client.call("hermes_read_file", {"path": "sub/new.txt"})[0]  # read not granted
    finally:
        b.stop()


def test_cancellation_during_a_mutating_call_marks_it_unknown(tmp_path):
    journal = GigacodeJournal(tmp_path / "journal.db")
    run_id, rows = _operator_row(journal, "c1", "hermes_memory", ["add"])
    b = _bridge(tmp_path, operator_rows=rows, journal=journal, agent=StubAgent(slow=3.0))
    b.context.run_id = run_id
    try:
        client = Client(b)
        client.initialize()
        threading.Timer(0.5, b.context.cancellation.set).start()
        is_error, text = client.call("hermes_memory", {"action": "add", "content": "x"})
        assert is_error and "Cancelled" in text
        assert [a["outcome"] for a in journal.audit(run_id)] == ["unknown"]
    finally:
        b.stop()


def test_expired_grants_stop_working(tmp_path):
    b = _bridge(tmp_path, expires_in=1.0)
    try:
        client = Client(b)
        client.initialize()
        time.sleep(1.2)
        assert client.post("tools/list")[0] == 401
    finally:
        b.stop()
