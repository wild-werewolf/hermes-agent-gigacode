"""Structured GigaCode runtime errors: a closed ``kind`` vocabulary plus a safe message.

``error`` in a turn result is ``{"kind", "message", "run_id"}`` — never raw stderr.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Closed vocabulary; the operator guide documents each kind.
ERROR_KINDS = frozenset({
    "config_invalid", "config_unverified", "context_too_large", "runtime_busy",
    "session_recovery_required", "unsupported_attachment", "spawn_failed", "bridge_failed",
    "protocol_limit", "protocol_truncated", "protocol_error", "protocol_missing_result",
    "protocol_empty_output", "tool_policy_violation", "cli_error", "api_error",
    "empty_final_response", "nonzero_exit", "cancelled", "timed_out", "persist_failed",
    "restart_before_start", "auth_refresh_required", "cleanup_incomplete", "duplicate_request",
    "cron_provenance_missing", "policy_scope_denied",
})

_SECRETISH = re.compile(
    r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]{8,}|(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+)"
)
SAFE_MESSAGE_MAX = 500


def safe_text(text: Any, limit: int = SAFE_MESSAGE_MAX) -> str:
    """Single-line, bounded, credential-redacted text for user-facing errors and logs."""
    value = " ".join(str(text or "").split())
    value = _SECRETISH.sub("[redacted]", value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


class GigacodeError(Exception):
    """A runtime failure carrying its result ``kind``."""

    def __init__(self, kind: str, message: str, run_id: Optional[str] = None) -> None:
        if kind not in ERROR_KINDS:
            raise ValueError(f"unknown GigaCode error kind: {kind}")
        super().__init__(kind, message)
        self.kind, self.message, self.run_id = kind, message, run_id

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"

    def as_dict(self, run_id: Optional[str] = None) -> dict[str, Any]:
        return {"kind": self.kind, "message": safe_text(self.message), "run_id": run_id or self.run_id}


def error_dict(kind: str, message: str, run_id: Optional[str]) -> dict[str, Any]:
    return GigacodeError(kind, message, run_id).as_dict()
