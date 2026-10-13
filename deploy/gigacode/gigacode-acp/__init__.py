"""GigaCode as the only model backend of Hermes, the way copilot-acp runs Hermes on Copilot.

A user model-provider plugin: copy this directory to
``$HERMES_HOME/plugins/model-providers/gigacode-acp/``. Every model call (the main turn and the
auxiliary tasks) starts ``gigacode --acp`` over stdio, sends the conversation as one prompt and
reads the reply; GigaCode's own login, model and policy answer it, and Hermes executes the tool
calls GigaCode returns. No API key lives in Hermes.

The transport is the bundled ACP client (``agent.copilot_acp_client``), which already speaks
generic ACP v1; only the launch command and the default model differ from Copilot.
"""

from functools import cache
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

# Placeholder model id: "whatever model GigaCode itself is configured for".
SESSION_DEFAULT_MODEL = "gigacode"


@cache
def _client_class() -> type:
    from agent.copilot_acp_client import CopilotACPClient

    class GigaCodeACPClient(CopilotACPClient):
        def _run_prompt(self, prompt_text: str, *, timeout_seconds: float, model: str | None = None) -> tuple[str, str]:
            # The placeholder is not a GigaCode model id; selecting it would only log a warning per call.
            if model == SESSION_DEFAULT_MODEL:
                model = None
            return super()._run_prompt(prompt_text, timeout_seconds=timeout_seconds, model=model)

    return GigaCodeACPClient


class GigaCodeACPProfile(ProviderProfile):
    """GigaCode CLI over ACP — external process, no REST models endpoint."""

    def create_client(self, **client_kwargs: Any) -> Any:
        return _client_class()(**client_kwargs)

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 15.0
    ) -> list[str] | None:
        """Models a short-lived GigaCode ACP session advertises, plus the session-default row."""
        from hermes_cli.auth import resolve_external_process_provider_credentials

        try:
            creds = resolve_external_process_provider_credentials(self.name)
            client = self.create_client(
                api_key=creds.get("api_key"), base_url=creds.get("base_url"),
                command=creds.get("command"), args=creds.get("args"))
            advertised = client.list_models(timeout_seconds=timeout)
        except (RuntimeError, TimeoutError, OSError):  # AuthError is a RuntimeError
            # Missing CLI, refused --acp, not logged in, probe timeout: "None if the fetch failed".
            return None
        return [SESSION_DEFAULT_MODEL, *(m for m in advertised if m != SESSION_DEFAULT_MODEL)]


gigacode_acp = GigaCodeACPProfile(
    name="gigacode", aliases=("gigacode-acp",),
    api_mode="chat_completions",
    env_vars=(),  # GigaCode owns its login
    base_url="acp://gigacode",
    auth_type="external_process",
    process_command="gigacode",
    process_args=("--acp",),
    process_command_env_vars=("HERMES_GIGACODE_ACP_COMMAND",),
    process_args_env_var="HERMES_GIGACODE_ACP_ARGS",
    fallback_models=(SESSION_DEFAULT_MODEL,),
)

register_provider(gigacode_acp)
