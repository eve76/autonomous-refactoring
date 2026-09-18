"""Verify Anthropic/OpenRouter/DeepSeek provider selection without API calls."""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from agents import provider                                             # noqa: E402
from agents.orchestrator import Orchestrator                            # noqa: E402
from config import (                                                    # noqa: E402
    Config,
    DEEPSEEK_ANTHROPIC_BASE_URL,
    OPENROUTER_ANTHROPIC_BASE_URL,
    OPENROUTER_OPUS_MODEL,
)
from main import load_project_dotenv, parse_args                        # noqa: E402
from scripts import production_validation                               # noqa: E402


FAILURES = []


def check(label, condition, detail=""):
    print(
        f"  {'PASS' if condition else 'FAIL'}  {label}"
        + (f"  [{detail}]" if detail else "")
    )
    if not condition:
        FAILURES.append(label)


def cfg(root: Path, **kwargs) -> Config:
    return Config(
        repo_root=root / "repo",
        target_subdir=".",
        work_root=root / "work",
        **kwargs,
    )


print("\n[1] provider defaults and validation")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    subscription_cfg = cfg(root)
    check("Claude subscription is the default provider",
          subscription_cfg.api_provider == "subscription")
    check("subscription mode stores no API credential variable",
          subscription_cfg.effective_api_key_env == "")
    check("subscription clean mode preserves OAuth instead of using --bare",
          "--safe-mode" in subscription_cfg.agent_cli_extra_args
          and "--bare" not in subscription_cfg.agent_cli_extra_args)
    anthropic_cfg = cfg(root, api_provider="anthropic")
    check("Anthropic keeps the Opus defaults",
          anthropic_cfg.orchestrator_model == "claude-opus-5"
          and anthropic_cfg.agent_model == "claude-opus-5")
    check("Anthropic uses its standard key variable",
          anthropic_cfg.effective_api_key_env == "ANTHROPIC_API_KEY")

    openrouter_cfg = cfg(root, api_provider="OpenRouter")
    check("OpenRouter selects its Anthropic skin and key variable",
          openrouter_cfg.api_provider == "openrouter"
          and openrouter_cfg.effective_api_base_url
          == OPENROUTER_ANTHROPIC_BASE_URL
          and openrouter_cfg.effective_api_key_env == "OPENROUTER_API_KEY")
    check("OpenRouter pins the equivalent Opus 5 slug for both roles",
          openrouter_cfg.orchestrator_model == OPENROUTER_OPUS_MODEL
          and openrouter_cfg.agent_model == OPENROUTER_OPUS_MODEL)
    check("OpenRouter accepts an audited USD cost ceiling",
          cfg(root, api_provider="openrouter", max_run_cost_usd=5.0)
          .max_run_cost_usd == 5.0)

    deepseek_cfg = cfg(root, api_provider="DeepSeek")
    check("provider names are normalized",
          deepseek_cfg.api_provider == "deepseek")
    check("DeepSeek Pro direct API model selected",
          deepseek_cfg.orchestrator_model == "deepseek-v4-pro")
    check("DeepSeek Pro 1M selected for Claude Code",
          deepseek_cfg.agent_model == "deepseek-v4-pro[1m]")
    check("official Anthropic-compatible endpoint selected",
          deepseek_cfg.effective_api_base_url == DEEPSEEK_ANTHROPIC_BASE_URL)
    check("DeepSeek key is read from its own variable",
          deepseek_cfg.effective_api_key_env == "DEEPSEEK_API_KEY")
    check("USD cost ceiling accepts pinned provider defaults",
          cfg(root, api_provider="deepseek", max_run_cost_usd=5.0)
          .max_run_cost_usd == 5.0)
    check("CNY cost ceiling accepts native DeepSeek pricing",
          cfg(root, api_provider="deepseek", max_run_cost_cny=300.0)
          .max_run_cost_cny == 300.0)

    try:
        cfg(
            root,
            api_provider="anthropic",
            max_run_cost_usd=1.0,
            orchestrator_model="unknown-model",
        )
    except ValueError as exc:
        check("cost ceiling rejects models without audited pricing",
              "no pinned pricing" in str(exc))
    else:
        check("cost ceiling rejects models without audited pricing", False)

    try:
        cfg(root, api_provider="unknown")
    except ValueError as exc:
        check("unknown provider fails loudly", "unknown" in str(exc))
    else:
        check("unknown provider fails loudly", False)


print("\n[2] credentials are resolved without entering argv or Config")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    deepseek_cfg = cfg(root, api_provider="deepseek")

    try:
        provider.agent_subprocess_environment(deepseek_cfg, {})
    except RuntimeError as exc:
        check("missing key fails before launching an agent",
              "DEEPSEEK_API_KEY" in str(exc))
    else:
        check("missing key fails before launching an agent", False)

    parent = {
        "PATH": os.environ.get("PATH", ""),
        "DEEPSEEK_API_KEY": "secret-test-value",
        "ANTHROPIC_API_KEY": "ambient-anthropic-key",
    }
    child = provider.agent_subprocess_environment(deepseek_cfg, parent)
    check("parent environment is not mutated",
          parent["ANTHROPIC_API_KEY"] == "ambient-anthropic-key")
    check("DeepSeek endpoint passed only to the child",
          child["ANTHROPIC_BASE_URL"] == DEEPSEEK_ANTHROPIC_BASE_URL)
    check("DeepSeek key mapped to Claude Code auth",
          child["ANTHROPIC_AUTH_TOKEN"] == "secret-test-value")
    check("ambient Anthropic key cannot override DeepSeek",
          "ANTHROPIC_API_KEY" not in child)
    check("agent and subagent model mapping is explicit",
          child["ANTHROPIC_MODEL"] == "deepseek-v4-pro[1m]"
          and child["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "deepseek-v4-pro[1m]"
          and child["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "deepseek-v4-pro[1m]"
          and child["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "deepseek-v4-pro[1m]"
          and child["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-v4-pro[1m]")
    check("coding-agent effort is explicit",
          child["CLAUDE_CODE_EFFORT_LEVEL"] == "max")
    check("Config stores the key name, never its value",
          "secret-test-value" not in repr(deepseek_cfg))

    openrouter_cfg = cfg(root, api_provider="openrouter")
    openrouter_child = provider.agent_subprocess_environment(
        openrouter_cfg,
        {
            "PATH": os.environ.get("PATH", ""),
            "OPENROUTER_API_KEY": "secret-openrouter-value",
            "ANTHROPIC_API_KEY": "ambient-anthropic-key",
        },
    )
    check("OpenRouter endpoint and auth token are private child settings",
          openrouter_child["ANTHROPIC_BASE_URL"]
          == OPENROUTER_ANTHROPIC_BASE_URL
          and openrouter_child["ANTHROPIC_AUTH_TOKEN"]
          == "secret-openrouter-value")
    check("OpenRouter explicitly blanks the conflicting Anthropic key",
          openrouter_child["ANTHROPIC_API_KEY"] == "")
    check("OpenRouter pins agent, class, and subagent model routing",
          all(openrouter_child[name] == OPENROUTER_OPUS_MODEL for name in (
              "ANTHROPIC_MODEL",
              "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "ANTHROPIC_DEFAULT_SONNET_MODEL",
              "ANTHROPIC_DEFAULT_HAIKU_MODEL",
              "CLAUDE_CODE_SUBAGENT_MODEL",
          )))
    check("OpenRouter does not inherit DeepSeek effort controls",
          "CLAUDE_CODE_EFFORT_LEVEL" not in openrouter_child)
    check("OpenRouter agents use isolated API mode",
          "--bare" in openrouter_cfg.agent_cli_extra_args
          and "--safe-mode" not in openrouter_cfg.agent_cli_extra_args)
    try:
        provider.agent_subprocess_environment(openrouter_cfg, {})
    except RuntimeError as exc:
        check("missing OpenRouter key fails before agent launch",
              "OPENROUTER_API_KEY" in str(exc))
    else:
        check("missing OpenRouter key fails before agent launch", False)

    subscription_parent = {
        "PATH": os.environ.get("PATH", ""),
        "ANTHROPIC_API_KEY": "must-not-reach-child",
        "ANTHROPIC_AUTH_TOKEN": "also-remove",
        "ANTHROPIC_BASE_URL": "https://api.example.invalid",
    }
    subscription_child = provider.agent_subprocess_environment(
        cfg(root), subscription_parent,
    )
    check("subscription children cannot inherit API billing variables",
          all(name not in subscription_child for name in (
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
              "ANTHROPIC_BASE_URL",
          )))

    try:
        cfg(root, max_run_cost_usd=1.0)
    except ValueError as exc:
        check("subscription rejects misleading billed-cost ceilings",
              "token ceilings" in str(exc))
    else:
        check("subscription rejects misleading billed-cost ceilings", False)


print("\n[3] SDK client uses structured non-thinking DeepSeek requests")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    deepseek_cfg = cfg(root, api_provider="deepseek")
    captured_client = {}
    real_anthropic = provider.Anthropic

    class FakeAnthropic:
        def __init__(self, **kwargs):
            captured_client.update(kwargs)

    try:
        provider.Anthropic = FakeAnthropic
        provider.make_orchestrator_client(
            deepseek_cfg, {"DEEPSEEK_API_KEY": "secret-sdk-value"},
        )
    finally:
        provider.Anthropic = real_anthropic

    check("SDK receives the DeepSeek endpoint",
          captured_client.get("base_url") == DEEPSEEK_ANTHROPIC_BASE_URL)
    check("SDK receives the provider key",
          captured_client.get("api_key") == "secret-sdk-value")

    openrouter_cfg = cfg(root, api_provider="openrouter")
    captured_openrouter = {}

    class FakeOpenRouterAnthropic:
        def __init__(self, **kwargs):
            captured_openrouter.update(kwargs)

    try:
        provider.Anthropic = FakeOpenRouterAnthropic
        provider.make_orchestrator_client(
            openrouter_cfg,
            {"OPENROUTER_API_KEY": "secret-openrouter-sdk-value"},
        )
    finally:
        provider.Anthropic = real_anthropic
    check("OpenRouter SDK uses the native Anthropic skin",
          captured_openrouter.get("base_url")
          == OPENROUTER_ANTHROPIC_BASE_URL)
    check("OpenRouter SDK receives only its provider key",
          captured_openrouter.get("api_key")
          == "secret-openrouter-sdk-value")

    request = {}

    class FakeMessages:
        def create(self, **kwargs):
            request.update(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        name="submit_assignment",
                        input={
                            "programmer_assignments": [{
                                "programmer_id": "PROG_1",
                                "issue_ids": ["ISSUE-0001"],
                            }],
                            "analyst_assignments": [],
                            "reasoning": "highest impact",
                        },
                    ),
                ],
                usage=SimpleNamespace(input_tokens=80, output_tokens=12),
            )

    orchestrator = object.__new__(Orchestrator)
    orchestrator.cfg = deepseek_cfg
    orchestrator.client = SimpleNamespace(messages=FakeMessages())
    payload = orchestrator._call("assign", "assignment")
    check("DeepSeek request uses the direct model id",
          request.get("model") == "deepseek-v4-pro")
    check("thinking is explicitly disabled and effort is absent",
          request.get("thinking") == {"type": "disabled"}
          and "output_config" not in request)
    check("assignment tool is forced with a JSON schema",
          request.get("tool_choice") == {
              "type": "tool", "name": "submit_assignment",
          }
          and request["tools"][0]["input_schema"]["type"] == "object")
    check("structured tool input is returned to the parser",
          payload["programmer_assignments"][0]["issue_ids"]
          == ["ISSUE-0001"])
    assignment = orchestrator._parse_assignment(payload)
    check("structured assignment reaches the coordinator decision",
          assignment.programmer_assignments == {
              "PROG_1": ["ISSUE-0001"],
          }
          and assignment.reasoning == "highest impact")
    stuck = orchestrator._parse_stuck({
        "terminate": ["PROG_2"],
        "keep": ["PROG_1"],
        "infeasible_issues": ["ISSUE-0099"],
        "reasoning": "one agent is looping",
    })
    check("structured stuck verdict reaches the coordinator decision",
          stuck.terminate == ["PROG_2"]
          and stuck.keep == ["PROG_1"]
          and stuck.infeasible_issues == ["ISSUE-0099"])
    usage_path = (
        deepseek_cfg.run_results_path
        / deepseek_cfg.orchestrator_usage_filename
    )
    usage_event = json.loads(usage_path.read_text().strip())
    check("orchestrator SDK usage is recorded by the real call path",
          usage_event["role"] == "orchestrator"
          and usage_event["input_tokens"] == 80
          and usage_event["output_tokens"] == 12)


print("\n[3b] subscription auth and orchestrator stay inside Claude Code")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    fake_cli = EXP / "tests" / "fake_claude_cli.py"
    subscription_cfg = cfg(root, claude_cli=str(fake_cli))
    auth = provider.validate_subscription_auth(subscription_cfg, {
        "PATH": os.environ.get("PATH", ""),
        "ANTHROPIC_API_KEY": "must-be-stripped",
    })
    check("preflight requires Claude.ai subscription metadata",
          auth["auth_method"] == "claude.ai"
          and auth["subscription_type"] == "max")
    subscription_orchestrator = Orchestrator(subscription_cfg)
    payload = subscription_orchestrator._call(
        "You are the orchestrator. Return an empty assignment.", "assignment",
    )
    check("subscription orchestrator receives schema-constrained CLI output",
          payload["programmer_assignments"] == []
          and payload["analyst_assignments"] == [])
    usage_path = (
        subscription_cfg.run_results_path
        / subscription_cfg.orchestrator_usage_filename
    )
    usage_event = json.loads(usage_path.read_text().strip())
    check("subscription orchestrator usage is captured from Claude CLI",
          usage_event["source"] == "orchestrator_cli"
          and usage_event["input_tokens"] == 300
          and usage_event["output_tokens"] == 30)

    tolerant_payload = subscription_orchestrator._call(
        "Return an assignment with one harmless extra field.", "assignment",
    )
    tolerant_decision = subscription_orchestrator._parse_assignment(
        tolerant_payload
    )
    check("subscription accepts typed input like the DeepSeek parser",
          tolerant_decision.programmer_assignments == {
              "PROG_3": ["ISSUE-0016", "ISSUE-0026"],
          })


print("\n[4] command-line provider selection and role-specific overrides")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    old_argv = sys.argv
    try:
        sys.argv = [
            "main.py",
            "--repo", str(root / "repo"),
            "--work-root", str(root / "work"),
            "--build-cmd", "echo build",
            "--test-cmd", "echo test",
        ]
        parsed = parse_args()
        check("CLI defaults every model role to subscription transport",
              parsed.api_provider == "subscription")

        sys.argv = [
            "main.py",
            "--repo", str(root / "repo"),
            "--work-root", str(root / "work"),
            "--build-cmd", "echo build",
            "--test-cmd", "echo test",
            "--provider", "openrouter",
        ]
        parsed = parse_args()
        check("--provider openrouter applies safe gateway defaults",
              parsed.effective_api_base_url
              == OPENROUTER_ANTHROPIC_BASE_URL
              and parsed.effective_api_key_env == "OPENROUTER_API_KEY"
              and parsed.orchestrator_model == OPENROUTER_OPUS_MODEL
              and parsed.agent_model == OPENROUTER_OPUS_MODEL)

        sys.argv = [
            "main.py",
            "--repo", str(root / "repo"),
            "--work-root", str(root / "work"),
            "--build-cmd", "echo build",
            "--test-cmd", "echo test",
            "--provider", "deepseek",
        ]
        parsed = parse_args()
        check("--provider deepseek applies both provider defaults",
              parsed.orchestrator_model == "deepseek-v4-pro"
              and parsed.agent_model == "deepseek-v4-pro[1m]")
        check("DeepSeek CLI defaults to the requested RMB ceiling",
              parsed.max_run_cost_cny == 300.0
              and parsed.max_run_cost_usd == 0.0)

        sys.argv = [
            "main.py",
            "--repo", str(root / "repo"),
            "--work-root", str(root / "work"),
            "--build-cmd", "echo build",
            "--test-cmd", "echo test",
            "--provider", "deepseek",
            "--model", "shared-model",
            "--orchestrator-model", "orchestrator-only",
            "--agent-model", "agent-only",
            "--api-key-env", "MY_DEEPSEEK_KEY",
            "--api-base-url", "https://example.invalid/anthropic",
            "--deepseek-effort", "high",
        ]
        parsed = parse_args()
        check("role-specific models override --model shorthand",
              parsed.orchestrator_model == "orchestrator-only"
              and parsed.agent_model == "agent-only")
        check("base URL and key-variable name are configurable",
              parsed.effective_api_base_url == "https://example.invalid/anthropic"
              and parsed.effective_api_key_env == "MY_DEEPSEEK_KEY")
        check("effort override is retained", parsed.deepseek_effort == "high")

        sys.argv = ["main.py", "--profile", "ferretdb"]
        parsed = parse_args()
        check("FerretDB production profile locks full-repo Go scope",
              parsed.production_profile == "ferretdb"
              and parsed.baseline_ref == "39afbdcafe3f00fc029e0e2b704640970bed8b4b"
              and parsed.target_subdir == "."
              and parsed.lizard_language == "go"
              and parsed.sparse_worktrees is False
              and parsed.serialize_merge_gate is False
              and parsed.dupl_binary.endswith("/dupl")
              and parsed.dupl_threshold_tokens == 100
              and parsed.duplo_binary == "")
        check("FerretDB profile uses real repository-native validation",
              "production_validation.py" in " ".join(parsed.build_cmd)
              and parsed.build_cmd[-2:] == ["ferretdb", "build"]
              and parsed.test_cmd[-2:] == ["ferretdb", "test"])
        parsed.validate_for_run()
        check("FerretDB production profile validates", True)

        sys.argv = [
            "main.py", "--profile", "mongodb-query",
            "--provider", "anthropic",
            "--max-run-cost-usd", "12.5",
        ]
        parsed = parse_args()
        check("MongoDB profile is locked to the query module",
              parsed.production_profile == "mongodb-query"
              and parsed.baseline_ref == "fbb28cf8c44023d334a646fe496fb95d355dc6f0"
              and parsed.target_subdir == "src/mongo/db/query"
              and parsed.lizard_language == "cpp"
              and parsed.sparse_worktrees is False
              and parsed.serialize_merge_gate is True
              and parsed.dupl_binary == ""
              and parsed.duplo_binary.endswith("/duplo")
              and parsed.duplo_min_block_lines == 4)
        check("CLI USD ceiling reaches production Config",
              parsed.max_run_cost_usd == 12.5
              and parsed.max_run_cost_cny == 0.0)
        check("MongoDB generated lockfile is the only gate exemption",
              parsed.gate_allowed_untracked_paths == ("MODULE.bazel.lock",))
        check("MongoDB prewarms the shared cache before model dispatch",
              parsed.prewarm_build_cache is True)
        check("MongoDB validation deadlines are independent and ordered",
              parsed.build_timeout_sec == 2 * 60 * 60
              and parsed.test_timeout_sec == 4 * 60 * 60
              and parsed.gate_timeout_sec > parsed.test_timeout_sec)
        parsed.validate_for_run()
        check("MongoDB production profile validates", True)

        sys.argv = [
            "main.py", "--repo", str(root / "repo"),
            "--work-root", str(root / "work"),
        ]
        try:
            parse_args()
        except SystemExit as exc:
            check("custom CLI runs cannot omit build/test validation",
                  "require real --build-cmd" in str(exc))
        else:
            check("custom CLI runs cannot omit build/test validation", False)

        sys.argv = [
            "main.py", "--profile", "mongodb-query", "--subdir", ".",
        ]
        try:
            parse_args()
        except SystemExit as exc:
            check("production profile scope cannot be overridden",
                  "locks scope and validation" in str(exc))
        else:
            check("production profile scope cannot be overridden", False)
    finally:
        sys.argv = old_argv


print("\n[5] production validation generates metadata")
commands = production_validation._commands("ferretdb", "build")
check("FerretDB validation generates version metadata first",
      commands[0][-2:] == ["generate", "./build/version"])
check("FerretDB build validation remains repository-native",
      "-run=^$" in commands[1])
mongo_test = production_validation._commands("mongodb-query", "test")
check("MongoDB correctness gate excludes service tests and benchmarks",
      "--test_tag_filters=-mongo_integration_test,"
      "-mongo_integration_test_debug,-mongo_benchmark,"
      "-mongo_benchmark_debug,-intermediate_debug"
      in mongo_test[0])
mongo_build = production_validation._commands("mongodb-query", "build")
mongo_build_cache = next(
    arg for arg in mongo_build[0] if arg.startswith("--disk_cache=")
)
mongo_test_cache = next(
    arg for arg in mongo_test[0] if arg.startswith("--disk_cache=")
)
check("MongoDB build and test share one Bazel disk cache",
      mongo_build_cache == mongo_test_cache)
old_output_base = os.environ.get(production_validation.BASELINE_OUTPUT_BASE_ENV)
try:
    os.environ[production_validation.BASELINE_OUTPUT_BASE_ENV] = "/tmp/baseline-output"
    isolated_build = production_validation._commands("mongodb-query", "build")[0]
    check("MongoDB baseline can use an isolated Bazel output base",
          isolated_build[1] == "--output_base=/tmp/baseline-output"
          and isolated_build[2] == "build")
finally:
    if old_output_base is None:
        os.environ.pop(production_validation.BASELINE_OUTPUT_BASE_ENV, None)
    else:
        os.environ[production_validation.BASELINE_OUTPUT_BASE_ENV] = old_output_base


print("\n[6] project .env loading is automatic and non-overriding")
with tempfile.TemporaryDirectory() as td:
    env_path = Path(td) / ".env"
    env_path.write_text(
        "# local credentials\n"
        "DEEPSEEK_API_KEY='from env file'\n"
        "export ANTHROPIC_API_KEY=\"anthropic-file-value\"\n"
        "EMPTY_VALUE=\n",
        encoding="utf-8",
    )
    names = ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY", "EMPTY_VALUE")
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ["DEEPSEEK_API_KEY"] = "explicit-shell-value"
        loaded = load_project_dotenv(env_path)
        check("existing shell value wins over .env",
              os.environ["DEEPSEEK_API_KEY"] == "explicit-shell-value")
        check("quoted and export assignments load",
              os.environ["ANTHROPIC_API_KEY"] == "anthropic-file-value")
        check("empty assignment is supported",
              os.environ["EMPTY_VALUE"] == "")
        check("loader reports names without secret values",
              loaded == ("ANTHROPIC_API_KEY", "EMPTY_VALUE")
              and "anthropic-file-value" not in repr(loaded))
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    bad_path = Path(td) / "bad.env"
    bad_path.write_text("NOT A NAME=secret\n", encoding="utf-8")
    try:
        load_project_dotenv(bad_path)
    except ValueError as exc:
        check("malformed assignments fail without echoing the secret",
              "secret" not in str(exc) and ":1" in str(exc))
    else:
        check("malformed assignments fail without echoing the secret", False)


print("\n" + "=" * 62)
print(
    f"FAILURES ({len(FAILURES)}): " + "; ".join(FAILURES)
    if FAILURES else "ALL CHECKS PASSED"
)
raise SystemExit(1 if FAILURES else 0)
