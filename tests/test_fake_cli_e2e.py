"""Full multi-agent E2E using a temporary FerretDB clone and a fake CLI.

This is deliberately separate from run_all.py because it needs the local
FerretDB checkout and Go toolchain. It exercises Coordinator.run(), all six
agent slots, Claude stream-json parsing, run-scoped state, three concurrent
programmer worktrees, the real merge gate, Lizard, Duplo, Go build/tests,
fast-forward integration, artefact generation, and baseline restoration.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
WORKSPACE = EXP.parent.parent
sys.path.insert(0, str(EXP))

from agents.orchestrator import AssignmentDecision, StuckDecision  # noqa: E402
from config import Config, SUPPORTED_API_PROVIDERS                  # noqa: E402
from coordination.coordinator import Coordinator                    # noqa: E402


FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(
        f"  {'PASS' if condition else 'FAIL'}  {label}"
        + (f"  [{detail}]" if detail else "")
    )
    if not condition:
        FAILURES.append(label)


def run(cmd: list[str], cwd: Path, check_rc: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), check=check_rc, text=True,
        capture_output=True,
    )


def find_gocognit() -> Path | None:
    candidate = EXP / ".venv" / "bin" / "gocognit"
    resolved = shutil.which("gocognit")
    if candidate.is_file():
        return candidate.resolve()
    return Path(resolved).resolve() if resolved else None


def bad_source(index: int) -> str:
    names = {1: "QualityOne", 2: "QualityTwo", 3: "QualityThree"}
    conditions = "\n".join(
        f"\tif v > {value} {{\n\t\tresult++\n\t}}"
        for value in range(1, 21)
    )
    return (
        "package e2efixture\n\n"
        f"func {names[index]}(v int) int {{\n"
        f"\tresult := 0\n{conditions}\n\treturn result\n}}\n"
    )


TEST_SOURCE = """package e2efixture

import "testing"

func expected(v int) int {
    result := 0
    for i := 1; i <= 20; i++ {
        if v > i {
            result++
        }
    }
    return result
}

func TestQualityFunctions(t *testing.T) {
    functions := []func(int) int{QualityOne, QualityTwo, QualityThree}
    for _, fn := range functions {
        for _, value := range []int{-1, 1, 7, 20, 21, 50} {
            if got, want := fn(value), expected(value); got != want {
                t.Fatalf("value %d: got %d, want %d", value, got, want)
            }
        }
    }
}
"""


class DeterministicOrchestrator:
    """Assign one controlled file to each analyst/programmer."""

    def assign(
        self, current_penalty, baseline_penalty, backlog,
        idle_programmers, idle_analysts, stagnation, metric_breakdown="",
    ) -> AssignmentDecision:
        todo = [
            item for item in backlog.items.values() if item.status == "TODO"
        ]
        if todo and idle_programmers:
            assignments: dict[str, list[str]] = {}
            for pid, item in zip(sorted(idle_programmers), sorted(
                todo, key=lambda value: value.file_path,
            )):
                assignments[pid] = [item.id]
            return AssignmentDecision(programmer_assignments=assignments)

        if idle_analysts:
            analysts = sorted(idle_analysts)
            return AssignmentDecision(
                dispatch_analysts=analysts,
                analyst_targets={
                    aid: f"quality_{index}.go"
                    for index, aid in enumerate(analysts, 1)
                    if index <= 3
                },
            )
        return AssignmentDecision()

    def evaluate_stuck(self, **kwargs) -> StuckDecision:
        return StuckDecision(keep=[
            item["programmer_id"] for item in kwargs.get("stuck", [])
        ])


def prepare_clone(source: Path, root: Path) -> tuple[Path, str]:
    repo = root / "FerretDB"
    run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(repo)], root)
    run(["git", "config", "user.email", "fake-e2e@example.com"], repo)
    run(["git", "config", "user.name", "Fake CLI E2E"], repo)

    fixture = repo / "e2e_fixture"
    fixture.mkdir()
    for index in range(1, 4):
        (fixture / f"quality_{index}.go").write_text(bad_source(index))
    (fixture / "quality_test.go").write_text(TEST_SOURCE)
    run(["gofmt", "-w", str(fixture)], repo)
    run(["git", "add", "e2e_fixture"], repo)
    run(["git", "commit", "-m", "add controlled multi-agent e2e fixture"], repo)
    baseline = run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    return repo, baseline


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo", type=Path,
        default=WORKSPACE / "ferret-dev" / "FerretDB",
        help="Source FerretDB checkout; a temporary clone is used",
    )
    parser.add_argument(
        "--provider", choices=SUPPORTED_API_PROVIDERS, default="anthropic",
        help="Exercise provider-specific CLI environment mapping",
    )
    args = parser.parse_args()
    source = args.repo.expanduser().resolve()
    if not (source / ".git").exists():
        raise SystemExit(f"FerretDB git checkout not found: {source}")
    if shutil.which("go") is None:
        raise SystemExit("Go toolchain not found")

    fake_cli = EXP / "tests" / "fake_claude_cli.py"
    lizard = Path(sys.executable).parent / "lizard"
    gocognit = find_gocognit()
    duplo = (EXP / "bin" / "duplo").resolve()
    if not lizard.exists() or not duplo.exists() or gocognit is None:
        raise SystemExit(
            "E2E requires the experiment venv's lizard/gocognit and bin/duplo"
        )

    with tempfile.TemporaryDirectory(prefix="ferretdb-fake-cli-e2e-") as td:
        root = Path(td)
        os.environ["GOCACHE"] = str(root / "go-cache")
        os.environ["MPLCONFIGDIR"] = str(root / "matplotlib-cache")
        if args.provider == "deepseek":
            # Fake CLI only: validate credential transport without contacting
            # DeepSeek or placing the sentinel in argv/run artefacts.
            os.environ["DEEPSEEK_API_KEY"] = "fake-e2e-deepseek-key"
        repo, baseline = prepare_clone(source, root)
        work = root / "agent-work"
        cfg = Config(
            repo_root=repo,
            target_subdir="e2e_fixture",
            work_root=work,
            run_id="ferretdb_fake_cli_e2e",
            baseline_ref=baseline,
            claude_cli=str(fake_cli),
            api_provider=args.provider,
            lizard_binary=str(lizard.resolve()),
            gocognit_binary=str(gocognit),
            lizard_language="go",
            duplo_binary=str(duplo),
            build_cmd=["go", "test", "-run", "^$", "./e2e_fixture"],
            test_cmd=["go", "test", "./e2e_fixture"],
            backlog_drain_interval_sec=0.05,
            issue_timeout_sec=60,
            programmer_timeout_sec=120,
            stuck_eval_interval_sec=30,
            empty_scan_limit=3,
        )
        # gocognit measures the Go cognitive metric on every baseline/gate
        # pass. Duplo is also executed, but is excluded from this
        # controlled objective so helper extraction cannot be rejected merely
        # because the three intentionally parallel fixtures resemble each other.
        cfg.weights["duplicates"] = 0

        print("\n[1] run the complete coordinator with 3 analysts + 3 programmers")
        coordinator = Coordinator(cfg, orchestrator=DeterministicOrchestrator())
        coordinator.run()

        summary_path = cfg.run_results_path / cfg.run_summary_filename
        summary = json.loads(summary_path.read_text())
        attempts_path = cfg.run_results_path / cfg.gate_attempts_filename
        attempts = [
            json.loads(line) for line in attempts_path.read_text().splitlines()
            if line.strip()
        ]

        print("\n[2] verify coordination, gate, tests, and experiment artefacts")
        check("all three programmer refactorings merged",
              summary["merges"] == 3, str(summary["merges"]))
        check("all three backlog issues are DONE",
              summary["backlog"]["done"] == 3
              and summary["backlog"]["todo"] == 0
              and summary["backlog"]["in_progress"] == 0,
              json.dumps(summary["backlog"], sort_keys=True))
        check("the run terminates after an empty verification scan",
              summary["stop_reason"] == "no_actionable_work",
              summary["stop_reason"])
        check("gate independently recorded three successful merges",
              summary["failures"]["merged"] == 3
              and len([a for a in attempts if a["outcome"] == "merged"]) == 3)
        check("no agent or static-analysis failure occurred",
              summary["failures"]["agent_crashes"] == 0
              and summary["failures"]["analysis_failures"] == 0)
        check("real Go build and test commands are recorded",
              summary["config"]["build_cmd"][:2] == ["go", "test"]
              and summary["config"]["test_cmd"] == ["go", "test", "./e2e_fixture"])
        check("penalty decreased",
              summary["final_penalty"] < summary["baseline_penalty"],
              f"{summary['baseline_penalty']:.2f} -> {summary['final_penalty']:.2f}")
        check("all expected run artefacts exist",
              all((cfg.run_results_path / name).exists() for name in (
                  cfg.penalty_history_filename,
                  cfg.penalty_plot_filename,
                  cfg.run_summary_filename,
                  cfg.gate_attempts_filename,
                  cfg.state_filename,
                  cfg.token_usage_filename,
              )))
        check("each gate record is tied to an issue",
              {a["issue_id"] for a in attempts if a["outcome"] == "merged"}
              == {"ISSUE-0001", "ISSUE-0002", "ISSUE-0003"})
        check("gate configs survived outside cleaned worktrees",
              len(list(cfg.gate_config_dir.glob("PROG_*.json"))) == 3
              and not any(path.name == ".gate_config.json"
                          for path in repo.rglob(".gate_config.json")))
        all_logs = "".join(
            path.read_text(errors="replace")
            for path in cfg.agent_log_dir.glob("*.log")
        )
        check("provider credential never appears in agent logs",
              "fake-e2e-deepseek-key" not in all_logs)
        check("provider recorded without credential value",
              summary["config"]["api_provider"] == args.provider
              and "fake-e2e-deepseek-key" not in summary_path.read_text())
        token_summary = summary["tokens"]
        check("all nine fake CLI sessions contribute token usage",
              token_summary["coverage"]["agent_logs"] == 9
              and token_summary["coverage"]["agent_logs_with_usage"] == 9)
        check("fake token totals are exact and cache-aware",
              token_summary["totals"]["input_tokens"] == 16050
              and token_summary["totals"]["output_tokens"] == 1200
              and token_summary["totals"]["total_tokens"] == 17250,
              json.dumps(token_summary["totals"], sort_keys=True))
        check("local leads were reviewed without changing the ISSUE protocol",
              len(coordinator.analyst_lead_seen_keys) == 3,
              str(len(coordinator.analyst_lead_seen_keys)))

        print("\n[3] verify branch isolation and behavior preservation")
        run_branch = cfg.integration_branch
        branch_source = run(
            ["git", "show", f"{run_branch}:e2e_fixture/quality_1.go"], repo,
        ).stdout
        baseline_source = (repo / "e2e_fixture" / "quality_1.go").read_text()
        check("run branch contains the extracted helper functions",
              "qualityOneBand0" in branch_source)
        check("primary working tree was restored to the baseline",
              "qualityOneBand0" not in baseline_source
              and run(["git", "rev-parse", "HEAD"], repo).stdout.strip() == baseline)
        check("primary branch remains clean",
              run(["git", "status", "--porcelain"], repo).stdout.strip() == "")

        run(["git", "checkout", run_branch], repo)
        behavior = run(["go", "test", "./e2e_fixture"], repo, check_rc=False)
        check("refactored run branch passes its real behavior test",
              behavior.returncode == 0, behavior.stderr.strip())
        run(["git", "checkout", "main"], repo)

        print(f"\nE2E artefacts were generated under temporary path: {cfg.run_results_path}")

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("FULL FERRETDB FAKE-CLI E2E PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
