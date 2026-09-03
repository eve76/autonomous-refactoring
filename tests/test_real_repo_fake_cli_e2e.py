#!/usr/bin/env python3
"""Run one complete fake-CLI refactoring against MongoDB and FerretDB.

Each source checkout is treated as read-only.  The test clones its exact HEAD
into a temporary directory, creates a controlled baseline branch, runs the
normal Coordinator with one Analyst and one Programmer, invokes a small set
of repository-native tests in the merge gate, and verifies branch isolation
and experiment artefacts.

This test is intentionally not part of ``run_all.py`` because it depends on
the two large local repositories and, for FerretDB, a Go toolchain.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
WORKSPACE = EXP.parent.parent
sys.path.insert(0, str(EXP))

from agents.orchestrator import AssignmentDecision, StuckDecision  # noqa: E402
from config import Config                                           # noqa: E402
from coordination.coordinator import Coordinator                    # noqa: E402


FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(
        f"  {'PASS' if condition else 'FAIL'}  {label}"
        + (f"  [{detail}]" if detail else "")
    )
    if not condition:
        FAILURES.append(label)


def run(
    cmd: list[str], cwd: Path, check_rc: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), check=check_rc, text=True,
        capture_output=True, env=env,
    )


def find_gocognit() -> str:
    candidate = EXP / ".venv" / "bin" / "gocognit"
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which("gocognit")
    return str(Path(resolved).resolve()) if resolved else ""


@dataclass(frozen=True)
class Scenario:
    name: str
    source: Path
    language: str
    metric: str
    metric_threshold: int
    target_subdir: str
    issue_file: str
    repo_file: str
    helper_marker: str
    build_cmd: list[str]
    test_cmd: list[str]


class OneIssueOrchestrator:
    """Route the controlled issue through one Analyst and one Programmer."""

    def __init__(self, issue_file: str):
        self.issue_file = issue_file

    def assign(
        self, current_penalty, baseline_penalty, backlog,
        idle_programmers, idle_analysts, stagnation, metric_breakdown="",
    ) -> AssignmentDecision:
        todo = sorted(
            (
                item for item in backlog.items.values()
                if item.status == "TODO"
            ),
            key=lambda item: item.id,
        )
        if todo and idle_programmers:
            return AssignmentDecision(programmer_assignments={
                sorted(idle_programmers)[0]: [todo[0].id],
            })
        if idle_analysts:
            aid = sorted(idle_analysts)[0]
            return AssignmentDecision(
                dispatch_analysts=[aid],
                analyst_targets={aid: self.issue_file},
            )
        return AssignmentDecision()

    def evaluate_stuck(self, **kwargs) -> StuckDecision:
        return StuckDecision(keep=[
            item["programmer_id"] for item in kwargs.get("stuck", [])
        ])


def source_snapshot(source: Path) -> tuple[str, str]:
    return (
        run(["git", "rev-parse", "HEAD"], source).stdout.strip(),
        run(["git", "status", "--porcelain"], source).stdout,
    )


def prepare_clone(source: Path, root: Path, name: str) -> tuple[Path, str]:
    source_head = run(["git", "rev-parse", "HEAD"], source).stdout.strip()
    repo = root / name
    run(
        # The MongoDB checkout is several gigabytes. A shared local clone is
        # safe here because Git objects are immutable and the source checkout
        # is read-only for the duration of this short-lived test.
        ["git", "clone", "--quiet", "--shared", str(source), str(repo)],
        root,
    )
    run(["git", "config", "user.email", "fake-repo-e2e@example.com"], repo)
    run(["git", "config", "user.name", "Fake Repo E2E"], repo)
    run(["git", "checkout", "-B", "e2e-baseline", source_head], repo)
    # Keep the user's checkout structurally read-only. The shared clone still
    # reads immutable objects through its alternates file, but no remote
    # remains that a future configuration change could accidentally push to.
    run(["git", "remote", "remove", "origin"], repo)
    return repo, source_head


def scenario_config(scenario: Scenario, repo: Path, root: Path, baseline: str) -> Config:
    build_cmd = [
        part.replace("{RUN_ROOT}", str(root)) for part in scenario.build_cmd
    ]
    test_cmd = [
        part.replace("{RUN_ROOT}", str(root)) for part in scenario.test_cmd
    ]
    cfg = Config(
        repo_root=repo,
        target_subdir=scenario.target_subdir,
        work_root=root / "agent-work",
        sparse_worktrees=False,
        run_id=f"{scenario.name}_fake_repo_e2e",
        baseline_ref=baseline,
        main_branch="e2e-baseline",
        run_branch_prefix="e2e-refactor",
        push_run_branch=False,
        num_analysts=1,
        num_programmers=1,
        claude_cli=str((EXP / "tests" / "fake_repo_cli.py").resolve()),
        lizard_binary=str((Path(sys.executable).parent / "lizard").resolve()),
        gocognit_binary=find_gocognit(),
        lizard_language=scenario.language,
        duplo_binary="",
        build_cmd=build_cmd,
        test_cmd=test_cmd,
        min_merge_gain=10.0,
        stagnation_limit=3,
        backlog_drain_interval_sec=0.05,
        issue_timeout_sec=90,
        programmer_timeout_sec=30 * 60,
        stuck_eval_interval_sec=60,
        empty_scan_limit=1,
    )
    cfg.thresholds.update({
        "ccn": 1_000_000,
        "cognitive": 1_000_000,
        "nloc": 1_000_000,
        "param": 1_000_000,
    })
    cfg.weights.update({
        "ccn": 0,
        "cognitive": 0,
        "nloc": 0,
        "param": 0,
        "duplicates": 0,
    })
    cfg.thresholds[scenario.metric] = scenario.metric_threshold
    cfg.weights[scenario.metric] = 1
    return cfg


def execute_scenario(scenario: Scenario, outer: Path) -> None:
    print(f"\n{'=' * 78}\nSCENARIO: {scenario.name}\n{'=' * 78}")
    before_source = source_snapshot(scenario.source)
    root = outer / scenario.name
    root.mkdir()
    repo, baseline = prepare_clone(scenario.source, root, scenario.name)
    cfg = scenario_config(scenario, repo, root, baseline)

    previous = os.environ.get("FAKE_REPO_SCENARIO")
    os.environ["FAKE_REPO_SCENARIO"] = scenario.name
    try:
        coordinator = Coordinator(
            cfg, orchestrator=OneIssueOrchestrator(scenario.issue_file),
        )
        coordinator.run()
    finally:
        if previous is None:
            os.environ.pop("FAKE_REPO_SCENARIO", None)
        else:
            os.environ["FAKE_REPO_SCENARIO"] = previous

    summary_path = cfg.run_results_path / cfg.run_summary_filename
    summary = json.loads(summary_path.read_text())
    attempts = [
        json.loads(line)
        for line in (
            cfg.run_results_path / cfg.gate_attempts_filename
        ).read_text().splitlines()
        if line.strip()
    ]

    print("\n[verification] coordinator, merge gate, native tests, and artefacts")
    check(f"{scenario.name}: exactly one merge",
          summary["merges"] == 1, str(summary["merges"]))
    check(f"{scenario.name}: issue completed",
          summary["backlog"]["done"] == 1
          and summary["backlog"]["todo"] == 0
          and summary["backlog"]["in_progress"] == 0,
          json.dumps(summary["backlog"], sort_keys=True))
    check(f"{scenario.name}: penalty decreased",
          summary["final_penalty"] < summary["baseline_penalty"],
          f"{summary['baseline_penalty']:.4f} -> {summary['final_penalty']:.4f}")
    check(f"{scenario.name}: gate accepted after build and test",
          len(attempts) == 1
          and attempts[0]["outcome"] == "merged"
          and attempts[0]["success"] is True,
          json.dumps(attempts, sort_keys=True))
    check(f"{scenario.name}: native commands recorded",
          summary["config"]["build_cmd"] == cfg.build_cmd
          and summary["config"]["test_cmd"] == cfg.test_cmd)
    check(f"{scenario.name}: no gate/agent failure",
          summary["failures"]["test_failures"] == 0
          and summary["failures"]["build_failures"] == 0
          and summary["failures"]["analysis_failures"] == 0
          and summary["failures"]["agent_crashes"] == 0)
    check(f"{scenario.name}: run branch was not pushed",
          summary["run_branch_pushed"] is False)
    check(f"{scenario.name}: verification scan terminates naturally",
          summary["stop_reason"] == "no_actionable_work", summary["stop_reason"])
    coverage = summary["tokens"]["coverage"]
    check(f"{scenario.name}: fake sessions all accounted",
          coverage["agent_logs"] >= 3
          and coverage["agent_logs_with_usage"] == coverage["agent_logs"],
          json.dumps(coverage, sort_keys=True))
    check(f"{scenario.name}: required artefacts exist",
          all((cfg.run_results_path / name).exists() for name in (
              cfg.penalty_history_filename,
              cfg.penalty_plot_filename,
              cfg.run_summary_filename,
              cfg.gate_attempts_filename,
              cfg.state_filename,
              cfg.token_usage_filename,
          )))

    run_branch = cfg.integration_branch
    branch_source = run(
        ["git", "show", f"{run_branch}:{scenario.repo_file}"], repo,
    ).stdout
    check(f"{scenario.name}: run branch contains helper extraction",
          scenario.helper_marker in branch_source)
    changed_files = run(
        ["git", "diff", "--name-only", f"{baseline}..{run_branch}"], repo,
    ).stdout.splitlines()
    check(f"{scenario.name}: refactoring stayed inside target scope",
          changed_files == [scenario.repo_file]
          and scenario.repo_file.startswith(
              scenario.target_subdir.rstrip("/") + "/"
          ),
          json.dumps(changed_files))
    check(f"{scenario.name}: baseline branch restored",
          run(["git", "branch", "--show-current"], repo).stdout.strip()
          == "e2e-baseline"
          and run(["git", "rev-parse", "HEAD"], repo).stdout.strip() == baseline
          and run(["git", "status", "--porcelain"], repo).stdout.strip() == "")

    run(["git", "checkout", run_branch], repo)
    native = run(cfg.test_cmd, repo, check_rc=False, env=os.environ.copy())
    check(f"{scenario.name}: native test passes on integrated branch",
          native.returncode == 0,
          (native.stdout + native.stderr)[-1000:].strip())
    run(["git", "checkout", "e2e-baseline"], repo)

    after_source = source_snapshot(scenario.source)
    check(f"{scenario.name}: source checkout was not modified",
          after_source == before_source)


def shutdown_bazel(scenario: Scenario, root: Path) -> None:
    """Best-effort cleanup for the scenario-specific Bazel server."""
    if scenario.language != "cpp":
        return
    build_cmd = [
        part.replace("{RUN_ROOT}", str(root)) for part in scenario.build_cmd
    ]
    # Startup options precede the Bazel command in the scenario definition.
    command_index = build_cmd.index("build")
    run(build_cmd[:command_index] + ["shutdown"], scenario.source, check_rc=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mongo-repo", type=Path, default=WORKSPACE / "dev" / "mongo",
    )
    parser.add_argument(
        "--ferret-repo", type=Path,
        default=WORKSPACE / "ferret-dev" / "FerretDB",
    )
    parser.add_argument(
        "--scenario",
        choices=("both", "mongo_query_multikey", "ferret_telemetry"),
        default="both",
    )
    args = parser.parse_args()

    mongo = args.mongo_repo.expanduser().resolve()
    ferret = args.ferret_repo.expanduser().resolve()
    selected: list[Scenario] = []
    if args.scenario in ("both", "mongo_query_multikey"):
        bazel = str(Path(shutil.which("bazel") or "").resolve())
        selected.append(Scenario(
            name="mongo_query_multikey",
            source=mongo,
            language="cpp",
            metric="ccn",
            metric_threshold=15,
            target_subdir="src/mongo/db/query/bson",
            issue_file="multikey_dotted_path_support.cpp",
            repo_file="src/mongo/db/query/bson/multikey_dotted_path_support.cpp",
            helper_marker="bool _isNumericPathComponent",
            build_cmd=[
                bazel,
                "--output_user_root={RUN_ROOT}/bazel-user-root",
                "--output_base={RUN_ROOT}/bazel-output",
                "build", "--config=local",
                "//src/mongo/db/query/bson:multikey_dotted_path_support",
            ],
            test_cmd=[
                bazel,
                "--output_user_root={RUN_ROOT}/bazel-user-root",
                "--output_base={RUN_ROOT}/bazel-output",
                "test", "--config=local", "--test_output=errors",
                "//src/mongo/db/query/bson:multikey_db_bson_test",
                "--test_arg=--suite=ExtractAllElementsAlongPath",
            ],
        ))
    if args.scenario in ("both", "ferret_telemetry"):
        if not find_gocognit() or not Path(find_gocognit()).is_file():
            raise SystemExit("gocognit is required for the FerretDB scenario")
        selected.append(Scenario(
            name="ferret_telemetry",
            source=ferret,
            language="go",
            metric="nloc",
            metric_threshold=30,
            target_subdir="internal/util/telemetry",
            issue_file="telemetry.go",
            repo_file="internal/util/telemetry/telemetry.go",
            helper_marker="func configuredState",
            build_cmd=[
                "go", "test", "-run", "^$", "./internal/util/telemetry",
            ],
            test_cmd=[
                "go", "test", "-count=1", "-tags=ferretdb_dev",
                "-run", "^TestState$", "./internal/util/telemetry",
            ],
        ))

    for scenario in selected:
        if not (scenario.source / ".git").exists():
            raise SystemExit(f"git checkout not found: {scenario.source}")
    if any(item.language == "go" for item in selected) and shutil.which("go") is None:
        raise SystemExit("Go toolchain not found")
    if any(item.language == "cpp" for item in selected) and shutil.which("bazel") is None:
        raise SystemExit("Bazel/Bazelisk not found")
    lizard = Path(sys.executable).parent / "lizard"
    if not lizard.exists():
        raise SystemExit("run with the experiment virtualenv (lizard missing)")

    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="real-repo-fake-cli-e2e-") as td:
        root = Path(td)
        os.environ["GOCACHE"] = str(root / "go-cache")
        os.environ["MPLCONFIGDIR"] = str(root / "matplotlib-cache")
        os.environ["TEST_TMPDIR"] = str(root / "bazel-tmp")
        Path(os.environ["TEST_TMPDIR"]).mkdir()
        for scenario in selected:
            try:
                execute_scenario(scenario, root)
            finally:
                shutdown_bazel(scenario, root)

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("REAL-REPOSITORY FAKE-CLI E2E PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
