"""One long-lived thread that performs every GigaCode process spawn.

Linux ties ``PR_SET_PDEATHSIG`` (bwrap ``--die-with-parent``, and the reaper's own parent-death
signal) to the *thread* that forked, not the process. Gateway turns run on recycled executor
threads, so a spawn from one of them could kill the sandbox the moment the pool retires that
worker. Workers therefore only hand a spawn request to this supervisor and wait for the handle.
"""

from __future__ import annotations

import queue
import subprocess
import threading
from concurrent.futures import Future
from typing import Any, Callable, Optional

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_requests: "queue.Queue[tuple[Callable[[], Any], Future]]" = queue.Queue()


def _serve() -> None:
    while True:
        fn, future = _requests.get()
        if not future.set_running_or_notify_cancel():
            continue
        try:
            future.set_result(fn())
        except BaseException as exc:  # health: allow BLE001 -- handed to the waiting worker, which re-raises it
            future.set_exception(exc)


def supervisor_thread() -> threading.Thread:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            # health: allow HX012 -- process-lifetime spawn thread; no per-turn context may ride it
            _thread = threading.Thread(target=_serve, name="gigacode-spawn-supervisor", daemon=True)
            _thread.start()
        return _thread


def spawn(argv: list[str], **popen_kwargs: Any) -> tuple[subprocess.Popen, int]:
    """``Popen(argv, **kwargs)`` executed on the supervisor thread; returns (popen, spawning thread id)."""
    thread = supervisor_thread()
    future: Future = Future()
    _requests.put((lambda: subprocess.Popen(argv, **popen_kwargs), future))
    return future.result(), thread.ident or 0
