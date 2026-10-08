"""Inbound attachment helpers moved out of ``gateway/run_inbound.py``: re-homing for multiplexed
gateways, audio/video path notes, and the GigaCode-session check that skips STT pre-processing."""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from gateway.platforms.event import MessageEvent

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


def rehome_inbound_media(event: MessageEvent) -> None:
    """Move adapter-cached attachments into the ACTIVE profile's ``cache/`` and repoint the event.

    Adapters download and cache an attachment BEFORE the gateway routes the event to a profile, so
    on a multiplexed gateway the file lands under the launch home while the routed turn's sandbox
    mounts (``get_cache_directory_mounts``) and vision's ``_media_cache_roots`` resolve the routed
    profile's ``cache/`` — the agent is handed a mounted, empty directory (#101134). Runs inside the
    routed scope at the shared preprocessing choke point (every adapter, every media kind); a no-op
    when the active home is the launch home, and idempotent (a moved entry is no longer under it).
    """
    if not event.media_urls:
        return
    from hermes_constants import get_hermes_home, get_routing_process_hermes_home, hermes_home_key
    active, launch = Path(get_hermes_home()), Path(get_routing_process_hermes_home())
    if hermes_home_key(active) == hermes_home_key(launch):
        return
    from tools.credential_files import to_agent_visible_cache_path
    rewritten = list(event.media_urls)
    for i, raw in enumerate(event.media_urls):
        src = Path(raw)
        try:
            rel = src.relative_to(launch / "cache")
        except ValueError:
            continue
        dest = active / "cache" / rel
        try:
            if not src.is_file():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except OSError:
            logger.warning("Could not move inbound attachment %s into the routed profile's cache", raw, exc_info=True)
            continue
        rewritten[i] = str(dest)
        if event.text and raw in event.text:  # note an adapter already baked in (observed/replied media)
            event.text = event.text.replace(raw, to_agent_visible_cache_path(str(dest)))
    event.media_urls = rewritten


def inbound_attachment_display_name(path: str) -> tuple[str, str]:
    """``(display_name, agent_visible_path)``: cache filename is ``<id>_<id>_<original>``; the
    path is translated to the in-container mount under a Docker backend."""
    from tools.credential_files import to_agent_visible_cache_path
    basename = os.path.basename(path)
    parts = basename.split("_", 2)
    return re.sub(r'[^\w.\- ]', '_', parts[2] if len(parts) >= 3 else basename), to_agent_visible_cache_path(path)


def prepend_inbound_media_file_notes(message_text: str, audio_file_paths: list[str], video_paths: list[str]) -> str:
    """Prepend a path-pointing note per audio-file / video attachment (content is not inlined)."""
    for kind, noun, verb, tool, paths in (
        ("an audio file attachment", "audio", "transcribe or process", "a transcription or media tool", audio_file_paths),
        ("a video attachment", "video", "inspect or process", "a video analysis or media tool", video_paths),
    ):
        for _path in paths:
            _display, _agent_path = inbound_attachment_display_name(_path)
            message_text = (
                f"[The user sent {kind}: '{_display}'. "
                f"It is saved at: {_agent_path}. "
                f"Its content is not inlined here. If the user's request involves "
                f"what the {noun} contains, {verb} it yourself — for "
                f"example by passing the path to {tool} — "
                f"instead of asking the user to describe it. Only ask what to do "
                f"with it if their intent is genuinely unclear.]"
                f"\n\n{message_text}"
            )
    return message_text


def gigacode_session(runner, source, session_key) -> bool:
    """Whether this session's turn runs on the GigaCode CLI runtime (no STT/vision pre-processing)."""
    try:
        _model, runtime = runner._resolve_session_agent_runtime(source=source, session_key=session_key)
    except Exception:
        logger.debug("gigacode_session: runtime resolution failed", exc_info=True)
        return False
    return str((runtime or {}).get("api_mode") or "").lower() == "gigacode_cli"
