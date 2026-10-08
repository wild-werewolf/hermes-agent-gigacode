"""One GigaCode CLI process: stdin in, ``stream-json`` events out, bounded and cancellable.

Both pipes are drained concurrently to EOF on their own threads. Limits:

* 1 MiB per stdout record and 4 MiB of normalized text (enforced by the parser);
* 32 MiB of accepted stdout per run — past it the run fails ``protocol_limit`` and the process
  is cancelled, while the pipe keeps draining so the child never blocks on a full pipe;
* stderr is a 2 MiB ring buffer: old bytes are dropped (``stderr_truncated``), reading never stops.

Silence on stdout only raises a warning callback (a long tool call or reasoning can be quiet);
cancellation, shutdown and the wall deadline terminate the whole process tree via the driver.
After cancellation or timeout, late events are ignored and never turn the run into a success.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.gigacode.driver import Driver, LaunchDescriptor, ProcessHandle
from agent.gigacode.stream_parser import ProtocolViolation, StreamOutcome, StreamParser

logger = logging.getLogger(__name__)

STDOUT_LIMIT_BYTES = 32 << 20
STDERR_RING_BYTES = 2 << 20
_READ_CHUNK = 65536
_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class RunLimits:
    wall_timeout_seconds: float
    silence_warning_seconds: float
    termination_grace_seconds: float
    stdout_limit_bytes: int = STDOUT_LIMIT_BYTES
    stderr_ring_bytes: int = STDERR_RING_BYTES


@dataclass
class ProcessResult:
    outcome: StreamOutcome
    exit_code: Optional[int]
    stderr_tail: bytes
    stderr_truncated: bool
    started_at: float
    finished_at: float
    cancelled: bool
    timed_out: bool
    cleanup_complete: bool
    identity: dict[str, Any] = field(default_factory=dict)
    silence_warnings: int = 0


class _StderrRing:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.buffer = bytearray()
        self.truncated = False

    def add(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        overflow = len(self.buffer) - self.capacity
        if overflow > 0:
            del self.buffer[:overflow]
            self.truncated = True


class GigacodeCliSession:
    """Runs one descriptor to completion. Not reusable: one process per Hermes request."""

    def __init__(self, driver: Driver, descriptor: LaunchDescriptor, limits: RunLimits, parser: StreamParser,
                 *, on_silence: Optional[Callable[[float], None]] = None,
                 on_spawn: Optional[Callable[[ProcessHandle], None]] = None) -> None:
        self._driver, self._descriptor, self._limits, self._parser = driver, descriptor, limits, parser
        self._on_silence, self._on_spawn = on_silence, on_spawn
        self._lock = threading.Lock()
        self._last_stdout = time.monotonic()
        self._stdout_total = 0
        self._stdout_limited = False
        self._stderr = _StderrRing(limits.stderr_ring_bytes)
        self._stop_feeding = False

    # -- pipe pumps -----------------------------------------------------------------------------
    def _write_stdin(self, handle: ProcessHandle, payload: bytes) -> None:
        stdin = handle.popen.stdin
        try:
            stdin.write(payload)
            stdin.flush()
        except (BrokenPipeError, OSError):
            logger.info("gigacode stdin closed early by the CLI")
        finally:
            try:
                stdin.close()
            except OSError:
                logger.debug("gigacode stdin close failed", exc_info=True)

    def _pump_stdout(self, handle: ProcessHandle) -> None:
        fd = handle.popen.stdout.fileno()
        while chunk := os.read(fd, _READ_CHUNK):
            with self._lock:
                self._last_stdout = time.monotonic()
                self._stdout_total += len(chunk)
                if self._stdout_total > self._limits.stdout_limit_bytes:
                    self._stdout_limited = True
                if self._stdout_limited or self._stop_feeding:
                    continue  # keep draining, stop interpreting
                self._parser.feed(chunk)
        with self._lock:
            if not self._stdout_limited and not self._stop_feeding:
                self._parser.finish()

    def _pump_stderr(self, handle: ProcessHandle) -> None:
        fd = handle.popen.stderr.fileno()
        while chunk := os.read(fd, _READ_CHUNK):
            self._stderr.add(chunk)

    # -- main loop ------------------------------------------------------------------------------
    def run(self, stdin_payload: bytes, cancel: threading.Event,
            should_cancel: Optional[Callable[[], bool]] = None) -> ProcessResult:
        handle = self._driver.spawn(self._descriptor)
        if self._on_spawn is not None:
            self._on_spawn(handle)
        started = time.time()
        deadline = time.monotonic() + self._limits.wall_timeout_seconds
        pumps = (("stdin", self._write_stdin, (handle, stdin_payload)), ("stdout", self._pump_stdout, (handle,)),
                 ("stderr", self._pump_stderr, (handle,)))
        threads = [
            # health: allow HX012 -- pipe pumps carry no per-turn context; they only move bytes
            threading.Thread(target=fn, args=args, daemon=True, name=f"gigacode-{name}") for name, fn, args in pumps
        ]
        for thread in threads:
            thread.start()
        cancelled = timed_out = False
        warnings = 0
        while handle.popen.poll() is None or any(t.is_alive() for t in threads[1:]):
            if handle.popen.poll() is None:
                stop_reason = self._stop_reason(cancel, should_cancel, deadline)
                if stop_reason is not None:
                    cancelled, timed_out = stop_reason == "cancel", stop_reason == "timeout"
                    self._halt(handle)
                    break
                warnings += self._check_silence(warnings)
            time.sleep(_POLL_SECONDS)
        for thread in threads:
            thread.join(timeout=self._limits.termination_grace_seconds + 5.0)
        exit_code = handle.popen.wait(timeout=self._limits.termination_grace_seconds + 10.0)
        cleanup_complete = self._driver.verify_cleanup(handle) and not any(t.is_alive() for t in threads)
        if cleanup_complete:
            handle.popen.stdout.close()
            handle.popen.stderr.close()
        with self._lock:
            if self._stdout_limited and self._parser.violation is None:
                self._parser.violation = ProtocolViolation("protocol_limit", "stdout exceeded 32 MiB")
            outcome = self._parser.outcome(exit_code, cancelled=cancelled, timed_out=timed_out)
        return ProcessResult(
            outcome=outcome, exit_code=exit_code, stderr_tail=bytes(self._stderr.buffer),
            stderr_truncated=self._stderr.truncated, started_at=started, finished_at=time.time(),
            cancelled=cancelled, timed_out=timed_out, cleanup_complete=cleanup_complete,
            identity=handle.identity(), silence_warnings=warnings,
        )

    def _stop_reason(self, cancel: threading.Event, should_cancel: Optional[Callable[[], bool]],
                     deadline: float) -> Optional[str]:
        if cancel.is_set() or (should_cancel is not None and should_cancel()):
            return "cancel"
        if time.monotonic() >= deadline:
            return "timeout"
        with self._lock:
            if self._stdout_limited or self._parser.violation is not None:
                return "protocol"
        return None

    def _halt(self, handle: ProcessHandle) -> None:
        """Stop interpreting output, then terminate the tree (SIGTERM → grace → SIGKILL)."""
        with self._lock:
            self._stop_feeding = True
        self._driver.terminate(handle, self._limits.termination_grace_seconds)

    def _check_silence(self, warnings_so_far: int) -> int:
        with self._lock:
            silent_for = time.monotonic() - self._last_stdout
        threshold = self._limits.silence_warning_seconds * (warnings_so_far + 1)
        if silent_for < threshold:
            return 0
        logger.warning("gigacode run silent on stdout for %.0fs (still running)", silent_for)
        if self._on_silence is not None:
            self._on_silence(silent_for)
        return 1
