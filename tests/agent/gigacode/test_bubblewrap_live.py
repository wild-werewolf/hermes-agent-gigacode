"""BubblewrapDriver against a real ``bwrap`` with a minimal busybox rootfs (skipped when either is absent).

Proves the generated argv, read-only sealed mounts, the private HOME and process-tree cleanup on this
host — not the behaviour of a real GigaCode build (that is the operator's manifest acceptance run).
"""

from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from agent.gigacode.driver import ROOTFS_DIR_TARGETS, BubblewrapDriver, SealedFiles
from agent.gigacode.policy import build_argv, generate_settings
from agent.gigacode.stream_parser import StreamParser
from agent.transports.gigacode_cli_session import GigacodeCliSession, RunLimits

BWRAP, BUSYBOX = Path("/usr/bin/bwrap"), Path("/usr/bin/busybox")
pytestmark = [pytest.mark.platforms("linux"),
              pytest.mark.skipif(not (BWRAP.exists() and BUSYBOX.exists()), reason="bwrap/busybox not installed")]

CLI = r"""#!/bin/busybox sh
/bin/busybox cat > /work/stdin.txt
/bin/busybox test -r /run/hermes/mcp.json && M=yes || M=no
/bin/busybox touch /work/.gigacode/x 2>/dev/null && S=writable || S=readonly
/bin/busybox touch /home/gigacode/.gigacode/x 2>/dev/null && U=writable || U=readonly
/bin/busybox test -e "$HOST_PROBE" && H=visible || H=hidden
[ "$MODE" = "sleep" ] && /bin/busybox sleep 60
echo '{"type":"system","subtype":"init","model":"m","session_id":"s"}'
echo "{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"mcp=$M project=$S user=$U host=$H home=$HOME\"}"
"""


def _rootfs(tmp_path: Path, mode: str) -> Path:
    root = tmp_path / "rootfs"
    for rel in ROOTFS_DIR_TARGETS:
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / "run/hermes/mcp.json").touch()
    (root / "bin").mkdir()
    shutil.copy2(BUSYBOX, root / "bin/busybox")
    cli = root / "opt/gigacode/bin/gigacode"
    cli.parent.mkdir(parents=True)
    cli.write_text(CLI.replace("$MODE", mode).replace("$HOST_PROBE", str(Path(__file__).resolve())),
                   encoding="utf-8")
    cli.chmod(0o755)
    return root


def _run(tmp_path, mode="once", cancel=None):
    root = _rootfs(tmp_path, mode)
    driver = BubblewrapDriver(bwrap=str(BWRAP), rootfs=str(root), executable="/opt/gigacode/bin/gigacode",
                              grace_seconds=2)
    sealed = SealedFiles(mcp_config={"mcpServers": {}}, settings=generate_settings(), instructions_md="rules")
    run_dir = tmp_path / "run"
    descriptor = driver.prepare("gc_live", run_dir, sealed, {"oauth_creds.json": b"{}"},
                                build_argv("/opt/gigacode/bin/gigacode", "/run/hermes/mcp.json"))
    session = GigacodeCliSession(driver, descriptor, RunLimits(60, 60, 2), StreamParser())
    try:
        result = session.run(b"pack", cancel or threading.Event())
        stdin_seen = (run_dir / "scratch" / "stdin.txt").read_bytes() if (run_dir / "scratch" / "stdin.txt").exists() else None
        return result, stdin_seen
    finally:
        driver.cleanup(run_dir)


def test_sandbox_mounts_and_environment(tmp_path):
    result, stdin_seen = _run(tmp_path)
    if result.outcome.error_kind == "nonzero_exit" and b"Operation not permitted" in result.stderr_tail:
        pytest.skip("user namespaces are not permitted for bwrap on this host")
    assert result.outcome.completed, (result.outcome.error_kind, result.stderr_tail[-500:])
    assert result.outcome.final_text == "mcp=yes project=readonly user=readonly host=hidden home=/home/gigacode"
    assert stdin_seen == b"pack" and result.cleanup_complete


def test_cancellation_tears_down_the_namespace(tmp_path):
    cancel = threading.Event()
    threading.Timer(1.5, cancel.set).start()
    result, _ = _run(tmp_path, mode="sleep", cancel=cancel)
    if result.outcome.error_kind == "nonzero_exit":
        pytest.skip("bwrap could not start on this host")
    assert result.cancelled and result.cleanup_complete  # reaper confirmed no process of the run remains
