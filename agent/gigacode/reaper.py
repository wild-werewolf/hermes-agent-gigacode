"""Per-run process supervisor (stdlib only; launched by path with ``python -I``).

``python -I reaper.py --grace S --status-fd N -- ARGV...``

* Becomes a Linux child subreaper, so descendants that ``setsid``/double-fork away from the
  CLI are re-parented here instead of to init and stay findable.
* Asks the kernel for SIGTERM if the Hermes supervisor thread that spawned it dies.
* Runs ARGV in its own session with inherited stdin/stdout/stderr (the run's pipes).
* On SIGTERM, or as soon as the CLI exits, terminates every descendant: SIGTERM, then SIGKILL
  after the grace period; verifies via ``/proc`` that none is left.
* Writes JSON status lines to ``--status-fd``: ``{"child_pid": ...}`` at start and
  ``{"exit_code", "signal", "killed", "cleanup"}`` at the end (``cleanup`` is ``complete`` only
  when no descendant remains).
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time

PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36
_stop_requested = False


def _prctl(option: int, value: int) -> bool:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(option, value, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _raw_stat(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    return data[data.rfind(")") + 2:].split()


def _stat(pid: int) -> tuple[int, int] | None:
    """(ppid, starttime) from /proc/<pid>/stat, or None when the process is gone or a zombie."""
    fields = _raw_stat(pid)
    if fields is None or fields[0] == "Z":
        return None
    return int(fields[1]), int(fields[19])


def descendants(root: int) -> dict[int, int]:
    """Live descendants of ``root`` → their start time (zombies excluded)."""
    parent: dict[int, tuple[int, int]] = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            info = _stat(int(name))
            if info is not None:
                parent[int(name)] = info
    found: dict[int, int] = {}
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for pid, (ppid, start) in parent.items():
            if ppid == current and pid not in found:
                found[pid] = start
                frontier.append(pid)
    return found


def _signal_all(pids: dict[int, int], sig: int) -> None:
    for pid, start in pids.items():
        info = _stat(pid)
        if info is not None and info[1] == start:  # never signal a recycled PID
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass


def _reap(keep: int) -> None:
    """Collect zombie children (re-parented orphans) except ``keep``, whose status Popen owns."""
    me = str(os.getpid())
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == keep:
            continue
        fields = _raw_stat(int(name))
        if fields is not None and fields[0] == "Z" and fields[1] == me:
            try:
                os.waitpid(int(name), os.WNOHANG)
            except ChildProcessError:
                pass


def terminate_tree(grace: float, keep: int) -> tuple[int, bool]:
    """SIGTERM every descendant, SIGKILL survivors after ``grace``; (count, all_gone)."""
    me = os.getpid()
    targets = descendants(me)
    _signal_all(targets, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and descendants(me):
        _reap(keep)
        time.sleep(0.05)
    remaining = descendants(me)
    _signal_all(remaining, signal.SIGKILL)  # windows-footgun: ok — Linux-only (prctl/procfs)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and descendants(me):
        _reap(keep)
        time.sleep(0.02)
    _reap(keep)
    return len(targets), not descendants(me)


def _on_term(signum, frame) -> None:  # noqa: ARG001 - signal handler signature
    global _stop_requested
    _stop_requested = True


def _write_status(fd: int, payload: dict) -> None:
    try:
        os.write(fd, (json.dumps(payload) + "\n").encode("utf-8"))
    except OSError:
        pass


def main(argv: list[str]) -> int:
    grace, status_fd = 5.0, -1
    while argv and argv[0] != "--":
        flag, value, argv = argv[0], argv[1], argv[2:]
        if flag == "--grace":
            grace = float(value)
        elif flag == "--status-fd":
            status_fd = int(value)
    command = argv[1:]
    if not command:
        return 2
    subreaper = _prctl(PR_SET_CHILD_SUBREAPER, 1)
    _prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    try:
        child = subprocess.Popen(command, start_new_session=True, close_fds=True)
    except OSError as exc:
        _write_status(status_fd, {"spawn_error": type(exc).__name__, "cleanup": "complete"})
        return 127
    _write_status(status_fd, {"child_pid": child.pid, "subreaper": subreaper})
    os.close(0)  # the CLI owns stdin; the reaper must not hold the read end open
    while child.poll() is None and not _stop_requested:
        _reap(child.pid)
        time.sleep(0.05)
    killed, gone = terminate_tree(grace, child.pid)
    try:
        code = child.wait(timeout=10)  # already SIGKILLed by terminate_tree when it outlived the grace
    except subprocess.TimeoutExpired:
        code, gone = 125, False  # unkillable (e.g. uninterruptible I/O): report, never claim cleanup
    _write_status(status_fd, {
        "exit_code": code if code >= 0 else None, "signal": -code if code < 0 else None,
        "killed": killed, "stopped": _stop_requested, "cleanup": "complete" if gone else "incomplete",
    })
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
