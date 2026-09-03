"""Git infrastructure: per-run integration branch and agent worktrees.

Thesis §4.4 requires two things of every run:

  - "the codebase was reset to its original state before each run", across
    10 independent runs per configuration, and
  - "both systems create a dedicated git branch in the target repository
    at the start of execution. All commits made during the run are
    recorded on this branch. At the end of the run, the branch is pushed
    to the remote repository and the working directory is reset to the
    original baseline, leaving the branch intact as a permanent record."

Both are satisfied by never merging into the repository's primary branch.
Each run creates `refactor/<run_id>` from a fixed baseline ref and the
merge gate fast-forwards *that* branch instead. The primary branch is
therefore still sitting on the baseline when the run ends, so the next
run starts from an identical state without any destructive reset, and
checking the primary branch back out restores the original working
directory.

The implementation also records the exact pre-run checkout identity. This is
necessary for repositories that begin on a detached HEAD: shutdown restores
that commit rather than assuming that a local primary branch is checked out.
"""

import shutil
import subprocess
from pathlib import Path


def _run(cmd: list[str], cwd: Path) -> str:
    result = subprocess.run(
        cmd, cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return result.stdout


def _try(cmd: list[str], cwd: Path) -> bool:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True
    ).returncode == 0


def ref_exists(repo_root: Path, ref: str) -> bool:
    return _try(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo_root)


def resolve_ref(repo_root: Path, ref: str) -> str:
    """Return the commit sha a ref points at."""
    return _run(["git", "rev-parse", f"{ref}^{{commit}}"], cwd=repo_root).strip()


def repo_is_dirty(repo_root: Path, allowed_untracked_paths=()) -> bool:
    out = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    allowed = {
        str(path).strip().strip("/").replace("\\", "/")
        for path in allowed_untracked_paths if str(path).strip()
    }
    for entry in out.stdout.split("\0"):
        if not entry:
            continue
        status = entry[:2]
        path = entry[3:].replace("\\", "/").strip("/")
        if status == "??" and any(
            path == item or path.startswith(item + "/") for item in allowed
        ):
            continue
        return True
    return False


def capture_checkout(repo_root: Path) -> tuple[str, str, bool]:
    """Return (restore ref, commit, detached) for the user's checkout."""
    branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True,
    )
    commit = resolve_ref(repo_root, "HEAD")
    if branch.returncode == 0 and branch.stdout.strip():
        return branch.stdout.strip(), commit, False
    return commit, commit, True


def restore_checkout(
    repo_root: Path,
    checkout_ref: str,
    expected_commit: str,
    detached: bool,
) -> bool:
    """Restore the exact branch or detached commit present before the run."""
    command = (
        ["git", "checkout", "--detach", checkout_ref]
        if detached else ["git", "checkout", checkout_ref]
    )
    if not _try(command, repo_root):
        return False
    return resolve_ref(repo_root, "HEAD") == expected_commit


def resolve_baseline_ref(repo_root: Path, configured: str, main_branch: str) -> str:
    """Pick the ref that defines the run's starting state.

    An explicitly configured ref always wins. Otherwise prefer
    `origin/<main>` so repeated runs are pinned to the fetched upstream
    state, and fall back to the local branch when there is no remote.
    """
    if configured:
        if not ref_exists(repo_root, configured):
            raise ValueError(f"baseline ref does not resolve: {configured}")
        return configured
    remote = f"origin/{main_branch}"
    if ref_exists(repo_root, remote):
        return remote
    if ref_exists(repo_root, main_branch):
        return main_branch
    raise ValueError(
        f"neither {remote} nor {main_branch} resolves; pass an explicit --baseline-ref"
    )


def prepare_run_branch(
    repo_root: Path,
    integration_branch: str,
    baseline_ref: str,
    fetch: bool = True,
    allowed_untracked_paths=(),
) -> str:
    """Create and check out the run's integration branch at the baseline.

    Returns the resolved baseline commit sha. Refuses to run against a
    dirty repository rather than silently discarding the author's work.
    """
    if repo_is_dirty(repo_root, allowed_untracked_paths):
        raise RuntimeError(
            f"{repo_root} has uncommitted changes; commit or stash them before a run"
        )
    if fetch:
        # Best effort: a repository without a remote is still usable.
        _try(["git", "fetch", "origin"], repo_root)

    baseline_commit = resolve_ref(repo_root, baseline_ref)

    # A re-run with the same run id starts over from the baseline.
    _try(["git", "checkout", "--detach", baseline_commit], repo_root)
    _try(["git", "branch", "-D", integration_branch], repo_root)
    _run(["git", "checkout", "-b", integration_branch, baseline_commit], cwd=repo_root)
    return baseline_commit


def resume_run_branch(
    repo_root: Path,
    integration_branch: str,
    baseline_ref: str,
    fetch: bool = True,
    allowed_untracked_paths=(),
) -> str:
    """Check out an existing run branch without moving it.

    Resume must preserve every commit already accepted before the crash.
    In particular it must never call `prepare_run_branch`, whose intentional
    behaviour for a fresh run is to recreate the branch at the baseline.
    """
    if repo_is_dirty(repo_root, allowed_untracked_paths):
        raise RuntimeError(
            f"{repo_root} has uncommitted changes; commit or stash them before resume"
        )
    if fetch:
        _try(["git", "fetch", "origin"], repo_root)
    baseline_commit = resolve_ref(repo_root, baseline_ref)
    if not ref_exists(repo_root, integration_branch):
        raise ValueError(
            f"cannot resume: integration branch does not exist: {integration_branch}"
        )
    _run(["git", "checkout", integration_branch], cwd=repo_root)
    return baseline_commit


def push_branch(repo_root: Path, branch: str) -> bool:
    """Push the run branch to origin. Returns False if there is no remote."""
    if not _try(["git", "remote", "get-url", "origin"], repo_root):
        return False
    return _try(["git", "push", "--set-upstream", "origin", branch], repo_root)


def restore_baseline(repo_root: Path, main_branch: str, baseline_commit: str) -> bool:
    """Return the working directory to the original baseline.

    The primary branch was never advanced during the run, so this is a
    plain checkout. It is verified against the recorded baseline commit
    so a mismatch is reported rather than silently accepted.
    """
    if not ref_exists(repo_root, main_branch):
        return False
    if not _try(["git", "checkout", main_branch], repo_root):
        return False
    return resolve_ref(repo_root, main_branch) == baseline_commit


def create_worktree(
    repo_root: Path,
    work_root: Path,
    agent_id: str,
    target_subdir: str,
    base_ref: str,
    branch: str | None = None,
    detached: bool = False,
) -> Path:
    """Materialise a worktree for one agent, based at `base_ref`.

    By default a feature branch named `feature/<agent_id>` is created
    (used by programmers). Pass `detached=True` for analysts so the
    worktree tracks the integration branch without holding a branch ref.
    """
    worktree_path = work_root / agent_id

    if worktree_path.exists():
        shutil.rmtree(worktree_path)

    _run(["git", "worktree", "prune"], cwd=repo_root)

    if detached:
        # Detached HEAD so we don't compete with the main worktree for the
        # integration branch (git refuses to check out a branch that is
        # already checked out elsewhere).
        _run(
            ["git", "worktree", "add", "--detach", str(worktree_path), base_ref],
            cwd=repo_root,
        )
    else:
        branch = branch or f"feature/{agent_id}"
        _try(["git", "branch", "-D", branch], repo_root)
        _run(
            ["git", "worktree", "add", "-b", branch, str(worktree_path), base_ref],
            cwd=repo_root,
        )

    if target_subdir not in (".", ""):
        _enable_sparse_checkout(worktree_path, target_subdir)

    return worktree_path


def _enable_sparse_checkout(worktree_path: Path, subdir: str) -> None:
    _run(["git", "sparse-checkout", "init", "--cone"], cwd=worktree_path)
    _run(["git", "sparse-checkout", "set", subdir], cwd=worktree_path)


def reset_worktree(worktree_path: Path, integration_branch: str) -> None:
    """Discard all uncommitted changes and reset to the integration branch.

    Resets to the *local* integration branch. The merge gate fast-forwards
    it on every accepted change and never pushes mid-run, so any remote
    ref would be stale for the whole run — resetting there would throw
    away every refactoring accepted so far.
    """
    _run(["git", "reset", "--hard", integration_branch], cwd=worktree_path)
    _run(["git", "clean", "-fd"], cwd=worktree_path)


def remove_worktree(repo_root: Path, worktree_path: Path) -> None:
    try:
        _run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=repo_root)
    except subprocess.CalledProcessError:
        if worktree_path.exists():
            shutil.rmtree(worktree_path)
