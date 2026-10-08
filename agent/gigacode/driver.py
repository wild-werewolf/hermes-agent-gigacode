"""Execution drivers: ``prepare → spawn → terminate → verify_cleanup → cleanup``.

Every driver launches the CLI under :mod:`agent.gigacode.reaper` (a per-run subreaper) from the
long-lived :mod:`agent.gigacode.spawn_supervisor` thread.

* :class:`BubblewrapDriver` — the only production driver (``execution_driver: bubblewrap``):
  read-only verified rootfs, new user/PID/IPC/UTS namespaces, sealed config mounted read-only,
  writable ``/work`` scratch per run. Host loopback stays reachable (documented boundary).
* :class:`DirectDriver` — test-only, reachable solely by dependency injection (no config value
  selects it). It runs a launch prefix such as ``python fake_gigacode.py`` without a sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from agent.gigacode import reaper as _reaper
from agent.gigacode import spawn_supervisor
from agent.gigacode.errors import GigacodeError

SANDBOX_WORK = "/work"
SANDBOX_HOME = "/home/gigacode"
SANDBOX_MCP = "/run/hermes/mcp.json"
ROOTFS_DIR_TARGETS = ("work", "home/gigacode/.gigacode", "run/hermes", "tmp", "proc", "dev")
ROOTFS_FILE_TARGETS = ("run/hermes/mcp.json",)
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
# Never accepted inside an operator auth bundle, whatever the manifest says.
_BUNDLE_FORBIDDEN_NAMES = frozenset({"settings.json", "mcp.json", "GIGACODE.md", "QWEN.md", "GEMINI.md",
                                     "AGENTS.md", "memory.md", "MEMORY.md"})
_BUNDLE_FORBIDDEN_DIRS = frozenset({"extensions", "commands", "agents", "skills", "memory", "mcp"})


@dataclass(frozen=True)
class SealedFiles:
    """Generated per-run material; written 0600 outside the writable scratch."""

    mcp_config: Mapping[str, Any]
    settings: Mapping[str, Any]
    instructions_md: str


@dataclass(frozen=True)
class LaunchDescriptor:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    run_dir: str
    driver: str
    mcp_config_path: str


@dataclass
class ProcessHandle:
    popen: subprocess.Popen
    supervisor_pid: int
    supervisor_start: Optional[int]
    spawn_thread_ident: int
    status_fd: int
    started_at: float
    driver: str
    status: list[dict[str, Any]] = field(default_factory=list)
    killed_snapshot: dict[int, int] = field(default_factory=dict)

    def identity(self) -> dict[str, Any]:
        child = next((s.get("child_pid") for s in self.status if "child_pid" in s), None)
        return {"supervisor_pid": self.supervisor_pid, "supervisor_start": self.supervisor_start,
                "child_pid": child, "driver": self.driver, "started_at": self.started_at}


class Driver(Protocol):
    name: str

    def prepare(self, run_id: str, run_dir: Path, sealed: SealedFiles, auth_files: Mapping[str, bytes],
                cli_args: Sequence[str]) -> LaunchDescriptor: ...

    def spawn(self, descriptor: LaunchDescriptor) -> ProcessHandle: ...

    def terminate(self, handle: ProcessHandle, grace_seconds: float) -> None: ...

    def verify_cleanup(self, handle: ProcessHandle) -> bool: ...

    def cleanup(self, run_dir: Path) -> None: ...


# --- sealing --------------------------------------------------------------------------------------


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def seal_run(run_dir: Path, sealed: SealedFiles, auth_files: Mapping[str, bytes]) -> dict[str, Path]:
    """Lay out ``run_dir`` (0700): ``sealed/`` read-only material, ``scratch/`` writable /work."""
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    os.chmod(run_dir, 0o700)
    sealed_dir = run_dir / "sealed"
    paths = {
        "mcp": sealed_dir / "mcp.json",
        "project": sealed_dir / "project-gigacode",
        "instructions": sealed_dir / "GIGACODE.md",
        "user": sealed_dir / "user-gigacode",
        "scratch": run_dir / "scratch",
    }
    _write_private(paths["mcp"], _json_bytes(sealed.mcp_config))
    _write_private(paths["project"] / "settings.json", _json_bytes(sealed.settings))
    _write_private(paths["instructions"], sealed.instructions_md.encode("utf-8"))
    _write_private(paths["user"] / "settings.json", _json_bytes(sealed.settings))
    for rel, data in sorted(auth_files.items()):
        _write_private(paths["user"] / rel, data)
    scratch = paths["scratch"]
    (scratch / ".gigacode").mkdir(parents=True, mode=0o700)
    (scratch / "GIGACODE.md").touch(mode=0o600)  # mount target for the sealed copy
    return paths


def load_auth_bundle(bundle_dir: Optional[str], allowlist: Sequence[Mapping[str, Any]]) -> dict[str, bytes]:
    """Read exactly the manifest-listed credential files; anything else is ``config_unverified``."""
    expected = {str(e.get("path")): e for e in allowlist}
    if bundle_dir is None:
        if expected:
            raise GigacodeError("config_unverified", "manifest lists auth files but gigacode.auth_bundle is unset")
        return {}
    root = Path(bundle_dir)
    found: dict[str, bytes] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                raise GigacodeError("config_unverified", f"auth bundle entry {rel!r} is a symlink")
            parts = rel.split("/")
            if any(p in _BUNDLE_FORBIDDEN_DIRS for p in parts[:-1]) or parts[-1] in _BUNDLE_FORBIDDEN_DIRS or parts[-1] in _BUNDLE_FORBIDDEN_NAMES:
                raise GigacodeError("config_unverified", f"auth bundle entry {rel!r} is not a credential file")
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode) or rel not in expected:
                raise GigacodeError("config_unverified", f"auth bundle entry {rel!r} is not in the manifest allowlist")
            data = full.read_bytes()
            entry = expected[rel]
            if len(data) != entry.get("size") or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
                raise GigacodeError("config_unverified", f"auth bundle file {rel!r} differs from the manifest")
            found[rel] = data
    missing = sorted(set(expected) - set(found))
    if missing:
        raise GigacodeError("config_unverified", "auth bundle is missing: " + ", ".join(missing))
    return found


def clean_env(home: str, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The whole environment the CLI sees: no Hermes .env, no Telegram token, no service keys."""
    env = {"PATH": SAFE_PATH, "HOME": home, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
           "TERM": "dumb", "NO_COLOR": "1"}
    env.update(extra or {})
    return env


# --- spawn / terminate shared by both drivers ----------------------------------------------------


def _proc_start(pid: int) -> Optional[int]:
    info = _reaper._stat(pid)
    return info[1] if info else None


def _spawn_with_reaper(descriptor: LaunchDescriptor, grace: float) -> ProcessHandle:
    status_r, status_w = os.pipe()
    argv = [sys.executable, "-I", _reaper.__file__, "--grace", str(grace), "--status-fd", str(status_w),
            "--", *descriptor.argv]
    try:
        popen, thread_ident = spawn_supervisor.spawn(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=descriptor.cwd, env=dict(descriptor.env), pass_fds=(status_w,), start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        os.close(status_r)
        raise GigacodeError("spawn_failed", f"could not start the GigaCode supervisor: {type(exc).__name__}") from exc
    finally:
        os.close(status_w)
    return ProcessHandle(popen=popen, supervisor_pid=popen.pid, supervisor_start=_proc_start(popen.pid),
                         spawn_thread_ident=thread_ident, status_fd=status_r, started_at=time.time(),
                         driver=descriptor.driver)


def _terminate(handle: ProcessHandle, grace: float) -> None:
    """SIGTERM the reaper (it stops the whole tree); hard-kill the tree if it does not finish."""
    popen = handle.popen
    if popen.poll() is not None:
        return
    if _proc_start(popen.pid) == handle.supervisor_start:
        popen.send_signal(signal.SIGTERM)
    try:
        popen.wait(timeout=grace + 4.0)
        return
    except subprocess.TimeoutExpired:
        pass
    handle.killed_snapshot = _reaper.descendants(popen.pid)
    _reaper._signal_all(handle.killed_snapshot, signal.SIGKILL)  # windows-footgun: ok — Linux-only driver (procfs)
    popen.kill()
    popen.wait(timeout=10)


def _read_status(handle: ProcessHandle) -> None:
    if handle.status_fd < 0:
        return
    chunks = []
    while chunk := os.read(handle.status_fd, 65536):
        chunks.append(chunk)
    os.close(handle.status_fd)
    handle.status_fd = -1
    for line in b"".join(chunks).decode("utf-8", "replace").splitlines():
        try:
            handle.status.append(json.loads(line))
        except ValueError:
            continue


def _verify_cleanup(handle: ProcessHandle) -> bool:
    """Complete only when the reaper exited, reported a clean tree, and no killed pid survives."""
    if handle.popen.poll() is None:
        return False
    _read_status(handle)
    final = next((s for s in reversed(handle.status) if "cleanup" in s), None)
    survivors = [pid for pid, start in handle.killed_snapshot.items() if _proc_start(pid) == start]
    return bool(final and final.get("cleanup") == "complete" and not survivors)


class _BaseDriver:
    name = "base"

    def __init__(self, grace_seconds: float = 5.0) -> None:
        self._grace = grace_seconds

    def spawn(self, descriptor: LaunchDescriptor) -> ProcessHandle:
        return _spawn_with_reaper(descriptor, self._grace)

    def terminate(self, handle: ProcessHandle, grace_seconds: float) -> None:
        _terminate(handle, grace_seconds)

    def verify_cleanup(self, handle: ProcessHandle) -> bool:
        return _verify_cleanup(handle)

    def cleanup(self, run_dir: Path) -> None:
        shutil.rmtree(run_dir, ignore_errors=False)


class DirectDriver(_BaseDriver):
    """Test-only driver: ``launch_prefix`` replaces the executable, no sandbox."""

    name = "direct"

    def __init__(self, launch_prefix: Sequence[str], grace_seconds: float = 5.0,
                 extra_env: Optional[Mapping[str, str]] = None) -> None:
        super().__init__(grace_seconds)
        self._prefix = tuple(launch_prefix)
        self._extra_env = dict(extra_env or {})

    def prepare(self, run_id: str, run_dir: Path, sealed: SealedFiles, auth_files: Mapping[str, bytes],
                cli_args: Sequence[str]) -> LaunchDescriptor:
        paths = seal_run(run_dir, sealed, auth_files)
        home = run_dir / "home"
        home.mkdir(mode=0o700)
        os.symlink(paths["user"], home / ".gigacode")
        args = [str(paths["mcp"]) if a == SANDBOX_MCP else a for a in cli_args]
        return LaunchDescriptor(argv=(*self._prefix, *args[1:]), cwd=str(paths["scratch"]),
                                env=clean_env(str(home), {"GIGACODE_RUN_ID": run_id, **self._extra_env}),
                                run_dir=str(run_dir), driver=self.name, mcp_config_path=str(paths["mcp"]))


class BubblewrapDriver(_BaseDriver):
    """Linux bubblewrap sandbox under the Hermes UID (production driver)."""

    name = "bubblewrap"

    def __init__(self, *, bwrap: str, rootfs: str, executable: str, system_settings_paths: Sequence[str] = (),
                 grace_seconds: float = 5.0) -> None:
        super().__init__(grace_seconds)
        self._bwrap, self._rootfs, self._executable = bwrap, Path(rootfs), executable
        self._system_paths = tuple(system_settings_paths)

    def check_rootfs(self, expected_settings: Mapping[str, Any]) -> None:
        """Mount targets pre-exist in the hashed rootfs; system-scope settings match the policy."""
        for rel in ROOTFS_DIR_TARGETS:
            if not (self._rootfs / rel).is_dir() or (self._rootfs / rel).is_symlink():
                raise GigacodeError("config_unverified", f"rootfs lacks mount target directory /{rel}")
        for rel in ROOTFS_FILE_TARGETS:
            target = self._rootfs / rel
            if not target.is_file() or target.is_symlink() or target.stat().st_size != 0:
                raise GigacodeError("config_unverified", f"rootfs lacks empty mount target file /{rel}")
        for sys_path in self._system_paths:
            target = self._rootfs / sys_path.lstrip("/")
            if target.is_symlink():
                raise GigacodeError("config_unverified", f"system settings path {sys_path} is a symlink")
            if not target.exists():
                continue
            try:
                content = json.loads(target.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise GigacodeError("config_unverified", f"system settings {sys_path} unreadable") from exc
            if content != expected_settings:
                raise GigacodeError("config_unverified", f"system settings {sys_path} differ from the approved policy")

    def prepare(self, run_id: str, run_dir: Path, sealed: SealedFiles, auth_files: Mapping[str, bytes],
                cli_args: Sequence[str]) -> LaunchDescriptor:
        self.check_rootfs(sealed.settings)
        paths = seal_run(run_dir, sealed, auth_files)
        argv = (
            self._bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--new-session", "--die-with-parent", "--cap-drop", "ALL",
            "--ro-bind", str(self._rootfs), "/",
            # no-tmp: ok — sandbox-internal tmpfs mount target, not a host path
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--bind", str(paths["scratch"]), SANDBOX_WORK,
            "--ro-bind", str(paths["project"]), f"{SANDBOX_WORK}/.gigacode",
            "--ro-bind", str(paths["instructions"]), f"{SANDBOX_WORK}/GIGACODE.md",
            "--ro-bind", str(paths["mcp"]), SANDBOX_MCP,
            "--ro-bind", str(paths["user"]), f"{SANDBOX_HOME}/.gigacode",
            "--chdir", SANDBOX_WORK, "--setenv", "HOME", SANDBOX_HOME,
            "--", *cli_args,
        )
        return LaunchDescriptor(argv=argv, cwd=str(paths["scratch"]), env=clean_env(SANDBOX_HOME),
                                run_dir=str(run_dir), driver=self.name, mcp_config_path=SANDBOX_MCP)
