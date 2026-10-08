"""Operator verification manifest: checked before ANY real GigaCode launch.

The manifest is written by the operator from acceptance tests of the exact build (never from
``--help``). :func:`verify_activation` refuses with ``config_unverified`` — before the CLI starts —
when the manifest is missing or unreadable, a ``REQUIRED_FROM_PREFLIGHT`` marker remains, or any
pinned value (CLI version/hash, rootfs tree hash, policy hash, bwrap hash, model, isolation checks)
differs from what is on disk now. :func:`compute_facts` prints the hashable facts for a new
manifest (``hermes gigacode preflight``); acceptance results are filled in by the operator.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from agent.gigacode.config import GigacodeSettings, STREAM_PROTOCOL
from agent.gigacode.errors import GigacodeError
from agent.gigacode.policy import policy_digest
from agent.gigacode.tools import CATALOG

SCHEMA = "hermes-gigacode-manifest/1"
_TREE_CACHE: dict[tuple[str, str], str] = {}
_TREE_LOCK = threading.Lock()


@dataclass(frozen=True)
class VerifiedManifest:
    data: Mapping[str, Any]

    @property
    def cli_version(self) -> str:
        return str(self.data["cli"]["version"])

    @property
    def system_settings_paths(self) -> tuple[str, ...]:
        return tuple(self.data["rootfs"].get("system_settings_paths") or ())

    @property
    def auth_files(self) -> tuple[Mapping[str, Any], ...]:
        return tuple((self.data.get("auth_bundle") or {}).get("files") or ())

    @property
    def model(self) -> Mapping[str, Any]:
        return self.data.get("model") or {}

    @property
    def qualified_tool_prefix(self) -> str:
        return str(self.data["mcp"]["qualified_tool_prefix"])


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path) -> list[tuple[str, os.stat_result]]:
    entries: list[tuple[str, os.stat_result]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            full = Path(dirpath) / name
            entries.append((full.relative_to(root).as_posix(), os.lstat(full)))
    return sorted(entries)


def tree_sha256(root: Path) -> str:
    """Content hash of a directory tree (types, modes, file bytes, symlink targets).

    A cheap stat fingerprint guards an in-process cache so a large rootfs is re-hashed only when
    something in it changed (size, mtime, inode or mode of any entry).
    """
    entries = _walk(root)
    fingerprint = hashlib.sha256(repr([(rel, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
                                       for rel, st in entries]).encode()).hexdigest()
    key = (str(root), fingerprint)
    with _TREE_LOCK:
        if key in _TREE_CACHE:
            return _TREE_CACHE[key]
    digest = hashlib.sha256()
    for rel, st in entries:
        mode = stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            line = f"L {rel} {os.readlink(root / rel)}"
        elif stat.S_ISDIR(st.st_mode):
            line = f"D {rel} {mode:o}"
        elif stat.S_ISREG(st.st_mode):
            line = f"F {rel} {mode:o} {file_sha256(root / rel)}"
        else:
            line = f"O {rel} {stat.S_IFMT(st.st_mode):o}"
        digest.update(line.encode("utf-8", "surrogateescape") + b"\n")
    value = digest.hexdigest()
    with _TREE_LOCK:
        _TREE_CACHE[key] = value
    return value


def cli_path_in_rootfs(rootfs: str, executable: str) -> Path:
    """Host path of the in-sandbox executable; must stay inside the rootfs (no symlink escape)."""
    root = Path(rootfs).resolve()
    host = (root / executable.lstrip("/")).resolve()
    if root not in host.parents or not host.is_file():
        raise GigacodeError("config_unverified", "gigacode.executable is not a file inside runtime_rootfs")
    return host


def compute_facts(settings: GigacodeSettings) -> dict[str, Any]:
    """Hashes an operator copies into a manifest after the acceptance run (no CLI is executed)."""
    facts: dict[str, Any] = {
        "schema": SCHEMA, "stream_protocol": STREAM_PROTOCOL,
        "policy_sha256": policy_digest(wire_catalog=CATALOG, model=settings.model),
    }
    if settings.runtime_rootfs:
        facts["rootfs"] = {"path": settings.runtime_rootfs, "tree_sha256": tree_sha256(Path(settings.runtime_rootfs))}
        if settings.executable:
            facts["cli"] = {"executable": settings.executable,
                            "sha256": file_sha256(cli_path_in_rootfs(settings.runtime_rootfs, settings.executable))}
    if settings.bwrap_executable:
        facts["bwrap"] = {"executable": settings.bwrap_executable,
                          "sha256": file_sha256(Path(settings.bwrap_executable))}
    if settings.auth_bundle:
        root = Path(settings.auth_bundle)
        facts["auth_bundle"] = {"files": [
            {"path": rel, "size": st.st_size, "sha256": file_sha256(root / rel)}
            for rel, st in _walk(root) if stat.S_ISREG(st.st_mode)]}
    return facts


def _load(path: str) -> dict[str, Any]:
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise GigacodeError("config_unverified", "verification manifest must be a regular file")
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise GigacodeError("config_unverified", f"verification manifest unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise GigacodeError("config_unverified", f"verification manifest schema must be {SCHEMA}")
    return data


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise GigacodeError("config_unverified", message)


def _dig(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        data = data.get(key) if isinstance(data, Mapping) else None
    return data


def verify_activation(settings: GigacodeSettings) -> VerifiedManifest:
    """Every check that must pass before a real process may start. Raises ``config_unverified``."""
    settings.require_verified()
    data = _load(settings.verification_manifest or "")
    _require(isinstance(data.get("verified_at"), str) and bool(data["verified_at"]), "manifest lacks verified_at")
    _require(data.get("stream_protocol") == settings.stream_protocol, "manifest stream_protocol mismatch")
    _require(_dig(data, "cli", "version") == settings.verified_version, "CLI version differs from verified_version")
    _require(_dig(data, "cli", "executable") == settings.executable, "manifest CLI path differs from config")
    _require(_dig(data, "rootfs", "path") == settings.runtime_rootfs, "manifest rootfs path differs from config")
    _require(_dig(data, "bwrap", "executable") == settings.bwrap_executable, "manifest bwrap path differs")
    for flag in (("mcp", "flags_verified"), ("mcp", "native_deny_verified"), ("os_isolation", "checks_passed"),
                 ("os_isolation", "loopback_inventory_reviewed"), ("os_isolation", "cleanup_verified")):
        _require(_dig(data, *flag) is True, f"manifest acceptance check {'.'.join(flag)} is not true")
    _require(isinstance(_dig(data, "mcp", "qualified_tool_prefix"), str), "manifest lacks mcp.qualified_tool_prefix")
    model_id = _dig(data, "model", "id")
    _require(isinstance(model_id, str) and bool(model_id), "manifest lacks the verified model id")
    _require(settings.model in (None, model_id), "configured model differs from the verified model")
    expected_policy = policy_digest(wire_catalog=CATALOG, model=settings.model)
    _require(data.get("policy_sha256") == expected_policy, "tool/settings policy hash differs from the manifest")
    facts = compute_facts(settings)
    _require(_dig(data, "rootfs", "tree_sha256") == _dig(facts, "rootfs", "tree_sha256"), "rootfs hash mismatch")
    _require(_dig(data, "cli", "sha256") == _dig(facts, "cli", "sha256"), "CLI binary hash mismatch")
    _require(_dig(data, "bwrap", "sha256") == _dig(facts, "bwrap", "sha256"), "bwrap binary hash mismatch")
    return VerifiedManifest(data)
