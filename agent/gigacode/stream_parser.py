"""Incremental parser for the ``maestro_stream_json_v1`` event profile (NDJSON on stdout).

One JSON object per line, UTF-8, LF or CRLF, arriving in arbitrary chunks. The profile is
deliberately stricter than the Maestro reader: raw text, objects without ``type`` and unknown
structural types are protocol errors, never a silent switch to text mode. The parser never
dispatches tools — tool events are display/audit observations only; the MCP bridge journal is
the source of truth for side effects.

Usage: ``feed(chunk)`` while reading, ``finish()`` at EOF, then ``outcome(exit_code, ...)``.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

LINE_LIMIT_BYTES = 1 << 20
STORED_TEXT_LIMIT_BYTES = 4 << 20
API_ERROR_RE = re.compile(r"^\[API Error:[\s\S]*\]$")
STRUCTURAL_TYPES = frozenset({"system", "assistant", "user", "result"})
# Additional event types this profile knowingly ignores. Empty for maestro_stream_json_v1: a new
# type is added here only together with a fixture from the build that emits it.
IGNORED_TYPES: frozenset[str] = frozenset()
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "total_tokens")
_ASSISTANT_BLOCKS = frozenset({"text", "tool_use", "thinking", "redacted_thinking"})


class ProtocolViolation(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class ToolObservation:
    call_id: str
    name: str
    arguments: Any
    result: Optional[str] = None
    is_error: Optional[bool] = None

    @property
    def closed(self) -> bool:
        return self.result is not None


@dataclass
class StreamOutcome:
    completed: bool
    final_text: Optional[str]
    error_kind: Optional[str]
    error_message: Optional[str]
    usage: Optional[dict[str, Optional[float]]]
    usage_partial: Optional[dict[str, Optional[float]]]
    total_cost_usd: Optional[float]
    model: Optional[str]
    cli_session_id: Optional[str]
    observations: list[ToolObservation]
    unmatched_results: list[dict[str, Any]]
    partial_text: Optional[str]
    stdout_bytes: int


def normalize_usage(raw: Any) -> Optional[dict[str, Optional[float]]]:
    """Known usage fields as non-negative numbers; absent or invalid fields stay ``None``."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Optional[float]] = {}
    for key in USAGE_FIELDS:
        value = raw.get(key)
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
        out[key] = (int(value) if float(value).is_integer() else float(value)) if ok else None
    return out


def _add_usage(acc: Optional[dict], new: Optional[dict]) -> Optional[dict]:
    if new is None:
        return acc
    if acc is None:
        return dict(new)
    return {k: (None if acc.get(k) is None and new.get(k) is None else (acc.get(k) or 0) + (new.get(k) or 0))
            for k in USAGE_FIELDS}


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return json.dumps(content, ensure_ascii=False, default=str)


def _is_api_error(text: Optional[str]) -> bool:
    return bool(text) and bool(API_ERROR_RE.match(text.strip()))


@dataclass
class _State:
    model: Optional[str] = None
    cli_session_id: Optional[str] = None
    seen_init: bool = False
    last_text: Optional[str] = None
    last_event_blocks: list[str] = field(default_factory=list)
    usage_partial: Optional[dict] = None
    result: Optional[dict] = None
    observations: dict[str, ToolObservation] = field(default_factory=dict)
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    stored_bytes: int = 0


class StreamParser:
    """Strict line parser. The first violation is latched; later input is ignored."""

    def __init__(self, *, allowed_tools: Optional[frozenset[str]] = None,
                 on_event: Optional[Callable[[str, dict[str, Any]], None]] = None) -> None:
        self._allowed_tools = allowed_tools
        self._on_event = on_event
        self._buffer = bytearray()
        self._state = _State()
        self.stdout_bytes = 0
        self.violation: Optional[ProtocolViolation] = None

    # -- input --------------------------------------------------------------------------------
    def feed(self, chunk: bytes) -> None:
        self.stdout_bytes += len(chunk)
        if self.violation is not None:
            return
        self._buffer.extend(chunk)
        try:
            while (newline := self._buffer.find(b"\n")) >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                self._line(line)
            if len(self._buffer) > LINE_LIMIT_BYTES:
                raise ProtocolViolation("protocol_limit", "stream-json record exceeds 1 MiB")
        except ProtocolViolation as exc:
            self.violation = exc
            self._buffer.clear()

    def finish(self) -> None:
        """EOF: a non-empty unterminated tail is a truncated record even after a result."""
        if self.violation is None and self._buffer.strip():
            self.violation = ProtocolViolation("protocol_truncated", "stream ended inside a record")
        self._buffer.clear()

    def _line(self, raw: bytes) -> None:
        if len(raw) > LINE_LIMIT_BYTES:
            raise ProtocolViolation("protocol_limit", "stream-json record exceeds 1 MiB")
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        if not raw.strip():
            return
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolViolation("protocol_error", f"non-JSON stdout record: {type(exc).__name__}") from exc
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ProtocolViolation("protocol_error", "stdout record is not an object with a type")
        kind = event["type"]
        if kind in IGNORED_TYPES:
            return
        if kind not in STRUCTURAL_TYPES:
            raise ProtocolViolation("protocol_error", f"unknown event type {kind[:40]!r}")
        if self._state.result is not None:
            raise ProtocolViolation("protocol_error", "event after the terminal result")
        _HANDLERS[kind](self, event)
        if self._on_event is not None:
            self._on_event(kind, event)

    # -- event handlers ------------------------------------------------------------------------
    def _system(self, event: dict) -> None:
        if event.get("subtype") != "init":
            return
        st = self._state
        if st.seen_init:
            raise ProtocolViolation("protocol_error", "repeated system/init")
        st.seen_init = True
        st.model = event.get("model") if isinstance(event.get("model"), str) else None
        st.cli_session_id = event.get("session_id") if isinstance(event.get("session_id"), str) else None
        tools = event.get("tools")
        if isinstance(tools, list) and self._allowed_tools is not None:
            extra = sorted(str(t) for t in tools if str(t) not in self._allowed_tools)
            if extra:
                raise ProtocolViolation("tool_policy_violation",
                                        "CLI advertises tools outside the grant: " + ", ".join(extra[:10]))

    def _store(self, text: str) -> None:
        self._state.stored_bytes += len(text.encode("utf-8"))
        if self._state.stored_bytes > STORED_TEXT_LIMIT_BYTES:
            raise ProtocolViolation("protocol_limit", "normalized text/observations exceed 4 MiB")

    def _assistant(self, event: dict) -> None:
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            raise ProtocolViolation("protocol_error", "assistant event without message.content list")
        st = self._state
        if isinstance(message.get("model"), str):
            st.model = st.model or message["model"]
        texts: list[str] = []
        for block in message["content"]:
            btype = block.get("type") if isinstance(block, dict) else None
            if btype not in _ASSISTANT_BLOCKS:
                raise ProtocolViolation("protocol_error", f"unknown assistant block {str(btype)[:40]!r}")
            if btype == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise ProtocolViolation("protocol_error", "assistant text block without text")
                self._store(text)
                texts.append(text)
            elif btype == "tool_use":
                self._tool_use(block)
            # thinking / redacted_thinking: never published, not stored.
        st.last_event_blocks = texts
        if texts:
            st.last_text = "\n".join(texts)
        st.usage_partial = _add_usage(st.usage_partial, normalize_usage(message.get("usage")))

    def _tool_use(self, block: dict) -> None:
        call_id, name = block.get("id"), block.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
            raise ProtocolViolation("protocol_error", "tool_use block without id/name")
        if call_id in self._state.observations:
            raise ProtocolViolation("protocol_error", f"duplicate tool_use id {call_id[:60]!r}")
        if self._allowed_tools is not None and name not in self._allowed_tools:
            raise ProtocolViolation("tool_policy_violation", f"CLI used an ungranted tool {name[:80]!r}")
        self._store(json.dumps(block.get("input"), ensure_ascii=False, default=str))
        self._state.observations[call_id] = ToolObservation(call_id, name, block.get("input"))

    def _user(self, event: dict) -> None:
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            raise ProtocolViolation("protocol_error", "user event without message.content list")
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") not in ("tool_result", "text"):
                raise ProtocolViolation("protocol_error", "unknown user block")
            if block["type"] == "text":
                continue  # CLI echo of input; Hermes already holds the user turn
            text = _result_text(block.get("content"))
            self._store(text)
            obs = self._state.observations.get(block.get("tool_use_id") or "")
            if obs is None or obs.closed:
                self._state.unmatched.append({"tool_use_id": block.get("tool_use_id"), "content": text[:2000]})
                continue
            obs.result, obs.is_error = text, bool(block.get("is_error"))

    def _result(self, event: dict) -> None:
        self._state.result = event

    # -- outcome -------------------------------------------------------------------------------
    def outcome(self, exit_code: Optional[int], *, cancelled: bool = False, timed_out: bool = False) -> StreamOutcome:
        st = self._state
        result = st.result or {}
        usage = normalize_usage(result.get("usage")) if st.result is not None else None
        cost = result.get("total_cost_usd")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost >= 0 else None
        kind, message, final = self._classify(exit_code, cancelled, timed_out)
        return StreamOutcome(
            completed=kind is None, final_text=final if kind is None else None,
            error_kind=kind, error_message=message, usage=usage,
            usage_partial=None if usage is not None else st.usage_partial, total_cost_usd=cost,
            model=st.model, cli_session_id=st.cli_session_id,
            observations=list(st.observations.values()), unmatched_results=list(st.unmatched),
            partial_text=st.last_text, stdout_bytes=self.stdout_bytes,
        )

    def _classify(self, exit_code: Optional[int], cancelled: bool,
                  timed_out: bool) -> tuple[Optional[str], Optional[str], Optional[str]]:
        st = self._state
        if timed_out:
            return "timed_out", "GigaCode run exceeded its wall-clock limit", None
        if cancelled:
            return "cancelled", "GigaCode run was cancelled", None
        if self.violation is not None:
            return self.violation.kind, self.violation.message, None
        failed_exit = exit_code != 0
        if self.stdout_bytes == 0 or (st.result is None and not st.seen_init and st.last_text is None):
            if failed_exit:
                return "nonzero_exit", f"GigaCode exited with code {exit_code} without output", None
            return "protocol_empty_output", "GigaCode produced no events", None
        if st.result is None:
            if failed_exit:
                return "nonzero_exit", f"GigaCode exited with code {exit_code} before a result", None
            return "protocol_missing_result", "stream ended without a terminal result", None
        result = st.result
        if result.get("is_error") is True or result.get("subtype") != "success":
            err = result.get("error")
            detail = err.get("message") if isinstance(err, dict) else (err or result.get("result") or "")
            return "cli_error", f"GigaCode reported an error: {detail}", None
        res_text = result.get("result")
        final = res_text if isinstance(res_text, str) and res_text.strip() else st.last_text
        single_block = st.last_event_blocks[0] if len(st.last_event_blocks) == 1 else None
        if _is_api_error(final) or _is_api_error(single_block):
            return "api_error", "GigaCode reported a model API error behind a success result", None
        if not final or not final.strip():
            return "empty_final_response", "GigaCode finished without a final answer", None
        if failed_exit:
            return "nonzero_exit", f"GigaCode exited with code {exit_code}", None
        return None, None, final.strip()


_HANDLERS: dict[str, Callable[[StreamParser, dict], None]] = {
    "system": StreamParser._system,
    "assistant": StreamParser._assistant,
    "user": StreamParser._user,
    "result": StreamParser._result,
}
