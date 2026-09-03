"""Offline integration checks for dynamic-gate mode and safe artifacts."""

import json
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from analysis.penalty import compute_dynamic_penalty, dynamic_regression_penalty  # noqa: E402
from config import Config  # noqa: E402
from dynamic_metrics.runner import DynamicBenchmarkRunner, DynamicEvaluation, ExecutionResult  # noqa: E402
from dynamic_metrics import runner as dynamic_runner_module  # noqa: E402
from merge_gate.gate import CommandResult, MergeGate  # noqa: E402
from coordination import gate_attempts  # noqa: E402
from production_profiles import get_production_profile  # noqa: E402
import main as entrypoint  # noqa: E402


def check(label, condition):
    if not condition:
        raise AssertionError(label)
    print(" PASS", label)


class FakeEvaluator:
    def __init__(self, evaluation):
        self.evaluation, self.calls, self.artifacts = evaluation, 0, []

    def evaluate(self, baseline, candidate, artifact):
        self.calls += 1
        self.artifacts.append(artifact)
        if self.evaluation.artifact_dir:
            return self.evaluation
        return DynamicEvaluation(
            self.evaluation.profile, self.evaluation.available,
            self.evaluation.comparable, self.evaluation.dynamic_penalty,
            self.evaluation.breakdown, self.evaluation.reason, str(artifact),
            self.evaluation.comparison,
            candidate_available=self.evaluation.candidate_available,
            baseline_commit=self.evaluation.baseline_commit,
            candidate_commit=self.evaluation.candidate_commit,
            baseline_artifact_dir=self.evaluation.baseline_artifact_dir,
            candidate_artifact_dir=self.evaluation.candidate_artifact_dir,
        )


class PromotingEvaluator(FakeEvaluator):
    def __init__(self, evaluation):
        super().__init__(evaluation)
        self.promoted = []

    def promote_candidate(self, evaluation):
        self.promoted.append(evaluation)


class FakeGate(MergeGate):
    def _is_dirty(self): return False
    def _rebase_onto_integration(self): return True
    def _out_of_scope_paths(self): return []
    def _capture_patch_and_check_duplicate(self): return False
    def _compute_penalty(self, target): return 100.0 if target == self._integration_target() else 90.0
    def _build(self): return CommandResult(0)
    def _test(self): return CommandResult(0)
    def _fast_forward_merge(self): return True


def gate(mode, evaluator, artifact):
    root = artifact / "repo"
    wt = artifact / "worktree"
    root.mkdir(parents=True, exist_ok=True)
    wt.mkdir(parents=True, exist_ok=True)
    return FakeGate(
        worktree=wt, repo_root=root, target_subdir=".", thresholds={}, build_cmd=["build"], test_cmd=["test"],
        integration_branch="run/test", dynamic_evaluator=evaluator,
        dynamic_config={"dynamic_mode": mode, "dynamic_artifact_dir": str(artifact / "benchmarks")},
    ).run()


def main():
    print("[1] config stores only the FerretDB URL environment-variable name")
    cfg = Config(repo_root=Path("/tmp/repo"), target_subdir=".", work_root=Path("/tmp/work"),
                 dynamic_mode="observe", dynamic_ferretdb_url_env="BENCH_URL",
                 gate_timeout_sec=4 * 60 * 60)
    check("config preserves selected dynamic mode", cfg.dynamic_mode == "observe")
    check("dynamic measurements default to seven repetitions", cfg.dynamic_repetitions == 7)
    check("config has no URL value field", not any("postgresql_url" in key for key in vars(cfg)))
    # The coordinator imports its provider transport at module import time.
    # Supply the tiny SDK surface needed for this offline fingerprint check;
    # no client is constructed and no network request is possible.
    if "anthropic" not in sys.modules:
        fake_anthropic = types.ModuleType("anthropic")
        fake_anthropic.Anthropic = object
        sys.modules["anthropic"] = fake_anthropic
    from coordination.coordinator import Coordinator
    coordinator = Coordinator(cfg, orchestrator=object())
    original_timeout = cfg.dynamic_timeout_sec
    before_fingerprint = coordinator._optimization_fingerprint()
    cfg.dynamic_timeout_sec = original_timeout + 1
    check("resume fingerprint binds dynamic benchmark timeout",
          coordinator._optimization_fingerprint() != before_fingerprint)
    cfg.dynamic_timeout_sec = original_timeout
    try:
        Config(repo_root=Path("/tmp/repo"), target_subdir=".", work_root=Path("/tmp/work"),
               dynamic_weights={"ferretdb_ns_per_op": -0.1})
    except ValueError:
        check("config rejects negative dynamic weights", True)
    else:
        check("config rejects negative dynamic weights", False)
    try:
        Config(repo_root=Path("/tmp/repo"), target_subdir=".", work_root=Path("/tmp/work"),
               dynamic_mode="observe", build_timeout_sec=10, test_timeout_sec=20,
               dynamic_timeout_sec=30, gate_timeout_sec=60)
    except ValueError:
        check("dynamic mode reserves cumulative gate time", True)
    else:
        check("dynamic mode reserves cumulative gate time", False)
    for profile_name in ("ferretdb", "mongodb-query"):
        profile = get_production_profile(profile_name)
        Config(
            repo_root=profile.repo_root, target_subdir=profile.target_subdir,
            work_root=profile.work_root, production_profile=profile.name,
            build_timeout_sec=profile.build_timeout_sec,
            test_timeout_sec=profile.test_timeout_sec,
            gate_timeout_sec=profile.gate_timeout_sec,
            dynamic_mode="observe",
        )
    check("production profiles reserve time for dynamic benchmarks", True)
    original_argv = sys.argv
    try:
        sys.argv = ["main.py", "--profile", "ferretdb", "--dynamic-weights",
                    '{"ferretdb_ns_per_op": -0.1}']
        entrypoint.parse_args()
    except SystemExit as exc:
        check("CLI rejects negative dynamic weights", "finite non-negative" in str(exc))
    else:
        check("CLI rejects negative dynamic weights", False)
    finally:
        sys.argv = original_argv

    print("[2] relative-only dynamic penalty")
    check("tolerance has zero penalty", dynamic_regression_penalty(1.05, .05) == 0)
    try:
        compute_dynamic_penalty("ferretdb", {
            "ferretdb_ns_per_op": [1.0], "ferretdb_bytes_per_op": [1.0],
        }, tolerance=.05, weights={"ferretdb_ns_per_op": -1.0})
    except ValueError:
        check("penalty defensively rejects negative dynamic weights", True)
    else:
        check("penalty defensively rejects negative dynamic weights", False)
    penalty, breakdown = compute_dynamic_penalty("ferretdb", {
        "ferretdb_ns_per_op": [1.20], "ferretdb_bytes_per_op": [1.0],
    }, tolerance=.05, weights={"ferretdb_ns_per_op": .7, "ferretdb_bytes_per_op": .3})
    check("only measured regression contributes", penalty > 0 and breakdown["ferretdb_bytes_per_op"]["penalty"] == 0)

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        print("[3] gate modes preserve static behavior and fail closed")
        off = FakeEvaluator(DynamicEvaluation("ferretdb", True, True, 99.0))
        result = gate("off", off, base / "off")
        check("off skips evaluator and merges", result.success and off.calls == 0)
        observe = FakeEvaluator(DynamicEvaluation("ferretdb", True, True, 99.0, artifact_dir="safe"))
        result = gate("observe", observe, base / "observe")
        check("observe records but does not reject", result.success and result.dynamic_penalty == 99.0)
        record = gate_attempts.make_record("P", result, "ISSUE-1")
        check("attempt record keeps static and explicit dynamic fields", record["penalty_after"] == 90.0 and record["dynamic"]["penalty"] == 99.0)
        unavailable = FakeEvaluator(DynamicEvaluation("ferretdb", False, False, reason="no env"))
        result = gate("enforce", unavailable, base / "unavailable")
        check("enforce rejects unavailable", result.outcome == gate_attempts.DYNAMIC_UNAVAILABLE)
        regression = FakeEvaluator(DynamicEvaluation("ferretdb", True, True, 20.0))
        result = gate("enforce", regression, base / "regression")
        check("enforce uses combined static + dynamic penalty", result.outcome == gate_attempts.DYNAMIC_REGRESSION)

        promoting = PromotingEvaluator(DynamicEvaluation(
            "ferretdb", True, True, candidate_available=True,
            baseline_commit="base", candidate_commit="candidate",
        ))
        result = gate("observe", promoting, base / "promoting")
        check("successful gate promotes its measured candidate",
              result.success and len(promoting.promoted) == 1)

        first = FakeEvaluator(DynamicEvaluation("ferretdb", True, True))
        second = FakeEvaluator(DynamicEvaluation("ferretdb", True, True))
        gate("observe", first, base / "attempt-unique")
        gate("observe", second, base / "attempt-unique")
        check("dynamic retries use attempt-unique artifact directories",
              len(first.artifacts) == len(second.artifacts) == 1
              and first.artifacts[0] != second.artifacts[0]
              and first.artifacts[0].parent == second.artifacts[0].parent)

        original_discovery = dynamic_runner_module.discover_query_benchmark_targets
        try:
            dynamic_runner_module.discover_query_benchmark_targets = lambda _root: (
                "//src/mongo/db/query/../../other:escaped_bm",
            )
            mongo_runner = DynamicBenchmarkRunner({"production_profile": "mongodb-query"})
            try:
                mongo_runner._mongodb(base / "unused-baseline", base / "unused-candidate", base / "scope")
            except Exception as exc:
                check("runner rejects traversal target via canonical predicate",
                      "escaped query scope" in str(exc))
            else:
                check("runner rejects traversal target via canonical predicate", False)
        finally:
            dynamic_runner_module.discover_query_benchmark_targets = original_discovery

        try:
            DynamicBenchmarkRunner({
                "production_profile": "mongodb-query", "dynamic_repetitions": 1,
            })._run_mongo_side(base, base / "one-repetition", ())
        except Exception as exc:
            check("MongoDB runner rejects one repetition before execution",
                  "at least two benchmark repetitions" in str(exc))
        else:
            check("MongoDB runner rejects one repetition before execution", False)

        print("[4] FerretDB runner writes redacted artifacts with fake executor")
        secret = "postgresql://user:secret@db/internal"
        fixture = (HERE / "fixtures" / "ferretdb_go_benchmark.txt").read_text()
        def executor(argv, cwd, env, timeout):
            # The URL reaches only this in-memory boundary, never artifacts.
            check("executor received runtime URL only", any(secret in part for part in argv))
            return ExecutionResult(0, fixture, "connected " + secret)
        for name in ("baseline", "candidate"):
            (base / "ferret" / name / "integration").mkdir(parents=True, exist_ok=True)
        runner = DynamicBenchmarkRunner({
            "production_profile": "ferretdb", "dynamic_repetitions": 3,
            "dynamic_tolerance": .05, "dynamic_weights": {"ferretdb_ns_per_op": .7, "ferretdb_bytes_per_op": .3},
            "dynamic_ferretdb_url_env": "BENCH_URL",
        }, executor=executor, environ={"BENCH_URL": secret})
        evaluated = runner.evaluate(base / "ferret" / "baseline", base / "ferret" / "candidate", base / "artifacts")
        check("fixture comparison is available", evaluated.available and evaluated.comparable)
        all_artifacts = "\n".join(path.read_text() for path in (base / "artifacts").rglob("*") if path.is_file())
        check("URL value is redacted from all artifacts", secret not in all_artifacts)
        check("comparison JSON exists", json.loads((base / "artifacts" / "summaries" / "comparison.json").read_text())["comparable"])

        print("[5] rolling baseline runs once and promotes accepted candidate")
        rolling_base = base / "rolling"
        baseline_repo = rolling_base / "baseline-repo"
        candidate_repo = rolling_base / "candidate-repo"
        for repo, marker in ((baseline_repo, "baseline"), (candidate_repo, "candidate")):
            (repo / "integration").mkdir(parents=True)
            (repo / "marker.txt").write_text(marker)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "add", "marker.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", marker], cwd=repo, check=True,
            )
        baseline_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=baseline_repo,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        candidate_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=candidate_repo,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        rolling_calls = []
        def rolling_executor(argv, cwd, env, timeout):
            rolling_calls.append((tuple(argv), cwd))
            return ExecutionResult(0, fixture, "")
        baseline_state = rolling_base / "dynamic_baseline.json"
        rolling_runner = DynamicBenchmarkRunner({
            "production_profile": "ferretdb",
            "dynamic_repetitions": 7,
            "dynamic_tolerance": .05,
            "dynamic_weights": {"ferretdb_ns_per_op": .7, "ferretdb_bytes_per_op": .3},
            "dynamic_ferretdb_url_env": "BENCH_URL",
            "dynamic_baseline_state_path": str(baseline_state),
        }, executor=rolling_executor, environ={"BENCH_URL": secret})
        initialized = rolling_runner.initialize_baseline(
            baseline_repo, rolling_base / "initial-baseline", baseline_commit,
        )
        check("initial dynamic baseline is measured once", initialized.available and len(rolling_calls) == 2)
        rolling_evaluation = rolling_runner.evaluate(
            baseline_repo, candidate_repo, rolling_base / "candidate-attempt",
        )
        check("gate executes only candidate warmup and measurement",
              rolling_evaluation.comparable and len(rolling_calls) == 4)
        rolling_runner.promote_candidate(rolling_evaluation)
        promoted = json.loads(baseline_state.read_text())
        check("accepted candidate becomes next baseline",
              promoted["commit"] == candidate_commit
              and promoted["measurement_dir"] == rolling_evaluation.candidate_artifact_dir)
        check("rolling measurements use seven repetitions",
              all(any(part == "-count=7" for part in argv) or any(part == "-count=1" for part in argv)
                  for argv, _cwd in rolling_calls))

        print("[5b] rolling dynamic evaluations serialize shared benchmark state")
        active = 0
        maximum_active = 0
        guard = threading.Lock()

        def hold_benchmark_lock():
            nonlocal active, maximum_active
            runner = DynamicBenchmarkRunner({
                "dynamic_baseline_state_path": str(baseline_state),
            })
            with runner._benchmark_lock():
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                time.sleep(.05)
                with guard:
                    active -= 1

        lock_threads = [
            threading.Thread(target=hold_benchmark_lock) for _ in range(3)
        ]
        for thread in lock_threads:
            thread.start()
        for thread in lock_threads:
            thread.join()
        check("concurrent gates cannot overlap dynamic benchmark execution",
              maximum_active == 1)

        print("[6] MongoDB rolling gate does not execute the baseline twice")
        for repo in (baseline_repo, candidate_repo):
            query = repo / "src" / "mongo" / "db" / "query"
            query.mkdir(parents=True)
            (query / "BUILD.bazel").write_text(
                'mongo_cc_benchmark(name = "canonical_query_bm")\n'
            )
            subprocess.run(["git", "add", "src/mongo/db/query/BUILD.bazel"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "add benchmark"], cwd=repo, check=True,
            )
        baseline_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=baseline_repo,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        mongo_calls = []
        mongo_fixture = (HERE / "fixtures" / "mongodb_google_benchmark.json").read_text()
        def mongo_executor(argv, cwd, env, timeout):
            mongo_calls.append((tuple(argv), cwd))
            if len(argv) > 1 and argv[1] == "cquery":
                return ExecutionResult(0, "benchmark-bin\n", "")
            output = next((part.split("=", 1)[1] for part in argv
                           if part.startswith("--benchmark_out=")), "")
            if output:
                Path(output).write_text(mongo_fixture)
            return ExecutionResult(0, "", "")
        mongo_state = rolling_base / "mongo_dynamic_baseline.json"
        mongo_runner = DynamicBenchmarkRunner({
            "production_profile": "mongodb-query",
            "dynamic_repetitions": 7,
            # The fixture CV is 0.20. Crossing this diagnostic threshold must
            # be reported without making the baseline or gate unavailable.
            "dynamic_max_cv": .10,
            "dynamic_baseline_state_path": str(mongo_state),
        }, executor=mongo_executor)
        initialized = mongo_runner.initialize_baseline(
            baseline_repo, rolling_base / "mongo-initial-baseline", baseline_commit,
        )
        check("MongoDB startup measures one baseline side",
              initialized.available and len(mongo_calls) == 4)
        baseline_summary = json.loads(
            (rolling_base / "mongo-initial-baseline" / "baseline_summary.json").read_text()
        )
        check("high baseline CV is diagnostic only",
              baseline_summary["diagnostics"]["mongodb_cv"]["high_case_count"] > 0)
        evaluated = mongo_runner.evaluate(
            baseline_repo, candidate_repo, rolling_base / "mongo-candidate-attempt",
        )
        check("MongoDB gate adds only one candidate side",
              evaluated.comparable and len(mongo_calls) == 8)
        check("high candidate CV remains comparable and is recorded",
              evaluated.diagnostics["mongodb_cv"]["informational_only"] is True
              and evaluated.diagnostics["mongodb_cv"]["high_case_count"] > 0)
        check("MongoDB formal invocations request seven repetitions",
              sum(any(part == "--benchmark_repetitions=7" for part in argv)
                  for argv, _cwd in mongo_calls) == 2)
    print("ALL dynamic-gate checks PASSED")


if __name__ == "__main__":
    main()
