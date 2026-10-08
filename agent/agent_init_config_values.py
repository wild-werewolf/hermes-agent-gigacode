"""Tolerant coercion of raw config.yaml values for agent init (malformed values fall back)."""

from __future__ import annotations

from typing import Any


def _parse_config_int(raw: Any, default: int) -> int:
    """Strict int coercion: rejects bool (YAML ``true`` → 1) and fractional floats."""
    if isinstance(raw, bool):
        return default
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _cfg_flag(cfg: dict[str, Any], key: str, default: bool) -> bool:
    """Legacy string-set truthiness used by the ``compression`` section."""
    return str(cfg.get(key, default)).lower() in {"true", "1", "yes"}


def _cfg_dict(cfg: dict[str, Any], key: str) -> dict[str, Any]:
    """``cfg[key]`` if it is a mapping, else ``{}`` (malformed sections are ignored)."""
    section = cfg.get(key, {})
    return section if isinstance(section, dict) else {}
