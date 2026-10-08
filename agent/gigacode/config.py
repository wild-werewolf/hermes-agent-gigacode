"""``gigacode:`` config section → validated, immutable :class:`GigacodeSettings`.

Validation happens before any process starts. Two failure kinds:

* ``config_invalid`` — a value is malformed (wrong type, out of range, a feature this version
  does not support such as ``fallback`` other than ``disabled``).
* ``config_unverified`` — a ``REQUIRED_FROM_PREFLIGHT`` marker is still present or a value that
  only operator acceptance testing can supply is missing. Such a config never launches the real CLI.

There is deliberately no switch that selects a simulator or skips verification: tests inject a
driver through :class:`agent.gigacode_runtime.GigacodeRuntimeDeps`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from agent.gigacode.errors import GigacodeError

REQUIRED_MARKER = "REQUIRED_FROM_PREFLIGHT"
STREAM_PROTOCOL = "maestro_stream_json_v1"
TOOL_POLICIES = frozenset({"owner_private"})
EXECUTION_DRIVERS = frozenset({"bubblewrap"})
QUEUE_LIMIT = 32

DEFAULTS: dict[str, Any] = {
    "executable": None,
    "verified_version": REQUIRED_MARKER,
    "model": None,
    "max_concurrent_runs": 1,
    "wall_timeout_seconds": 1800,
    "silence_warning_seconds": 180,
    "termination_grace_seconds": 5,
    "stream_protocol": STREAM_PROTOCOL,
    "tool_policy": "owner_private",
    "prompt_budget_tokens": REQUIRED_MARKER,
    "verification_manifest": None,
    "execution_driver": "bubblewrap",
    "runtime_rootfs": REQUIRED_MARKER,
    "bwrap_executable": None,
    "auth_bundle": None,
    "skills_allowlist": [],
    "named_subagents": False,
    "fork_subagents": False,
    "fallback": "disabled",
}


@dataclass(frozen=True)
class GigacodeSettings:
    """Validated runtime settings. ``unverified`` lists fields still needing operator values."""

    executable: Optional[str]
    verified_version: Optional[str]
    model: Optional[str]
    max_concurrent_runs: int
    wall_timeout_seconds: int
    silence_warning_seconds: int
    termination_grace_seconds: int
    stream_protocol: str
    tool_policy: str
    prompt_budget_tokens: Optional[int]
    verification_manifest: Optional[str]
    execution_driver: str
    runtime_rootfs: Optional[str]
    bwrap_executable: Optional[str]
    auth_bundle: Optional[str]
    skills_allowlist: tuple[str, ...]
    unverified: tuple[str, ...] = field(default=())

    def require_verified(self) -> None:
        """Raise ``config_unverified`` unless every operator-supplied value is present."""
        if self.unverified:
            raise GigacodeError(
                "config_unverified",
                "gigacode config is not activated; replace or set: " + ", ".join(sorted(self.unverified)),
            )


def _int(raw: Mapping[str, Any], key: str, lo: int, hi: int, problems: list[str]) -> int:
    value = raw.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        problems.append(f"{key} must be an integer in [{lo}, {hi}]")
        return int(DEFAULTS[key]) if isinstance(DEFAULTS[key], int) else lo
    return value


def _opt_str(raw: Mapping[str, Any], key: str, problems: list[str]) -> Optional[str]:
    value = raw.get(key, DEFAULTS[key])
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        problems.append(f"{key} must be a string")
        return None
    return value


def _abs_path(raw: Mapping[str, Any], key: str, problems: list[str], unverified: list[str]) -> Optional[str]:
    value = _opt_str(raw, key, problems)
    if value is None or value == REQUIRED_MARKER:
        unverified.append(key)
        return None
    if not os.path.isabs(value):
        problems.append(f"{key} must be an absolute path")
        return None
    return os.path.normpath(value)


def _choice(raw: Mapping[str, Any], key: str, allowed: frozenset[str], problems: list[str]) -> str:
    value = raw.get(key, DEFAULTS[key])
    if value not in allowed:
        problems.append(f"{key} must be one of {sorted(allowed)}")
        return str(DEFAULTS[key])
    return value


def _disabled_features(raw: Mapping[str, Any], problems: list[str]) -> None:
    for key in ("named_subagents", "fork_subagents"):
        if raw.get(key, False) is not False:
            problems.append(f"{key} is not supported in this version; set it to false")
    if raw.get("fallback", "disabled") != "disabled":
        problems.append("fallback must be 'disabled': GigaCode errors never fall back to another provider")


def _budget(raw: Mapping[str, Any], problems: list[str], unverified: list[str]) -> Optional[int]:
    value = raw.get("prompt_budget_tokens", DEFAULTS["prompt_budget_tokens"])
    if value in (None, REQUIRED_MARKER):
        unverified.append("prompt_budget_tokens")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1024:
        problems.append("prompt_budget_tokens must be an integer >= 1024")
        return None
    return value


def _allowlist(raw: Mapping[str, Any], problems: list[str]) -> tuple[str, ...]:
    value = raw.get("skills_allowlist", [])
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        problems.append("skills_allowlist must be a list of skill names")
        return ()
    return tuple(sorted({v.strip() for v in value}))


def parse_settings(section: Any) -> GigacodeSettings:
    """Validate ``config['gigacode']``; raises ``config_invalid`` on any malformed value."""
    if section is None:
        section = {}
    if not isinstance(section, Mapping):
        raise GigacodeError("config_invalid", "gigacode config section must be a mapping")
    problems: list[str] = []
    unverified: list[str] = []
    unknown = sorted(set(section) - set(DEFAULTS))
    if unknown:
        problems.append("unknown gigacode keys: " + ", ".join(unknown))
    _disabled_features(section, problems)
    version = _opt_str(section, "verified_version", problems)
    if version in (None, REQUIRED_MARKER):
        unverified.append("verified_version")
        version = None
    manifest = _abs_path(section, "verification_manifest", problems, unverified)
    settings = GigacodeSettings(
        executable=_abs_path(section, "executable", problems, unverified),
        verified_version=version,
        model=_opt_str(section, "model", problems),
        max_concurrent_runs=_int(section, "max_concurrent_runs", 1, 16, problems),
        wall_timeout_seconds=_int(section, "wall_timeout_seconds", 10, 86400, problems),
        silence_warning_seconds=_int(section, "silence_warning_seconds", 5, 86400, problems),
        termination_grace_seconds=_int(section, "termination_grace_seconds", 1, 120, problems),
        stream_protocol=_choice(section, "stream_protocol", frozenset({STREAM_PROTOCOL}), problems),
        tool_policy=_choice(section, "tool_policy", TOOL_POLICIES, problems),
        prompt_budget_tokens=_budget(section, problems, unverified),
        verification_manifest=manifest,
        execution_driver=_choice(section, "execution_driver", EXECUTION_DRIVERS, problems),
        runtime_rootfs=_abs_path(section, "runtime_rootfs", problems, unverified),
        bwrap_executable=_abs_path(section, "bwrap_executable", problems, unverified),
        auth_bundle=_optional_abs(section, "auth_bundle", problems),
        skills_allowlist=_allowlist(section, problems),
        unverified=tuple(sorted(set(unverified))),
    )
    if problems:
        raise GigacodeError("config_invalid", "; ".join(problems))
    return settings


def _optional_abs(raw: Mapping[str, Any], key: str, problems: list[str]) -> Optional[str]:
    value = _opt_str(raw, key, problems)
    if value is not None and not os.path.isabs(value):
        problems.append(f"{key} must be an absolute path")
        return None
    return os.path.normpath(value) if value else None


def load_settings(config: Optional[Mapping[str, Any]] = None) -> GigacodeSettings:
    """Settings from the active profile's config (call-time read; never cached at import)."""
    if config is None:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
    return parse_settings((config or {}).get("gigacode"))
