"""Descriptor-relative file access confined to a granted root (no symlink following, no TOCTOU).

Every path component is opened with ``O_NOFOLLOW`` relative to the previous directory descriptor,
starting from a descriptor of the granted root. A symlink anywhere on the path, ``..`` escaping the
root, or a non-directory intermediate component is refused. The write path creates the file in the
already-opened parent descriptor, so a path swapped between check and write cannot redirect it.
"""

from __future__ import annotations

import os
import stat
from pathlib import PurePosixPath
from typing import Sequence

MAX_READ_BYTES = 1 << 20
MAX_WRITE_BYTES = 1 << 20
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class PathDenied(Exception):
    pass


def _components(root: str, requested: str) -> list[str]:
    path = PurePosixPath(requested)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise PathDenied("path is outside the granted root") from exc
    parts: list[str] = []
    for part in path.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PathDenied("path escapes the granted root")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise PathDenied("path names the root itself")
    return parts


def pick_root(roots: Sequence[str], requested: str) -> str:
    """The granted root a request targets: the one containing an absolute path, else the only root."""
    if PurePosixPath(requested).is_absolute():
        for root in roots:
            if requested == root or requested.startswith(root.rstrip("/") + "/"):
                return root
        raise PathDenied("path is outside the granted roots")
    if len(roots) != 1:
        raise PathDenied("relative paths need exactly one granted root")
    return roots[0]


def _open_parent(root: str, parts: list[str]) -> int:
    fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise PathDenied(f"intermediate path component is missing, a symlink or not a directory: {part}") from exc
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_confined(roots: Sequence[str], requested: str) -> str:
    root = pick_root(roots, requested)
    parts = _components(root, requested)
    parent = _open_parent(root, parts)
    try:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
        except OSError as exc:
            raise PathDenied("file is missing or a symlink") from exc
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise PathDenied("not a regular file")
        data = fh.read(MAX_READ_BYTES + 1)
    if len(data) > MAX_READ_BYTES:
        raise PathDenied("file exceeds the 1 MiB read limit")
    return data.decode("utf-8", "replace")


def write_confined(roots: Sequence[str], requested: str, content: str) -> int:
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise PathDenied("content exceeds the 1 MiB write limit")
    root = pick_root(roots, requested)
    parts = _components(root, requested)
    parent = _open_parent(root, parts)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(parts[-1], flags, 0o600, dir_fd=parent)
        except OSError as exc:
            raise PathDenied("target is a symlink or cannot be created") from exc
    finally:
        os.close(parent)
    with os.fdopen(fd, "wb") as fh:
        info = os.fstat(fh.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PathDenied("not a regular, singly-linked file")  # a hard link could alias a file outside
        os.ftruncate(fh.fileno(), 0)
        fh.write(data)
    return len(data)


def canonical_root(path: str) -> str:
    """Operator-supplied root → canonical existing directory (realpath once, at issue time)."""
    real = os.path.realpath(path)
    if not os.path.isabs(path) or not os.path.isdir(real):
        raise PathDenied(f"path root {path!r} must be an existing absolute directory")
    return real
