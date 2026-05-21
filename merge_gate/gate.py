"""Merge gate: commit -> pre-pull penalty check -> rebase -> fresh
penalty baseline -> build -> test -> ff-merge.

This pipeline matches gnomad-kiro:
  - The gate, not the programmer, makes the commit (`git add -A`).
  - The penalty baseline is the *current* main's penalty, re-measured
    on every gate run, not a stale value passed in.
  - A pre-pull check bails early when the worktree is already worse
    than main, avoiding a wasted rebase/build/test cycle.
  - On any failure the worktree is NOT reset; a .patch is dumped to
    `reverted_dir` for post-mortem and the programmer keeps its state.
"""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from analysis.penalty import compute_total_penalty, DEFAULT_WEIGHTS
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
        duplo_min_block_lines: int = 10,
        lizard_binary: str = "lizard",
        lizard_language: str = "cpp",
        weights: dict | None = None,
        reverted_dir: Path | None = None,
        worktree_idx: int = 0,
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
        self.lizard_binary = lizard_binary
        self.lizard_language = lizard_language
        self.weights = weights if weights is not None else dict(DEFAULT_WEIGHTS)
        self.reverted_dir = reverted_dir
        self.worktree_idx = worktree_idx

    MAX_FF_RETRIES = 3

    def run(self) -> GateResult:
        # Step 1 (gnomad-kiro parity): commit whatever is in the
        # worktree. The programmer no longer needs to commit itself.
        self._git(["add", "-A"], cwd=self.worktree)
        self._git(
            ["commit", "-m", f"Fix from programmer {self.worktree_idx}"],
            cwd=self.worktree,
        )

        for _ in range(self.MAX_FF_RETRIES):
            result = self._run_once()
            if result.success or not result.merged_race:
                return result
        return GateResult(
            success=False,
            reason="ff-merge race exceeded retries",
        )

    def _run_once(self) -> "GateResult":
        # Step 2: pre-pull check — measure both worktree and current
        # main, bail if the worktree isn't already an improvement.
        lizard_main_pre, cog_main_pre, main_dup_pre = run_static_analysis(
            self._main_target(), self.thresholds,
            duplo_binary=self.duplo_binary,
            duplo_min_block_lines=self.duplo_min_block_lines,
            lizard_binary=self.lizard_binary,
            language=self.lizard_language,
        )
        penalty_before = compute_total_penalty(
            lizard_main_pre, cog_main_pre, self.thresholds, main_dup_pre, self.weights,
        )

        penalty_pre = self._compute_penalty(self.worktree)
        if penalty_pre >= penalty_before:
            self._save_patch("pre_pull_no_improvement")
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_pre,
                reason="pre-pull: worktree not better than main",
            )

        # Step 3: rebase onto fresh main.
        if not self._rebase_onto_main():
            self._save_patch("rebase_conflict")
            return GateResult(success=False, rebase_conflict=True, reason="rebase conflict")

        # Step 4: re-measure main's penalty *after* the rebase (it may
        # have changed if another programmer merged in the interim).
        lizard_main, cog_main, main_dup = run_static_analysis(
            self._main_target(), self.thresholds,
            duplo_binary=self.duplo_binary,
            duplo_min_block_lines=self.duplo_min_block_lines,
            lizard_binary=self.lizard_binary,
            language=self.lizard_language,
        )
        penalty_before = compute_total_penalty(
            lizard_main, cog_main, self.thresholds, main_dup, self.weights,
        )

        penalty_after = self._compute_penalty(self.worktree)
        if penalty_after >= penalty_before:
            self._save_patch("penalty_not_decreased")
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                reason="penalty did not decrease",
            )

        if not self._build():
            self._save_patch("build_failed")
            return GateResult(
                success=False,
                penalty_before=penalty_before,
                penalty_after=penalty_after,
                reason="build failed",
            )

        if not self._test():
            self._save_patch("tests_failed")
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

    def _main_target(self) -> Path:
        target = self.repo_root
        if self.target_subdir not in (".", ""):
            target = target / self.target_subdir
        return target

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

    def _compute_penalty(self, root: Path) -> float:
        target = root / self.target_subdir if self.target_subdir not in (".", "") else root
        lizard_records, cognitive_records, dup_ratio = run_static_analysis(
            target, self.thresholds,
            duplo_binary=self.duplo_binary,
            duplo_min_block_lines=self.duplo_min_block_lines,
            lizard_binary=self.lizard_binary,
            language=self.lizard_language,
        )
        return compute_total_penalty(
            lizard_records, cognitive_records,
            self.thresholds, dup_ratio, self.weights,
        )

    def _build(self) -> bool:
        return self._exec(self.build_cmd) == 0

    def _test(self) -> bool:
        return self._exec(self.test_cmd) == 0

    def _save_patch(self, reason: str) -> None:
        """Save current worktree diff as .patch in reverted_dir."""
        if self.reverted_dir is None:
            return
        try:
            self.reverted_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        patch_path = self.reverted_dir / f"prog{self.worktree_idx}_{reason}_{stamp}.patch"
        try:
            proc = subprocess.run(
                ["git", "diff", "HEAD~1..HEAD"],
                cwd=str(self.worktree),
                capture_output=True, text=True,
            )
            patch_path.write_text(proc.stdout)
        except OSError:
            pass

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
