"""Process-wide admission for GigaCode runs.

* At most ``max_concurrent_runs`` processes (default 1) across the runtime;
* at most :data:`~agent.gigacode.config.QUEUE_LIMIT` requests waiting — the next one is refused
  ``runtime_busy`` (a repeated request key never gets here: the journal returns its existing run);
* one session runs strictly sequentially, including the history save of its previous run;
* waiting never holds a database transaction or the gateway event loop (this runs on the turn's
  worker thread) and gives up on the user's interrupt, shutdown, or wall deadline.

:func:`cancel_all` (shutdown, ``atexit``) cancels every active run and waits for their cleanup.
"""

from __future__ import annotations

import atexit
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from agent.gigacode.config import QUEUE_LIMIT
from agent.gigacode.errors import GigacodeError

_POLL = 0.1


class RunScheduler:
    def __init__(self, queue_limit: int = QUEUE_LIMIT) -> None:
        self._queue_limit = queue_limit
        self._cond = threading.Condition()
        self._active = 0
        self._waiting = 0
        self._sessions: dict[str, threading.Lock] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._shutting_down = False

    def _session_lock(self, key: str) -> threading.Lock:
        with self._cond:
            return self._sessions.setdefault(key, threading.Lock())

    @contextmanager
    def slot(self, *, session_key: str, limit: int, should_abort: Callable[[], bool],
             deadline: float) -> Iterator[None]:
        with self._cond:
            if self._shutting_down:
                raise GigacodeError("cancelled", "Hermes is shutting down")
            if self._waiting >= self._queue_limit:
                raise GigacodeError("runtime_busy", "the GigaCode queue is full; try again later")
            self._waiting += 1
        session_lock = self._session_lock(session_key)
        acquired_session = False
        try:
            while not session_lock.acquire(timeout=_POLL):
                self._check_abort(should_abort, deadline)
            acquired_session = True
            with self._cond:
                while self._active >= limit:
                    self._check_abort(should_abort, deadline)
                    self._cond.wait(timeout=_POLL)
                self._active += 1
        except BaseException:
            if acquired_session:
                session_lock.release()
            raise
        finally:
            with self._cond:
                self._waiting -= 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()
            session_lock.release()

    def _check_abort(self, should_abort: Callable[[], bool], deadline: float) -> None:
        if self._shutting_down or should_abort():
            raise GigacodeError("cancelled", "the request was cancelled while waiting in the queue")
        if time.monotonic() >= deadline:
            raise GigacodeError("timed_out", "the request waited longer than the wall-clock limit")

    def register(self, run_id: str, cancel: threading.Event) -> None:
        with self._cond:
            self._cancels[run_id] = cancel

    def unregister(self, run_id: str) -> None:
        with self._cond:
            self._cancels.pop(run_id, None)
            self._cond.notify_all()

    def cancel(self, run_id: str) -> bool:
        with self._cond:
            event = self._cancels.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def cancel_all(self, wait_seconds: float = 30.0) -> None:
        """Cancel every active run (process trees are terminated by their own run loops)."""
        with self._cond:
            self._shutting_down = True
            events = list(self._cancels.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + wait_seconds
        with self._cond:
            while self._cancels and time.monotonic() < deadline:
                self._cond.wait(timeout=_POLL)

    @property
    def waiting(self) -> int:
        with self._cond:
            return self._waiting

    @property
    def active(self) -> int:
        with self._cond:
            return self._active


SCHEDULER = RunScheduler()
atexit.register(SCHEDULER.cancel_all)


def active_runs() -> Optional[int]:
    return SCHEDULER.active
