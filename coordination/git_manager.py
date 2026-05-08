"""Git worktree provisioning for isolated programmer workspaces.

One worktree per programmer, each on its own feature branch. When the
target lives in a subdirectory, sparse checkout is enabled so only
that subtree is materialised.
"""

import shutil
import subprocess
from pathlib import Path


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout


def ensure_clean_main(repo_root: Path) -> None:
    _run(["git", "fetch", "origin"], cwd=repo_root)
    _run(["git", "checkout", "main"], cwd=repo_root)
    _run(["git", "pull", "--ff-only", "origin", "main"], cwd=repo_root)


def create_worktree(
    repo_root: Path,
    work_root: Path,
    agent_id: str,
    target_subdir: str,
    branch: str | None = None,
    detach_to_main: bool = False,
) -> Path:
    """Materialise a worktree for one agent.

    By default a feature branch named `feature/<agent_id>` is created
    (used by programmers). Pass `detach_to_main=True` for analysts so
    the worktree tracks main directly without a feature branch.
    """
    worktree_path = work_root / agent_id

    if worktree_path.exists():
        shutil.rmtree(worktree_path)

    _run(["git", "worktree", "prune"], cwd=repo_root)

    if detach_to_main:
        # Detached HEAD so we don't compete with the main worktree for
        # the `main` branch (git refuses to check out a branch that is
        # already checked out elsewhere).
        _run(
            ["git", "worktree", "add", "--detach", str(worktree_path), "main"],
            cwd=repo_root,
        )
    else:
        branch = branch or f"feature/{agent_id}"
        try:
            _run(["git", "branch", "-D", branch], cwd=repo_root)
        except subprocess.CalledProcessError:
            pass
        _run(
            ["git", "worktree", "add", "-b", branch, str(worktree_path), "main"],
            cwd=repo_root,
        )

    if target_subdir not in (".", ""):
        _enable_sparse_checkout(worktree_path, target_subdir)

    return worktree_path


def _enable_sparse_checkout(worktree_path: Path, subdir: str) -> None:
    _run(["git", "sparse-checkout", "init", "--cone"], cwd=worktree_path)
    _run(["git", "sparse-checkout", "set", subdir], cwd=worktree_path)


def reset_worktree(worktree_path: Path, main_branch: str = "main") -> None:
    """Discard all uncommitted changes and reset to main tip."""
    try:
        _run(["git", "fetch", "origin", main_branch], cwd=worktree_path)
        _run(["git", "reset", "--hard", f"origin/{main_branch}"], cwd=worktree_path)
    except subprocess.CalledProcessError:
        # No remote configured -> fall back to local main
        _run(["git", "reset", "--hard", main_branch], cwd=worktree_path)
    _run(["git", "clean", "-fd"], cwd=worktree_path)


def remove_worktree(repo_root: Path, worktree_path: Path) -> None:
    try:
        _run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo_root)
    except subprocess.CalledProcessError:
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
