"""GigaCode CLI runtime (``api_mode: gigacode_cli``): one sandboxed ``gigacode`` process per request.

``run_gigacode_turn`` is entered from ``conversation_loop`` after the shared turn setup (session row,
user row, system prompt, permissions) and BEFORE any Hermes model call or compressor. Order:

1. validate config; resolve the durable request key; refuse attachments with a clear answer;
2. journal admission (a repeated key returns the existing run, never a second execution);
3. refuse a session that has an unresolved ``recovery_required`` run;
4. wait for a slot (global limit, 32-deep queue, strict per-session order);
5. verify the operator manifest (``config_unverified`` → the CLI never starts);
6. consume operator grants, build immutable grants, build the context pack (on dequeue);
7. start the per-run MCP bridge, seal config, launch via the driver, read ``stream-json``;
8. revoke the token, clean the run directory, project history, save it ONCE through the agent's
   existing persistence path, and only then mark the run ``succeeded``.

There is no fallback to another provider and no Codex recovery: every failure is reported in this
mode's own result. ``api_calls`` is 0 (Hermes made no model call), ``agent_runs`` is 1, and
``agent_api_calls`` stays ``None`` because the CLI does not report its internal call count reliably.
Tests inject a simulator through ``agent._gigacode_deps`` (:class:`GigacodeRuntimeDeps`); no config
value selects it.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from agent.gigacode import config as gc_config
from agent.gigacode import manifest as gc_manifest
from agent.gigacode import policy, prompt_pack, request_identity
from agent.gigacode.driver import SANDBOX_MCP, BubblewrapDriver, Driver, SealedFiles, load_auth_bundle
from agent.gigacode.errors import GigacodeError, safe_text
from agent.gigacode.journal import GigacodeJournal
from agent.gigacode.scheduler import SCHEDULER, RunScheduler
from agent.gigacode.stream_parser import StreamOutcome, StreamParser
from agent.gigacode.tools import Scope, build_grants
from agent.transports.gigacode_cli_session import GigacodeCliSession, ProcessResult, RunLimits
from agent.transports.gigacode_tools_mcp_server import BridgeContext, GigacodeToolsBridge

logger = logging.getLogger(__name__)

RECOVERY_TEXT = ("Предыдущая задача требует проверки оператором. Код задачи: {run_id}. "
                 "Новые действия в этой сессии пока приостановлены.")
_USER_TEXT = {
    "config_invalid": "Режим GigaCode настроен неверно; запрос не выполнен. Обратитесь к оператору.",
    "config_unverified": "Режим GigaCode не активирован оператором (нет проверенного манифеста); запрос не выполнен.",
    "context_too_large": "Запрос вместе с обязательными инструкциями не помещается в контекст GigaCode.",
    "runtime_busy": "GigaCode сейчас занят: очередь заполнена. Повторите запрос позже.",
    "unsupported_attachment": "Вложения (изображения, файлы, голос) пока не поддерживаются в режиме GigaCode. "
                              "Отправьте запрос текстом.",
    "cancelled": "Задача отменена.",
    "timed_out": "Задача прервана: превышено время выполнения.",
    "cron_provenance_missing": "Задание по расписанию не имеет владельца и пропущено. Оператору нужно "
                               "назначить владельца и профиль (см. инструкцию GigaCode runtime).",
    "restart_before_start": "Hermes перезапустился до начала выполнения. Отправьте запрос ещё раз.",
    "policy_scope_denied": "Режим GigaCode пока работает только в личных сообщениях владельца.",
}
_STATE_FOR_KIND = {"cancelled": "cancelled", "timed_out": "timed_out"}
_MAX_TOOL_RESULT_CHARS = 20000


def user_text(kind: str, run_id: Optional[str]) -> str:
    if kind == "session_recovery_required":
        return RECOVERY_TEXT.format(run_id=run_id or "?")
    base = _USER_TEXT.get(kind, "GigaCode не смог выполнить запрос ({kind}).".format(kind=kind))
    return f"{base} Код задачи: {run_id}." if run_id else base


def _runs_root() -> Path:
    from hermes_constants import get_hermes_home

    root = get_hermes_home() / "gigacode" / "runs"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _bubblewrap_driver(settings: gc_config.GigacodeSettings, manifest: gc_manifest.VerifiedManifest) -> Driver:
    return BubblewrapDriver(bwrap=settings.bwrap_executable or "", rootfs=settings.runtime_rootfs or "",
                            executable=settings.executable or "",
                            system_settings_paths=manifest.system_settings_paths,
                            grace_seconds=settings.termination_grace_seconds)


@dataclass
class GigacodeRuntimeDeps:
    load_settings: Callable[[], gc_config.GigacodeSettings] = gc_config.load_settings
    verify: Callable[[gc_config.GigacodeSettings], gc_manifest.VerifiedManifest] = gc_manifest.verify_activation
    make_driver: Callable[[gc_config.GigacodeSettings, gc_manifest.VerifiedManifest], Driver] = _bubblewrap_driver
    journal: Callable[[], GigacodeJournal] = GigacodeJournal
    scheduler: RunScheduler = field(default_factory=lambda: SCHEDULER)
    runs_root: Callable[[], Path] = _runs_root


def apply_gigacode_agent_policy(agent: Any) -> None:
    """Switch off every Hermes-side model path for this agent (called at the end of init).

    Compression is replaced by deterministic pack truncation; background memory/skill review and
    titling upgrades would call the previous provider; the fallback chain must never activate.
    """
    agent.compression_enabled = False
    agent.skip_background_review = True
    agent._memory_nudge_interval = 0
    agent._skill_nudge_interval = 0
    agent._fallback_chain = []
    agent._fallback_model = None


def cancel_agent_run(agent: Any) -> None:
    """Cancel the agent's in-flight run (agent close / gateway shutdown)."""
    event = getattr(agent, "_gigacode_cancel", None)
    if event is not None:
        event.set()


def _consume_interrupt(agent: Any) -> tuple[bool, Any]:
    interrupted = bool(getattr(agent, "_interrupt_requested", False))
    message = getattr(agent, "_interrupt_message", None) if interrupted else None
    if interrupted and hasattr(agent, "clear_interrupt"):
        agent.clear_interrupt()
    return interrupted, message


def project_messages(outcome: StreamOutcome, qualified_prefix: str) -> list[dict[str, Any]]:
    """Hermes rows for the turn: matched tool call/result pairs (display + audit cross-reference, never
    re-executed) followed by the final answer. Unpaired observations stay in the run journal only."""
    pairs = [o for o in outcome.observations if o.closed]
    rows: list[dict[str, Any]] = []
    if pairs:
        rows.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": o.call_id, "type": "function",
            "function": {"name": o.name.removeprefix(qualified_prefix),
                         "arguments": json.dumps(o.arguments, ensure_ascii=False, default=str)},
        } for o in pairs]})
        for o in pairs:
            rows.append({"role": "tool", "tool_call_id": o.call_id, "name": o.name.removeprefix(qualified_prefix),
                         "content": (o.result or "")[:_MAX_TOOL_RESULT_CHARS]})
    rows.append({"role": "assistant", "content": outcome.final_text or ""})
    return rows


def _usage_fields(outcome: StreamOutcome) -> dict[str, Any]:
    return {"usage": outcome.usage, "usage_partial": outcome.usage_partial,
            "total_cost_usd": outcome.total_cost_usd,
            "input_tokens": (outcome.usage or {}).get("input_tokens"),
            "output_tokens": (outcome.usage or {}).get("output_tokens")}


def _stderr_kind(process: ProcessResult) -> Optional[str]:
    tail = process.stderr_tail.lower()
    if b"/home/gigacode/.gigacode" in tail and (b"erofs" in tail or b"read-only file system" in tail):
        return "auth_refresh_required"
    return None


class _TurnRun:
    def __init__(self, agent: Any, deps: GigacodeRuntimeDeps, user_message: Any,
                 messages: list[dict[str, Any]], task_id: str) -> None:
        self.agent, self.deps = agent, deps
        self.user_message, self.messages, self.task_id = user_message, messages, task_id
        self.run_id: Optional[str] = None
        self.journal: Optional[GigacodeJournal] = None
        self.cancel = threading.Event()

    # -- results ------------------------------------------------------------------------------
    def _result(self, *, completed: bool, final_response: str, error: Optional[GigacodeError] = None,
                agent_persisted: bool = True, **extra: Any) -> dict[str, Any]:
        interrupted, interrupt_message = _consume_interrupt(self.agent)
        result = {
            "final_response": final_response, "messages": self.messages, "completed": completed,
            "partial": not completed, "interrupted": interrupted or (error is not None and error.kind == "cancelled"),
            "error": error.as_dict(self.run_id) if error else None, "failed": not completed,
            "failure_reason": error.kind if error else None,
            "api_calls": 0, "agent_runs": 1 if self.run_id else 0, "agent_api_calls": None,
            "run_id": self.run_id, "agent_persisted": agent_persisted, **extra,
        }
        if interrupt_message:
            result["interrupt_message"] = interrupt_message
        return result

    def _error(self, exc: GigacodeError, state: Optional[str] = None, **fields: Any) -> dict[str, Any]:
        if self.journal is not None and self.run_id is not None:
            self.journal.update(self.run_id, state=state or _STATE_FOR_KIND.get(exc.kind, "failed"),
                                error_kind=exc.kind, error_message=safe_text(exc.message),
                                finished_at=time.time(), **fields)
        logger.warning("gigacode run %s failed: %s", self.run_id, exc)
        return self._result(completed=False, final_response=user_text(exc.kind, self.run_id), error=exc)

    # -- entry --------------------------------------------------------------------------------
    def execute(self) -> dict[str, Any]:
        try:
            settings = self.deps.load_settings()
            identity = request_identity.resolve(self.agent)
        except GigacodeError as exc:
            return self._error(exc)
        except LookupError as exc:
            logger.warning("gigacode: skipping cron job without provenance: %s", exc)
            return self._error(GigacodeError("cron_provenance_missing", str(exc)))
        chat_type = getattr(self.agent, "_chat_type", None)
        if identity.source == "owner_private" and chat_type not in (None, "", "dm"):
            # owner_private covers the owner's private chat only; groups need their own isolation review.
            return self._error(GigacodeError("policy_scope_denied", f"chat type {chat_type!r} is not private"))
        if prompt_pack.has_attachments(self.user_message):
            return self._error(GigacodeError("unsupported_attachment", "attachments are not supported"))
        self.journal = self.deps.journal()
        self.journal.recover_orphans()
        session_id = getattr(self.agent, "session_id", None)
        admission = self.journal.admit(profile_id=identity.profile_id, channel=identity.channel,
                                       user_id=identity.user_id, request_id=identity.request_id,
                                       session_id=session_id, source=identity.source)
        self.run_id = admission.run_id
        if admission.duplicate:
            return self._duplicate(admission.row)
        blocker: Optional[str] = None
        try:
            blocker = self.journal.session_blocker(session_id)
            if blocker is not None:
                raise GigacodeError("session_recovery_required", RECOVERY_TEXT.format(run_id=blocker))
            deadline = time.monotonic() + settings.wall_timeout_seconds
            with self.deps.scheduler.slot(session_key=session_id or self.run_id, limit=settings.max_concurrent_runs,
                                          should_abort=self._should_cancel, deadline=deadline):
                return self._run_admitted(settings, identity)
        except GigacodeError as exc:
            if exc.kind == "session_recovery_required":
                return self._blocked(exc, blocker)
            return self._error(exc)

    def _blocked(self, exc: GigacodeError, blocker: Optional[str]) -> dict[str, Any]:
        self.journal.update(self.run_id, state="failed", error_kind=exc.kind, error_message=exc.message,
                            finished_at=time.time())
        return self._result(completed=False, final_response=RECOVERY_TEXT.format(run_id=blocker), error=exc)

    def _duplicate(self, row: dict[str, Any]) -> dict[str, Any]:
        """A redelivered request: report the existing run, never execute again."""
        exc = GigacodeError("duplicate_request", f"request already handled by {row['run_id']} ({row['state']})")
        if row["state"] in ("succeeded", "resolved_succeeded") and row.get("final_response"):
            return self._result(completed=False, final_response=row["final_response"], error=exc)
        text = f"Этот запрос уже принят (состояние: {row['state']}). Код задачи: {row['run_id']}."
        return self._result(completed=False, final_response=text, error=exc)

    def _should_cancel(self) -> bool:
        if self.cancel.is_set():
            return True
        if getattr(self.agent, "_interrupt_requested", False):
            self.cancel.set()
            return True
        return False

    # -- admitted run -------------------------------------------------------------------------
    def _run_admitted(self, settings: gc_config.GigacodeSettings,
                      identity: request_identity.RequestIdentity) -> dict[str, Any]:
        manifest = self.deps.verify(settings)
        rows = self.journal.consume_grants(self.run_id)
        scope = Scope(identity.profile_id, identity.channel, identity.user_id, getattr(self.agent, "session_id", None))
        grants = build_grants(policy=settings.tool_policy, source=identity.source, scope=scope,
                              expires_at=time.time() + settings.wall_timeout_seconds, operator_rows=rows,
                              enabled_tools=getattr(self.agent, "valid_tool_names", ()) or ())
        pack = self._build_pack(settings, identity, grants)
        bridge = GigacodeToolsBridge(BridgeContext(
            run_id=self.run_id, session_id=scope.session_id, profile_id=identity.profile_id,
            user_id=identity.user_id, task_id=self.task_id, grants=grants, cancellation=self.cancel,
            agent=self.agent, journal=self.journal, expires_at=time.time() + settings.wall_timeout_seconds,
            skills_allowlist=frozenset(settings.skills_allowlist),
        ))
        self.agent._gigacode_cancel = self.cancel
        self.deps.scheduler.register(self.run_id, self.cancel)
        try:
            bridge.start()
            process = self._launch(settings, manifest, bridge, pack)
        except OSError as exc:
            raise GigacodeError("bridge_failed", f"could not start the run: {type(exc).__name__}") from exc
        finally:
            bridge.stop()
            self.journal.update(self.run_id, token_revoked=1,
                                untrusted_input_seen=int(bridge.context.untrusted_input_seen))
            self.deps.scheduler.unregister(self.run_id)
            self.agent._gigacode_cancel = None
        return self._finish(process, manifest)

    def _build_pack(self, settings: gc_config.GigacodeSettings, identity: request_identity.RequestIdentity,
                    grants: tuple) -> prompt_pack.PromptPack:
        system = getattr(self.agent, "_cached_system_prompt", None) or ""
        if getattr(self.agent, "ephemeral_system_prompt", None):
            system = (system + "\n\n" + self.agent.ephemeral_system_prompt).strip()
        session_data = {
            "scope": {"session": prompt_pack.opaque_id("session", getattr(self.agent, "session_id", None)),
                      "profile": prompt_pack.opaque_id("profile", identity.profile_id),
                      "user": prompt_pack.opaque_id("user", identity.user_id),
                      "channel": identity.channel.split(":", 1)[0]},
            "granted_tools": [{"tool": g.wire_tool, "actions": sorted(g.actions)} for g in grants],
            "memory": "Saved memory is part of SYSTEM INSTRUCTIONS; call hermes_memory_read for its current state.",
            "unresolved_actions": [
                {"run": e["run_id"], "tool": e["wire_tool"], "action": e["action"],
                 "note": "outcome unknown; do not repeat it automatically"}
                for e in self.journal.unknown_effects(getattr(self.agent, "session_id", None))],
        }
        history = self.messages[:-1] if self.messages and self.messages[-1].get("role") == "user" else self.messages
        return prompt_pack.build(system_instructions=system, session_data=session_data, history=history,
                                 request=prompt_pack.text_of(self.user_message),
                                 budget_tokens=int(settings.prompt_budget_tokens or 0))

    def _launch(self, settings: gc_config.GigacodeSettings, manifest: gc_manifest.VerifiedManifest,
                bridge: GigacodeToolsBridge, pack: prompt_pack.PromptPack) -> ProcessResult:
        driver = self.deps.make_driver(settings, manifest)
        wire = bridge.context.wire_tools
        sealed = SealedFiles(
            mcp_config=policy.mcp_config(url=bridge.url, token=bridge.token, wire_tools=wire,
                                         tool_timeout_ms=min(600_000, settings.wall_timeout_seconds * 1000)),
            settings=policy.generate_settings(model=settings.model),
            instructions_md=prompt_pack.instructions_md(),
        )
        auth = load_auth_bundle(settings.auth_bundle, manifest.auth_files)
        cli_args = policy.build_argv(settings.executable or "", SANDBOX_MCP, model=settings.model)
        run_dir = self.deps.runs_root() / self.run_id
        try:
            descriptor = driver.prepare(self.run_id, run_dir, sealed, auth, cli_args)
            parser = StreamParser(allowed_tools=frozenset(manifest.qualified_tool_prefix + w for w in wire),
                                  on_event=self._on_event(manifest.qualified_tool_prefix))
            limits = RunLimits(wall_timeout_seconds=settings.wall_timeout_seconds,
                               silence_warning_seconds=settings.silence_warning_seconds,
                               termination_grace_seconds=settings.termination_grace_seconds)
            session = GigacodeCliSession(
                driver, descriptor, limits, parser,
                on_spawn=lambda handle: self.journal.update(self.run_id, process_identity=handle.identity()))
            self.journal.mark_running(self.run_id, {"driver": driver.name})
            return session.run(pack.text.encode("utf-8"), self.cancel, should_cancel=self._should_cancel)
        finally:
            if run_dir.exists():
                driver.cleanup(run_dir)

    def _on_event(self, prefix: str) -> Callable[[str, dict[str, Any]], None]:
        """Safe progress for the chat surface: tool names only (never arguments, results or reasoning)."""
        agent = self.agent

        def emit(kind: str, event: dict[str, Any]) -> None:
            touch = getattr(agent, "_touch_activity", None)
            if callable(touch):
                touch(f"gigacode: {kind}")
            callback = getattr(agent, "tool_progress_callback", None)
            if kind != "assistant" or not callable(callback):
                return
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "").removeprefix(prefix)
                    try:
                        callback("tool.started", name, None, {})
                    except Exception:
                        logger.debug("tool_progress_callback raised", exc_info=True)
        return emit

    # -- completion ---------------------------------------------------------------------------
    def _finish(self, process: ProcessResult, manifest: gc_manifest.VerifiedManifest) -> dict[str, Any]:
        outcome = process.outcome
        unknown = self.journal.mark_unfinished_operations_unknown(self.run_id) > 0 or any(
            a["outcome"] == "unknown" for a in self.journal.audit(self.run_id))
        self.journal.update(
            self.run_id, exit_code=process.exit_code, model=outcome.model, cli_session_id=outcome.cli_session_id,
            cli_version=manifest.data["cli"]["version"],
            cleanup_state="complete" if process.cleanup_complete else "incomplete",
            usage={"final": outcome.usage, "partial": outcome.usage_partial, "cost_usd": outcome.total_cost_usd,
                   "stderr_truncated": process.stderr_truncated, "stdout_bytes": outcome.stdout_bytes},
        )
        usage = _usage_fields(outcome)
        if not process.cleanup_complete:
            return self._error(GigacodeError("cleanup_incomplete", "the process tree could not be confirmed stopped"),
                               state="recovery_required")
        if not outcome.completed:
            kind = _stderr_kind(process) or outcome.error_kind or "protocol_error"
            state = "recovery_required" if unknown else None
            return {**self._error(GigacodeError(kind, outcome.error_message or kind), state=state), **usage}
        return {**self._persist(outcome, manifest.qualified_tool_prefix), **usage}

    def _persist(self, outcome: StreamOutcome, prefix: str) -> dict[str, Any]:
        """History has one owner: the agent's existing session-DB flush, called once; ``succeeded``
        is recorded only after it confirms. A failed save leaves the run ``recovery_required``."""
        from agent.message_metadata import append_message, stamp_message_uid

        projected = project_messages(outcome, prefix)
        for row in projected:
            stamp_message_uid(row)  # the same logical rows if reconcile has to write them later
        start = len(self.messages)
        for row in projected:
            append_message(self.messages, row)
        if getattr(self.agent, "_session_db", None) is None:
            self.journal.update(self.run_id, state="succeeded", cli_completed=1, finished_at=time.time(),
                                final_response=outcome.final_text)
            return self._result(completed=True, final_response=outcome.final_text or "", agent_persisted=False)
        try:
            flushed = self.agent._flush_messages_to_session_db(self.messages)
        except Exception:
            logger.warning("gigacode history flush raised", exc_info=True)
            flushed = False
        if flushed is not True:
            del self.messages[start:]
            exc = GigacodeError("persist_failed", "the answer was produced but history could not be saved")
            return self._error(exc, state="recovery_required", cli_completed=1, final_response=outcome.final_text,
                               projected_messages=projected)
        self.journal.update(self.run_id, state="succeeded", cli_completed=1, history_written=1,
                            finished_at=time.time(), final_response=outcome.final_text)
        return self._result(completed=True, final_response=outcome.final_text or "")


def run_gigacode_turn(agent: Any, *, user_message: Any, messages: list[dict[str, Any]],
                      effective_task_id: str) -> dict[str, Any]:
    """Run one Hermes turn through the GigaCode CLI. ``messages`` already ends with the user row."""
    deps = getattr(agent, "_gigacode_deps", None) or GigacodeRuntimeDeps()
    return _TurnRun(agent, deps, user_message, messages, effective_task_id).execute()
