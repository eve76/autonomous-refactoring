"""LLM-provider transport configuration.

Anthropic and DeepSeek both use the Anthropic SDK/wire format here.
Secrets are resolved from an environment-variable name at runtime and are
never inserted into process argv or reproduction artefacts.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from anthropic import Anthropic

from config import Config


def _required_api_key(
    cfg: Config,
    environ: Mapping[str, str],
) -> str:
    name = cfg.effective_api_key_env
    value = environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{cfg.api_provider} provider requires environment variable {name}"
        )
    return value


def make_orchestrator_client(
    cfg: Config,
    environ: Mapping[str, str] | None = None,
):
    """Build the SDK client without exposing its credential."""
    source = os.environ if environ is None else environ
    if cfg.api_provider == "anthropic" and not cfg.api_base_url and not cfg.api_key_env:
        # Preserve the SDK's normal Anthropic environment handling.
        return Anthropic()

    kwargs = {"api_key": _required_api_key(cfg, source)}
    if cfg.effective_api_base_url:
        kwargs["base_url"] = cfg.effective_api_base_url
    return Anthropic(**kwargs)


def agent_subprocess_environment(
    cfg: Config,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    """Return the child environment for the selected CLI provider.

    Anthropic keeps normal process inheritance. DeepSeek's documented Claude
    Code variables are added to a private copy, so the coordinator process
    and other commands are not globally redirected to another API endpoint.
    """
    if cfg.api_provider == "anthropic" and not cfg.api_base_url and not cfg.api_key_env:
        return None

    source = os.environ if environ is None else environ
    child = dict(source)
    key = _required_api_key(cfg, source)

    child["ANTHROPIC_BASE_URL"] = cfg.effective_api_base_url
    child["ANTHROPIC_AUTH_TOKEN"] = key
    # Prevent an ambient Anthropic key from taking precedence over the
    # provider-specific auth token in Claude Code.
    child.pop("ANTHROPIC_API_KEY", None)

    if cfg.api_provider == "deepseek":
        child["ANTHROPIC_MODEL"] = cfg.agent_model
        child["ANTHROPIC_DEFAULT_OPUS_MODEL"] = cfg.agent_model
        child["ANTHROPIC_DEFAULT_SONNET_MODEL"] = cfg.agent_model
        child["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = cfg.agent_model
        child["CLAUDE_CODE_SUBAGENT_MODEL"] = cfg.agent_model
        child["CLAUDE_CODE_EFFORT_LEVEL"] = cfg.deepseek_effort
    return child
