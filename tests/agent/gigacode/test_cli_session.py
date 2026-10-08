"""Process transport against a real child process (the fake CLI under the reaper supervisor)."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from agent.gigacode import spawn_supervisor
from agent.gigacode.driver import SANDBOX_MCP, SealedFiles
from agent.gigacode.policy import build_argv, generate_settings
from agent.gigacode.stream_parser import StreamParser
from agent.transports.gigacode_cli_session import GigacodeCliSession, RunLimits
from tests.agent.gigacode.conftest import direct_driver, success_steps

pytestmark = pytest.mark.platforms("linux")


def _sealed() -> SealedFiles:
    mcp = {"mcpServers": {"hermes": {"httpUrl": "http://127.0.0.1:9/mcp", "headers": {"Authorization": "Bearer t0k"},
                                     "includeTools": []}}}
    return SealedFiles(mcp_config=mcp, settings=generate_settings(), instructions_md="rules\n")


def _run(tmp_path, scenario_path, *, payload=b"pack", limits=None, cancel=None, should_cancel=None, **limit_kw):
    driver = direct_driver(scenario_path)
    descriptor = driver.prepare("gc_test", tmp_path / "run", _sealed(), {}, build_argv("/opt/gigacode", SANDBOX_MCP))
    limits = limits or RunLimits(wall_timeout_seconds=limit_kw.get("wall", 30), silence_warning_seconds=60,
                                 termination_grace_seconds=2, **{k: v for k, v in limit_kw.items() if k != "wall"})
    session = GigacodeCliSession(driver, descriptor, limits, StreamParser())
    try:
        return session.run(payload, cancel or threading.Event(), should_cancel=should_cancel)
    finally:
        driver.cleanup(tmp_path / "run")


def test_success_stdin_eof_argv_and_clean_env(tmp_path, scenario, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-telegram")
    record = tmp_path / "record.json"
    result = _run(tmp_path, scenario(steps=success_steps(), record=str(record)), payload="контекст".encode())
    assert result.outcome.completed and result.outcome.final_text == "Готово." and result.exit_code == 0
    assert result.cleanup_complete
    seen = json.loads(record.read_text())
    assert seen["stdin"] == "контекст"  # whole pack on stdin, then EOF (read() returned)
    assert "--approval-mode=auto-edit" in seen["argv"] and "контекст" not in " ".join(seen["argv"])
    assert "TELEGRAM_BOT_TOKEN" not in seen["env_keys"]
    assert not any("t0k" in key for key in seen["env_keys"])


def test_large_stderr_is_ring_buffered_without_deadlock(tmp_path, scenario):
    steps = [{"stderr_flood": 5 << 20}, *success_steps()]
    result = _run(tmp_path, scenario(steps=steps))
    assert result.outcome.completed
    assert result.stderr_truncated and len(result.stderr_tail) <= 2 << 20


def test_stdout_limit_cancels_and_keeps_draining(tmp_path, scenario):
    result = _run(tmp_path, scenario(steps=[{"stdout_flood": 3 << 20}, *success_steps()]),
                  stdout_limit_bytes=1 << 20)
    assert not result.outcome.completed and result.outcome.error_kind == "protocol_limit"
    assert result.cleanup_complete


def test_wall_timeout_terminates_the_tree(tmp_path, scenario):
    started = time.monotonic()
    result = _run(tmp_path, scenario(steps=[{"sleep": 60}, *success_steps()]), wall=2)
    assert result.timed_out and result.outcome.error_kind == "timed_out"
    assert result.cleanup_complete and time.monotonic() - started < 30


def test_user_cancellation_stops_the_run(tmp_path, scenario):
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    result = _run(tmp_path, scenario(steps=[{"sleep": 60}, *success_steps()]), cancel=cancel)
    assert result.cancelled and result.outcome.error_kind == "cancelled" and result.cleanup_complete


def test_setsid_descendant_is_reaped_on_cancellation(tmp_path, scenario):
    pidfile = tmp_path / "grandchild.pid"
    cancel = threading.Event()

    def cancel_when_spawned():
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        cancel.set()
    threading.Thread(target=cancel_when_spawned, daemon=True).start()
    result = _run(tmp_path, scenario(steps=[{"spawn_setsid_child": str(pidfile)}, {"sleep": 60}]), cancel=cancel)
    grandchild = int(pidfile.read_text())
    assert result.cancelled and result.cleanup_complete
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)


def test_setsid_descendant_left_behind_after_success_is_reaped(tmp_path, scenario):
    pidfile = tmp_path / "leftover.pid"
    result = _run(tmp_path, scenario(steps=[{"spawn_setsid_child": str(pidfile)}, *success_steps()]))
    assert result.outcome.completed and result.cleanup_complete
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_spawn_runs_on_the_long_lived_supervisor_thread(tmp_path, scenario):
    """A short-lived worker hands the spawn to the supervisor and exits; the process keeps running
    (its parent-death signal is tied to the supervisor thread, not the worker)."""
    driver = direct_driver(scenario(steps=[{"sleep": 2}, *success_steps()]))
    descriptor = driver.prepare("gc_t", tmp_path / "run", _sealed(), {}, build_argv("/opt/gigacode", SANDBOX_MCP))
    box = {}

    def worker():
        box["handle"] = driver.spawn(descriptor)
        box["worker"] = threading.get_ident()
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    handle = box["handle"]
    assert handle.spawn_thread_ident == spawn_supervisor.supervisor_thread().ident != box["worker"]
    time.sleep(0.5)
    assert handle.popen.poll() is None  # still alive after the worker thread is gone
    handle.popen.stdin.close()
    assert handle.popen.wait(timeout=20) == 0
    assert driver.verify_cleanup(handle)
    driver.cleanup(tmp_path / "run")


def test_rejected_flag_exits_nonzero_without_output(tmp_path, scenario):
    driver = direct_driver(scenario(steps=success_steps()))
    argv = [*build_argv("/opt/gigacode", SANDBOX_MCP), "--yolo"]
    descriptor = driver.prepare("gc_bad", tmp_path / "run", _sealed(), {}, argv)
    session = GigacodeCliSession(driver, descriptor, RunLimits(30, 60, 2), StreamParser())
    result = session.run(b"x", threading.Event())
    assert result.outcome.error_kind == "nonzero_exit" and b"Unknown argument" in result.stderr_tail
    driver.cleanup(tmp_path / "run")
