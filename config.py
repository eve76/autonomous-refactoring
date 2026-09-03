"""Central configuration for the multi-agent system."""

from dataclasses import dataclass, field
from pathlib import Path
import shutil
import math

from analysis import tools
from coordination.model_pricing import require_model_price


SUPPORTED_API_PROVIDERS = ("anthropic", "deepseek")
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
# Static analysis runs before the command-specific phases and does not have a
# separate deadline.  Reserve a small, explicit budget for it when a dynamic
# benchmark is enabled; otherwise a gate can be killed between valid phases.
GATE_STATIC_ANALYSIS_MARGIN_SEC = 5 * 60


@dataclass
class Config:
    repo_root: Path
    target_subdir: str
    work_root: Path
    production_profile: str = ""
    # By default a subdirectory target produces sparse agent worktrees.
    # Disable this when repository-native build/tests need files outside the
    # measured/refactored subtree. Measurement remains scoped to target_subdir.
    sparse_worktrees: bool = True

    num_analysts: int = 3
    num_programmers: int = 3
    # Token-saving views. These affect only how much context the LLM sees;
    # the complete backlog and static-analysis results remain local.
    orchestrator_backlog_top_k: int = 24
    analyst_lead_page_size: int = 15
    # Stop after repeated orchestrator responses that assign no usable work.
    # This bounds wasted calls if a provider returns empty or malformed text.
    orchestrator_no_progress_limit: int = 3

    claude_cli: str = "claude"
    # Both providers use the Anthropic SDK/wire format. DeepSeek exposes an
    # official Anthropic-compatible endpoint and documents Claude Code as a
    # supported agent harness.
    api_provider: str = "anthropic"
    api_base_url: str = ""
    # Environment-variable *name* only. The secret value is read at runtime
    # and is never put in argv, Config serialization, or run_summary.json.
    api_key_env: str = ""
    orchestrator_model: str = ""
    agent_model: str = ""
    # Thinking effort for Claude Code Analyst/Programmer subprocesses only.
    # The synchronous Orchestrator deliberately runs without thinking.
    deepseek_effort: str = "max"
    # Headless Claude sessions must be able to run the git/static-analysis/
    # build/test commands that implement the paper's workflow. `acceptEdits`
    # alone only approves file edits, so Bash is explicitly allowlisted.
    agent_allowed_tools: tuple[str, ...] = (
        "Bash", "Edit", "Write", "Read", "Glob", "Grep",
    )
    # Disable project/user memory, CLAUDE.md discovery, hooks, plugins and
    # session persistence so each dispatch starts from the clean context the
    # thesis requires.
    agent_bare_mode: bool = True

    # Stagnation: counter increments when a merge yields < min_merge_gain
    # in penalty reduction, or when a programmer is killed for timeout.
    # System stops once counter reaches stagnation_limit without an
    # intervening merge above min_merge_gain.
    min_merge_gain: float = 10.0
    stagnation_limit: int = 3

    # Hard kill: a programmer that has not produced a successful merge
    # within 30 minutes is terminated and the stagnation counter ticks.
    programmer_timeout_sec: int = 30 * 60
    # Analysts have no thesis-level soft timeout, but an external tool call
    # must not be able to hold an unattended run forever.  This hard ceiling
    # safely kills the isolated Analyst process and makes its reserved leads
    # available for a later dispatch.
    analyst_timeout_sec: int = 15 * 60
    # Soft threshold that triggers the orchestrator's stuck-agent
    # evaluation (thesis: the programmer prompt specifies a ten-minute
    # limit per issue). A discretionary termination here does NOT tick
    # the stagnation counter; only the hard timeout does.
    issue_timeout_sec: int = 10 * 60
    # Minimum gap between successive stuck-agent evaluations of the same
    # programmer, so the orchestrator is not re-queried every tick.
    stuck_eval_interval_sec: int = 5 * 60
    # If analysts repeatedly discover no new actionable issue, stop rather
    # than querying the orchestrator forever. Three empty scans correspond to
    # one full pass of the default three-analyst pool.
    empty_scan_limit: int = 3

    backlog_drain_interval_sec: float = 1.0

    # Penalty thresholds per metric — values exceeding the threshold
    # contribute to penalty via the hyperbolic function (Eq 4.4).
    # Duplicates use Eq 4.5 instead and have no real threshold; the
    # entry exists so the dict reads naturally.
    thresholds: dict = field(default_factory=lambda: {
        "ccn": 15,
        "cognitive": 15,
        "nloc": 30,
        "param": 5,
        "duplicates": 0,
    })

    # Per-metric weights. Setting any to 0 disables that metric's
    # contribution to the penalty totals, which is how single-metric
    # and ablation runs are configured.
    weights: dict = field(default_factory=lambda: {
        "ccn": 1,
        "nloc": 1,
        "cognitive": 1,
        "param": 1,
        "duplicates": 1,
    })

    # Dynamic benchmark policy is deliberately separate from correctness
    # validation.  "off" preserves the historical static-only gate;
    # "observe" writes a comparison but never changes acceptance; "enforce"
    # rejects unavailable comparisons and regressions after build/test pass.
    dynamic_mode: str = "off"
    dynamic_repetitions: int = 7
    # Diagnostic threshold only. High CV is recorded but never rejects a gate.
    dynamic_max_cv: float = 0.10
    dynamic_tolerance: float = 0.05
    # These weights apply only to the profile's approved metrics.  MongoDB
    # consumes real_time; FerretDB consumes ns/op and B/op.  The URL is never
    # a Config value: only its environment-variable name is serialised.
    dynamic_weights: dict = field(default_factory=lambda: {
        "mongodb_real_time": 1.0,
        "ferretdb_ns_per_op": 0.7,
        "ferretdb_bytes_per_op": 0.3,
    })
    dynamic_ferretdb_url_env: str = "FERRETDB_BENCHMARK_POSTGRESQL_URL"
    dynamic_bazel_binary: str = "bazel"
    dynamic_timeout_sec: int = 60 * 60

    # Language-specific duplication backends. Go uses mibk/dupl; C/C++
    # retains dlidstrom/Duplo. Empty string disables that backend.
    dupl_binary: str = ""
    dupl_threshold_tokens: int = 100
    duplo_binary: str = ""
    # Duplo -ml flag. The thesis never specifies a minimum block size, so
    # this is Duplo's own default; raising it suppresses short duplicate
    # blocks and lowers both the ratio and the block count. Calibrate
    # against Table 4.1 (7 blocks at a 2.41% ratio) on the target repo.
    duplo_min_block_lines: int = 4

    # Directory names excluded from every measurement (§4.3.2: "Test
    # directories are excluded since they do not contribute to the penalty
    # score"). The thesis does not enumerate them, so this is a parameter
    # of the experiment — set it to match the target repository's layout.
    exclude_dirs: tuple[str, ...] = field(
        default_factory=lambda: tools.DEFAULT_EXCLUDE_DIRS
    )

    # Lizard CLI binary. Invoked as a subprocess rather than through the
    # Python API so the language is pinned by -l instead of auto-detected.
    # Left on PATH by default so this is portable across machines.
    lizard_binary: str = "lizard"
    # Go cognitive-complexity CLI. Required for Go measurements and resolved
    # through PATH unless an explicit executable path is supplied.
    gocognit_binary: str = "gocognit"
    # Target language for Lizard / file-suffix filter. The thesis targets
    # C/C++; Go uses gocognit while other non-C/C++ languages currently
    # produce an empty cognitive population.
    lizard_language: str = "cpp"

    log_dir: Path = field(default_factory=lambda: Path("logs"))
    backlog_path: Path = field(default_factory=lambda: Path("backlog.json"))

    # -- run archiving / reproduction artefacts ------------------------
    # Each run gets work_root/results/<run_id>/ holding the penalty
    # time series, the penalty plot and the final run summary.
    results_dir: Path = field(default_factory=lambda: Path("results"))
    penalty_history_filename: str = "penalty_history.json"
    penalty_plot_filename: str = "penalty.png"
    run_summary_filename: str = "run_summary.json"
    # One line per merge-gate attempt, appended by the gate subprocesses.
    # Source of the failure counts in run_summary.json (§5.1, §5.4).
    gate_attempts_filename: str = "gate_attempts.jsonl"
    # Provider-neutral token accounting. Agent usage is recovered from the
    # full stream-json logs; synchronous orchestrator calls append here.
    token_usage_filename: str = "token_usage.json"
    orchestrator_usage_filename: str = "orchestrator_usage.jsonl"
    # Append-only, full SDK responses from the orchestrator. Unlike the
    # compact usage log this preserves the exact model payload and the text
    # forwarded to the assignment parser for post-run protocol diagnosis.
    orchestrator_raw_responses_filename: str = "orchestrator_raw_responses.jsonl"
    # Optional dispatch ceilings. Zero disables the ceiling. Because usage is
    # reported after model turns complete, these are conservative boundaries:
    # new work stops, while already-running agents are allowed to finish.
    max_run_input_tokens: int = 0
    max_run_output_tokens: int = 0
    # Reproducible USD dispatch ceiling. Cost is calculated from the pinned
    # provider price snapshot; zero disables it. In-flight agents finish.
    max_run_cost_usd: float = 0.0
    # Native DeepSeek RMB ceiling. The CLI defaults this to CNY 300 for
    # DeepSeek runs; zero disables it. It is not an exchange-rate conversion.
    max_run_cost_cny: float = 0.0
    # Crash-recovery state, written after every merge and on shutdown.
    state_filename: str = "run_state.json"
    # Resume from an existing state file instead of starting fresh.
    resume: bool = False
    # Overridden by main.py with a wall-clock stamp; kept here so every
    # component can derive the same results directory.
    run_id: str = ""

    # Build / test commands invoked by the merge gate inside each worktree.
    # Empty defaults fail closed at run startup; use a locked production
    # profile or supply both explicitly for a custom repository.
    build_cmd: list[str] = field(default_factory=list)
    test_cmd: list[str] = field(default_factory=list)
    # Production profiles may validate the immutable baseline with the exact
    # build command before any model dispatch.  This both fails fast on an
    # invalid environment and warms a shared Bazel disk cache.
    prewarm_build_cache: bool = False
    # Build and test run inside the merge gate and have their own deadlines.
    # They are deliberately independent of programmer_timeout_sec, which
    # governs model work rather than repository validation.
    build_timeout_sec: int = 60 * 60
    test_timeout_sec: int = 60 * 60
    # Last-resort bound for an active gate, including static analysis,
    # build, and test.  It must exceed both command-specific deadlines.
    # Four hours accommodates the default 1h build, 1h correctness test,
    # 1h dynamic benchmark and the explicit static-analysis margin when
    # callers enable dynamic mode directly on Config.
    gate_timeout_sec: int = 4 * 60 * 60
    # Exact repository-relative paths that build tooling may create as
    # *untracked* files.  The merge gate still rejects tracked modifications,
    # and the list is empty by default.  This avoids teaching the gate broad
    # repository-specific ignore rules.
    gate_allowed_untracked_paths: tuple[str, ...] = ()
    # Exact repository-root untracked paths that may already exist before a
    # run. Production profiles keep this list narrow; tracked edits are never
    # allowed. Directory entries also cover their descendants.
    repo_allowed_untracked_paths: tuple[str, ...] = ()
    # The Programmer prompt specifies three gate attempts per issue. Enforce
    # the same boundary in the gate CLI so a model cannot turn one issue into
    # an unbounded build loop.
    max_gate_attempts_per_issue: int = 3
    # Run-wide assignment boundary. An issue may be handed to a Programmer
    # at most this many times, even when a stuck/failed session returns it to
    # TODO. This is distinct from gate invocations within one dispatch.
    max_issue_dispatches: int = 3

    # The repository's primary branch. It is never advanced during a run;
    # it defines the baseline and is checked back out at the end.
    main_branch: str = "main"
    # Ref defining the run's starting state (thesis §4.4: "the codebase was
    # reset to its original state before each run"). Empty resolves to
    # origin/<main_branch>, falling back to <main_branch> without a remote.
    baseline_ref: str = ""
    # Per-run integration branch (thesis §4.4: "a dedicated git branch ...
    # All commits made during the run are recorded on this branch"). The
    # merge gate fast-forwards this branch, not main, so consecutive runs
    # all start from the same baseline without a destructive reset.
    run_branch_prefix: str = "refactor"
    # Push the run branch at the end of the run. Thesis §4.4 does this, but
    # it writes to the target repository's remote, so it is opt-in.
    push_run_branch: bool = False

    gate_config_filename: str = ".gate_config.json"

    def __post_init__(self) -> None:
        self.api_provider = self.api_provider.strip().lower()
        if self.api_provider not in SUPPORTED_API_PROVIDERS:
            raise ValueError(
                f"unsupported API provider {self.api_provider!r}; "
                f"choose one of {', '.join(SUPPORTED_API_PROVIDERS)}"
            )
        if self.deepseek_effort not in ("high", "max"):
            raise ValueError("deepseek_effort must be 'high' or 'max'")
        if self.orchestrator_backlog_top_k <= 0:
            raise ValueError("orchestrator_backlog_top_k must be positive")
        if self.analyst_lead_page_size <= 0:
            raise ValueError("analyst_lead_page_size must be positive")
        if self.orchestrator_no_progress_limit <= 0:
            raise ValueError("orchestrator_no_progress_limit must be positive")
        if self.max_run_input_tokens < 0 or self.max_run_output_tokens < 0:
            raise ValueError("run token ceilings cannot be negative")
        if self.max_run_cost_usd < 0 or self.max_run_cost_cny < 0:
            raise ValueError("run cost ceiling cannot be negative")
        if self.max_run_cost_usd and self.max_run_cost_cny:
            raise ValueError("choose only one run cost currency")
        if self.max_run_cost_cny and self.api_provider != "deepseek":
            raise ValueError("CNY cost ceiling is supported only for DeepSeek")
        if self.analyst_timeout_sec <= 0:
            raise ValueError("analyst_timeout_sec must be positive")
        if self.max_gate_attempts_per_issue <= 0:
            raise ValueError("max_gate_attempts_per_issue must be positive")
        if self.max_issue_dispatches <= 0:
            raise ValueError("max_issue_dispatches must be positive")
        if self.build_timeout_sec <= 0 or self.test_timeout_sec <= 0:
            raise ValueError("build/test timeouts must be positive")
        if self.dynamic_mode not in ("off", "observe", "enforce"):
            raise ValueError("dynamic_mode must be 'off', 'observe', or 'enforce'")
        if self.dynamic_repetitions <= 0:
            raise ValueError("dynamic_repetitions must be positive")
        if self.dynamic_max_cv < 0:
            raise ValueError("dynamic_max_cv cannot be negative")
        if self.dynamic_tolerance < 0:
            raise ValueError("dynamic_tolerance cannot be negative")
        if self.dynamic_timeout_sec <= 0:
            raise ValueError("dynamic_timeout_sec must be positive")
        self.validate_dynamic_policy()
        required_gate_timeout = max(self.build_timeout_sec, self.test_timeout_sec)
        if self.dynamic_mode != "off":
            required_gate_timeout = (
                self.build_timeout_sec
                + self.test_timeout_sec
                + self.dynamic_timeout_sec
                + GATE_STATIC_ANALYSIS_MARGIN_SEC
            )
        if self.gate_timeout_sec <= required_gate_timeout:
            if self.dynamic_mode == "off":
                raise ValueError(
                    "gate_timeout_sec must exceed build_timeout_sec and "
                    "test_timeout_sec"
                )
            raise ValueError(
                "dynamic benchmarks require gate_timeout_sec to exceed "
                "build_timeout_sec + test_timeout_sec + dynamic_timeout_sec "
                f"+ {GATE_STATIC_ANALYSIS_MARGIN_SEC}s static-analysis margin"
            )
        import re
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.dynamic_ferretdb_url_env):
            raise ValueError("dynamic_ferretdb_url_env must be an environment-variable name")

        if not self.orchestrator_model:
            self.orchestrator_model = (
                "deepseek-v4-pro"
                if self.api_provider == "deepseek"
                else "claude-opus-4-7"
            )
        if not self.agent_model:
            self.agent_model = (
                "deepseek-v4-pro[1m]"
                if self.api_provider == "deepseek"
                else "claude-opus-4-7"
            )
        if self.max_run_cost_usd or self.max_run_cost_cny:
            require_model_price(self.api_provider, self.orchestrator_model)
            require_model_price(self.api_provider, self.agent_model)

    def validate_dynamic_policy(self) -> None:
        """Reject malformed dynamic weighting before it can affect a gate.

        This is public because ``main.py`` applies CLI overrides after
        dataclass construction.  Keeping the validation here makes direct
        Config users and CLI users follow the same rule.
        """
        if not isinstance(self.dynamic_weights, dict):
            raise ValueError("dynamic_weights must be a mapping")
        for metric, raw_weight in self.dynamic_weights.items():
            try:
                weight = float(raw_weight)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"dynamic weight for {metric!r} must be a finite non-negative number"
                ) from exc
            if not math.isfinite(weight) or weight < 0:
                raise ValueError(
                    f"dynamic weight for {metric!r} must be a finite non-negative number"
                )

    def validate_for_run(self) -> None:
        """Reject unsafe/no-op validation before creating run state."""
        # CLI overrides and programmatic callers can mutate a Config after
        # dataclass construction, so repeat the dynamic policy/deadline
        # checks at the point where a real run is authorised.
        self.validate_dynamic_policy()
        if self.dynamic_mode != "off":
            required_gate_timeout = (
                self.build_timeout_sec
                + self.test_timeout_sec
                + self.dynamic_timeout_sec
                + GATE_STATIC_ANALYSIS_MARGIN_SEC
            )
            if self.gate_timeout_sec <= required_gate_timeout:
                raise ValueError(
                    "dynamic benchmarks require gate_timeout_sec to exceed "
                    "build_timeout_sec + test_timeout_sec + dynamic_timeout_sec "
                    f"+ {GATE_STATIC_ANALYSIS_MARGIN_SEC}s static-analysis margin"
                )
        for label, command in (
            ("build_cmd", self.build_cmd),
            ("test_cmd", self.test_cmd),
        ):
            if not command:
                raise ValueError(
                    f"{label} is empty; choose --profile or configure a real command"
                )
            executable = Path(command[0]).name.lower()
            if executable in ("true", ":"):
                raise ValueError(
                    f"{label} is a no-op ({command[0]!r}); real validation is required"
                )
        if not shutil.which(self.lizard_binary):
            raise ValueError(f"lizard binary not found: {self.lizard_binary}")
        if self.lizard_language.lower() == "go" and not shutil.which(
            self.gocognit_binary
        ):
            raise ValueError(
                f"gocognit binary not found: {self.gocognit_binary}"
            )
        if float(self.weights.get("duplicates", 1)) > 0:
            if self.lizard_language.lower() == "go":
                duplicate_binary = self.dupl_binary
                duplicate_name = "dupl_binary"
                if self.dupl_threshold_tokens <= 0:
                    raise ValueError("dupl_threshold_tokens must be positive")
            else:
                duplicate_binary = self.duplo_binary
                duplicate_name = "duplo_binary"
                if self.duplo_min_block_lines <= 0:
                    raise ValueError("duplo_min_block_lines must be positive")
            if not duplicate_binary or not shutil.which(duplicate_binary):
                raise ValueError(
                    f"duplicates weight is enabled but {duplicate_name} is unavailable"
                )

    @property
    def effective_api_base_url(self) -> str:
        if self.api_base_url:
            return self.api_base_url
        if self.api_provider == "deepseek":
            return DEEPSEEK_ANTHROPIC_BASE_URL
        return ""

    @property
    def effective_api_key_env(self) -> str:
        if self.api_key_env:
            return self.api_key_env
        return (
            "DEEPSEEK_API_KEY"
            if self.api_provider == "deepseek"
            else "ANTHROPIC_API_KEY"
        )

    @property
    def target_path(self) -> Path:
        return self.repo_root / self.target_subdir

    @property
    def run_results_path(self) -> Path:
        """Timestamped directory holding this run's artefacts."""
        base = self.work_root / self.results_dir
        return base / self.run_id if self.run_id else base

    @property
    def agent_log_dir(self) -> Path:
        """Agent logs live inside the run's results directory.

        Thesis §4.4: each run's results directory holds "the penalty
        history over time, the baseline and final quality metrics values,
        and the full agent logs". Keeping them here also stops a second
        run from overwriting the first run's logs.
        """
        return self.run_results_path / self.log_dir

    @property
    def state_path(self) -> Path:
        """Crash-recovery state file scoped to this run."""
        return self.run_results_path / self.state_filename

    @property
    def backlog_file_path(self) -> Path:
        """Run-scoped backlog; independent runs must never share issue state."""
        return self.run_results_path / self.backlog_path

    @property
    def agent_work_root(self) -> Path:
        """Run-scoped directory containing analyst/programmer worktrees."""
        name = self.run_id if self.run_id else "default"
        return self.work_root / "worktrees" / name

    @property
    def gate_config_dir(self) -> Path:
        """Gate configs live outside target git worktrees.

        Keeping them under the results directory prevents `git clean -fd`
        from deleting them and prevents `git status` from treating them as
        uncommitted target-repository content.
        """
        return self.run_results_path / "gate_configs"

    @property
    def gate_status_dir(self) -> Path:
        """Run-scoped active-gate markers used by timeout supervision."""
        return self.run_results_path / "gate_status"

    @property
    def baseline_build_log_path(self) -> Path:
        return self.run_results_path / "baseline-build.log"

    @property
    def baseline_build_output_base_path(self) -> Path:
        """Ephemeral Bazel state used only to make cache warming complete."""
        return self.run_results_path / "baseline-bazel-output"

    @property
    def dynamic_baseline_state_path(self) -> Path:
        """Run-scoped pointer to the currently accepted dynamic measurement."""
        return self.run_results_path / "dynamic_baseline.json"

    @property
    def dynamic_initial_baseline_artifact_path(self) -> Path:
        """Artifacts collected once before the first programmer dispatch."""
        return self.run_results_path / "benchmarks" / "initial_baseline"

    @property
    def issue_history_dir(self) -> Path:
        """Run-scoped per-issue attempt records and pre-revert patches."""
        return self.run_results_path / "issues"

    @property
    def agent_cli_extra_args(self) -> list[str]:
        args = [
            "--no-session-persistence",
            "--allowedTools",
            ",".join(self.agent_allowed_tools),
        ]
        if self.agent_bare_mode:
            args.insert(0, "--bare")
        return args

    @property
    def integration_branch(self) -> str:
        """Branch the merge gate fast-forwards into for this run."""
        return f"{self.run_branch_prefix}/{self.run_id}" if self.run_id else self.main_branch
