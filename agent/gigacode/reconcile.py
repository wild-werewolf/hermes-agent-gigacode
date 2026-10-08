"""Operator resolution of ``recovery_required`` runs (``hermes gigacode reconcile``).

Never runs GigaCode, never sends a message. Before any change it checks that the run's process tree
is gone, its bridge token is revoked (the bridge dies with its owner process) and no other run of
the session is active. Decisions:

* ``succeeded`` — only for a confirmed successful CLI result with stored projected history; the
  history is written ONCE through ``SessionDB.append_messages_batch`` (one transaction), then the
  run becomes ``resolved_succeeded``;
* ``failed`` → ``resolved_failed`` (nothing is delivered as a success);
* ``abandoned`` → ``abandoned`` (unknown effects stay marked unknown; nothing is replayed).

The decision is durable and idempotent: repeating the same decision is a no-op, a different one
is refused. After any decision the session accepts new requests again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from agent.gigacode import reaper as _reaper
from agent.gigacode.journal import GigacodeJournal, JournalError, _owner_alive

DECISIONS = {"succeeded": "resolved_succeeded", "failed": "resolved_failed", "abandoned": "abandoned"}


@dataclass(frozen=True)
class ReconcilePlan:
    run_id: str
    decision: str
    new_state: str
    writes_history: bool
    message_count: int
    already_resolved: bool = False


def _process_gone(run: dict[str, Any]) -> bool:
    identity = json.loads(run.get("process_identity") or "{}")
    pid, start = identity.get("supervisor_pid"), identity.get("supervisor_start")
    if pid is None:
        return True
    info = _reaper._stat(int(pid))
    return info is None or info[1] != start


def plan(journal: GigacodeJournal, run_id: str, decision: str) -> ReconcilePlan:
    if decision not in DECISIONS:
        raise JournalError(f"decision must be one of {sorted(DECISIONS)}")
    run = journal.run(run_id)
    if run is None:
        raise JournalError(f"unknown run {run_id}")
    existing = journal.resolution(run_id)
    if existing is not None:
        if existing["decision"] != decision:
            raise JournalError(f"run already resolved as {existing['decision']!r}; a different decision is refused")
        return ReconcilePlan(run_id, decision, DECISIONS[decision], False, 0, already_resolved=True)
    if run["state"] != "recovery_required":
        raise JournalError(f"run is {run['state']}; only recovery_required runs are reconciled")
    if _owner_alive(run["owner_pid"], run["owner_create"]) and not run["token_revoked"]:
        raise JournalError("the Hermes process that owns this run is alive and its token is not revoked")
    if not _process_gone(run):
        raise JournalError("the run's process tree is still alive; stop it first")
    messages = json.loads(run.get("projected_messages") or "[]")
    writes = decision == "succeeded" and not run["history_written"]
    if decision == "succeeded" and not (run["cli_completed"] and run.get("final_response") and messages):
        raise JournalError("no confirmed successful CLI result is stored; decide failed or abandoned")
    return ReconcilePlan(run_id, decision, DECISIONS[decision], writes, len(messages) if writes else 0)


def apply(journal: GigacodeJournal, run_id: str, decision: str, *, reason: str, operator: str,
          open_session_db: Callable[[], Any]) -> ReconcilePlan:
    if not reason.strip():
        raise JournalError("--reason is required")
    result = plan(journal, run_id, decision)
    if result.already_resolved:
        return result
    run = journal.run(run_id) or {}
    if result.writes_history:
        _write_history(run, open_session_db)
    journal.record_resolution(run_id, decision=decision, reason=reason.strip(), operator=operator,
                              new_state=result.new_state, history_written=result.writes_history)
    return result


def _write_history(run: dict[str, Any], open_session_db: Callable[[], Any]) -> None:
    session_id: Optional[str] = run.get("session_id")
    if not session_id:
        raise JournalError("run has no session to write history to")
    messages = json.loads(run.get("projected_messages") or "[]")
    db = open_session_db()
    try:
        # Rows carry the message_uid minted at projection: any row an interrupted earlier attempt (or the
        # original flush) already committed is skipped, so the history lands exactly once.
        uids = [m.get("message_uid") for m in messages if m.get("message_uid")]
        present: set[str] = set()
        if uids:
            marks = ",".join("?" for _ in uids)
            rows = db._read_all(f"SELECT message_uid FROM messages WHERE session_id = ? AND active = 1 "
                                f"AND message_uid IN ({marks})", [session_id, *uids])
            present = {str(r["message_uid"]) for r in rows}
        pending = [m for m in messages if m.get("message_uid") not in present]
        if pending:
            db.append_messages_batch(session_id, pending)
    finally:
        db.close()
