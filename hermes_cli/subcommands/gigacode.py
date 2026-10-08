"""``hermes gigacode`` subcommand parser."""

from __future__ import annotations


def build_gigacode_parser(subparsers) -> None:
    """Attach the GigaCode runtime operator commands (journal, reconcile, grants, verification)."""
    parser = subparsers.add_parser(
        "gigacode",
        help="Operate the GigaCode CLI runtime (verify, runs, reconcile, grants)",
        description=(
            "Local operator commands for api_mode gigacode_cli: manifest preflight and verification, "
            "the run journal, recovery resolution and one-shot tool grants."
        ),
    )
    from hermes_cli.gigacode_cmd import gigacode_command, register_cli

    register_cli(parser)
    parser.set_defaults(func=gigacode_command)
