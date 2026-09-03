"""LLM transport, subscription authentication, and child isolation.

The default transport uses Claude Code's Claude.ai OAuth credentials. Optional
Anthropic and DeepSeek API transports use the Anthropic SDK/wire format.
Secrets are never inserted into argv or reproduction artefacts.
"""

from __future__ import annotations

import os
import json
import subprocess
from collections.abc import Mapping

from anthropic import Anthropic

from config import Config


_API_ENVIRONMENT_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)


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
    if cfg.api_provider == "subscription":
        raise RuntimeError(
            "Claude subscription mode uses the Claude Code CLI, not the "
            "Anthropic Messages API"
        )
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

    Subscription mode removes API routing variables. Anthropic keeps normal
    inheritance, while DeepSeek's documented variables are added only to a
    private copy so the coordinator is not globally redirected.
    """
    if cfg.api_provider == "subscription":
        # Claude Code OAuth/keychain credentials belong to the subscription.
        # Remove every API/gateway routing variable from the child so an
        # ambient Console key cannot silently change the billing surface.
        source = os.environ if environ is None else environ
        child = dict(source)
        for name in _API_ENVIRONMENT_KEYS:
            child.pop(name, None)
        return child

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


def validate_subscription_auth(
    cfg: Config,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Fail closed unless Claude Code reports Claude.ai subscription auth.

    ``--bare`` cannot read OAuth/keychain credentials. Subscription runs use
    safe mode instead, and this preflight makes a missing or Console-backed
    login fail before the coordinator creates any experimental state.
    """
    if cfg.api_provider != "subscription":
        return {}
    completed = subprocess.run(
        [cfg.claude_cli, "auth", "status", "--json"],
        text=True,
        capture_output=True,
        env=agent_subprocess_environment(cfg, environ),
        timeout=30,
    )
    try:
        status = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "could not parse `claude auth status --json`; run `claude auth login` "
            "and choose your Claude.ai Pro/Max subscription"
        ) from exc
    auth_method = str(status.get("authMethod") or "").lower()
    subscription_type = status.get("subscriptionType")
    if (
        completed.returncode != 0
        or status.get("loggedIn") is not True
        or auth_method not in ("claude.ai", "oauth_token")
        or not subscription_type
    ):
        raise RuntimeError(
            "Claude Code subscription authentication is required; run "
            "`claude auth login`, choose Claude.ai Pro/Max, then confirm that "
            "`claude auth status --json` reports a subscriptionType"
        )
    version = subprocess.run(
        [cfg.claude_cli, "--version"],
        text=True,
        capture_output=True,
        env=agent_subprocess_environment(cfg, environ),
        timeout=30,
    )
    return {
        "auth_method": auth_method,
        "subscription_type": str(subscription_type),
        "api_provider": str(status.get("apiProvider") or ""),
        "claude_cli_version": version.stdout.strip(),
    }
