"""Native-tool deny set, wire tool names, generated GigaCode settings and the fixed argv.

Everything the sandboxed CLI is told about tools derives from the constants here, and
:func:`policy_digest` hashes exactly that material so the operator's verification manifest
pins it. The deny list is the starting point carried over from the Maestro sources (Qwen
family names and aliases); it is not proof that a given GigaCode build honours it — the
manifest records the build-specific acceptance test.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional

MCP_SERVER_NAME = "hermes"
WIRE_PREFIX = "hermes_"
# Qualified name profile of ``maestro_stream_json_v1`` fixtures; the manifest records the real one.
QUALIFIED_TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

NATIVE_DENY: tuple[str, ...] = (
    "read_file", "write_file", "edit", "replace", "read_many_files", "notebook_edit",
    "list_directory", "ls", "glob", "grep_search", "search_file_content",
    "create_file", "delete_file", "move_file", "copy_file", "read_directory", "read_folder", "file_search",
    "run_shell_command", "shell", "bash", "run_command", "execute_command", "terminal", "exec",
    "web_fetch", "web_search", "google_web_search", "fetch", "http_fetch", "url_fetch",
    "http_request", "download", "download_file", "curl",
    "add_mcp_server", "remove_mcp_server", "list_mcp_servers", "mcp_add", "mcp",
    "save_memory", "memory",
    "computer_use__click", "computer_use__drag", "computer_use__get_app_state", "computer_use__list_apps",
    "computer_use__perform_secondary_action", "computer_use__press_key", "computer_use__scroll",
    "computer_use__set_value", "computer_use__type_text",
    "ask_user_question", "ask_user", "ask_question", "request_user_input", "user_input", "prompt_user", "confirm",
)
# Native skills and subagents are off in this version (their loader is a separate extension).
SKILL_DENY: tuple[str, ...] = ("skill", "skills", "use_skill", "invoke_skill", "run_skill", "load_skill")

FIXED_ARGV_FLAGS: tuple[str, ...] = (
    "--output-format", "stream-json",
    "--allowed-mcp-server-names", MCP_SERVER_NAME,
    "--allowed-tools", f"mcp__{MCP_SERVER_NAME}",
    "--approval-mode=auto-edit",
)
_FORBIDDEN_EXTRA_FLAGS = ("--output-format", "--yolo", "--mcp-config", "--allowed-tools",
                          "--allowed-mcp-server-names", "--approval-mode", "--resume")


def combined_deny(existing_nested: Iterable[str] = (), existing_legacy: Iterable[str] = ()) -> list[str]:
    """``union(existing_nested_deny, existing_legacy_deny, native_deny, skill_deny)``, sorted."""
    return sorted(set(existing_nested) | set(existing_legacy) | set(NATIVE_DENY) | set(SKILL_DENY))


def check_wire_names(wire_names: Iterable[str]) -> None:
    """Wire names must carry the prefix and never collide with a denied native/skill name."""
    deny = set(NATIVE_DENY) | set(SKILL_DENY)
    for name in wire_names:
        if not name.startswith(WIRE_PREFIX):
            raise ValueError(f"wire tool {name!r} lacks the {WIRE_PREFIX!r} prefix")
        if name in deny:
            raise ValueError(f"wire tool {name!r} collides with the native deny set")


def generate_settings(*, model: Optional[str] = None, existing: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Project/user-scope ``settings.json`` carrying the same deny set in both known field forms.

    ``existing`` contributes only its deny entries: inherited allow-overrides are never kept.
    """
    existing = existing or {}
    tools = existing.get("tools")
    nested = (tools.get("exclude") or []) if isinstance(tools, Mapping) else []
    legacy = existing.get("excludeTools") or []
    deny = combined_deny(nested, legacy)
    settings: dict[str, Any] = {
        "tools": {"exclude": deny},
        "excludeTools": list(deny),
        "mcp": {"allowed": [MCP_SERVER_NAME]},
    }
    if model:
        settings["model"] = {"name": model}
    return settings


def mcp_config(*, url: str, token: str, wire_tools: Iterable[str], tool_timeout_ms: int) -> dict[str, Any]:
    """The per-run MCP config: only the Hermes bridge, only the granted wire tools."""
    include = sorted(wire_tools)
    check_wire_names(include)
    return {"mcpServers": {MCP_SERVER_NAME: {
        "httpUrl": url,
        "headers": {"Authorization": f"Bearer {token}"},
        "includeTools": include,
        "discoveryTimeoutMs": 10000,
        "timeout": tool_timeout_ms,
        "trust": True,
    }}}


def build_argv(executable: str, mcp_config_path: str, *, model: Optional[str] = None) -> list[str]:
    """Fixed argv (no shell). Prompt and history travel on stdin, never in arguments."""
    argv = [executable, "--output-format", "stream-json", "--mcp-config", mcp_config_path,
            *FIXED_ARGV_FLAGS[2:]]
    if model:
        if model.startswith("-") or any(model.startswith(flag) for flag in _FORBIDDEN_EXTRA_FLAGS):
            raise ValueError("model id must not look like a flag")
        argv += ["--model", model]
    return argv


def qualified_tool_name(wire: str) -> str:
    return QUALIFIED_TOOL_PREFIX + wire


def policy_digest(*, wire_catalog: Iterable[str], model: Optional[str]) -> str:
    """SHA-256 over every policy input the manifest must pin (deny sets, argv flags, catalog, model)."""
    material = {
        "native_deny": sorted(NATIVE_DENY),
        "skill_deny": sorted(SKILL_DENY),
        "argv_flags": list(FIXED_ARGV_FLAGS),
        "mcp_server": MCP_SERVER_NAME,
        "wire_catalog": sorted(wire_catalog),
        "settings": generate_settings(model=model),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
