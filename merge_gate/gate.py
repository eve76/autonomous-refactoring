"""Merge gate: rebase -> penalty -> build -> test -> ff-merge.

The pipeline follows the thesis Method chapter (§4.3.2, "Evaluation"
and "Integration") literally:

  - The *programmer* commits its edits to the feature branch before
    invoking the gate; the gate does not stage or commit anything.
  - The gate "first rebases the feature branch onto the latest main"
    (the per-run integration branch, see coordination/git_manager).
    If the rebase produces a conflict the conflicted state is LEFT IN
    PLACE, because the thesis has the programmer resolve it manually
    and re-run the gate.
  - It then runs the three static-analysis tools, computes the total
    penalty via the hyperbolic penalty function, and compares against
    the penalty before the refactoring. The integration branch's
    penalty is re-measured on every run so a concurrent merge cannot
    leave a stale baseline.
  - "If the penalty did not decrease, the changes are reverted to the
    pre-refactoring state and the programmer moves to the next issue."
  - Build and test failures are reported WITHOUT reverting, because the
    thesis has the programmer read the failure output, fix its
    refactored code, and re-run the gate.
  - On success it fast-forwards the integration branch to the tip of
    the feature branch,
    retrying the rebase/penalty/build/test cycle if another programmer
    won the race.
"""

import subprocess
import hashlib
import time
import uuid
import fcntl
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from analysis.penalty import compute_total_penalty, compose_total_penalty, DEFAULT_WEIGHTS
from analysis.tools import run_static_analysis, StaticAnalysisError
from coordination import gate_attempts
from coordination import issue_history


@dataclass
class GateResult:
    success: bool
    rebase_conflict: bool = False
    penalty_before: float = 0.0
    penalty_after: float = 0.0
    tests_passed: bool = False
    merged: bool = False
    merged_race: bool = False
    reverted: bool = False
    reason: str = ""
    # Machine-readable classification of this attempt, set at each exit
    # point so the failure counts §5.1 and §5.4 report never have to be
    # inferred from the prose in `reason`.
    outcome: str = ""
    patch_id: str = ""
    patch_file: str = ""
    base_commit: str = ""
    commit_hash: str = ""
    strategy: str = ""
    changed_files: list[str] = field(default_factory=list)
    # Static fields above remain the coordinator's scalar source of truth.
    # Dynamic data is explicit and safe to archive independently.
    dynamic_mode: str = "off"
    dynamic_available: bool = False
    dynamic_comparable: bool = False
    dynamic_penalty: float = 0.0
    total_penalty_after: float = 0.0
    dynamic_reason: str = ""
    dynamic_artifact_dir: str = ""
    dynamic_comparison: dict | None = None


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    timed_out: bool = False


class MergeGate:
    def __init__(
        self,
        worktree: Path,
        repo_root: Path,
        target_subdir: str,
        thresholds: dict,
        build_cmd: list[str],
        test_cmd: list[str],
        integration_branch: str,
        duplo_binary: str = "",
        duplo_min_block_lines: int = 4,
        dupl_binary: str = "",
        dupl_threshold_tokens: int = 100,
        lizard_binary: str = "lizard",
        gocognit_binary: str = "gocognit",
        lizard_language: str = "cpp",
        weights: dict | None = None,
        attempt_log: Path | None = None,
        agent_id: str = "",
        exclude_dirs=None,
        issue_id: str = "",
        allowed_untracked_paths=(),
        issue_history_dir: Path | None = None,
        build_timeout_sec: int = 60 * 60,
        test_timeout_sec: int = 60 * 60,
        dynamic_config: dict | None = None,
        dynamic_evaluator=None,
    ):
        self.worktree = worktree
        self.repo_root = repo_root
        self.target_subdir = target_subdir
        self.thresholds = thresholds
        self.build_cmd = build_cmd
        self.test_cmd = test_cmd
        self.integration_branch = integration_branch
        self.duplo_binary = duplo_binary
        self.duplo_min_block_lines = duplo_min_block_lines
        self.dupl_binary = dupl_binary
        self.dupl_threshold_tokens = dupl_threshold_tokens
        self.lizard_binary = lizard_binary
        self.gocognit_binary = gocognit_binary
        self.lizard_language = lizard_language
        self.weights = weights if weights is not None else dict(DEFAULT_WEIGHTS)
        self.attempt_log = attempt_log
        self.agent_id = agent_id
        self.issue_id = issue_id
        self.allowed_untracked_paths = {
            PurePosixPath(str(path).replace("\\", "/")).as_posix()
            for path in allowed_untracked_paths
        }
        self.issue_history_dir = issue_history_dir
        self.build_timeout_sec = build_timeout_sec
        self.test_timeout_sec = test_timeout_sec
        self.dynamic_config = dict(dynamic_config or {"dynamic_mode": "off"})
        self.dynamic_evaluator = dynamic_evaluator
        self._dynamic_evaluation = None
        self._patch_metadata: dict = {}
        # None means "use the tools default"; an empty list means
        # "exclude nothing", so this must not collapse to `or`.
        self.exclude_dirs = exclude_dirs

    MAX_FF_RETRIES = 3

    def run(self) -> GateResult:
        for attempt in range(self.MAX_FF_RETRIES):
            result = self._run_once()
            if (
                result.merged_race
                and attempt == self.MAX_FF_RETRIES - 1
            ):
                result.outcome = gate_attempts.FF_RACE_EXHAUSTED
                result.reason = "ff-merge race exceeded retries"
            self._record_attempt(result)
            if result.success or not result.merged_race:
                return result
            if result.outcome == gate_attempts.FF_RACE_EXHAUSTED:
                return result
        raise AssertionError("unreachable")

    def _record_attempt(self, result: GateResult) -> None:
        """Append this attempt to the run-level log, if one was configured.

        Recording is strictly observational: a failure to write it must
        never turn an accepted refactoring into a rejected one, so every
        error here is swallowed.
        """
        for key, value in self._patch_metadata.items():
            setattr(result, key, value)
        record = gate_attempts.make_record(
            self.agent_id, result, self.issue_id,
        )
        if self.attempt_log is not None:
            try:
                gate_attempts.append(self.attempt_log, record)
            except Exception:
                pass
        if self.issue_history_dir is None or not self.issue_id:
            return
        try:
            issue_history.append(
                issue_history.history_path(
                    self.issue_history_dir, self.issue_id,
                ),
                record,
            )
        except Exception:
            pass

    def _run_once(self) -> "GateResult":
        # Step 0: the programmer commits its own edits, so anything left
        # uncommitted would be silently excluded from the evaluation.
        # Say so explicitly rather than letting the rebase fail opaquely.
        if self._is_dirty():
            return GateResult(
                success=False,
                outcome=gate_attempts.UNCOMMITTED,
                reason="uncommitted changes - commit your edits, then re-run the gate",
            )

        # Step 1: rebase the feature branch onto the integration
        # branch, picking up anything merged in the meantime. On
        # conflict the rebase is left in progress for the programmer to
        # resolve manually (thesis: "the programmer attempts to resolve
        # it manually and re-runs the gate").
        if not self._rebase_onto_integration():
            return GateResult(
                success=False,
                rebase_conflict=True,
                outcome=gate_attempts.REBASE_CONFLICT,
                reason="rebase conflict - resolve manually, then re-run the gate",
            )

        # A full worktree may be required for repository-native tests, but
        # that must not expand the refactoring authorization. Reject commits
        # that touch anything outside the configured target subtree before
        # measuring or executing repository code.
        outside = self._out_of_scope_paths()
        if outside:
            preview = ", ".join(outside[:5])
            if len(outside) > 5:
                preview += f", ... (+{len(outside) - 5} more)"
            return GateResult(
                success=False,
                outcome=gate_attempts.OUT_OF_SCOPE,
                reason=f"changes outside target_subdir: {preview}",
            )

        # Preserve the committed patch before any penalty rejection can reset
        # the worktree. Exact repeats for the same issue are rejected before
        # static analysis, build, and test consume more resources.
        if self._capture_patch_and_check_duplicate():
            return GateResult(
                success=False,
                outcome=gate_attempts.DUPLICATE_PATCH,
                reason="identical patch already evaluated for this issue",
            )

        # Step 2: measure the penalty before the refactoring, i.e. the
        # integration branch's current penalty, re-measured every run.
        try:
            penalty_before = self._compute_penalty(self._integration_target())
            penalty_after = self._compute_penalty(self._worktree_target())
        except StaticAnalysisError as exc:
            return GateResult(
                success=False,
                outcome=gate_attempts.ANALYSIS_FAILED,
                reason=f"static analysis failed: {exc}",
            )

        # Step 3: penalty must decrease, else revert to pre-refactoring.
        if penalty_after >= penalty_before:
            self._revert()
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                reverted=True,
                outcome=gate_attempts.PENALTY_REJECTED,
                reason="penalty did not decrease - changes reverted",
            )

        # Step 4: build and test. No revert on failure; the programmer
        # is expected to fix its refactored code and re-run the gate.
        build = self._build()
        if build.timed_out:
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                outcome=gate_attempts.BUILD_TIMEOUT,
                reason=(
                    f"build timed out after {self.build_timeout_sec}s - "
                    "the patch was not classified as a compile failure"
                ),
            )
        if build.returncode < 0:
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                outcome=gate_attempts.BUILD_INTERRUPTED,
                reason=(
                    f"build interrupted by signal {-build.returncode} - "
                    "the patch was not classified as a compile failure"
                ),
            )
        if build.returncode != 0:
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                outcome=gate_attempts.BUILD_FAILED,
                reason="build failed - fix and re-run the gate",
            )

        test = self._test()
        if test.timed_out:
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                tests_passed=False,
                outcome=gate_attempts.TEST_TIMEOUT,
                reason=(
                    f"tests timed out after {self.test_timeout_sec}s - "
                    "the patch was not classified as a test failure"
                ),
            )
        if test.returncode < 0:
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                tests_passed=False,
                outcome=gate_attempts.TEST_INTERRUPTED,
                reason=(
                    f"tests interrupted by signal {-test.returncode} - "
                    "the patch was not classified as a test failure"
                ),
            )
        if test.returncode != 0:
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                tests_passed=False,
                outcome=gate_attempts.TEST_FAILED,
                reason="tests failed - fix and re-run the gate",
            )

        # Dynamic benchmarks are a distinct phase: normal build and
        # correctness testing have already succeeded.  Off preserves the
        # historical gate exactly; observe archives evidence only; enforce is
        # fail-closed and includes the regression penalty in acceptance.
        dynamic = self._evaluate_dynamic()
        mode = str(self.dynamic_config.get("dynamic_mode", "off"))
        dynamic["total_penalty_after"] = compose_total_penalty(
            penalty_after, float(dynamic["dynamic_penalty"])
        )
        if mode == "enforce" and (not dynamic["dynamic_available"] or not dynamic["dynamic_comparable"]):
            return GateResult(
                success=False, penalty_before=penalty_before, penalty_after=penalty_after,
                tests_passed=True, outcome=gate_attempts.DYNAMIC_UNAVAILABLE,
                reason="dynamic benchmark comparison unavailable - fix environment/data and re-run gate",
                **dynamic,
            )
        if mode == "enforce" and compose_total_penalty(penalty_after, dynamic["dynamic_penalty"]) >= penalty_before:
            return GateResult(
                success=False, penalty_before=penalty_before, penalty_after=penalty_after,
                tests_passed=True, outcome=gate_attempts.DYNAMIC_REGRESSION,
                reason="combined static and dynamic penalty did not decrease - performance regression rejected",
                **dynamic,
            )

        # Step 5: integration via fast-forward merge.  The dynamic baseline
        # pointer advances in the same critical section as the integration
        # ref, so a concurrent loser retries against the winner's candidate
        # measurement instead of re-running that new baseline.
        with self._dynamic_merge_lock():
            if (
                not self._dynamic_baseline_still_current()
                or not self._fast_forward_merge()
            ):
                return GateResult(
                    success=False,
                    penalty_before=penalty_before,
                    penalty_after=penalty_after,
                    tests_passed=True,
                    merged_race=True,
                    outcome=gate_attempts.FF_RACE,
                    reason="ff-merge lost race; will retry",
                    **dynamic,
                )
            promotion_error = self._promote_dynamic_candidate()
            if promotion_error:
                dynamic["dynamic_reason"] = promotion_error

        return GateResult(
            success=True,
            penalty_before=penalty_before,
            penalty_after=penalty_after,
            tests_passed=True,
            merged=True,
            outcome=gate_attempts.MERGED,
            **dynamic,
        )

    # -- pipeline steps -----------------------------------------------

    def _integration_target(self) -> Path:
        return self._resolve_target(self.repo_root)

    def _worktree_target(self) -> Path:
        return self._resolve_target(self.worktree)

    def _resolve_target(self, root: Path) -> Path:
        target = self._normalized_target_subdir()
        if target in (None, ""):
            return root
        return root / target

    def _is_dirty(self) -> bool:
        out = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
            cwd=str(self.worktree), capture_output=True, text=True,
        )
        if out.returncode != 0:
            return True
        for entry in out.stdout.split("\0"):
            if not entry:
                continue
            status = entry[:2]
            path = entry[3:].replace("\\", "/")
            if (
                status == "??"
                and PurePosixPath(path).as_posix()
                in self.allowed_untracked_paths
            ):
                continue
            # Tracked modifications are never exempt, even when their path
            # matches an allowed untracked build byproduct.
            return True
        return False

    def _rebase_onto_integration(self) -> bool:
        """Rebase onto the integration branch. Leaves conflicts in place.

        The rebase target is the *local* integration branch. Every
        worktree shares the repository's object database, so that ref
        already reflects each fast-forward merge the gate performs the
        instant it happens. Rebasing onto a remote ref would instead pin
        every programmer to whatever the branch looked like when the run
        started, since the gate never pushes mid-run — which would make
        the second concurrent merge impossible.
        """
        if self._git(["rebase", self.integration_branch]) == 0:
            return True
        # A conflicted rebase must NOT be aborted here: the thesis has
        # the programmer resolve it by hand and re-run the gate.
        return False

    def _compute_penalty(self, target: Path) -> float:
        lizard_records, cognitive_records, duplication = run_static_analysis(
            target, self.thresholds,
            duplo_binary=self.duplo_binary,
            duplo_min_block_lines=self.duplo_min_block_lines,
            dupl_binary=self.dupl_binary,
            dupl_threshold_tokens=self.dupl_threshold_tokens,
            lizard_binary=self.lizard_binary,
            gocognit_binary=self.gocognit_binary,
            language=self.lizard_language,
            exclude_dirs=self.exclude_dirs,
        )
        return compute_total_penalty(
            lizard_records, cognitive_records,
            self.thresholds, duplication.ratio, self.weights,
        )

    def _out_of_scope_paths(self) -> list[str]:
        target = self._normalized_target_subdir()
        if target is None:
            return ["(invalid target_subdir)"]
        if target == "":
            return []
        changed = subprocess.run(
            [
                "git", "diff", "--name-only", "--no-renames",
                "--diff-filter=ACDMRTUXB",
                f"{self.integration_branch}...HEAD",
            ],
            cwd=str(self.worktree), capture_output=True, text=True,
        )
        if changed.returncode != 0:
            # Fail closed: an unresolved diff is not safe to merge.
            return ["(unable to determine changed paths)"]
        prefix = target + "/"
        return sorted({
            path.strip()
            for path in changed.stdout.splitlines()
            if path.strip()
            and path.strip() != target
            and not path.strip().startswith(prefix)
        })

    def _normalized_target_subdir(self) -> str | None:
        """Return a repository-relative POSIX target, or None if unsafe."""
        raw = self.target_subdir.strip().replace("\\", "/")
        while raw.startswith("./"):
            raw = raw[2:]
        raw = raw.rstrip("/")
        if raw in ("", "."):
            return ""
        path = PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts:
            return None
        return path.as_posix()

    def _capture_patch_and_check_duplicate(self) -> bool:
        self._patch_metadata = {}
        diff = subprocess.run(
            [
                "git", "diff", "--binary", "--no-ext-diff",
                f"{self.integration_branch}...HEAD",
            ],
            cwd=str(self.worktree), capture_output=True, text=True,
        )
        if diff.returncode != 0 or not diff.stdout.strip():
            return False
        patch_id_result = subprocess.run(
            ["git", "patch-id", "--stable"], input=diff.stdout,
            capture_output=True, text=True,
        )
        if patch_id_result.returncode != 0 or not patch_id_result.stdout.strip():
            return False
        patch_id = patch_id_result.stdout.split()[0]
        base_commit = self._git_output(["rev-parse", self.integration_branch])
        commit_hash = self._git_output(["rev-parse", "HEAD"])
        strategy = self._git_output(["log", "-1", "--pretty=%s"])
        changed = self._git_output([
            "diff", "--name-only", "--no-renames",
            f"{self.integration_branch}...HEAD",
        ]).splitlines()
        patch_file = ""
        history_file = None
        if self.issue_history_dir is not None and self.issue_id:
            issue_root = issue_history.issue_dir(
                self.issue_history_dir, self.issue_id,
            )
            history_file = issue_history.history_path(
                self.issue_history_dir, self.issue_id,
            )
            relative_patch = Path("patches") / f"{patch_id}.patch"
            patch_path = issue_root / relative_patch
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            if not patch_path.exists():
                patch_path.write_text(diff.stdout)
            patch_file = relative_patch.as_posix()
        self._patch_metadata = {
            "patch_id": patch_id,
            "patch_file": patch_file,
            "base_commit": base_commit,
            "commit_hash": commit_hash,
            "strategy": strategy,
            "changed_files": [path for path in changed if path],
        }
        if history_file is None:
            return False
        return any(
            record.get("patch_id") == patch_id
            and record.get("outcome") not in (
                gate_attempts.FF_RACE,
                gate_attempts.FF_RACE_EXHAUSTED,
            )
            for record in issue_history.load(history_file)
        )

    def _build(self) -> CommandResult:
        return self._exec(self.build_cmd, self.build_timeout_sec)

    def _test(self) -> CommandResult:
        return self._exec(self.test_cmd, self.test_timeout_sec)

    def _evaluate_dynamic(self) -> dict:
        mode = str(self.dynamic_config.get("dynamic_mode", "off"))
        base = {
            "dynamic_mode": mode, "dynamic_available": False,
            "dynamic_comparable": False, "dynamic_penalty": 0.0,
            "total_penalty_after": 0.0, "dynamic_reason": "",
            "dynamic_artifact_dir": "", "dynamic_comparison": None,
        }
        if mode == "off":
            self._dynamic_evaluation = None
            return base
        try:
            evaluator = self.dynamic_evaluator
            if evaluator is None:
                from dynamic_metrics.runner import DynamicBenchmarkRunner
                evaluator = DynamicBenchmarkRunner(self.dynamic_config)
            artifact_raw = str(self.dynamic_config.get("dynamic_artifact_dir", ""))
            if not artifact_raw:
                raise ValueError("dynamic artifact directory is not configured")
            artifact_dir = self._dynamic_attempt_artifact_dir(Path(artifact_raw))
            evaluated = evaluator.evaluate(self.repo_root, self.worktree, artifact_dir)
            self.dynamic_evaluator = evaluator
            self._dynamic_evaluation = evaluated
            base.update({
                "dynamic_available": bool(evaluated.available),
                "dynamic_comparable": bool(evaluated.comparable),
                "dynamic_penalty": float(evaluated.dynamic_penalty),
                "total_penalty_after": 0.0, # populated below by the gate result consumer
                "dynamic_reason": str(evaluated.reason),
                "dynamic_artifact_dir": str(evaluated.artifact_dir),
                "dynamic_comparison": evaluated.comparison,
            })
        except Exception as exc:
            # No URL is retained in config. Avoid surfacing unknown command
            # output in this cross-process record.
            base["dynamic_reason"] = f"dynamic evaluator failed: {type(exc).__name__}"
        return base

    @contextmanager
    def _dynamic_merge_lock(self):
        """Serialize integration-ref updates with rolling-baseline promotion."""
        state_raw = str(
            self.dynamic_config.get("dynamic_baseline_state_path", "")
        )
        if not state_raw:
            yield
            return
        lock_path = Path(state_raw + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _promote_dynamic_candidate(self) -> str:
        """Promote a measured accepted candidate; return an archival error."""
        if str(self.dynamic_config.get("dynamic_mode", "off")) == "off":
            return ""
        evaluation = self._dynamic_evaluation
        evaluator = self.dynamic_evaluator
        if evaluation is None or evaluator is None:
            return "dynamic candidate promotion unavailable"
        promote = getattr(evaluator, "promote_candidate", None)
        if promote is None:
            # Injectable test evaluators and legacy callers need not implement
            # persistence; production DynamicBenchmarkRunner always does.
            return ""
        try:
            promote(evaluation)
        except Exception as exc:
            return f"dynamic candidate promotion failed: {type(exc).__name__}"
        return ""

    def _dynamic_baseline_still_current(self) -> bool:
        """Reject a candidate measured before another concurrent merge."""
        if not self.dynamic_config.get("dynamic_baseline_state_path"):
            return True
        evaluation = self._dynamic_evaluation
        expected = str(getattr(evaluation, "baseline_commit", ""))
        if not expected:
            return True
        result = subprocess.run(
            ["git", "rev-parse", f"{self.integration_branch}^{{commit}}"],
            cwd=str(self.repo_root), capture_output=True, text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == expected

    def _dynamic_attempt_artifact_dir(self, artifact_root: Path) -> Path:
        """Allocate an attempt-specific artifact directory under ``artifact_root``.

        A rejected dynamic result is evidence, and retries (including a
        fast-forward race retry) must never replace it.  The issue label is
        reduced to a safe path component and the timestamp/UUID pair prevents
        collisions across gate processes started in the same nanosecond.
        """
        raw_issue = self.issue_id or "manual"
        safe_issue = "".join(
            char if char.isalnum() or char in "._-" else "_"
            for char in raw_issue
        ).strip(".") or "issue"
        if safe_issue != raw_issue:
            safe_issue = f"{safe_issue}-{hashlib.sha256(raw_issue.encode()).hexdigest()[:10]}"
        attempt_id = f"attempt-{time.time_ns()}-{uuid.uuid4().hex[:12]}"
        return artifact_root / safe_issue / attempt_id

    def _revert(self) -> None:
        """Reset the worktree to the pre-refactoring state.

        That state is the current tip of the integration branch: the
        programmer starts each issue from a clean worktree synced to it,
        so resetting there discards exactly this attempt's commits and
        nothing else.
        """
        self._git(["reset", "--hard", self.integration_branch])
        self._git(["clean", "-fd"])

    def _fast_forward_merge(self) -> bool:
        branch = self._current_branch()
        if not branch:
            return False
        # Idempotent guard: repo_root sits on the integration branch for
        # the whole run, but a crash could have left it elsewhere.
        self._git(["checkout", self.integration_branch], cwd=self.repo_root)
        return self._git(
            ["merge", "--ff-only", branch], cwd=self.repo_root
        ) == 0

    # -- helpers -------------------------------------------------------

    def _git(self, args: list[str], cwd: Path | None = None) -> int:
        return subprocess.run(
            ["git", *args], cwd=str(cwd if cwd is not None else self.worktree)
        ).returncode

    def _git_output(self, args: list[str]) -> str:
        result = subprocess.run(
            ["git", *args], cwd=str(self.worktree),
            capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    def _exec(self, cmd: list[str], timeout_sec: int) -> CommandResult:
        if not cmd:
            return CommandResult(127)
        try:
            completed = subprocess.run(
                cmd, cwd=str(self.worktree), timeout=timeout_sec,
            )
            return CommandResult(completed.returncode)
        except subprocess.TimeoutExpired:
            return CommandResult(124, timed_out=True)
        except OSError:
            return CommandResult(127)

    def _current_branch(self) -> str:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(self.worktree), capture_output=True, text=True,
        )
        return out.stdout.strip()
