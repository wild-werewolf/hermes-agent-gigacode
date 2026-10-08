"""Bridge tool catalog, immutable grants and context-bound dispatch.

Wire names carry the ``hermes_`` prefix so they can never coincide with a denied native GigaCode
tool (``web_search`` is denied natively; the bridge serves ``hermes_web_search``). The internal
name is looked up in the fixed :data:`CATALOG`, never derived from the request.

A :class:`Grant` is server-side state built at dequeue from the tool policy plus consumed operator
grants; nothing a client sends can create, widen or re-scope one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from agent.gigacode import safe_files
from agent.gigacode.policy import check_wire_names

def _const(action: str) -> Callable[[Mapping[str, Any]], tuple[str, ...]]:
    return lambda args: (action,)


def _memory_actions(args: Mapping[str, Any]) -> tuple[str, ...]:
    """Every operation the call would perform; an unknown or missing action maps to ``?`` (denied)."""
    ops = args.get("operations")
    if isinstance(ops, list) and ops:
        return tuple(str(op.get("action")) if isinstance(op, dict) else "?" for op in ops)
    action = args.get("action")
    return (action,) if isinstance(action, str) else ("?",)


@dataclass(frozen=True)
class ToolSpec:
    wire: str
    internal: str
    actions: frozenset[str]
    action_of: Callable[[Mapping[str, Any]], tuple[str, ...]]
    untrusted_output: bool = False
    mutating: bool = False
    file_access: bool = False
    schema: Optional[Mapping[str, Any]] = None  # None → the agent's own schema of ``internal``
    description: str = ""
    needs_tool: Optional[str] = None  # Hermes tool that must be enabled on the agent


_EMPTY_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
_PATH_SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"],
                "additionalProperties": False}
_WRITE_SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                 "required": ["path", "content"], "additionalProperties": False}

CATALOG: Mapping[str, ToolSpec] = MappingProxyType({spec.wire: spec for spec in (
    ToolSpec("hermes_web_search", "web_search", frozenset({"search"}), _const("search"), untrusted_output=True),
    ToolSpec("hermes_web_extract", "web_extract", frozenset({"extract"}), _const("extract"), untrusted_output=True),
    ToolSpec("hermes_memory_read", "memory_read", frozenset({"read"}), _const("read"), schema=_EMPTY_SCHEMA,
             description="Read the saved memory of the current profile owner (no arguments).", needs_tool="memory"),
    ToolSpec("hermes_memory", "memory", frozenset({"add", "replace", "remove"}), _memory_actions, mutating=True),
    ToolSpec("hermes_session_search", "session_search", frozenset({"search"}), _const("search"),
             untrusted_output=True),
    ToolSpec("hermes_skills_list", "skills_list", frozenset({"list"}), _const("list")),
    ToolSpec("hermes_skill_view", "skill_view", frozenset({"view"}), _const("view")),
    ToolSpec("hermes_read_file", "read_file", frozenset({"read"}), _const("read"), schema=_PATH_SCHEMA,
             description="Read a UTF-8 text file inside an operator-granted root.", file_access=True,
             untrusted_output=True),
    ToolSpec("hermes_write_file", "write_file", frozenset({"write"}), _const("write"), schema=_WRITE_SCHEMA,
             description="Create or overwrite a UTF-8 text file inside an operator-granted root.",
             file_access=True, mutating=True),
)})
check_wire_names(CATALOG)

# owner_private: read-only defaults. Memory mutation, files, terminal, installs and delegation are
# never part of a policy default; only an operator_explicit grant can add memory or file actions.
POLICY_DEFAULTS: Mapping[str, tuple[tuple[str, frozenset[str]], ...]] = MappingProxyType({
    "owner_private": (
        ("hermes_web_search", frozenset({"search"})), ("hermes_web_extract", frozenset({"extract"})),
        ("hermes_memory_read", frozenset({"read"})), ("hermes_session_search", frozenset({"search"})),
        ("hermes_skills_list", frozenset({"list"})), ("hermes_skill_view", frozenset({"view"})),
    ),
})


@dataclass(frozen=True)
class Scope:
    profile_id: str
    channel: str
    user_id: str
    session_id: Optional[str]


@dataclass(frozen=True)
class Grant:
    wire_tool: str
    internal_tool: str
    actions: frozenset[str]
    scope: Scope
    path_roots: tuple[str, ...]
    expires_at: float
    source: str
    grant_id: Optional[str] = None


def validate_operator_request(wire_tool: str, actions: Iterable[str], path_roots: Sequence[str]) -> tuple[
        frozenset[str], tuple[str, ...]]:
    """Closed-set validation used by ``grants issue`` and again when a grant is consumed."""
    spec = CATALOG.get(wire_tool)
    if spec is None:
        raise ValueError(f"unknown wire tool {wire_tool!r}; known: {', '.join(sorted(CATALOG))}")
    wanted = frozenset(a.strip() for a in actions if a.strip())
    if not wanted or not wanted <= spec.actions:
        raise ValueError(f"actions for {wire_tool} must be a non-empty subset of {sorted(spec.actions)}")
    if spec.file_access and not path_roots:
        raise ValueError(f"{wire_tool} needs at least one --path-root")
    if not spec.file_access and path_roots:
        raise ValueError(f"{wire_tool} takes no path roots")
    return wanted, tuple(safe_files.canonical_root(p) for p in path_roots)


def build_grants(*, policy: str, source: str, scope: Scope, expires_at: float,
                 operator_rows: Sequence[Mapping[str, Any]], enabled_tools: Iterable[str]) -> tuple[Grant, ...]:
    """Immutable grant set for one run: policy defaults ∪ consumed operator grants, each limited
    to tools Hermes itself has enabled for this agent."""
    enabled = set(enabled_tools)
    grants: list[Grant] = []
    for wire, actions in POLICY_DEFAULTS[policy]:
        spec = CATALOG[wire]
        if (spec.needs_tool or spec.internal) in enabled:
            grants.append(Grant(wire, spec.internal, actions, scope, (), expires_at, source))
    for row in operator_rows:
        wire = str(row["wire_tool"])
        actions, roots = validate_operator_request(wire, json.loads(row["actions"]), json.loads(row["path_roots"]))
        spec = CATALOG[wire]
        if not spec.file_access and (spec.needs_tool or spec.internal) not in enabled:
            continue
        grants.append(Grant(wire, spec.internal, actions, scope, roots,
                            min(expires_at, float(row["expires_at"])), "operator_explicit", str(row["grant_id"])))
    return tuple(grants)


def tool_schema(spec: ToolSpec, agent: Any) -> Optional[dict[str, Any]]:
    """MCP tool entry: the agent's own schema and description under the wire name; ``None`` when this
    agent does not expose the internal tool (then it is neither listed nor callable)."""
    if spec.schema is not None:
        return {"name": spec.wire, "description": spec.description, "inputSchema": dict(spec.schema)}
    for tool in getattr(agent, "tools", None) or []:
        fn = tool.get("function", {}) if isinstance(tool, dict) else {}
        if fn.get("name") == spec.internal:
            return {"name": spec.wire, "description": fn.get("description", ""),
                    "inputSchema": fn.get("parameters") or dict(_EMPTY_SCHEMA)}
    return None


# --- context-bound handlers ----------------------------------------------------------------------


def _owned_session_filter(agent: Any, scope: Scope) -> tuple[set[str], list[str]]:
    """(sessions owned by the scope user, every other session id of this profile's DB)."""
    platform, _, raw_user = scope.user_id.partition(":")
    db = agent._get_session_db_for_recall()
    rows = db._read_all("SELECT id, user_id, source FROM sessions", []) if db is not None else []
    owned = {str(r["id"]) for r in rows
             if str(r["user_id"] or "") == raw_user and str(r["source"] or "").lower() == platform.lower()}
    if scope.session_id:
        owned.add(scope.session_id)
    foreign = sorted(str(r["id"]) for r in rows if str(r["id"]) not in owned)
    return owned, foreign


def _session_search(agent: Any, args: dict[str, Any], grant: Grant, call: "CallInfo") -> str:
    args = {k: v for k, v in args.items() if k not in ("profile", "exclude_session_ids")}
    owned, foreign = _owned_session_filter(agent, grant.scope)
    if args.get("session_id") and str(args["session_id"]) not in owned:
        return json.dumps({"success": False, "error": "session is not readable in this scope"})
    args["exclude_session_ids"] = foreign
    return agent._invoke_tool("session_search", args, call.task_id, tool_call_id=call.call_id)


def _memory_read(agent: Any, args: dict[str, Any], grant: Grant, call: "CallInfo") -> str:
    store = getattr(agent, "_memory_store", None)
    if store is None:
        return json.dumps({"success": False, "error": "memory is not available for this profile"})
    return json.dumps({"success": True, "memory": list(store.memory_entries), "user": list(store.user_entries)},
                      ensure_ascii=False)


def _skills_list(agent: Any, args: dict[str, Any], grant: Grant, call: "CallInfo") -> str:
    raw = agent._invoke_tool("skills_list", args, call.task_id, tool_call_id=call.call_id)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return json.dumps({"success": False, "error": "skills_list returned an unreadable result"})
    if isinstance(data, dict) and isinstance(data.get("skills"), list):
        data["skills"] = [s for s in data["skills"]
                          if isinstance(s, dict) and s.get("name") in call.skills_allowlist]
        data.pop("categories", None)
    return json.dumps(data, ensure_ascii=False)


def _skill_view(agent: Any, args: dict[str, Any], grant: Grant, call: "CallInfo") -> str:
    if str(args.get("name", "")) not in call.skills_allowlist:
        return json.dumps({"success": False, "error": "skill is not in the operator allowlist"})
    return agent._invoke_tool("skill_view", args, call.task_id, tool_call_id=call.call_id)


def _read_file(agent: Any, args: dict[str, Any], grant: Grant, call: "CallInfo") -> str:
    content = safe_files.read_confined(grant.path_roots, str(args["path"]))
    return json.dumps({"success": True, "content": content}, ensure_ascii=False)


def _write_file(agent: Any, args: dict[str, Any], grant: Grant, call: "CallInfo") -> str:
    written = safe_files.write_confined(grant.path_roots, str(args["path"]), str(args["content"]))
    return json.dumps({"success": True, "bytes_written": written})


@dataclass(frozen=True)
class CallInfo:
    task_id: str
    call_id: str
    skills_allowlist: frozenset[str]


_CONTEXT_HANDLERS: Mapping[str, Callable[[Any, dict[str, Any], Grant, CallInfo], str]] = MappingProxyType({
    "hermes_memory_read": _memory_read,
    "hermes_session_search": _session_search,
    "hermes_skills_list": _skills_list,
    "hermes_skill_view": _skill_view,
    "hermes_read_file": _read_file,
    "hermes_write_file": _write_file,
})


def dispatch(spec: ToolSpec, args: dict[str, Any], *, agent: Any, grant: Grant, call: CallInfo) -> str:
    """Run an authorized call through the active agent's own handlers (never a context-free path)."""
    handler = _CONTEXT_HANDLERS.get(spec.wire)
    if handler is not None:
        return handler(agent, args, grant, call)
    return agent._invoke_tool(spec.internal, args, call.task_id, tool_call_id=call.call_id)


def _matches_type(value: Any, expected: Any) -> bool:
    types = expected if isinstance(expected, list) else [expected]
    checks = {"string": lambda v: isinstance(v, str), "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
              "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
              "boolean": lambda v: isinstance(v, bool), "array": lambda v: isinstance(v, list),
              "object": lambda v: isinstance(v, dict), "null": lambda v: v is None}
    return any(checks.get(t, lambda v: True)(value) for t in types)


def validate_arguments(schema: Mapping[str, Any], args: Any) -> Optional[str]:
    """Top-level JSON Schema check (object, required, property types, additionalProperties: false)."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    props = schema.get("properties") or {}
    missing = [k for k in schema.get("required") or [] if k not in args]
    if missing:
        return "missing required arguments: " + ", ".join(missing)
    for key, value in args.items():
        if key not in props:
            if schema.get("additionalProperties") is False:
                return f"unexpected argument {key!r}"
            continue
        expected = (props[key] or {}).get("type")
        if expected and not _matches_type(value, expected):
            return f"argument {key!r} has the wrong type"
        enum = (props[key] or {}).get("enum")
        if enum and value not in enum:
            return f"argument {key!r} is not one of the allowed values"
    return None


def args_digest(args: Any) -> str:
    import hashlib

    blob = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
