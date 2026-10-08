"""Durable request identity for the GigaCode runtime.

The journal keys every run by ``(profile_id, channel, user_id, request_id)``:

* Telegram (and any adapter that reports an update id): ``channel = "<platform>:<bot id>"``,
  ``request_id = <update id>`` — a redelivered update finds the existing run.
* Cron: ``channel = "cron"``, ``request_id = sha256(job_id + ':' + scheduled fire time UTC)``;
  a manual fire uses its durable execution id (reused by a redelivery of the same fire).
* Anything else (CLI, TUI, API): a fresh id per turn — no redelivery to deduplicate.

The gateway binds :data:`_GATEWAY_REQUEST` while it prepares a turn; the executor thread that
runs the agent inherits it through ``copy_context``.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class GatewayRequest:
    channel: str
    request_id: str


@dataclass(frozen=True)
class RequestIdentity:
    profile_id: str
    channel: str
    user_id: str
    request_id: str
    source: str  # owner_private | cron_job:<id>
    cron_job_id: Optional[str] = None


_GATEWAY_REQUEST: ContextVar[Optional[GatewayRequest]] = ContextVar("gigacode_gateway_request", default=None)


def bind_gateway_request(event: Any, adapter: Any) -> None:
    """Called by the gateway for every prepared turn (``None`` when no update id is known)."""
    update_id = getattr(event, "platform_update_id", None)
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None)
    bot_id = getattr(getattr(adapter, "_bot", None), "id", None)
    if update_id is None or not platform or bot_id is None:
        _GATEWAY_REQUEST.set(None)
        return
    _GATEWAY_REQUEST.set(GatewayRequest(channel=f"{platform}:{bot_id}", request_id=str(update_id)))


def _profile_id() -> str:
    from hermes_cli.profiles import current_profile_name

    return current_profile_name(default="default") or "default"


def _cron_identity(profile_id: str) -> Optional[RequestIdentity]:
    from cron.execution_identity import current_cron_execution

    from agent.gigacode.journal import cron_request_id

    execution = current_cron_execution()
    if execution is None:
        return None
    owner = _cron_owner(execution.job_id)
    if owner is None:
        raise LookupError(f"cron job {execution.job_id} has no owner provenance")
    request_id = (cron_request_id(execution.job_id, execution.scheduled_instant)
                  if execution.scheduled_instant else f"manual:{execution.execution_id}")
    return RequestIdentity(profile_id=profile_id, channel="cron", user_id=owner, request_id=request_id,
                           source=f"cron_job:{execution.job_id}", cron_job_id=execution.job_id)


def _cron_owner(job_id: str) -> Optional[str]:
    """Owner of a cron job = the user recorded in its ``origin`` (who created it, from where)."""
    from cron.jobs import get_job

    job = get_job(job_id) or {}
    origin = job.get("origin") if isinstance(job.get("origin"), dict) else {}
    user = str(origin.get("user_id") or "").strip()
    platform = str(origin.get("platform") or "").strip()
    return f"{platform}:{user}" if user and platform else None


def resolve(agent: Any) -> RequestIdentity:
    """Identity of the current turn. Raises ``LookupError`` for a cron job without provenance."""
    profile_id = _profile_id()
    if getattr(agent, "platform", None) == "cron":
        identity = _cron_identity(profile_id)
        if identity is not None:
            return identity
    user = str(getattr(agent, "_user_id", None) or "").strip()
    platform = str(getattr(agent, "platform", None) or "cli")
    gateway = _GATEWAY_REQUEST.get()
    if gateway is not None and user:
        return RequestIdentity(profile_id, gateway.channel, f"{platform}:{user}", gateway.request_id, "owner_private")
    return RequestIdentity(profile_id, f"{platform}:local", f"{platform}:{user or 'owner'}",
                           f"turn:{uuid.uuid4().hex}", "owner_private")
