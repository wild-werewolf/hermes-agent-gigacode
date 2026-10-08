"""``hermes gigacode`` operator commands through the real argparse tree and a temp HERMES_HOME."""

from __future__ import annotations

import json

import pytest

from agent.gigacode.journal import GigacodeJournal


def _hermes(argv: list[str], capsys) -> tuple[int, str, str]:
    from hermes_cli.main import _build_cli_parser

    parser, _ = _build_cli_parser()
    args = parser.parse_args(["gigacode", *argv])
    code = args.func(args)
    out = capsys.readouterr()
    return code, out.out, out.err


def _scope_args(user="telegram:7"):
    return ["--profile", "default", "--channel", "telegram:42", "--user", user]


def test_grants_issue_list_revoke(capsys):
    code, out, err = _hermes(["grants", "issue", *_scope_args(), "--tool", "hermes_memory", "--actions", "add",
                              "--next-request", "--ttl", "120"], capsys)
    assert code == 0, err
    grant = json.loads(out)
    assert grant["actions"] == ["add"] and grant["selector"] == "next_request"
    code, out, _ = _hermes(["grants", "list", "--profile", "default"], capsys)
    assert [g["grant_id"] for g in json.loads(out)] == [grant["grant_id"]]
    code, out, _ = _hermes(["grants", "revoke", grant["grant_id"]], capsys)
    assert json.loads(out)["state"] == "revoked"


@pytest.mark.parametrize("extra, message", [
    (["--tool", "hermes_memory", "--actions", "wipe", "--next-request", "--ttl", "60"], "subset"),
    (["--tool", "terminal", "--actions", "run", "--next-request", "--ttl", "60"], "unknown wire tool"),
    (["--tool", "hermes_memory", "--actions", "add", "--next-request", "--ttl", "0"], "--ttl"),
    (["--tool", "hermes_memory", "--actions", "add", "--next-request", "--ttl", "999999"], "--ttl"),
])
def test_grants_issue_refuses_bad_requests(capsys, extra, message):
    code, _, err = _hermes(["grants", "issue", *_scope_args(), *extra], capsys)
    assert code == 1 and message in err


def test_grants_issue_refuses_another_profile(capsys):
    code, _, err = _hermes(["grants", "issue", "--profile", "other", "--channel", "c", "--user", "u",
                            "--tool", "hermes_memory", "--actions", "add", "--next-request", "--ttl", "60"], capsys)
    assert code == 1 and "not the active profile" in err


def test_runs_show_and_reconcile_dry_run_then_abandon(capsys):
    journal = GigacodeJournal()
    run = journal.admit(profile_id="default", channel="telegram:42", user_id="telegram:7", request_id="9",
                        session_id="s", source="owner_private")
    journal.update(run.run_id, state="recovery_required", token_revoked=1, owner_pid=None)
    code, out, _ = _hermes(["runs", "show", run.run_id], capsys)
    shown = json.loads(out)
    assert code == 0 and shown["run"]["request_key"] == '["default","telegram:42","telegram:7","9"]'
    code, out, err = _hermes(["reconcile", run.run_id, "--decision", "succeeded", "--reason", "x", "--dry-run"], capsys)
    assert code == 1 and "no confirmed successful CLI result" in err
    code, out, _ = _hermes(["reconcile", run.run_id, "--decision", "abandoned", "--reason", "checked", "--dry-run"],
                           capsys)
    assert json.loads(out)["dry_run"] is True and journal.run(run.run_id)["state"] == "recovery_required"
    for _ in range(2):
        code, out, _ = _hermes(["reconcile", run.run_id, "--decision", "abandoned", "--reason", "checked"], capsys)
        assert code == 0
    assert journal.run(run.run_id)["state"] == "abandoned"
    code, _, err = _hermes(["reconcile", run.run_id, "--decision", "failed", "--reason", "x"], capsys)
    assert code == 1 and "already resolved" in err


def test_verify_without_activation_is_unverified(capsys):
    code, _, err = _hermes(["verify"], capsys)
    assert code == 1 and "config_unverified" in err
