"""Runtime collection for approved benchmark outputs.

The runner is intentionally injectable: unit tests pass a fake executor and
fixtures, while production uses ``subprocess`` only after the merge gate has
completed normal build and correctness validation.  Raw output and every
reported error are redacted before writing artifacts.
"""

from __future__ import annotations

import json
import fcntl
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping

from analysis.penalty import compute_dynamic_penalty
from .compare import compare_ferretdb_metrics, compare_mongodb_real_time
from .ferretdb import (
    FerretDBBenchmarkConfig, build_ferretdb_benchmark_command,
    ferretdb_benchmark_availability, parse_ferretdb_benchmark_text,
)
from .mongodb import (
    approved_query_dynamic_benchmark_targets,
    discover_query_benchmark_targets,
    is_query_benchmark_target,
    parse_mongodb_benchmark_artifacts,
)
from .models import DynamicMetricError, FerretDBComparisonResult, MongoComparisonResult


@dataclass(frozen=True)
class ExecutionResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Executor = Callable[[list[str], Path, Mapping[str, str], int], ExecutionResult]


@dataclass(frozen=True)
class DynamicEvaluation:
    profile: str
    available: bool
    comparable: bool
    dynamic_penalty: float = 0.0
    breakdown: dict | None = None
    reason: str = ""
    artifact_dir: str = ""
    comparison: dict | None = None
    candidate_available: bool = False
    baseline_commit: str = ""
    candidate_commit: str = ""
    baseline_artifact_dir: str = ""
    candidate_artifact_dir: str = ""
    diagnostics: dict | None = None

    def safe_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DynamicBaselineResult:
    profile: str
    available: bool
    commit: str
    artifact_dir: str
    reason: str = ""
    reused: bool = False


def _default_executor(argv: list[str], cwd: Path, env: Mapping[str, str], timeout: int) -> ExecutionResult:
    try:
        completed = subprocess.run(argv, cwd=str(cwd), env=dict(env), capture_output=True,
                                   text=True, timeout=timeout)
        return ExecutionResult(completed.returncode, completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired:
        return ExecutionResult(124, "", "benchmark command timed out")
    except OSError as exc:
        return ExecutionResult(127, "", str(exc))


def _redact(value: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            value = value.replace(secret, "<redacted>")
    # PostgreSQL URLs returned by a tool are never useful in results.
    return re.sub(r"(?:postgres(?:ql)?://|mongodb(?:\+srv)?://)\S+", "<redacted-url>", value)


class DynamicBenchmarkRunner:
    """Collect and advance a run-scoped rolling benchmark baseline."""

    def __init__(self, config: dict, *, executor: Executor | None = None,
                 environ: Mapping[str, str] | None = None):
        self.config = dict(config)
        self.executor = executor or _default_executor
        self.environ = dict(os.environ if environ is None else environ)

    def initialize_baseline(
        self, root: Path, artifact_dir: Path, commit: str,
    ) -> DynamicBaselineResult:
        """Measure the initial integration commit once, before dispatch.

        A valid state for the same commit and measurement configuration is
        reused on resume.  Every later accepted candidate replaces this state;
        rejected candidates never modify it.
        """
        profile = str(self.config.get("production_profile", ""))
        artifact_dir = artifact_dir.resolve()
        try:
            existing = self._load_baseline_state(expected_commit=commit)
            return DynamicBaselineResult(
                profile, True, commit, str(existing["measurement_dir"]), reused=True,
            )
        except (DynamicMetricError, ValueError, OSError):
            pass

        artifact_dir.mkdir(parents=True, exist_ok=True)
        try:
            if profile == "mongodb-query":
                discovered = discover_query_benchmark_targets(root)
                if not discovered or any(
                    not is_query_benchmark_target(target) for target in discovered
                ):
                    raise DynamicMetricError(
                        "MongoDB benchmark discovery escaped query scope"
                    )
                targets = approved_query_dynamic_benchmark_targets(discovered)
                measured = self._run_mongo_side(
                    root, artifact_dir, targets, commit=commit, run_id="baseline"
                )
                if not measured.complete:
                    raise DynamicMetricError(
                        "initial MongoDB benchmark baseline is incomplete"
                    )
                diagnostics = self._mongo_cv_diagnostics(
                    (("baseline", measured.metrics),)
                )
            elif profile == "ferretdb":
                targets = ()
                config = self._ferret_config()
                measured = parse_ferretdb_benchmark_text(
                    self._run_ferret_side(root, artifact_dir, config),
                    source_output=str(artifact_dir / "run_logs" / "run.log"),
                    run_id="baseline",
                )
                if not measured.complete:
                    raise DynamicMetricError(
                        "initial FerretDB benchmark baseline is incomplete"
                    )
                diagnostics = {}
            else:
                raise DynamicMetricError(
                    "dynamic metrics require a locked production profile"
                )
            state = {
                "version": 1,
                "profile": profile,
                "commit": commit,
                "repetitions": self._repetitions(),
                "measurement_dir": str(artifact_dir),
                "targets": list(targets),
                "updated_at": time.time(),
            }
            self._write_json_atomic(self._baseline_state_path(), state)
            self._write_json_atomic(
                artifact_dir / "baseline_summary.json",
                {
                    "profile": profile,
                    "available": True,
                    "commit": commit,
                    "repetitions": self._repetitions(),
                    "targets": list(targets),
                    "diagnostics": diagnostics,
                },
            )
            return DynamicBaselineResult(profile, True, commit, str(artifact_dir))
        except (DynamicMetricError, ValueError, OSError) as exc:
            reason = _redact(str(exc), self._secrets())
            self._write_json_atomic(
                artifact_dir / "baseline_summary.json",
                {
                    "profile": profile,
                    "available": False,
                    "commit": commit,
                    "repetitions": self._repetitions(),
                    "reason": reason,
                },
            )
            return DynamicBaselineResult(
                profile, False, commit, str(artifact_dir), reason=reason,
            )

    def evaluate(self, baseline_root: Path, candidate_root: Path, artifact_dir: Path) -> DynamicEvaluation:
        # FerretDB benchmark processes share one external database and use
        # deterministic collection names. Concurrent gates would otherwise
        # collide on inserts/drop cleanup and corrupt each other's samples.
        # The lock is run-scoped and also protects future shared benchmark
        # adapters without serialising normal static/build/test gate phases.
        with self._benchmark_lock():
            if self.config.get("dynamic_baseline_state_path"):
                return self._evaluate_rolling(
                    baseline_root, candidate_root, artifact_dir
                )
            # Compatibility for direct library callers which have not opted
            # into run-scoped orchestration.
            return self._evaluate_two_sided(
                baseline_root, candidate_root, artifact_dir
            )

    @contextmanager
    def _benchmark_lock(self):
        state_raw = str(self.config.get("dynamic_baseline_state_path", ""))
        if not state_raw:
            yield
            return
        lock_path = Path(state_raw + ".benchmark.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _evaluate_rolling(
        self, baseline_root: Path, candidate_root: Path, artifact_dir: Path,
    ) -> DynamicEvaluation:
        profile = str(self.config.get("production_profile", ""))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if profile not in ("mongodb-query", "ferretdb"):
            return self._unavailable(profile, artifact_dir, "dynamic metrics require a locked production profile")
        try:
            baseline_commit = self._git_commit(baseline_root)
            candidate_commit = self._git_commit(candidate_root)
            state = self._load_baseline_state(expected_commit=baseline_commit)
            baseline_dir = Path(str(state["measurement_dir"]))
            candidate_dir = artifact_dir / "candidate"
            if profile == "mongodb-query":
                targets = tuple(str(target) for target in state.get("targets", ()))
                if not targets:
                    raise DynamicMetricError("rolling MongoDB baseline has no targets")
                before = self._load_mongo_measurement(
                    baseline_dir, targets, baseline_commit, "baseline"
                )
                after = self._run_mongo_side(
                    candidate_root, candidate_dir, targets,
                    commit=candidate_commit, run_id="candidate",
                )
                candidate_available = after.complete
                comparison = compare_mongodb_real_time(before, after)
                comparable = comparison.comparable
                ratios = {"mongodb_real_time": [x.ratio for x in comparison.matched if x.ratio is not None]}
                comparison_dict = comparison.to_dict()
                diagnostics = self._mongo_cv_diagnostics((
                    ("baseline", before.metrics),
                    ("candidate", after.metrics),
                ))
            else:
                config = self._ferret_config()
                before = parse_ferretdb_benchmark_text(
                    (baseline_dir / "run_logs" / "run.log").read_text(),
                    source_output=str(baseline_dir / "run_logs" / "run.log"),
                    run_id="baseline",
                )
                after = parse_ferretdb_benchmark_text(
                    self._run_ferret_side(candidate_root, candidate_dir, config),
                    source_output=str(candidate_dir / "run_logs" / "run.log"),
                    run_id="candidate",
                )
                candidate_available = after.complete
                comparison = compare_ferretdb_metrics(before, after)
                comparable = comparison.comparable
                ratios = {
                    "ferretdb_ns_per_op": [x.ns_per_op_ratio for x in comparison.matched if x.ns_per_op_ratio is not None],
                    "ferretdb_bytes_per_op": [x.bytes_per_op_ratio for x in comparison.matched if x.bytes_per_op_ratio is not None],
                }
                comparison_dict = comparison.to_dict()
                diagnostics = {}
            if not comparable:
                return self._write_evaluation(DynamicEvaluation(
                    profile, True, False,
                    reason="benchmark cases are unavailable or not comparable",
                    artifact_dir=str(artifact_dir), comparison=comparison_dict,
                    candidate_available=candidate_available,
                    baseline_commit=baseline_commit,
                    candidate_commit=candidate_commit,
                    baseline_artifact_dir=str(baseline_dir),
                    candidate_artifact_dir=str(candidate_dir),
                    diagnostics=diagnostics,
                ), artifact_dir)
            penalty, breakdown = compute_dynamic_penalty(profile, ratios,
                tolerance=float(self.config.get("dynamic_tolerance", 0.05)),
                weights=dict(self.config.get("dynamic_weights", {})))
            return self._write_evaluation(DynamicEvaluation(
                profile, True, True, penalty, breakdown,
                artifact_dir=str(artifact_dir), comparison=comparison_dict,
                candidate_available=candidate_available,
                baseline_commit=baseline_commit,
                candidate_commit=candidate_commit,
                baseline_artifact_dir=str(baseline_dir),
                candidate_artifact_dir=str(candidate_dir),
                diagnostics=diagnostics,
            ), artifact_dir)
        except (DynamicMetricError, ValueError, OSError) as exc:
            return self._unavailable(profile, artifact_dir, _redact(str(exc), self._secrets()))

    def _evaluate_two_sided(
        self, baseline_root: Path, candidate_root: Path, artifact_dir: Path,
    ) -> DynamicEvaluation:
        profile = str(self.config.get("production_profile", ""))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if profile not in ("mongodb-query", "ferretdb"):
            return self._unavailable(profile, artifact_dir, "dynamic metrics require a locked production profile")
        try:
            if profile == "mongodb-query":
                comparison = self._mongodb(baseline_root, candidate_root, artifact_dir)
                ratios = {"mongodb_real_time": [x.ratio for x in comparison.matched if x.ratio is not None]}
                diagnostics = self._mongo_comparison_cv_diagnostics(comparison)
            else:
                comparison = self._ferretdb(baseline_root, candidate_root, artifact_dir)
                ratios = {
                    "ferretdb_ns_per_op": [x.ns_per_op_ratio for x in comparison.matched if x.ns_per_op_ratio is not None],
                    "ferretdb_bytes_per_op": [x.bytes_per_op_ratio for x in comparison.matched if x.bytes_per_op_ratio is not None],
                }
                diagnostics = {}
            comparison_dict = comparison.to_dict()
            if not comparison.comparable:
                return self._write_evaluation(DynamicEvaluation(profile, True, False, reason="benchmark cases are unavailable or not comparable", artifact_dir=str(artifact_dir), comparison=comparison_dict, diagnostics=diagnostics), artifact_dir)
            penalty, breakdown = compute_dynamic_penalty(profile, ratios,
                tolerance=float(self.config.get("dynamic_tolerance", 0.05)),
                weights=dict(self.config.get("dynamic_weights", {})))
            return self._write_evaluation(DynamicEvaluation(profile, True, True, penalty, breakdown,
                artifact_dir=str(artifact_dir), comparison=comparison_dict,
                diagnostics=diagnostics), artifact_dir)
        except (DynamicMetricError, ValueError, OSError) as exc:
            return self._unavailable(profile, artifact_dir, _redact(str(exc), self._secrets()))

    def promote_candidate(self, evaluation: DynamicEvaluation) -> None:
        """Make an accepted candidate the next rolling baseline atomically."""
        if not evaluation.candidate_available:
            raise DynamicMetricError("accepted candidate has no reusable dynamic measurement")
        state = self._load_baseline_state(
            expected_commit=evaluation.baseline_commit
        )
        if state["profile"] != evaluation.profile:
            raise DynamicMetricError("dynamic baseline profile changed during gate")
        promoted = dict(state)
        promoted.update({
            "commit": evaluation.candidate_commit,
            "measurement_dir": evaluation.candidate_artifact_dir,
            "updated_at": time.time(),
            "promoted_from": evaluation.artifact_dir,
        })
        self._write_json_atomic(self._baseline_state_path(), promoted)

    def _unavailable(self, profile: str, artifact_dir: Path, reason: str) -> DynamicEvaluation:
        return self._write_evaluation(DynamicEvaluation(profile, False, False, reason=reason, artifact_dir=str(artifact_dir)), artifact_dir)

    def _write_evaluation(self, evaluation: DynamicEvaluation, artifact_dir: Path) -> DynamicEvaluation:
        payload = evaluation.safe_dict()
        (artifact_dir / "summaries").mkdir(parents=True, exist_ok=True)
        (artifact_dir / "summaries" / "comparison.json").write_text(json.dumps(payload, indent=2, sort_keys=True))
        lines = [f"# Dynamic benchmark comparison", "", f"Profile: `{evaluation.profile}`", "", f"Available: `{evaluation.available}`", f"Comparable: `{evaluation.comparable}`", f"Dynamic penalty: `{evaluation.dynamic_penalty:.4f}`"]
        if evaluation.reason:
            lines.extend(["", f"Reason: {evaluation.reason}"])
        cv = (evaluation.diagnostics or {}).get("mongodb_cv", {})
        if cv:
            lines.extend([
                "",
                f"Maximum observed CV: `{cv['maximum_observed']:.4f}`",
                f"Diagnostic high-CV threshold: `{cv['high_threshold']:.4f}`",
                f"High-CV cases (informational only): `{cv['high_case_count']}`",
            ])
        (artifact_dir / "summaries" / "comparison.md").write_text("\n".join(lines) + "\n")
        manifest = {
            "profile": evaluation.profile,
            "baseline_artifact_dir": evaluation.baseline_artifact_dir,
            "artifacts": ["candidate", "summaries/comparison.json", "summaries/comparison.md"],
        }
        (artifact_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        return evaluation

    def _baseline_state_path(self) -> Path:
        raw = str(self.config.get("dynamic_baseline_state_path", ""))
        if not raw:
            raise DynamicMetricError("dynamic baseline state path is not configured")
        return Path(raw)

    def _load_baseline_state(self, *, expected_commit: str) -> dict:
        path = self._baseline_state_path()
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise DynamicMetricError(f"cannot load dynamic baseline state: {exc}") from exc
        if not isinstance(state, dict) or state.get("version") != 1:
            raise DynamicMetricError("dynamic baseline state has an unsupported format")
        if state.get("profile") != self.config.get("production_profile"):
            raise DynamicMetricError("dynamic baseline profile does not match gate profile")
        if int(state.get("repetitions", 0)) != self._repetitions():
            raise DynamicMetricError("dynamic baseline repetitions do not match gate configuration")
        if state.get("commit") != expected_commit:
            raise DynamicMetricError(
                "dynamic baseline commit does not match current integration commit"
            )
        measurement_dir = Path(str(state.get("measurement_dir", "")))
        if not measurement_dir.is_dir():
            raise DynamicMetricError("dynamic baseline artifact directory is missing")
        return state

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def _git_commit(root: Path) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(root),
            capture_output=True, text=True,
        )
        commit = result.stdout.strip()
        if result.returncode or not commit:
            raise DynamicMetricError(f"cannot resolve benchmark commit in {root}")
        return commit

    def _repetitions(self) -> int:
        return int(self.config.get("dynamic_repetitions", 7))

    def _ferret_config(self) -> FerretDBBenchmarkConfig:
        return FerretDBBenchmarkConfig(
            enabled=True,
            postgresql_url_env_var=str(self.config.get(
                "dynamic_ferretdb_url_env",
                "FERRETDB_BENCHMARK_POSTGRESQL_URL",
            )),
            repetitions=self._repetitions(),
        )

    def _load_mongo_measurement(
        self, directory: Path, targets: tuple[str, ...], commit: str, run_id: str,
    ):
        artifacts = [
            (directory / "run_json" / self._mongo_target_token(target), target)
            for target in targets
        ]
        return parse_mongodb_benchmark_artifacts(
            artifacts, commit=commit, run_id=run_id
        )

    def _mongo_cv_diagnostics(self, sides) -> dict:
        """Record variability without making it a gate acceptance condition."""
        threshold = float(self.config.get("dynamic_max_cv", 0.10))
        cases = []
        for side, metrics in sides:
            for metric in metrics:
                cv = metric.coefficient_of_variation
                cases.append({
                    "side": side,
                    "target": metric.key.target,
                    "run_name": metric.key.run_name,
                    "coefficient_of_variation": cv,
                    "high": cv > threshold,
                })
        high_cases = [case for case in cases if case["high"]]
        return {
            "mongodb_cv": {
                "informational_only": True,
                "high_threshold": threshold,
                "maximum_observed": max(
                    (case["coefficient_of_variation"] for case in cases),
                    default=0.0,
                ),
                "high_case_count": len(high_cases),
                "cases": cases,
            }
        }

    def _mongo_comparison_cv_diagnostics(
        self, comparison: MongoComparisonResult,
    ) -> dict:
        baseline = tuple(
            case.baseline for case in comparison.comparisons
            if case.baseline is not None
        )
        candidate = tuple(
            case.candidate for case in comparison.comparisons
            if case.candidate is not None
        )
        return self._mongo_cv_diagnostics((
            ("baseline", baseline), ("candidate", candidate),
        ))

    @staticmethod
    def _mongo_target_token(target: str) -> str:
        return target.replace("//", "").replace("/", "_").replace(":", "_") + ".json"

    def _secrets(self) -> tuple[str, ...]:
        name = str(self.config.get("dynamic_ferretdb_url_env", ""))
        return (self.environ.get(name, ""),)

    def _run(self, argv: list[str], cwd: Path, log: Path) -> ExecutionResult:
        result = self.executor(argv, cwd, self.environ, int(self.config.get("dynamic_timeout_sec", 3600)))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(_redact(result.stdout + result.stderr, self._secrets()))
        if result.returncode:
            raise DynamicMetricError(f"benchmark command failed ({result.returncode}): {log.name}")
        return result

    def _mongodb(self, baseline: Path, candidate: Path, artifact_dir: Path) -> MongoComparisonResult:
        discovered = discover_query_benchmark_targets(baseline)
        if not discovered or any(not is_query_benchmark_target(target) for target in discovered):
            raise DynamicMetricError("MongoDB benchmark discovery escaped query scope")
        targets = approved_query_dynamic_benchmark_targets(discovered)
        before = self._run_mongo_side(baseline, artifact_dir / "baseline", targets)
        after = self._run_mongo_side(candidate, artifact_dir / "candidate", targets)
        return compare_mongodb_real_time(before, after)

    def _run_mongo_side(
        self, root: Path, side: Path, targets: tuple[str, ...],
        *, commit: str = "", run_id: str = "",
    ):
        artifacts: list[tuple[Path, str]] = []
        bazel = str(self.config.get("dynamic_bazel_binary", "bazel"))
        repetitions = self._repetitions()
        # MongoDB consumes Google Benchmark's median/mean/stddev aggregate
        # rows.  A single repetition produces no aggregates, which would
        # otherwise make the runner do a costly build and two executions
        # before failing as non-comparable.
        if repetitions < 2:
            raise DynamicMetricError(
                "MongoDB real_time dynamic metrics require at least two benchmark repetitions"
            )
        for target in targets:
            token = self._mongo_target_token(target).removesuffix(".json")
            self._run([bazel, "build", target], root, side / "build_logs" / f"{token}.log")
            output = self._run([bazel, "cquery", "--output=files", target], root, side / "run_logs" / f"{token}.cquery.log").stdout
            files = [line.strip() for line in output.splitlines() if line.strip()]
            if len(files) != 1:
                raise DynamicMetricError(f"{target}: expected one executable from bazel cquery")
            json_path = side / "run_json" / f"{token}.json"
            # Google Benchmark opens --benchmark_out directly; unlike our
            # command-log paths, it does not create a missing parent
            # directory.  Create it before the benchmark starts so a valid
            # native JSON report is not rejected as an invalid filename.
            json_path.parent.mkdir(parents=True, exist_ok=True)
            # Warm-up is deliberately not parsed or compared.
            self._run([str(root / files[0]), "--benchmark_repetitions=1", "--benchmark_report_aggregates_only=true"], root, side / "run_logs" / f"{token}.warmup.log")
            self._run([str(root / files[0]), f"--benchmark_repetitions={repetitions}", "--benchmark_report_aggregates_only=true", f"--benchmark_out={json_path}", "--benchmark_out_format=json"], root, side / "run_logs" / f"{token}.log")
            artifacts.append((json_path, target))
        return parse_mongodb_benchmark_artifacts(
            artifacts, commit=commit, run_id=run_id
        )

    def _ferretdb(self, baseline: Path, candidate: Path, artifact_dir: Path) -> FerretDBComparisonResult:
        config = self._ferret_config()
        availability = ferretdb_benchmark_availability(config, environ=self.environ)
        if not availability.available:
            raise DynamicMetricError(availability.reason)
        before = parse_ferretdb_benchmark_text(self._run_ferret_side(baseline, artifact_dir / "baseline", config), source_output="baseline/run.log")
        after = parse_ferretdb_benchmark_text(self._run_ferret_side(candidate, artifact_dir / "candidate", config), source_output="candidate/run.log")
        return compare_ferretdb_metrics(before, after)

    def _run_ferret_side(self, root: Path, side: Path, config: FerretDBBenchmarkConfig) -> str:
        command = build_ferretdb_benchmark_command(root, config, environ=self.environ)
        placeholder = "${" + command.postgresql_url_env_var + "}"
        # URL expansion happens only in the argv handed to the subprocess.
        url = self.environ[command.postgresql_url_env_var]
        argv = [part.replace(placeholder, url) for part in command.argv_template]
        warmup = [part for part in argv if not part.startswith("-count=")] + ["-count=1"]
        self._run(
            warmup, Path(command.working_directory), side / "run_logs" / "warmup.log"
        )
        return self._run(
            argv, Path(command.working_directory), side / "run_logs" / "run.log"
        ).stdout
