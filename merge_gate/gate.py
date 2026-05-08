"""Merge gate: rebase -> penalty check -> build -> test -> ff-merge."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from analysis.penalty import compute_total_penalty
from analysis.tools import run_static_analysis


@dataclass
class GateResult:
    success: bool
    rebase_conflict: bool = False
    penalty_before: float = 0.0
    penalty_after: float = 0.0
    tests_passed: bool = False
    merged: bool = False
    merged_race: bool = False
    reason: str = ""


class MergeGate:
    def __init__(
        self,
        worktree: Path,
        repo_root: Path,
        target_subdir: str,
        thresholds: dict,
        build_cmd: list[str],
        test_cmd: list[str],
        main_branch: str = "main",
        duplo_binary: str = "",
        duplo_min_block_lines: int = 6,
    ):
        self.worktree = worktree
        self.repo_root = repo_root
        self.target_subdir = target_subdir
        self.thresholds = thresholds
        self.build_cmd = build_cmd
        self.test_cmd = test_cmd
        self.main_branch = main_branch
        self.duplo_binary = duplo_binary
        self.duplo_min_block_lines = duplo_min_block_lines

    MAX_FF_RETRIES = 3

    def run(self, penalty_before: float) -> GateResult:
        for _ in range(self.MAX_FF_RETRIES):
            result = self._run_once(penalty_before)
            if result.success or not result.merged_race:
                return result
        return GateResult(
            success=False,
            penalty_before=penalty_before,
            reason="ff-merge race exceeded retries",
        )

    def _run_once(self, penalty_before: float) -> "GateResult":
        if not self._rebase_onto_main():
            return GateResult(success=False, rebase_conflict=True, reason="rebase conflict")

        penalty_after = self._compute_penalty()
        if penalty_after >= penalty_before:
            self._revert()
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                reason="penalty did not decrease",
            )

        if not self._build():
            self._revert()
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                reason="build failed",
            )

        if not self._test():
            self._revert()
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                tests_passed=False,
                reason="tests failed",
            )

        if not self._fast_forward_merge():
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                tests_passed=True,
                merged_race=True,
                reason="ff-merge lost race; will retry",
            )

        return GateResult(
            success=True,
            penalty_before=penalty_before,
            penalty_after=penalty_after,
            tests_passed=True,
            merged=True,
        )

    # -- pipeline steps -----------------------------------------------

    def _rebase_onto_main(self) -> bool:
        if self._git(["fetch", "origin", self.main_branch], cwd=self.worktree) != 0:
            self._git(["fetch", self.main_branch], cwd=self.worktree)
        rc = self._git(["rebase", f"origin/{self.main_branch}"], cwd=self.worktree)
        if rc == 0:
            return True
        rc = self._git(["rebase", self.main_branch], cwd=self.worktree)
        if rc == 0:
            return True
        self._git(["rebase", "--abort"], cwd=self.worktree)
        return False

    def _compute_penalty(self) -> float:
        target = self.worktree / self.target_subdir if self.target_subdir not in (".", "") else self.worktree
        records, dup_ratio = run_static_analysis(
            target, self.thresholds,
            duplo_binary=self.duplo_binary,
            duplo_min_block_lines=self.duplo_min_block_lines,
        )
        return compute_total_penalty(records, self.thresholds, dup_ratio)

    def _build(self) -> bool:
        return self._exec(self.build_cmd) == 0

    def _test(self) -> bool:
        return self._exec(self.test_cmd) == 0

    def _revert(self) -> None:
        self._git(["fetch", "origin", self.main_branch], cwd=self.worktree)
        self._git(["reset", "--hard", f"origin/{self.main_branch}"], cwd=self.worktree)

    def _fast_forward_merge(self) -> bool:
        branch = self._current_branch(self.worktree)
        if not branch:
            return False
        self._git(["fetch", "origin", self.main_branch], cwd=self.repo_root)
        self._git(["checkout", self.main_branch], cwd=self.repo_root)
        self._git(["pull", "--ff-only", "origin", self.main_branch], cwd=self.repo_root)
        rc = self._git(["merge", "--ff-only", branch], cwd=self.repo_root)
        return rc == 0

    # -- helpers -------------------------------------------------------

    def _git(self, args: list[str], cwd: Path) -> int:
        return subprocess.run(["git", *args], cwd=str(cwd)).returncode

    def _exec(self, cmd: list[str]) -> int:
        return subprocess.run(cmd, cwd=str(self.worktree)).returncode

    def _current_branch(self, cwd: Path) -> str:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(cwd), capture_output=True, text=True,
        )
        return out.stdout.strip()
