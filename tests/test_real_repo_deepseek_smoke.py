#!/usr/bin/env python3
"""Paid, bounded DeepSeek smoke test on MongoDB query/bson and Ferret telemetry.

This test is intentionally excluded from run_all.py. It uses the real
DeepSeek Anthropic API and real Claude Code CLI sessions. Assignment routing
is deterministic so each repository is limited to one discovered issue.
"""

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
WORKSPACE = EXP.parent.parent
NATIVE_RUNNER = (EXP / "tests" / "live_native_runner.py").resolve()
sys.path.insert(0, str(EXP))

from agents.orchestrator import Orchestrator  # noqa: E402
from config import Config                     # noqa: E402
from coordination.coordinator import Coordinator  # noqa: E402
from coordination import message_queue as mq  # noqa: E402
from main import load_project_dotenv          # noqa: E402
from tests.test_real_repo_fake_cli_e2e import (  # noqa: E402
    OneIssueOrchestrator,
    prepare_clone,
    source_snapshot,
)


@dataclass(frozen=True)
class LiveScenario:
    name: str
    source: Path
    target_subdir: str
    issue_file: str
    repo_file: str
    language: str
    metric: str
    threshold: int
    native_name: str
    issue_symbol: str


class BoundedLiveOrchestrator(OneIssueOrchestrator):
    """Allow at most one Analyst and one Programmer dispatch per run."""

    def __init__(self, issue_file: str):
        super().__init__(issue_file)
        self.analyst_dispatched = False
        self.programmer_dispatched = False

    def assign(
        self, current_penalty, baseline_penalty, backlog,
        idle_programmers, idle_analysts, stagnation, metric_breakdown="",
    ):
        todo = sorted(
            (
                item for item in backlog.items.values()
                if item.status == "TODO"
            ),
            key=lambda item: item.id,
        )
        if todo and idle_programmers and not self.programmer_dispatched:
            self.programmer_dispatched = True
            return super().assign(
                current_penalty, baseline_penalty, backlog,
                idle_programmers, [], stagnation, metric_breakdown,
            )
        if idle_analysts and not self.analyst_dispatched:
            self.analyst_dispatched = True
            return super().assign(
                current_penalty, baseline_penalty, backlog,
                [], idle_analysts, stagnation, metric_breakdown,
            )
        from agents.orchestrator import AssignmentDecision
        return AssignmentDecision()


class BoundedLiveCoordinator(Coordinator):
    """Accept only the single expected real Analyst finding."""

    def __init__(
        self, cfg, orchestrator, scenario: LiveScenario,
        analyst_evidence: Path | None = None,
    ):
        super().__init__(cfg, orchestrator=orchestrator)
        self._expected_repo_file = scenario.repo_file
        self._expected_symbol = scenario.issue_symbol.lower()
        self._accepted_live_issue = False
        self._analyst_evidence = analyst_evidence

    def _initialize(self):
        super()._initialize()
        if self._analyst_evidence is None:
            return

        source = self._analyst_evidence.resolve()
        destination = self.cfg.agent_log_dir / "ANALYST_1_001.log"
        shutil.copy2(source, destination)
        session = self.analyst_sessions["ANALYST_1"]
        matching = [
            lead for lead in self.local_candidate_leads
            if lead.function.lower() == self._expected_symbol
            and lead.file_path.replace("\\", "/").endswith(
                Path(self._expected_repo_file).name
            )
        ]
        if len(matching) != 1:
            raise RuntimeError(
                "imported Analyst evidence needs exactly one matching local lead"
            )
        session.log_path = destination
        session.candidate_leads = matching
        issues = session._parse_issues_from_log()
        if len(issues) != 1:
            raise RuntimeError(
                "imported Analyst evidence did not explicitly confirm "
                "the expected issue"
            )
        self._apply_message(mq.Message(
            sender="ANALYST_1",
            kind=mq.ADD_ISSUES,
            payload={"issues": issues},
        ))
        self.analyst_lead_seen_keys.update(
            lead.key for lead in self.local_candidate_leads
        )
        self.backlog.persist()
        self._save_state()
        (self.cfg.run_results_path / "analyst_recovery.json").write_text(
            json.dumps({
                "source_log": str(source),
                "copied_log": str(destination),
                "reason": "successful DeepSeek analysis omitted ISSUE line",
                "accepted_file": self._expected_repo_file,
                "accepted_symbol": self._expected_symbol,
            }, indent=2)
        )
        print(
            "[live-smoke] imported the prior real Analyst result after "
            "protocol-line recovery"
        )

    def _apply_message(self, msg):
        if msg.kind == mq.ADD_ISSUES:
            selected = []
            if not self._accepted_live_issue:
                for raw in msg.payload.get("issues", []):
                    resolved = self._resolve_issue_repo_path(
                        raw.get("file_path", "")
                    )
                    if (
                        resolved == self._expected_repo_file
                        and self._expected_symbol
                        in str(raw.get("message", "")).lower()
                    ):
                        selected = [raw]
                        self._accepted_live_issue = True
                        # The paid micro-smoke intentionally accepts exactly
                        # one issue. Once it is queued, all other local leads
                        # are out of this run's scope and an empty follow-up
                        # scan is logically complete.
                        self.analyst_lead_seen_keys.update(
                            lead.key for lead in self.local_candidate_leads
                        )
                        self.empty_analyst_scans = self.cfg.empty_scan_limit
                        break
            msg = mq.Message(
                sender=msg.sender,
                kind=msg.kind,
                payload={**msg.payload, "issues": selected},
            )
        return super()._apply_message(msg)


def run(
    command: list[str], cwd: Path, check: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=str(cwd), text=True, capture_output=True, check=check,
    )


def find_gocognit() -> str:
    candidate = EXP / ".venv" / "bin" / "gocognit"
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which("gocognit")
    return str(Path(resolved).resolve()) if resolved else ""


def configure(
    scenario: LiveScenario,
    repo: Path,
    baseline: str,
    temp_root: Path,
    evidence_root: Path,
) -> tuple[Config, Path]:
    counts = evidence_root / "native_command_counts.json"
    common = [
        sys.executable, str(NATIVE_RUNNER),
        "--scenario", scenario.native_name,
        "--run-root", str(temp_root),
        "--counts", str(counts),
    ]
    cfg = Config(
        repo_root=repo,
        target_subdir=scenario.target_subdir,
        work_root=evidence_root,
        sparse_worktrees=False,
        run_id=f"{scenario.name}_deepseek_live_smoke",
        baseline_ref=baseline,
        main_branch="e2e-baseline",
        run_branch_prefix="live-smoke",
        push_run_branch=False,
        num_analysts=1,
        num_programmers=1,
        claude_cli=str(Path(shutil.which("claude") or "").resolve()),
        api_provider="deepseek",
        lizard_binary=str((Path(sys.executable).parent / "lizard").resolve()),
        gocognit_binary=find_gocognit(),
        lizard_language=scenario.language,
        duplo_binary="",
        build_cmd=common[:2] + ["build"] + common[2:],
        test_cmd=common[:2] + ["test"] + common[2:],
        min_merge_gain=10.0,
        stagnation_limit=3,
        backlog_drain_interval_sec=0.1,
        issue_timeout_sec=5 * 60,
        programmer_timeout_sec=15 * 60,
        analyst_timeout_sec=5 * 60,
        stuck_eval_interval_sec=60,
        empty_scan_limit=1,
        gate_allowed_untracked_paths=(
            ("MODULE.bazel.lock",)
            if scenario.native_name == "mongo"
            else ()
        ),
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
    cfg.thresholds[scenario.metric] = scenario.threshold
    cfg.weights[scenario.metric] = 1
    return cfg, counts


def execute(
    scenario: LiveScenario,
    outer: Path,
    evidence_base: Path,
    analyst_evidence: Path | None = None,
) -> bool:
    print(f"\n{'=' * 78}\nLIVE DEEPSEEK: {scenario.name}\n{'=' * 78}")
    source_before = source_snapshot(scenario.source)
    scenario_temp = outer / scenario.name
    scenario_temp.mkdir()
    repo, baseline = prepare_clone(
        scenario.source, scenario_temp, scenario.name,
    )
    evidence_root = evidence_base / scenario.name
    evidence_root.mkdir(parents=True)
    cfg, counts_path = configure(
        scenario, repo, baseline, scenario_temp, evidence_root,
    )

    # Exercise the direct SDK transport once. Coordinator routing below stays
    # deterministic so the paid repository work cannot expand past one issue.
    if analyst_evidence is None:
        preflight_cfg = copy.copy(cfg)
        preflight_cfg.run_id = f"{cfg.run_id}_preflight"
        preflight_cfg.run_results_path.mkdir(parents=True, exist_ok=True)
        preflight = Orchestrator(preflight_cfg)._call(
            "This is a connectivity smoke test. Reply with exactly: DEEPSEEK_OK",
            "live_smoke_preflight",
        )
        if not preflight.strip():
            raise RuntimeError("DeepSeek SDK preflight returned no text")
        print("[live-smoke] DeepSeek SDK preflight returned text")

    bounded_orchestrator = BoundedLiveOrchestrator(scenario.issue_file)
    if analyst_evidence is not None:
        bounded_orchestrator.analyst_dispatched = True
    coordinator = BoundedLiveCoordinator(
        cfg,
        orchestrator=bounded_orchestrator,
        scenario=scenario,
        analyst_evidence=analyst_evidence,
    )
    coordinator.run()

    summary_path = cfg.run_results_path / cfg.run_summary_filename
    summary = json.loads(summary_path.read_text())
    counts = json.loads(counts_path.read_text()) if counts_path.exists() else {}
    attempts_path = cfg.run_results_path / cfg.gate_attempts_filename
    attempts = (
        [
            json.loads(line)
            for line in attempts_path.read_text().splitlines()
            if line.strip()
        ]
        if attempts_path.exists() else []
    )
    changed = run(
        ["git", "diff", "--name-only", f"{baseline}..{cfg.integration_branch}"],
        repo,
    ).stdout.splitlines()
    source_after = source_snapshot(scenario.source)

    checks = {
        "one issue completed": (
            summary["merges"] == 1
            and summary["backlog"]["done"] == 1
            and summary["backlog"]["total"] == 1
        ),
        "one successful gate attempt": (
            len(attempts) == 1 and attempts[0]["outcome"] == "merged"
        ),
        "penalty decreased": (
            summary["final_penalty"] < summary["baseline_penalty"]
        ),
        "exactly one build and two tests": (
            counts.get("build_commands") == 1
            and counts.get("test_commands") == 2
        ),
        "only the intended source file changed": changed == [scenario.repo_file],
        "run branch was not pushed": summary["run_branch_pushed"] is False,
        "source checkout unchanged": source_after == source_before,
        "Go cognitive population measured": (
            scenario.language != "go"
            or summary["metrics"]["baseline"]["cognitive"]["functions"] > 0
        ),
    }
    for label, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {label}")
    print(f"[live-smoke] standard results: {cfg.run_results_path}")
    return all(checks.values())


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
        help="DeepSeek key file; values are loaded without overriding the shell",
    )
    parser.add_argument(
        "--scenario", choices=("both", "mongo", "ferret"), default="both",
        help="Run both repositories or one bounded repository scenario",
    )
    parser.add_argument(
        "--analyst-evidence", type=Path,
        help=(
            "Reuse a successful real Analyst log whose explicit confirmation "
            "omitted the ISSUE protocol line"
        ),
    )
    args = parser.parse_args()
    load_project_dotenv(args.env_file.expanduser().resolve())
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("DEEPSEEK_API_KEY is not configured")
    if not shutil.which("claude"):
        raise SystemExit("Claude Code CLI not found")
    all_scenarios = (
        LiveScenario(
            name="mongo_query_bson",
            source=args.mongo_repo.resolve(),
            target_subdir="src/mongo/db/query/bson",
            issue_file="multikey_dotted_path_support.cpp",
            repo_file=(
                "src/mongo/db/query/bson/"
                "multikey_dotted_path_support.cpp"
            ),
            language="cpp",
            metric="ccn",
            threshold=15,
            native_name="mongo",
            issue_symbol="_extractAllElementsAlongPath",
        ),
        LiveScenario(
            name="ferret_telemetry",
            source=args.ferret_repo.resolve(),
            target_subdir="internal/util/telemetry",
            issue_file="telemetry.go",
            repo_file="internal/util/telemetry/telemetry.go",
            language="go",
            metric="nloc",
            threshold=30,
            native_name="ferret",
            issue_symbol="initialState",
        ),
    )
    scenarios = tuple(
        scenario for scenario in all_scenarios
        if args.scenario == "both"
        or args.scenario == scenario.native_name
    )
    analyst_evidence = (
        args.analyst_evidence.expanduser().resolve()
        if args.analyst_evidence else None
    )
    if analyst_evidence is not None:
        if len(scenarios) != 1:
            raise SystemExit("--analyst-evidence requires one --scenario")
        if not analyst_evidence.is_file():
            raise SystemExit(f"Analyst evidence not found: {analyst_evidence}")
    if any(s.native_name == "ferret" for s in scenarios) and not shutil.which("go"):
        raise SystemExit("Go is required for the FerretDB scenario")
    if (
        any(s.native_name == "ferret" for s in scenarios)
        and (not find_gocognit() or not Path(find_gocognit()).is_file())
    ):
        raise SystemExit("gocognit is required for the FerretDB scenario")
    if any(s.native_name == "mongo" for s in scenarios) and not shutil.which("bazel"):
        raise SystemExit("Bazel/Bazelisk is required for the MongoDB scenario")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    evidence_base = EXP / "live_smoke_results" / stamp
    for scenario in scenarios:
        if not (scenario.source / ".git").exists():
            raise SystemExit(f"git checkout not found: {scenario.source}")

    success = True
    with tempfile.TemporaryDirectory(prefix="deepseek-live-smoke-") as td:
        outer = Path(td)
        os.environ["GOCACHE"] = str(outer / "go-cache")
        os.environ["MPLCONFIGDIR"] = str(outer / "matplotlib-cache")
        os.environ["TEST_TMPDIR"] = str(outer / "bazel-tmp")
        Path(os.environ["TEST_TMPDIR"]).mkdir()
        for scenario in scenarios:
            success = execute(
                scenario, outer, evidence_base,
                analyst_evidence=analyst_evidence,
            ) and success

    print(f"\n{'=' * 78}")
    print(f"LIVE SMOKE EVIDENCE: {evidence_base}")
    print("LIVE DEEPSEEK SMOKE PASSED" if success else "LIVE DEEPSEEK SMOKE FAILED")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
