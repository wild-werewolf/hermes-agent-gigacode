"""Durable GigaCode run journal: ``<HERMES_HOME>/gigacode/journal.db`` (its own SQLite file).

Holds, per profile home:

* ``runs`` — one row per request key ``(profile_id, channel, user_id, request_id)``, written
  BEFORE the process starts. States: ``queued → running → succeeded | failed | cancelled |
  timed_out``; ``recovery_required`` after a crash with unknown effects or a failed history save;
  operator resolution moves it to ``resolved_succeeded | resolved_failed | abandoned``.
* ``pending_grants`` — operator-issued one-shot grants (``hermes gigacode grants issue``).
* ``tool_audit`` — every bridge ``tools/call`` (hash of arguments, never the arguments).
* ``resolutions`` — durable, idempotent ``hermes gigacode reconcile`` decisions.

A separate file keeps the existing ``state.db`` schema untouched (no migration of user data).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "timed_out", "resolved_succeeded",
                             "resolved_failed", "abandoned"})
ACTIVE_STATES = frozenset({"queued", "running"})
ALL_STATES = TERMINAL_STATES | ACTIVE_STATES | {"recovery_required"}
_lock = threading.Lock()


def journal_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "gigacode" / "journal.db"


def canonical_request_key(profile_id: str, channel: str, user_id: str, request_id: str) -> str:
    """The exact, displayed serialization ``runs show`` prints and ``--request-key`` accepts."""
    parts = [profile_id, channel, user_id, request_id]
    if not all(isinstance(p, str) and p for p in parts):
        raise ValueError("request key needs non-empty profile, channel, user and request ids")
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def cron_request_id(job_id: str, scheduled_fire_time_utc: str) -> str:
    """SHA-256 of ``job_id:scheduled_fire_time_utc`` (planned fire time, canonical UTC ISO form)."""
    return hashlib.sha256(f"{job_id}:{scheduled_fire_time_utc}".encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


def _owner() -> tuple[int, Optional[float]]:
    from hermes_cli.process_identity import _process_create_time

    return os.getpid(), _process_create_time()


def _owner_alive(pid: Optional[int], create_time: Optional[float]) -> bool:
    if not pid:
        return False
    from hermes_cli.process_identity import _pid_alive_matches

    alive = _pid_alive_matches(int(pid), create_time)
    return alive is not False  # unknown → assume alive (never auto-fail a run we cannot prove dead)


def _schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
          run_id TEXT PRIMARY KEY,
          request_key TEXT NOT NULL UNIQUE,
          profile_id TEXT NOT NULL, channel TEXT NOT NULL, user_id TEXT NOT NULL, request_id TEXT NOT NULL,
          session_id TEXT, source TEXT NOT NULL,
          state TEXT NOT NULL, error_kind TEXT, error_message TEXT,
          admitted_at REAL NOT NULL, started_at REAL, finished_at REAL,
          owner_pid INTEGER, owner_create REAL, process_identity TEXT,
          exit_code INTEGER, cleanup_state TEXT NOT NULL DEFAULT 'pending',
          token_revoked INTEGER NOT NULL DEFAULT 0,
          cli_completed INTEGER NOT NULL DEFAULT 0,
          final_response TEXT, projected_messages TEXT, usage TEXT,
          model TEXT, cli_session_id TEXT, cli_version TEXT,
          untrusted_input_seen INTEGER NOT NULL DEFAULT 0,
          history_written INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, state);
        CREATE INDEX IF NOT EXISTS idx_runs_scope ON runs(profile_id, channel, user_id, admitted_at);
        CREATE TABLE IF NOT EXISTS pending_grants (
          grant_id TEXT PRIMARY KEY,
          profile_id TEXT NOT NULL, channel TEXT NOT NULL, user_id TEXT NOT NULL,
          selector TEXT NOT NULL CHECK(selector IN ('request_key','next_request')),
          request_key TEXT,
          wire_tool TEXT NOT NULL, actions TEXT NOT NULL, path_roots TEXT NOT NULL,
          created_at REAL NOT NULL, expires_at REAL NOT NULL, operator TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('pending','consumed','revoked','expired')),
          consumed_by TEXT, revoked_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_grants_scope ON pending_grants(profile_id, channel, user_id, state);
        CREATE TABLE IF NOT EXISTS tool_audit (
          operation_id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL, wire_tool TEXT NOT NULL, internal_tool TEXT NOT NULL,
          action TEXT, args_sha256 TEXT NOT NULL, grant_source TEXT,
          started_at REAL NOT NULL, finished_at REAL,
          outcome TEXT NOT NULL CHECK(outcome IN ('started','succeeded','failed','unknown','denied')),
          detail TEXT, untrusted_input_before INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_audit_run ON tool_audit(run_id, started_at);
        CREATE TABLE IF NOT EXISTS resolutions (
          run_id TEXT PRIMARY KEY, decision TEXT NOT NULL, reason TEXT NOT NULL,
          operator TEXT NOT NULL, created_at REAL NOT NULL
        );
        """
    )


@dataclass(frozen=True)
class Admission:
    run_id: str
    state: str
    duplicate: bool
    row: dict[str, Any]


class JournalError(Exception):
    """Operator-facing refusal (bad grant, illegal transition, unknown run)."""


class GigacodeJournal:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else journal_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        from hermes_cli.sqlite_util import open_db, transaction

        with _lock:
            conn = open_db(self.path, db_label="gigacode/journal.db", synchronous_full=True, initialize=_schema)
            if self.path.exists():
                os.chmod(self.path, 0o600)
            with transaction(conn, immediate=True) as tx:
                yield tx

    # -- runs ---------------------------------------------------------------------------------
    def run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def run_by_key(self, request_key: str) -> Optional[dict[str, Any]]:
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE request_key=?", (request_key,)).fetchone()
            return dict(row) if row else None

    def recover_orphans(self) -> list[str]:
        """Non-terminal runs whose owner process is provably gone: ``running`` → ``recovery_required``;
        ``queued`` (never started) → ``failed``/``restart_before_start``. Nothing is re-queued."""
        changed: list[str] = []
        with self._txn() as conn:
            rows = conn.execute("SELECT run_id, state, owner_pid, owner_create FROM runs "
                                "WHERE state IN ('queued','running')").fetchall()
            for row in rows:
                if _owner_alive(row["owner_pid"], row["owner_create"]):
                    continue
                if row["state"] == "running":
                    conn.execute("UPDATE runs SET state='recovery_required', error_kind='cleanup_incomplete', "
                                 "error_message=?, finished_at=? WHERE run_id=?",
                                 ("Hermes stopped while the run was in progress; effects are unknown",
                                  _now(), row["run_id"]))
                else:
                    conn.execute("UPDATE runs SET state='failed', error_kind='restart_before_start', "
                                 "error_message=?, finished_at=? WHERE run_id=?",
                                 ("Hermes restarted before this request started; send it again",
                                  _now(), row["run_id"]))
                changed.append(row["run_id"])
        return changed

    def admit(self, *, profile_id: str, channel: str, user_id: str, request_id: str,
              session_id: Optional[str], source: str) -> Admission:
        """Insert the durable ``queued`` row, or return the existing run for a repeated request key."""
        key = canonical_request_key(profile_id, channel, user_id, request_id)
        pid, create = _owner()
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE request_key=?", (key,)).fetchone()
            if row is not None:
                return Admission(row["run_id"], row["state"], True, dict(row))
            run_id = "gc_" + uuid.uuid4().hex
            conn.execute(
                "INSERT INTO runs(run_id, request_key, profile_id, channel, user_id, request_id, session_id, "
                "source, state, admitted_at, owner_pid, owner_create) VALUES (?,?,?,?,?,?,?,?, 'queued', ?,?,?)",
                (run_id, key, profile_id, channel, user_id, request_id, session_id, source, _now(), pid, create),
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return Admission(run_id, "queued", False, dict(row))

    def session_blocker(self, session_id: Optional[str]) -> Optional[str]:
        """run_id of an unresolved ``recovery_required`` run in this session, if any."""
        if not session_id:
            return None
        with self._txn() as conn:
            row = conn.execute("SELECT run_id FROM runs WHERE session_id=? AND state='recovery_required' "
                               "ORDER BY admitted_at LIMIT 1", (session_id,)).fetchone()
            return row["run_id"] if row else None

    def unknown_effects(self, session_id: Optional[str], limit: int = 20) -> list[dict[str, Any]]:
        """Bridge operations with unknown outcome from earlier runs of this session (for the prompt)."""
        if not session_id:
            return []
        with self._txn() as conn:
            rows = conn.execute(
                "SELECT a.run_id, a.wire_tool, a.action, a.started_at FROM tool_audit a JOIN runs r "
                "ON r.run_id=a.run_id WHERE r.session_id=? AND a.outcome IN ('unknown','started') "
                "ORDER BY a.started_at DESC LIMIT ?", (session_id, limit)).fetchall()
            return [dict(r) for r in rows]

    def mark_running(self, run_id: str, identity: Mapping[str, Any]) -> None:
        with self._txn() as conn:
            cur = conn.execute("UPDATE runs SET state='running', started_at=?, process_identity=? "
                               "WHERE run_id=? AND state='queued'", (_now(), json.dumps(identity), run_id))
            if cur.rowcount != 1:
                raise JournalError(f"run {run_id} is no longer queued")

    def update(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "state" in fields and fields["state"] not in ALL_STATES:
            raise JournalError(f"unknown run state {fields['state']!r}")
        encoded = {k: (json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v)
                   for k, v in fields.items()}
        assignments = ", ".join(f"{k}=?" for k in encoded)
        with self._txn() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE run_id=?", (*encoded.values(), run_id))

    def list_runs(self, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._txn() as conn:
            rows = conn.execute("SELECT run_id, request_key, state, error_kind, admitted_at, finished_at "
                                "FROM runs ORDER BY admitted_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    # -- grants -------------------------------------------------------------------------------
    def issue_grant(self, *, profile_id: str, channel: str, user_id: str, wire_tool: str,
                    actions: Sequence[str], ttl_seconds: int, operator: str,
                    request_key: Optional[str] = None, next_request: bool = False,
                    path_roots: Sequence[str] = ()) -> dict[str, Any]:
        if bool(request_key) == bool(next_request):
            raise JournalError("exactly one of --request-key / --next-request is required")
        for value in (profile_id, channel, user_id):
            if not value or "*" in value or "," in value:
                raise JournalError("profile, channel and user must be single explicit ids (no wildcards)")
        now = _now()
        with self._txn() as conn:
            if request_key:
                parsed = json.loads(request_key)
                if parsed[:3] != [profile_id, channel, user_id]:
                    raise JournalError("request key scope differs from --profile/--channel/--user")
                run = conn.execute("SELECT state FROM runs WHERE request_key=?", (request_key,)).fetchone()
                if run is not None and run["state"] != "queued":
                    raise JournalError(f"request is already {run['state']}; grants cannot be added to it")
            else:
                clash = conn.execute(
                    "SELECT grant_id FROM pending_grants WHERE profile_id=? AND channel=? AND user_id=? "
                    "AND wire_tool=? AND selector='next_request' AND state='pending' AND expires_at>?",
                    (profile_id, channel, user_id, wire_tool, now)).fetchone()
                if clash is not None:
                    raise JournalError(f"a pending --next-request grant already exists: {clash['grant_id']}")
            grant_id = "gr_" + uuid.uuid4().hex[:16]
            conn.execute(
                "INSERT INTO pending_grants(grant_id, profile_id, channel, user_id, selector, request_key, "
                "wire_tool, actions, path_roots, created_at, expires_at, operator, state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending')",
                (grant_id, profile_id, channel, user_id, "request_key" if request_key else "next_request",
                 request_key, wire_tool, json.dumps(sorted(set(actions))), json.dumps(list(path_roots)),
                 now, now + ttl_seconds, operator),
            )
            return dict(conn.execute("SELECT * FROM pending_grants WHERE grant_id=?", (grant_id,)).fetchone())

    def list_grants(self, profile_id: str) -> list[dict[str, Any]]:
        with self._txn() as conn:
            conn.execute("UPDATE pending_grants SET state='expired' WHERE state='pending' AND expires_at<=?",
                         (_now(),))
            rows = conn.execute("SELECT * FROM pending_grants WHERE profile_id=? ORDER BY created_at DESC",
                                (profile_id,)).fetchall()
            return [dict(r) for r in rows]

    def revoke_grant(self, grant_id: str) -> dict[str, Any]:
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM pending_grants WHERE grant_id=?", (grant_id,)).fetchone()
            if row is None:
                raise JournalError(f"unknown grant {grant_id}")
            if row["state"] in ("pending", "consumed"):
                conn.execute("UPDATE pending_grants SET state='revoked', revoked_at=? WHERE grant_id=?",
                             (_now(), grant_id))
            return dict(conn.execute("SELECT * FROM pending_grants WHERE grant_id=?", (grant_id,)).fetchone())

    def grant_usable(self, grant_id: str, run_id: str) -> bool:
        """Re-checked on every ``tools/call``: consumed by this run, not revoked, not expired."""
        with self._txn() as conn:
            row = conn.execute("SELECT state, consumed_by, expires_at FROM pending_grants WHERE grant_id=?",
                               (grant_id,)).fetchone()
            return bool(row and row["state"] == "consumed" and row["consumed_by"] == run_id
                        and row["expires_at"] > _now())

    def consume_grants(self, run_id: str) -> list[dict[str, Any]]:
        """Atomically bind the matching pending grants to ``run_id`` at dequeue (single winner)."""
        now = _now()
        with self._txn() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise JournalError(f"unknown run {run_id}")
            conn.execute("UPDATE pending_grants SET state='expired' WHERE state='pending' AND expires_at<=?", (now,))
            candidates = conn.execute(
                "SELECT * FROM pending_grants WHERE state='pending' AND profile_id=? AND channel=? AND user_id=? "
                "AND (request_key=? OR (selector='next_request' AND created_at < ?)) ORDER BY created_at",
                (run["profile_id"], run["channel"], run["user_id"], run["request_key"], run["admitted_at"]),
            ).fetchall()
            won: list[dict[str, Any]] = []
            for grant in candidates:
                if grant["selector"] == "next_request" and self._earlier_request(conn, run, grant):
                    continue
                cur = conn.execute("UPDATE pending_grants SET state='consumed', consumed_by=? "
                                   "WHERE grant_id=? AND state='pending'", (run_id, grant["grant_id"]))
                if cur.rowcount == 1:
                    won.append(dict(grant) | {"state": "consumed", "consumed_by": run_id})
            return won

    @staticmethod
    def _earlier_request(conn: sqlite3.Connection, run: sqlite3.Row, grant: sqlite3.Row) -> bool:
        """A ``--next-request`` grant belongs to the FIRST request admitted after it was issued."""
        row = conn.execute(
            "SELECT 1 FROM runs WHERE profile_id=? AND channel=? AND user_id=? AND admitted_at > ? "
            "AND admitted_at < ? AND run_id != ? LIMIT 1",
            (run["profile_id"], run["channel"], run["user_id"], grant["created_at"], run["admitted_at"],
             run["run_id"])).fetchone()
        return row is not None

    # -- audit --------------------------------------------------------------------------------
    def audit_start(self, *, run_id: str, wire_tool: str, internal_tool: str, action: Optional[str],
                    args_sha256: str, grant_source: str, untrusted_before: bool) -> str:
        operation_id = "op_" + uuid.uuid4().hex
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO tool_audit(operation_id, run_id, wire_tool, internal_tool, action, args_sha256, "
                "grant_source, started_at, outcome, untrusted_input_before) VALUES (?,?,?,?,?,?,?,?, 'started', ?)",
                (operation_id, run_id, wire_tool, internal_tool, action, args_sha256, grant_source, _now(),
                 int(untrusted_before)))
        return operation_id

    def audit_finish(self, operation_id: str, outcome: str, detail: Optional[str] = None) -> None:
        with self._txn() as conn:
            conn.execute("UPDATE tool_audit SET outcome=?, finished_at=?, detail=? WHERE operation_id=?",
                         (outcome, _now(), detail, operation_id))

    def audit_denied(self, *, run_id: str, wire_tool: str, args_sha256: str, detail: str) -> None:
        with self._txn() as conn:
            conn.execute(
                "INSERT INTO tool_audit(operation_id, run_id, wire_tool, internal_tool, args_sha256, started_at, "
                "finished_at, outcome, detail) VALUES (?,?,?,?,?,?,?, 'denied', ?)",
                ("op_" + uuid.uuid4().hex, run_id, wire_tool, "-", args_sha256, _now(), _now(), detail))

    def audit(self, run_id: str) -> list[dict[str, Any]]:
        with self._txn() as conn:
            rows = conn.execute("SELECT * FROM tool_audit WHERE run_id=? ORDER BY started_at", (run_id,)).fetchall()
            return [dict(r) for r in rows]

    def mark_unfinished_operations_unknown(self, run_id: str) -> int:
        with self._txn() as conn:
            return conn.execute("UPDATE tool_audit SET outcome='unknown', finished_at=? "
                                "WHERE run_id=? AND outcome='started'", (_now(), run_id)).rowcount

    # -- reconcile ----------------------------------------------------------------------------
    def resolution(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._txn() as conn:
            row = conn.execute("SELECT * FROM resolutions WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def record_resolution(self, run_id: str, *, decision: str, reason: str, operator: str,
                          new_state: str, history_written: bool) -> None:
        with self._txn() as conn:
            conn.execute("INSERT INTO resolutions(run_id, decision, reason, operator, created_at) VALUES (?,?,?,?,?)",
                         (run_id, decision, reason, operator, _now()))
            conn.execute("UPDATE runs SET state=?, history_written=MAX(history_written, ?) WHERE run_id=? "
                         "AND state='recovery_required'", (new_state, int(history_written), run_id))
