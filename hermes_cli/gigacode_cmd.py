"""``hermes gigacode`` — local operator commands for the GigaCode CLI runtime.

Trusted local CLI only: none of these is an MCP tool or reachable from chat text.

* ``preflight`` — hashes for a new verification manifest (no CLI is executed);
* ``verify`` — the activation check every real launch performs;
* ``runs list|show`` — the run journal and bridge audit (argument hashes, never arguments);
* ``reconcile`` — resolve a ``recovery_required`` run (``--dry-run`` first);
* ``grants issue|list|revoke`` — one-shot operator grants for a single exact request.
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import sys
import time
from typing import Any, Callable

from agent.gigacode.errors import GigacodeError


def _print(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _operator() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def _journal():
    from agent.gigacode.journal import GigacodeJournal

    return GigacodeJournal()


def _settings_or_defaults():
    from agent.gigacode.config import load_settings

    return load_settings()


def _cmd_preflight(args) -> int:
    from agent.gigacode.manifest import compute_facts

    settings = _settings_or_defaults()
    _print({"facts": compute_facts(settings), "still_required": list(settings.unverified),
            "operator_fills": ["verified_at", "cli.version", "model.id/provider/endpoint",
                               "mcp.flags_verified", "mcp.native_deny_verified", "mcp.qualified_tool_prefix",
                               "os_isolation.checks_passed", "os_isolation.loopback_inventory_reviewed",
                               "os_isolation.cleanup_verified", "rootfs.system_settings_paths"]})
    return 0


def _cmd_verify(args) -> int:
    from agent.gigacode.manifest import verify_activation

    manifest = verify_activation(_settings_or_defaults())
    _print({"status": "verified", "cli_version": manifest.data["cli"]["version"],
            "model": manifest.model.get("id"), "verified_at": manifest.data.get("verified_at")})
    return 0


def _cmd_runs_list(args) -> int:
    _print(_journal().list_runs(limit=args.limit))
    return 0


def _cmd_runs_show(args) -> int:
    journal = _journal()
    run = journal.run(args.run_id)
    if run is None:
        print(f"unknown run {args.run_id}", file=sys.stderr)
        return 1
    projected = json.loads(run.pop("projected_messages") or "[]")
    final = run.pop("final_response") or ""
    run["final_response_preview"] = final[:200]
    run["projected_message_count"] = len(projected)
    _print({"run": run, "audit": journal.audit(args.run_id), "resolution": journal.resolution(args.run_id)})
    return 0


def _cmd_reconcile(args) -> int:
    from agent.gigacode import reconcile
    from hermes_state import SessionDB

    journal = _journal()
    if args.dry_run:
        _print({"dry_run": True, **reconcile.plan(journal, args.run_id, args.decision).__dict__})
        return 0
    result = reconcile.apply(journal, args.run_id, args.decision, reason=args.reason, operator=_operator(),
                             open_session_db=SessionDB)
    _print({"dry_run": False, **result.__dict__})
    return 0


def _require_profile_owner(profile: str) -> None:
    """Only the operator of THIS profile (write access to its config) may issue grants for it."""
    from hermes_cli.config import get_config_path
    from hermes_cli.profiles import current_profile_name

    active = current_profile_name(default="default") or "default"
    if profile != active:
        raise PermissionError(f"--profile {profile!r} is not the active profile {active!r}; "
                              f"run `hermes -p {profile} gigacode grants ...`")
    config_path = get_config_path()
    if not os.access(config_path if config_path.exists() else config_path.parent, os.W_OK):
        raise PermissionError("issuing grants needs write access to this profile's config.yaml")


def _cmd_grants_issue(args) -> int:
    from agent.gigacode.tools import validate_operator_request

    _require_profile_owner(args.profile)
    settings = _settings_or_defaults()
    if not 1 <= args.ttl <= settings.wall_timeout_seconds:
        raise ValueError(f"--ttl must be an integer in [1, {settings.wall_timeout_seconds}]")
    actions, roots = validate_operator_request(args.tool, args.actions.split(","), args.path_root or [])
    grant = _journal().issue_grant(
        profile_id=args.profile, channel=args.channel, user_id=args.user, wire_tool=args.tool,
        actions=sorted(actions), ttl_seconds=args.ttl, operator=_operator(),
        request_key=args.request_key, next_request=args.next_request, path_roots=roots)
    _print({"grant_id": grant["grant_id"], "scope": [grant["profile_id"], grant["channel"], grant["user_id"]],
            "selector": grant["selector"], "tool": grant["wire_tool"], "actions": json.loads(grant["actions"]),
            "expires_in_seconds": round(grant["expires_at"] - time.time())})
    return 0


def _cmd_grants_list(args) -> int:
    _print(_journal().list_grants(args.profile))
    return 0


def _cmd_grants_revoke(args) -> int:
    grant = _journal().revoke_grant(args.grant_id)
    _print({"grant_id": grant["grant_id"], "state": grant["state"]})
    return 0


def register_cli(parser) -> None:
    subs = parser.add_subparsers(dest="gigacode_action")
    subs.add_parser("preflight", help="Print hashes for a new verification manifest").set_defaults(
        _gigacode_handler=_cmd_preflight)
    subs.add_parser("verify", help="Run the activation check (config + manifest)").set_defaults(
        _gigacode_handler=_cmd_verify)

    runs = subs.add_parser("runs", help="Inspect the run journal").add_subparsers(dest="gigacode_runs_action")
    p_list = runs.add_parser("list", help="Recent runs")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(_gigacode_handler=_cmd_runs_list)
    p_show = runs.add_parser("show", help="One run with its bridge audit")
    p_show.add_argument("run_id")
    p_show.set_defaults(_gigacode_handler=_cmd_runs_show)

    p_rec = subs.add_parser("reconcile", help="Resolve a recovery_required run")
    p_rec.add_argument("run_id")
    p_rec.add_argument("--decision", required=True, choices=["succeeded", "failed", "abandoned"])
    p_rec.add_argument("--reason", required=True)
    p_rec.add_argument("--dry-run", action="store_true")
    p_rec.set_defaults(_gigacode_handler=_cmd_reconcile)

    grants = subs.add_parser("grants", help="One-shot operator grants").add_subparsers(dest="gigacode_grants_action")
    p_issue = grants.add_parser("issue", help="Grant extra tool actions to one exact request")
    for flag in ("--profile", "--channel", "--user", "--tool", "--actions"):
        p_issue.add_argument(flag, required=True)
    selector = p_issue.add_mutually_exclusive_group(required=True)
    selector.add_argument("--request-key", help="Exact key as printed by `runs show`")
    selector.add_argument("--next-request", action="store_true", help="The first new request in this scope")
    p_issue.add_argument("--ttl", type=int, required=True, help="Seconds (1..wall_timeout_seconds)")
    p_issue.add_argument("--path-root", action="append", help="Allowed root for file tools (repeatable)")
    p_issue.set_defaults(_gigacode_handler=_cmd_grants_issue)
    p_glist = grants.add_parser("list", help="Grants of a profile")
    p_glist.add_argument("--profile", required=True)
    p_glist.set_defaults(_gigacode_handler=_cmd_grants_list)
    p_revoke = grants.add_parser("revoke", help="Revoke a pending or consumed grant")
    p_revoke.add_argument("grant_id")
    p_revoke.set_defaults(_gigacode_handler=_cmd_grants_revoke)


def gigacode_command(args) -> int:
    from agent.gigacode.journal import JournalError

    handler: Callable[[Any], int] | None = getattr(args, "_gigacode_handler", None)
    if handler is None:
        print("usage: hermes gigacode {preflight,verify,runs,reconcile,grants} ...", file=sys.stderr)
        return 2
    try:
        return handler(args)
    except (GigacodeError, JournalError, PermissionError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
