"""GigaCode CLI provider profile (``model.provider: gigacode-cli``, ``api_mode: gigacode_cli``).

Selection and configuration only: this profile never builds a model client. The whole turn runs
in ``agent/gigacode_runtime.py`` (a sandboxed ``gigacode`` process per request); the CLI owns its
model and auth, Hermes owns sessions, memory, cron and tool permissions. There is no fallback
to another provider. Launch settings live under ``gigacode:`` in config.yaml, not in env vars.
"""

from providers import register_provider
from providers.base import ProviderProfile

gigacode_cli = ProviderProfile(
    name="gigacode-cli",
    aliases=("gigacode",),
    api_mode="gigacode_cli",
    display_name="GigaCode CLI",
    description="GigaCode CLI agent runs each turn; Hermes tools via a per-run MCP bridge",
    env_vars=(),
    base_url="gigacode://local",
    auth_type="external_process",
    supports_health_check=False,
    supports_model_listing=False,
    hidden=True,  # activation requires an operator verification manifest, not a picker click
)

register_provider(gigacode_cli)
