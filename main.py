"""Entry point for the multi-agent refactoring system."""

import argparse
import json
import os
import re
import shlex
import time
from pathlib import Path

from analysis.penalty import PENALTY_METRICS
from config import Config, SUPPORTED_API_PROVIDERS
from coordination.coordinator import Coordinator
from production_profiles import (
    PROFILE_NAMES,
    get_production_profile,
    project_tool,
)


PROJECT_ENV_PATH = Path(__file__).resolve().parent / ".env"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def load_project_dotenv(path: Path = PROJECT_ENV_PATH) -> tuple[str, ...]:
    """Load a small, shell-like .env file without overriding the process.

    Existing environment variables win, so CI and an explicit shell export
    cannot be silently replaced by a local file. Only loaded variable names
    are returned; secret values are never logged.
    """
    if not path.is_file():
        return ()

    loaded: list[str] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _ENV_NAME.fullmatch(name):
            raise ValueError(
                f"invalid .env assignment at {path}:{line_number}"
            )
        try:
            lexer = shlex.shlex(raw_value, posix=True)
            lexer.whitespace_split = True
            lexer.commenters = "#"
            value = " ".join(lexer)
        except ValueError as exc:
            raise ValueError(
                f"invalid .env value at {path}:{line_number}"
            ) from exc
        if name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return tuple(loaded)


def _reject_unknown_metrics(value: dict, label: str) -> None:
    """Fail loudly on a misspelled metric key.

    Both --weights and --thresholds are merged into the defaults rather
    than replacing them, so an unknown key is accepted and then never
    read. That makes a typo silent: `--weights '{"lloc":0}'` (the
    thesis's own spelling of the metric this code calls `nloc`) runs the
    complete model instead of the LLOC ablation, and the resulting table
    row is wrong with nothing to indicate it.
    """
    unknown = sorted(set(value) - set(PENALTY_METRICS))
    if unknown:
        raise SystemExit(
            f"--{label}: unknown metric key(s): {', '.join(unknown)}. "
            f"Valid keys are {', '.join(PENALTY_METRICS)} "
            f"(the thesis's LLOC is 'nloc' here)."
        )


def _parse_json_dict(raw: str, label: str) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--{label} is not valid JSON: {exc}")
    if not isinstance(value, dict):
        raise SystemExit(f"--{label} must be a JSON object")
    return value


def parse_args() -> Config:
    load_project_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument(
        "--profile", choices=PROFILE_NAMES,
        help="Locked production scope and native validation for a supported repository",
    )
    p.add_argument(
        "--repo", type=Path,
        help="Path to target git repository (profile supplies the local default)",
    )
    p.add_argument(
        "--subdir", default=None,
        help="Subdirectory under a custom repository to refactor",
    )
    p.add_argument(
        "--work-root", type=Path,
        help="Directory for worktrees/results (profile supplies a local default)",
    )
    p.add_argument(
        "--full-worktree", action="store_true",
        help="Check out the full repository in agent worktrees while measuring "
             "and refactoring only --subdir; use when native tests need other directories",
    )
    p.add_argument("--analysts", type=int, default=3)
    p.add_argument("--programmers", type=int, default=3)
    p.add_argument(
        "--orchestrator-backlog-top-k", type=int, default=24,
        help="Number of highest-impact TODO issues sent to the orchestrator",
    )
    p.add_argument(
        "--analyst-lead-page-size", type=int, default=15,
        help="Maximum non-authoritative local static-analysis leads per Analyst",
    )
    p.add_argument(
        "--orchestrator-no-progress-limit", type=int, default=3,
        help="Stop after this many consecutive unusable assignment responses",
    )
    p.add_argument(
        "--max-run-input-tokens", type=int, default=0,
        help="Stop new dispatches after this reported input-token total (0=off)",
    )
    p.add_argument(
        "--max-run-output-tokens", type=int, default=0,
        help="Stop new dispatches after this reported output-token total (0=off)",
    )
    p.add_argument(
        "--max-run-cost-usd", type=float, default=0.0,
        help="Stop new dispatches after this pinned-price run-cost estimate "
             "in USD (0=off; in-flight agents finish)",
    )
    p.add_argument(
        "--max-run-cost-cny", type=float, default=None,
        help="Stop new DeepSeek dispatches after this pinned-price RMB cost "
             "estimate (default: 300 for DeepSeek; 0=off; in-flight agents finish)",
    )
    p.add_argument("--language", default=None, help="Target language (cpp|go|java|python)")
    p.add_argument(
        "--lizard-binary", default="",
        help="Lizard executable (defaults to the project virtualenv/PATH)",
    )
    p.add_argument(
        "--gocognit-binary", default="",
        help="gocognit executable used for Go cognitive complexity",
    )
    p.add_argument("--duplo-binary", default="", help="Path to duplo binary")
    p.add_argument(
        "--dupl-binary", default="", help="Path to mibk/dupl (Go only)",
    )
    p.add_argument(
        "--dupl-threshold-tokens", type=int, default=None,
        help="mibk/dupl minimum clone size in syntax tokens (default 100)",
    )
    p.add_argument(
        "--duplo-min-block-lines", type=int, default=None,
        help="Duplo -ml, minimum duplicate block size (default 4 = Duplo's own; "
             "the thesis does not specify one). Calibrate against Table 4.1.",
    )
    p.add_argument(
        "--exclude-dirs", default=None,
        help="Comma-separated directory names excluded from all measurement "
             "(§4.3.2 excludes test directories but does not enumerate them). "
             "Omit to keep the default list; pass '' to exclude nothing.",
    )
    p.add_argument(
        "--build-cmd", default="",
        help="Build command run inside each worktree, e.g. 'make -j8'",
    )
    p.add_argument(
        "--test-cmd", default="",
        help="Test command run inside each worktree, e.g. 'ctest --output-on-failure'",
    )
    p.add_argument(
        "--provider", choices=SUPPORTED_API_PROVIDERS, default="subscription",
        help=(
            "model transport: subscription uses the logged-in Claude Code "
            "Pro/Max allocation for every role; anthropic/openrouter/deepseek "
            "use APIs"
        ),
    )
    p.add_argument(
        "--api-base-url", default="",
        help="Override an API provider's Anthropic-compatible base URL",
    )
    p.add_argument(
        "--api-key-env", default="",
        help="Name of the environment variable containing the API key "
             "(the value is never accepted on the command line)",
    )
    p.add_argument(
        "--deepseek-effort", choices=("high", "max"), default="max",
        help="DeepSeek thinking effort for Analyst/Programmer Claude Code sessions; "
             "the Orchestrator always runs without thinking",
    )
    p.add_argument(
        "--model", default="",
        help="Model for both roles (legacy shorthand; role-specific flags win)",
    )
    p.add_argument("--orchestrator-model", default="", help="Orchestrator model")
    p.add_argument("--agent-model", default="", help="Analyst/programmer CLI model")
    p.add_argument(
        "--weights", default="",
        help='JSON per-metric weights, e.g. \'{"ccn":1,"nloc":0}\' (0 disables a metric)',
    )
    p.add_argument(
        "--thresholds", default="",
        help='JSON per-metric thresholds, e.g. \'{"ccn":15,"nloc":30}\'',
    )
    p.add_argument("--min-merge-gain", type=float, default=None,
                   help="Stagnation threshold in penalty units (paper: 10)")
    p.add_argument("--stagnation-limit", type=int, default=None,
                   help="Consecutive low-gain merges before stopping (paper: 3)")
    p.add_argument("--run-id", default="",
                   help="Names this run's results directory and integration branch")
    p.add_argument("--baseline-ref", default="",
                   help="Ref defining the run's starting state "
                        "(default: origin/<main>, else <main>)")
    p.add_argument("--push-run-branch", action="store_true",
                   help="Push the run branch to origin at the end (thesis 4.4). "
                        "Off by default because it writes to the target remote.")
    p.add_argument("--resume", action="store_true",
                   help="Restore baseline/stagnation state from a previous run")
    args = p.parse_args()

    profile = get_production_profile(args.profile) if args.profile else None
    if profile is not None:
        locked_overrides = [
            flag for flag, supplied in (
                ("--subdir", args.subdir is not None),
                ("--language", args.language is not None),
                ("--full-worktree", args.full_worktree),
                ("--build-cmd", bool(args.build_cmd)),
                ("--test-cmd", bool(args.test_cmd)),
            )
            if supplied
        ]
        if locked_overrides:
            raise SystemExit(
                f"--profile {profile.name} locks scope and validation; remove "
                f"{', '.join(locked_overrides)}"
            )
        repo_root = (args.repo or profile.repo_root).expanduser().resolve()
        work_root = (args.work_root or profile.work_root).expanduser().resolve()
        target_subdir = profile.target_subdir
        language = profile.language
        sparse_worktrees = profile.sparse_worktrees
        build_cmd = list(profile.build_cmd)
        test_cmd = list(profile.test_cmd)
        prewarm_build_cache = profile.prewarm_build_cache
        build_timeout_sec = profile.build_timeout_sec
        test_timeout_sec = profile.test_timeout_sec
        gate_timeout_sec = profile.gate_timeout_sec
        serialize_merge_gate = profile.serialize_merge_gate
        allowed_untracked = profile.gate_allowed_untracked_paths
        repo_allowed_untracked = profile.repo_allowed_untracked_paths
        profile_baseline_ref = profile.baseline_ref
        lizard_binary = args.lizard_binary or profile.lizard_binary
        gocognit_binary = args.gocognit_binary or profile.gocognit_binary
        duplo_binary = args.duplo_binary or profile.duplo_binary
        dupl_binary = args.dupl_binary or profile.dupl_binary
    else:
        if args.repo is None or args.work_root is None:
            raise SystemExit(
                "custom runs require --repo and --work-root (or choose --profile)"
            )
        if not args.build_cmd or not args.test_cmd:
            raise SystemExit(
                "custom runs require real --build-cmd and --test-cmd; "
                "no-op defaults are disabled"
            )
        repo_root = args.repo.expanduser().resolve()
        work_root = args.work_root.expanduser().resolve()
        target_subdir = args.subdir or "."
        language = args.language or "cpp"
        sparse_worktrees = not args.full_worktree
        build_cmd = shlex.split(args.build_cmd)
        test_cmd = shlex.split(args.test_cmd)
        prewarm_build_cache = False
        build_timeout_sec = 60 * 60
        test_timeout_sec = 60 * 60
        gate_timeout_sec = 3 * 60 * 60
        serialize_merge_gate = False
        allowed_untracked = ()
        repo_allowed_untracked = ()
        profile_baseline_ref = ""
        lizard_binary = args.lizard_binary or project_tool("lizard")
        gocognit_binary = args.gocognit_binary or project_tool("gocognit")
        duplo_binary = args.duplo_binary
        dupl_binary = args.dupl_binary

    cfg = Config(
        repo_root=repo_root,
        target_subdir=target_subdir,
        work_root=work_root,
        production_profile=profile.name if profile is not None else "",
        sparse_worktrees=sparse_worktrees,
        num_analysts=args.analysts,
        num_programmers=args.programmers,
        orchestrator_backlog_top_k=args.orchestrator_backlog_top_k,
        analyst_lead_page_size=args.analyst_lead_page_size,
        orchestrator_no_progress_limit=args.orchestrator_no_progress_limit,
        max_run_input_tokens=args.max_run_input_tokens,
        max_run_output_tokens=args.max_run_output_tokens,
        max_run_cost_usd=args.max_run_cost_usd,
        max_run_cost_cny=(
            300.0
            if (
                args.max_run_cost_cny is None
                and args.provider == "deepseek"
                and not args.max_run_cost_usd
            )
            else float(args.max_run_cost_cny or 0.0)
        ),
        api_provider=args.provider,
        api_base_url=args.api_base_url,
        api_key_env=args.api_key_env,
        deepseek_effort=args.deepseek_effort,
        lizard_language=language,
        lizard_binary=lizard_binary,
        gocognit_binary=(
            str(Path(gocognit_binary).expanduser().resolve())
            if Path(gocognit_binary).parent != Path(".")
            else gocognit_binary
        ),
        # The gate runs from a target-repository worktree, not from this
        # process's cwd. Resolve executable paths now so a documented value
        # such as `./bin/duplo` keeps referring to the same file there.
        duplo_binary=(
            str(Path(duplo_binary).expanduser().resolve())
            if duplo_binary else ""
        ),
        dupl_binary=(
            str(Path(dupl_binary).expanduser().resolve())
            if dupl_binary else ""
        ),
        dupl_threshold_tokens=(
            profile.dupl_threshold_tokens if profile is not None else 100
        ),
        duplo_min_block_lines=(
            profile.duplo_min_block_lines if profile is not None else 4
        ),
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        prewarm_build_cache=prewarm_build_cache,
        build_timeout_sec=build_timeout_sec,
        test_timeout_sec=test_timeout_sec,
        gate_timeout_sec=gate_timeout_sec,
        serialize_merge_gate=serialize_merge_gate,
        gate_allowed_untracked_paths=allowed_untracked,
        repo_allowed_untracked_paths=repo_allowed_untracked,
        run_id=args.run_id or time.strftime("%Y%m%d_%H%M%S"),
        resume=args.resume,
        baseline_ref=args.baseline_ref or profile_baseline_ref,
        push_run_branch=args.push_run_branch,
    )

    if args.model:
        cfg.orchestrator_model = args.model
        cfg.agent_model = args.model
    if args.orchestrator_model:
        cfg.orchestrator_model = args.orchestrator_model
    if args.agent_model:
        cfg.agent_model = args.agent_model

    weights = _parse_json_dict(args.weights, "weights")
    if weights:
        _reject_unknown_metrics(weights, "weights")
        cfg.weights.update(weights)
    thresholds = _parse_json_dict(args.thresholds, "thresholds")
    if thresholds:
        _reject_unknown_metrics(thresholds, "thresholds")
        cfg.thresholds.update(thresholds)

    if args.min_merge_gain is not None:
        cfg.min_merge_gain = args.min_merge_gain
    if args.stagnation_limit is not None:
        cfg.stagnation_limit = args.stagnation_limit
    if args.duplo_min_block_lines is not None:
        cfg.duplo_min_block_lines = args.duplo_min_block_lines
    if args.dupl_threshold_tokens is not None:
        cfg.dupl_threshold_tokens = args.dupl_threshold_tokens
    # `None` = flag omitted, keep the default list. An explicit empty
    # string is a deliberate "measure everything", so it must survive.
    if args.exclude_dirs is not None:
        cfg.exclude_dirs = tuple(
            d.strip() for d in args.exclude_dirs.split(",") if d.strip()
        )

    return cfg


def main() -> None:
    cfg = parse_args()
    coordinator = Coordinator(cfg)
    coordinator.run()


if __name__ == "__main__":
    main()
