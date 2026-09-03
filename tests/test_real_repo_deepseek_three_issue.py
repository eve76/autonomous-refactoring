#!/usr/bin/env python3
"""Paid unattended DeepSeek validation over three fixed real-repo issues.

One command runs one MongoDB query/bson issue and two sequential FerretDB
telemetry issues. Routing is deterministic only to bound paid scope; the
Analyst and Programmer sessions, edits, merge gate, builds, tests, penalty
measurement, retry behavior, and result artifacts are the production paths.
"""

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
WORKSPACE = EXP.parent.parent
sys.path.insert(0, str(EXP))

from agents.orchestrator import (  # noqa: E402
    AssignmentDecision,
    Orchestrator,
    StuckDecision,
)
from coordination import message_queue as mq  # noqa: E402
from coordination.backlog import IN_PROGRESS, SKIPPED, TODO  # noqa: E402
from coordination.coordinator import Coordinator  # noqa: E402
from main import load_project_dotenv  # noqa: E402
from tests.test_real_repo_deepseek_smoke import (  # noqa: E402
    LiveScenario,
    configure,
    run,
)
from tests.test_real_repo_fake_cli_e2e import (  # noqa: E402
    prepare_clone,
    source_snapshot,
)


@dataclass(frozen=True)
class ExpectedIssue:
    repo_file: str
    symbol: str
    line: int


@dataclass(frozen=True)
class MultiIssueScenario:
    name: str
    source: Path
    target_subdir: str
    language: str
    metric: str
    threshold: int
    native_name: str
    issues: tuple[ExpectedIssue, ...]


class ThreeIssueOrchestrator:
    """Bound scope while retaining automatic retries and sequential routing."""

    def __init__(
        self,
        issues: tuple[ExpectedIssue, ...],
        *,
        max_analyst_attempts: int = 2,
        max_programmer_dispatches_per_issue: int = 2,
    ):
        self.issues = issues
        self.max_analyst_attempts = max_analyst_attempts
        self.max_programmer_dispatches_per_issue = (
            max_programmer_dispatches_per_issue
        )
        self.analyst_attempts: dict[int, int] = {}
        self.programmer_attempts: dict[str, int] = {}

    def assign(
        self, current_penalty, baseline_penalty, backlog,
        idle_programmers, idle_analysts, stagnation, metric_breakdown="",
    ) -> AssignmentDecision:
        todo = sorted(
            (
                item for item in backlog.items.values()
                if item.status == TODO
            ),
            key=lambda item: item.id,
        )
        if todo and idle_programmers:
            issue = todo[0]
            attempts = self.programmer_attempts.get(issue.id, 0)
            if attempts < self.max_programmer_dispatches_per_issue:
                self.programmer_attempts[issue.id] = attempts + 1
                return AssignmentDecision(programmer_assignments={
                    sorted(idle_programmers)[0]: [issue.id],
                })

        active = any(
            item.status in (TODO, IN_PROGRESS)
            for item in backlog.items.values()
        )
        accepted = len(backlog.items)
        if (
            not active
            and accepted < len(self.issues)
            and idle_analysts
        ):
            attempts = self.analyst_attempts.get(accepted, 0)
            if attempts < self.max_analyst_attempts:
                self.analyst_attempts[accepted] = attempts + 1
                analyst = sorted(idle_analysts)[0]
                return AssignmentDecision(
                    dispatch_analysts=[analyst],
                    analyst_targets={
                        analyst: self.issues[accepted].symbol,
                    },
                )
        return AssignmentDecision()

    def evaluate_stuck(self, **kwargs) -> StuckDecision:
        return StuckDecision(keep=[
            item["programmer_id"] for item in kwargs.get("stuck", [])
        ])


class ThreeIssueCoordinator(Coordinator):
    """Accept the three expected findings and reject scope expansion."""

    def __init__(
        self,
        cfg,
        orchestrator: ThreeIssueOrchestrator,
        issues: tuple[ExpectedIssue, ...],
    ):
        super().__init__(cfg, orchestrator=orchestrator)
        self._expected_issues = issues
        self._accepted_issue_count = 0

    def _apply_message(self, msg):
        final_issue_accepted = False
        if msg.kind == mq.ADD_ISSUES:
            selected = []
            if self._accepted_issue_count < len(self._expected_issues):
                expected = self._expected_issues[self._accepted_issue_count]
                for raw in msg.payload.get("issues", []):
                    resolved = self._resolve_issue_repo_path(
                        raw.get("file_path", "")
                    )
                    message = str(raw.get("message", "")).lower()
                    if (
                        resolved == expected.repo_file
                        and (
                            expected.symbol.lower() in message
                            or int(raw.get("line", 0)) == expected.line
                        )
                    ):
                        selected = [raw]
                        self._accepted_issue_count += 1
                        if (
                            self._accepted_issue_count
                            == len(self._expected_issues)
                        ):
                            final_issue_accepted = True
                            self.analyst_lead_seen_keys.update(
                                lead.key
                                for lead in self.local_candidate_leads
                            )
                        break
            msg = mq.Message(
                sender=msg.sender,
                kind=msg.kind,
                payload={**msg.payload, "issues": selected},
            )
        result = super()._apply_message(msg)
        if final_issue_accepted:
            self.empty_analyst_scans = self.cfg.empty_scan_limit
        return result

    def _apply_merge_result(self, msg):
        super()._apply_merge_result(msg)
        # The generic coordinator reopens discovery after every merge because
        # a normal run may contain more unknown issues. This bounded harness
        # knows the complete expected set: once its final finding has been
        # accepted, keep discovery exhausted after the final merge instead of
        # spending three no-op assignment cycles before stopping.
        if self._accepted_issue_count >= len(self._expected_issues):
            self.empty_analyst_scans = self.cfg.empty_scan_limit


def _live_scenario(scenario: MultiIssueScenario) -> LiveScenario:
    first = scenario.issues[0]
    return LiveScenario(
        name=scenario.name,
        source=scenario.source,
        target_subdir=scenario.target_subdir,
        issue_file=Path(first.repo_file).name,
        repo_file=first.repo_file,
        language=scenario.language,
        metric=scenario.metric,
        threshold=scenario.threshold,
        native_name=scenario.native_name,
        issue_symbol=first.symbol,
    )


def execute(
    scenario: MultiIssueScenario,
    outer: Path,
    evidence_base: Path,
    *,
    preflight: bool,
) -> tuple[bool, dict]:
    print(
        f"\n{'=' * 78}\n"
        f"UNATTENDED DEEPSEEK: {scenario.name} "
        f"({len(scenario.issues)} issue(s))\n"
        f"{'=' * 78}",
        flush=True,
    )
    source_before = source_snapshot(scenario.source)
    scenario_temp = outer / scenario.name
    scenario_temp.mkdir()
    repo, baseline = prepare_clone(
        scenario.source, scenario_temp, scenario.name,
    )
    evidence_root = evidence_base / scenario.name
    evidence_root.mkdir(parents=True)
    cfg, counts_path = configure(
        _live_scenario(scenario),
        repo,
        baseline,
        scenario_temp,
        evidence_root,
    )
    # One empty Analyst response may be retried automatically before the
    # normal no-actionable guard closes the run.
    cfg.empty_scan_limit = 2

    if preflight:
        preflight_cfg = copy.copy(cfg)
        preflight_cfg.run_id = f"{cfg.run_id}_preflight"
        preflight_cfg.run_results_path.mkdir(parents=True, exist_ok=True)
        response = Orchestrator(preflight_cfg)._call(
            "This is a connectivity smoke test. Reply with exactly: "
            "DEEPSEEK_OK",
            "three_issue_preflight",
        )
        if not response.strip():
            raise RuntimeError("DeepSeek SDK preflight returned no text")
        print("[three-issue] DeepSeek SDK preflight returned text", flush=True)

    coordinator = ThreeIssueCoordinator(
        cfg,
        orchestrator=ThreeIssueOrchestrator(scenario.issues),
        issues=scenario.issues,
    )
    coordinator.run()

    summary_path = cfg.run_results_path / cfg.run_summary_filename
    summary = json.loads(summary_path.read_text())
    counts = json.loads(counts_path.read_text()) if counts_path.exists() else {}
    attempts_path = cfg.run_results_path / cfg.gate_attempts_filename
    attempts = [
        json.loads(line)
        for line in attempts_path.read_text().splitlines()
        if line.strip()
    ] if attempts_path.exists() else []
    changed = run(
        ["git", "diff", "--name-only", f"{baseline}..{cfg.integration_branch}"],
        repo,
    ).stdout.splitlines()
    source_after = source_snapshot(scenario.source)
    expected_count = len(scenario.issues)
    expected_files = sorted({issue.repo_file for issue in scenario.issues})
    merged_attempts = sum(
        attempt.get("outcome") == "merged" for attempt in attempts
    )

    checks = {
        "all expected issues completed": (
            summary["merges"] == expected_count
            and summary["backlog"]["total"] == expected_count
            and summary["backlog"]["done"] == expected_count
            and summary["backlog"]["todo"] == 0
            and summary["backlog"]["in_progress"] == 0
            and summary["backlog"]["skipped"] == 0
        ),
        "one successful gate per issue": merged_attempts == expected_count,
        "penalty decreased": (
            summary["final_penalty"] < summary["baseline_penalty"]
        ),
        "native build/test path exercised per issue": (
            counts.get("build_commands", 0) >= expected_count
            and counts.get("test_commands", 0) >= 2 * expected_count
        ),
        "only expected source files changed": (
            sorted(changed) == expected_files
        ),
        "run branch was not pushed": summary["run_branch_pushed"] is False,
        "source checkout unchanged": source_after == source_before,
        "verification stops as no actionable work": (
            summary["stop_reason"] == "no_actionable_work"
        ),
    }
    for label, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {label}", flush=True)

    result = {
        "scenario": scenario.name,
        "expected_issues": [
            {
                "file": issue.repo_file,
                "symbol": issue.symbol,
                "line": issue.line,
            }
            for issue in scenario.issues
        ],
        "checks": checks,
        "run_summary": str(summary_path),
        "native_counts": str(counts_path),
        "gate_attempts": str(attempts_path),
        "merges": summary["merges"],
        "baseline_penalty": summary["baseline_penalty"],
        "final_penalty": summary["final_penalty"],
        "build_commands": counts.get("build_commands", 0),
        "test_commands": counts.get("test_commands", 0),
        "gate_outcomes": [
            attempt.get("outcome") for attempt in attempts
        ],
        "tokens": summary.get("tokens", {}).get("totals", {}),
    }
    print(f"[three-issue] standard results: {cfg.run_results_path}", flush=True)
    return all(checks.values()), result


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
        "--env-file", type=Path, default=EXP / ".env",
    )
    parser.add_argument(
        "--scenario", choices=("all", "mongo", "ferret"), default="all",
    )
    args = parser.parse_args()
    load_project_dotenv(args.env_file.expanduser().resolve())
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is not configured")
    if not shutil.which("claude"):
        raise SystemExit("Claude Code CLI not found")

    mongo = args.mongo_repo.resolve()
    ferret = args.ferret_repo.resolve()
    all_scenarios = (
        MultiIssueScenario(
            name="mongo_query_bson_three_issue_validation",
            source=mongo,
            target_subdir="src/mongo/db/query/bson",
            language="cpp",
            metric="ccn",
            threshold=15,
            native_name="mongo",
            issues=(ExpectedIssue(
                repo_file=(
                    "src/mongo/db/query/bson/"
                    "multikey_dotted_path_support.cpp"
                ),
                symbol="_extractAllElementsAlongPath",
                line=55,
            ),),
        ),
        MultiIssueScenario(
            name="ferret_telemetry_three_issue_validation",
            source=ferret,
            target_subdir="internal/util/telemetry",
            language="go",
            metric="nloc",
            threshold=30,
            native_name="ferret",
            issues=(
                ExpectedIssue(
                    repo_file="internal/util/telemetry/telemetry.go",
                    symbol="initialState",
                    line=73,
                ),
                ExpectedIssue(
                    repo_file="internal/util/telemetry/reporter.go",
                    symbol="makeReport",
                    line=191,
                ),
            ),
        ),
    )
    scenarios = tuple(
        scenario for scenario in all_scenarios
        if args.scenario == "all"
        or args.scenario == scenario.native_name
    )
    if any(s.native_name == "mongo" for s in scenarios):
        if not shutil.which("bazel"):
            raise SystemExit("Bazel/Bazelisk is required")
    if any(s.native_name == "ferret" for s in scenarios):
        if not shutil.which("go"):
            raise SystemExit("Go is required")
    for scenario in scenarios:
        if not (scenario.source / ".git").exists():
            raise SystemExit(f"git checkout not found: {scenario.source}")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    evidence_base = EXP / "live_smoke_results" / f"{stamp}_three_issue"
    success = True
    results = []
    with tempfile.TemporaryDirectory(
        prefix="deepseek-three-issue-",
    ) as td:
        outer = Path(td)
        os.environ["GOCACHE"] = str(outer / "go-cache")
        os.environ["MPLCONFIGDIR"] = str(outer / "matplotlib-cache")
        os.environ["TEST_TMPDIR"] = str(outer / "bazel-tmp")
        Path(os.environ["TEST_TMPDIR"]).mkdir()
        for index, scenario in enumerate(scenarios):
            passed, result = execute(
                scenario,
                outer,
                evidence_base,
                preflight=index == 0,
            )
            success = passed and success
            results.append(result)

    overall = {
        "command_completed_without_external_intervention": True,
        "passed": success,
        "expected_issue_count": sum(len(s.issues) for s in scenarios),
        "completed_merge_count": sum(r["merges"] for r in results),
        "scenarios": results,
    }
    overall_path = evidence_base / "three_issue_summary.json"
    overall_path.write_text(json.dumps(overall, indent=2))
    print(f"\n{'=' * 78}")
    print(f"THREE-ISSUE EVIDENCE: {evidence_base}")
    print(f"OVERALL SUMMARY: {overall_path}")
    print(
        "UNATTENDED THREE-ISSUE DEEPSEEK VALIDATION PASSED"
        if success else
        "UNATTENDED THREE-ISSUE DEEPSEEK VALIDATION FAILED"
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
