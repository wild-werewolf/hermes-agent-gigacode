"""Per-run, context-bound Hermes tools bridge for the GigaCode CLI (MCP over Streamable HTTP).

Unlike ``hermes_tools_mcp_server`` (static stdio server, fixed tool list, no agent context), this
server lives inside the Hermes process for exactly one run:

* listens on ``127.0.0.1`` on an ephemeral port; checks ``Host``/``Origin`` (DNS-rebinding guard);
* requires the per-run Bearer token on every request; ``initialize`` additionally binds an
  ``Mcp-Session-Id`` to the run (the session id never replaces the token);
* serves only the run's granted tools under their ``hermes_*`` wire names;
* runs every ``tools/call`` through ``authorize → validate_arguments → audit_start →
  dispatch_with_context → audit_finish``, with the permission context fixed server-side
  (:class:`BridgeContext`) — never taken from tool arguments;
* is revoked (token dead, in-flight calls cancelled) the moment the run ends or is cancelled.

Responses use the JSON (non-SSE) mode of Streamable HTTP; ``GET`` (server push) is not offered.
"""

from __future__ import annotations

import contextvars
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from agent.gigacode.errors import safe_text
from agent.gigacode.journal import GigacodeJournal
from agent.gigacode.safe_files import PathDenied
from agent.gigacode.tools import CATALOG, CallInfo, Grant, args_digest, dispatch, tool_schema, validate_arguments

logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY_BYTES = 1 << 20
MAX_RESULT_CHARS = 1 << 20
SERVER_INFO = {"name": "hermes", "version": "1"}


@dataclass
class BridgeContext:
    """Server-side permission context of one run. Clients can neither send nor modify it."""

    run_id: str
    session_id: Optional[str]
    profile_id: str
    user_id: str
    task_id: str
    grants: tuple[Grant, ...]
    cancellation: threading.Event
    agent: Any
    journal: GigacodeJournal
    expires_at: float
    skills_allowlist: frozenset[str] = frozenset()
    turn_context: contextvars.Context = field(default_factory=contextvars.copy_context)
    untrusted_input_seen: bool = False

    @property
    def wire_tools(self) -> list[str]:
        return sorted({g.wire_tool for g in self.grants})


class _Denied(Exception):
    pass


class GigacodeToolsBridge:
    """One loopback MCP endpoint for one run."""

    def __init__(self, context: BridgeContext) -> None:
        self.context = context
        self.token = secrets.token_urlsafe(32)
        self._revoked = threading.Event()
        self._mcp_session: Optional[str] = None
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.calls = 0

    # -- lifecycle ------------------------------------------------------------------------------
    @property
    def port(self) -> int:
        assert self._server is not None
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self) -> "GigacodeToolsBridge":
        bridge = self

        class Handler(_Handler):
            owner = bridge

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        # health: allow HX012 -- the HTTP accept loop carries no turn context; calls re-enter it explicitly
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                                        name=f"gigacode-bridge-{self.context.run_id[-8:]}", daemon=True)
        self._thread.start()
        return self

    def revoke(self) -> None:
        """Kill the token and cancel in-flight calls (idempotent)."""
        self._revoked.set()
        self.context.cancellation.set()

    def stop(self) -> None:
        self.revoke()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def revoked(self) -> bool:
        return self._revoked.is_set()

    # -- request checks -------------------------------------------------------------------------
    def authorized(self, header: Optional[str]) -> bool:
        if self.revoked or time.time() >= self.context.expires_at or not header:
            return False
        return hmac.compare_digest(header.encode("utf-8"), f"Bearer {self.token}".encode("utf-8"))

    def host_allowed(self, host: Optional[str], origin: Optional[str]) -> bool:
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if host not in allowed:
            return False
        return origin is None or origin in {f"http://{h}" for h in allowed}

    # -- JSON-RPC methods -----------------------------------------------------------------------
    def rpc_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO}

    def rpc_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    def rpc_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        entries = (tool_schema(CATALOG[w], self.context.agent) for w in self.context.wire_tools)
        return {"tools": [entry for entry in entries if entry is not None]}

    def rpc_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments")
        args = {} if args is None else args
        digest = args_digest(args)
        try:
            spec, grant, actions = self._authorize(name, args)
        except _Denied as exc:
            self.context.journal.audit_denied(run_id=self.context.run_id, wire_tool=str(name)[:120],
                                              args_sha256=digest, detail=str(exc))
            return _tool_result(f"Denied by Hermes: {exc}", is_error=True)
        schema = tool_schema(spec, self.context.agent)
        problem = "the tool is not available on this agent" if schema is None else validate_arguments(
            schema["inputSchema"], args)
        if problem:
            self.context.journal.audit_denied(run_id=self.context.run_id, wire_tool=spec.wire,
                                              args_sha256=digest, detail=f"invalid arguments: {problem}")
            return _tool_result(f"Invalid arguments: {problem}", is_error=True)
        return self._run_call(spec, grant, args, actions, digest)

    def _authorize(self, name: Any, args: Any) -> tuple[Any, Grant, tuple[str, ...]]:
        ctx = self.context
        if ctx.cancellation.is_set() or self.revoked:
            raise _Denied("the run is no longer active")
        spec = CATALOG.get(name) if isinstance(name, str) else None
        candidates = [g for g in ctx.grants if spec is not None and g.wire_tool == spec.wire]
        if not candidates:
            raise _Denied(f"tool {str(name)[:80]!r} is not granted for this run")
        actions = spec.action_of(args if isinstance(args, dict) else {})
        now = time.time()
        for grant in candidates:
            if not set(actions) <= grant.actions or now >= grant.expires_at:
                continue
            if grant.grant_id is not None and not ctx.journal.grant_usable(grant.grant_id, ctx.run_id):
                continue
            return spec, grant, actions
        raise _Denied(f"action(s) {', '.join(actions)} not granted for {spec.wire}")

    def _run_call(self, spec: Any, grant: Grant, args: dict[str, Any], actions: tuple[str, ...],
                  digest: str) -> dict[str, Any]:
        ctx = self.context
        with self._lock:
            self.calls += 1
            untrusted_before = ctx.untrusted_input_seen
        operation_id = ctx.journal.audit_start(
            run_id=ctx.run_id, wire_tool=spec.wire, internal_tool=spec.internal, action=",".join(actions),
            args_sha256=digest, grant_source=grant.source, untrusted_before=untrusted_before and spec.mutating)
        outcome, text = self._dispatch_with_context(spec, grant, args, operation_id)
        ctx.journal.audit_finish(operation_id, outcome)
        if spec.untrusted_output and outcome == "succeeded":
            with self._lock:
                ctx.untrusted_input_seen = True
        return _tool_result(text, is_error=outcome != "succeeded")

    def _dispatch_with_context(self, spec: Any, grant: Grant, args: dict[str, Any],
                               operation_id: str) -> tuple[str, str]:
        """Run the handler on a helper thread inside a copy of the turn's context (profile scope,
        session vars); wait while honouring cancellation. An unfinished mutating call becomes
        ``unknown`` — it may have taken effect and is never retried automatically."""
        ctx = self.context
        box: dict[str, Any] = {}
        call = CallInfo(task_id=ctx.task_id, call_id=operation_id, skills_allowlist=ctx.skills_allowlist)

        def work() -> None:
            try:
                box["result"] = dispatch(spec, dict(args), agent=ctx.agent, grant=grant, call=call)
            except PathDenied as exc:
                box["denied"] = str(exc)
            except Exception as exc:
                logger.warning("gigacode bridge tool %s failed", spec.wire, exc_info=True)
                box["error"] = type(exc).__name__

        runner = ctx.turn_context.copy()
        # health: allow HX012 -- the thread runs inside an explicit copy of the turn's context
        worker = threading.Thread(target=runner.run, args=(work,), daemon=True, name="gigacode-bridge-call")
        worker.start()
        while worker.is_alive():
            worker.join(timeout=0.1)
            if ctx.cancellation.is_set() and worker.is_alive():
                return ("unknown" if spec.mutating else "failed"), "Cancelled: the run was stopped"
        if "denied" in box:
            return "failed", f"Denied by Hermes: {box['denied']}"
        if "error" in box:
            return "failed", f"Tool failed: {box['error']}"
        result = box.get("result")
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return ("failed" if _looks_failed(text) else "succeeded"), text[:MAX_RESULT_CHARS]

    def end_session(self) -> None:
        with self._lock:
            self._mcp_session = None

    def open_session(self) -> str:
        with self._lock:
            if self._mcp_session is None:
                self._mcp_session = secrets.token_hex(16)
            return self._mcp_session

    def session_matches(self, value: Optional[str]) -> Optional[bool]:
        with self._lock:
            if self._mcp_session is None:
                return None
            return value is not None and hmac.compare_digest(value, self._mcp_session)


def _looks_failed(text: str) -> bool:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and (data.get("success") is False or (
        "error" in data and data.get("success") is not True))


def _tool_result(text: str, *, is_error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


_METHODS: dict[str, Callable[[GigacodeToolsBridge, dict[str, Any]], dict[str, Any]]] = {
    "initialize": GigacodeToolsBridge.rpc_initialize,
    "ping": GigacodeToolsBridge.rpc_ping,
    "tools/list": GigacodeToolsBridge.rpc_tools_list,
    "tools/call": GigacodeToolsBridge.rpc_tools_call,
}


class _Handler(BaseHTTPRequestHandler):
    owner: GigacodeToolsBridge
    protocol_version = "HTTP/1.1"
    server_version = "hermes-gigacode-bridge"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.debug("bridge: " + format, *args)

    def _send(self, status: int, payload: Optional[dict[str, Any]] = None,
              headers: Optional[dict[str, str]] = None) -> None:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _gate(self) -> bool:
        owner = self.owner
        if self.path.split("?", 1)[0] != "/mcp":
            self._send(404, {"error": "not found"})
            return False
        if not owner.host_allowed(self.headers.get("Host"), self.headers.get("Origin")):
            self._send(403, {"error": "forbidden host"})
            return False
        if not owner.authorized(self.headers.get("Authorization")):
            self._send(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self._gate():
            self._send(405, {"error": "server push is not offered"}, {"Allow": "POST, DELETE"})

    def do_DELETE(self) -> None:  # noqa: N802
        if self._gate():
            if self.owner.session_matches(self.headers.get("Mcp-Session-Id")) is not True:
                self._send(404, {"error": "unknown session"})
                return
            self.owner.end_session()
            self._send(200)

    def do_POST(self) -> None:  # noqa: N802
        if not self._gate():
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send(413, {"error": "body too large or empty"})
            return
        try:
            message = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send(400, _rpc_error(None, -32700, "parse error"))
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            self._send(400, _rpc_error(None, -32600, "invalid request (batches are not supported)"))
            return
        self._handle(message)

    def _handle(self, message: dict[str, Any]) -> None:
        owner, method = self.owner, message["method"]
        headers: dict[str, str] = {}
        if method == "initialize":
            headers["Mcp-Session-Id"] = owner.open_session()
        else:
            header = self.headers.get("Mcp-Session-Id")
            if header is None or owner.session_matches(header) is not True:
                self._send(400 if header is None else 404,
                           _rpc_error(message.get("id"), -32600, "missing or unknown MCP session"))
                return
        if "id" not in message:  # notification (e.g. notifications/initialized)
            self._send(202, None, headers)
            return
        handler = _METHODS.get(method)
        if handler is None:
            self._send(200, _rpc_error(message["id"], -32601, f"method not found: {method[:60]}"), headers)
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        try:
            result = handler(owner, params)
        except Exception as exc:
            logger.warning("gigacode bridge %s failed", method, exc_info=True)
            self._send(200, _rpc_error(message["id"], -32603, safe_text(type(exc).__name__)), headers)
            return
        self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": result}, headers)


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
