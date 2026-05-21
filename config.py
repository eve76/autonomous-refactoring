"""Central configuration for the multi-agent system."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    repo_root: Path
    target_subdir: str
    work_root: Path

    num_analysts: int = 3
    num_programmers: int = 3

    claude_cli: str = "claude"
    orchestrator_model: str = "claude-opus-4-7"
    agent_model: str = "claude-opus-4-7"

    # Stagnation: counter increments when a merge yields < min_merge_gain
    # in penalty reduction, or when a programmer is killed for timeout.
    # System stops once counter reaches stagnation_limit without an
    # intervening merge above min_merge_gain.
    min_merge_gain: float = 10.0
    stagnation_limit: int = 3

    programmer_timeout_sec: int = 30 * 60

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

    # Per-metric weights (gnomad-kiro parity). Setting any to 0 disables
    # that metric's contribution to the penalty totals.
    weights: dict = field(default_factory=lambda: {
        "ccn": 1,
        "nloc": 1,
        "cognitive": 1,
        "param": 1,
        "duplicates": 1,
    })

    # Duplo binary used by the merge gate and the coordinator's
    # baseline measurement. Empty string -> duplicates not measured.
    duplo_binary: str = ""
    # gnomad-kiro hardcodes -ml 10
    duplo_min_block_lines: int = 10

    # Lizard CLI binary. gnomad-kiro shells out to lizard rather than
    # using the Python API; we follow suit but leave the path on PATH
    # by default so this is portable across machines.
    lizard_binary: str = "lizard"
    # Target language for Lizard / file-suffix filter. gnomad-kiro is
    # C/C++ only; setting "go" (or anything non-cpp) makes the pipeline
    # walk Go files and skip the cognitive-complexity tool (which only
    # supports C/C++).
    lizard_language: str = "cpp"

    log_dir: Path = field(default_factory=lambda: Path("logs"))
    backlog_path: Path = field(default_factory=lambda: Path("backlog.json"))
    # Failed gate runs save a .patch here instead of resetting the
    # worktree (gnomad-kiro parity).
    reverted_dir: Path = field(default_factory=lambda: Path("reverted_patches"))

    # Build / test commands invoked by the merge gate inside each
    # worktree. Default to no-op so the skeleton runs end-to-end on
    # any repo; override for real targets.
    build_cmd: list[str] = field(default_factory=lambda: ["true"])
    test_cmd: list[str] = field(default_factory=lambda: ["true"])

    main_branch: str = "main"
    gate_config_filename: str = ".gate_config.json"

    @property
    def target_path(self) -> Path:
        return self.repo_root / self.target_subdir
