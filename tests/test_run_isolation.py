"""Verify the per-run branch protocol from thesis §4.4.

  "the codebase was reset to its original state before each run"
  "both systems create a dedicated git branch in the target repository at
   the start of execution. All commits made during the run are recorded on
   this branch. At the end of the run, the branch is pushed to the remote
   repository and the working directory is reset to the original baseline,
   leaving the branch intact as a permanent record."

Both are satisfied by never advancing the primary branch: each run merges
into `refactor/<run_id>` instead. This suite drives two consecutive runs
through the real gate and checks that the second one starts from the
original baseline rather than the first run's output.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from analysis.tools import run_static_analysis                          # noqa: E402
from analysis.penalty import compute_total_penalty                       # noqa: E402
from coordination.git_manager import (                                   # noqa: E402
    capture_checkout, create_worktree, prepare_run_branch, push_branch,
    ref_exists, repo_is_dirty,
    remove_worktree, resolve_baseline_ref, resolve_ref, restore_baseline,
    restore_checkout, resume_run_branch,
)

def _find_lizard() -> str:
    """Prefer the lizard next to the running interpreter (virtualenv), then PATH."""
    candidate = Path(sys.executable).parent / "lizard"
    if candidate.exists():
        return str(candidate)
    return shutil.which("lizard") or "lizard"


LIZARD = _find_lizard()


THRESHOLDS = {"ccn": 15, "cognitive": 15, "nloc": 30, "param": 5, "duplicates": 0}
WEIGHTS = {"ccn": 1, "nloc": 1, "cognitive": 1, "param": 1, "duplicates": 1}

FAILURES = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


def bad(fn: str, n: int = 20) -> str:
    body = "\n".join(f"    if (a > {i} && b < {i * 2}) r += {i};" for i in range(1, n + 1))
    return ("#include <cstdio>\n\n"
            f"int {fn}(int a, int b) {{\n    int r = 0;\n{body}\n    return r;\n}}\n")


def good(fn: str) -> str:
    parts = []
    for band in range(4):
        lo = band * 5 + 1
        lines = "\n".join(
            f"    if (a > {i} && b < {i * 2}) r += {i};" for i in range(lo, lo + 5))
        parts.append(f"static int {fn}B{band}(int a, int b) {{\n"
                     f"    int r = 0;\n{lines}\n    return r;\n}}\n")
    call = " + ".join(f"{fn}B{b}(a, b)" for b in range(4))
    return ("#include <cstdio>\n\n" + "\n".join(parts)
            + f"\nint {fn}(int a, int b) {{\n    return {call};\n}}\n")


def git(args, cwd, check_rc=True):
    return subprocess.run(["git", *args], cwd=str(cwd), check=check_rc,
                          capture_output=True, text=True)


def measure(target: Path) -> float:
    lz, cog, dup = run_static_analysis(
        target, THRESHOLDS, duplo_binary="", lizard_binary=LIZARD, language="cpp")
    return compute_total_penalty(lz, cog, THRESHOLDS, dup.ratio, WEIGHTS)


def setup_repo(root: Path) -> Path:
    origin, repo = root / "origin.git", root / "repo"
    origin.mkdir()
    git(["init", "--bare", "-b", "main"], origin)
    git(["clone", str(origin), str(repo)], root)
    for k, v in [("user.email", "v@e.com"), ("user.name", "verifier")]:
        git(["config", k, v], repo)
    src = repo / "src"
    src.mkdir()
    (src / "alpha.cpp").write_text(bad("alpha"))
    (src / "beta.cpp").write_text(bad("beta"))
    git(["add", "-A"], repo)
    git(["commit", "-m", "baseline"], repo)
    git(["push", "-u", "origin", "main"], repo)
    return repo


def write_gate_config(worktree: Path, repo: Path, integration_branch: str):
    cfg_path = worktree.parent / f"{worktree.name}_gate_config.json"
    cfg_path.write_text(json.dumps({
        "repo_root": str(repo),
        "target_subdir": "src",
        "thresholds": THRESHOLDS,
        "weights": WEIGHTS,
        "build_cmd": ["true"],
        "test_cmd": ["true"],
        "integration_branch": integration_branch,
        "duplo_binary": "",
        "duplo_min_block_lines": 4,
        "lizard_binary": LIZARD,
        "lizard_language": "cpp",
    }))
    return cfg_path


def run_gate(worktree: Path, cfg_path: Path) -> dict:
    proc = subprocess.run(
        [
            sys.executable, str(EXP / "merge_gate" / "cli.py"),
            "--config", str(cfg_path),
        ],
        cwd=str(worktree), capture_output=True, text=True,
    )
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"no JSON from gate.\nout:\n{proc.stdout}\nerr:\n{proc.stderr}")


def do_run(repo: Path, work: Path, run_id: str, refactor_file: str, fn: str) -> dict:
    """Drive one full run: branch -> worktree -> refactor -> gate -> finalize."""
    branch = f"refactor/{run_id}"
    baseline_ref = resolve_baseline_ref(repo, "", "main")
    baseline_commit = prepare_run_branch(repo, branch, baseline_ref)

    wt = create_worktree(repo, work, "PROG_1", "src", base_ref=branch)
    for k, v in [("user.email", "v@e.com"), ("user.name", "verifier")]:
        git(["config", k, v], wt)
    cfg_path = write_gate_config(wt, repo, branch)

    start_penalty = measure(wt / "src")
    start_content = (wt / "src" / refactor_file).read_text()

    (wt / "src" / refactor_file).write_text(good(fn))
    git(["add", "-A"], wt)
    git(["commit", "-m", f"refactor {fn}"], wt)
    gate = run_gate(wt, cfg_path)

    remove_worktree(repo, wt)
    restored = restore_baseline(repo, "main", baseline_commit)
    return {
        "branch": branch, "baseline_commit": baseline_commit, "gate": gate,
        "start_penalty": start_penalty, "start_content": start_content,
        "restored": restored,
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        work = root / "work"
        work.mkdir()
        repo = setup_repo(root)
        baseline_commit = resolve_ref(repo, "main")
        baseline_penalty = measure(repo / "src")

        print("\n[1] baseline ref resolution")
        check("prefers origin/<main> when a remote exists",
              resolve_baseline_ref(repo, "", "main") == "origin/main")
        check("an explicit ref is honoured",
              resolve_baseline_ref(repo, "main", "main") == "main")
        try:
            resolve_baseline_ref(repo, "no/such/ref", "main")
            check("unresolvable ref raises", False)
        except ValueError:
            check("unresolvable ref raises", True)

        print("\n[2] a dirty repository is refused, not silently reset")
        (repo / "src" / "scratch.cpp").write_text("int x;\n")
        try:
            prepare_run_branch(repo, "refactor/dirty", "main")
            check("dirty repo refused", False)
        except RuntimeError as exc:
            check("dirty repo refused", "uncommitted" in str(exc))
        (repo / "src" / "scratch.cpp").unlink()

        print("\n[3] RUN 1")
        r1 = do_run(repo, work, "run1", "alpha.cpp", "alpha")
        print(f"      gate -> success={r1['gate']['success']} "
              f"{r1['gate']['penalty_before']:.1f} -> {r1['gate']['penalty_after']:.1f}")
        check("run 1 merged", r1["gate"]["success"] and r1["gate"]["merged"],
              r1["gate"].get("reason", ""))
        check("run 1 started from the baseline penalty",
              abs(r1["start_penalty"] - baseline_penalty) < 1e-9,
              f"{r1['start_penalty']:.2f}")
        check("run branch holds the refactoring",
              "alphaB0" in git(["show", f"{r1['branch']}:src/alpha.cpp"], repo).stdout)
        check("PRIMARY BRANCH NOT ADVANCED",
              resolve_ref(repo, "main") == baseline_commit,
              f"main={resolve_ref(repo,'main')[:8]} baseline={baseline_commit[:8]}")
        check("working directory restored to baseline", r1["restored"] is True)
        check("baseline file content restored on disk",
              "alphaB0" not in (repo / "src" / "alpha.cpp").read_text())
        check("working tree measures the baseline penalty again",
              abs(measure(repo / "src") - baseline_penalty) < 1e-9,
              f"{measure(repo / 'src'):.2f}")

        print("\n[4] RUN 2 — must start from the ORIGINAL baseline")
        r2 = do_run(repo, work, "run2", "beta.cpp", "beta")
        print(f"      gate -> success={r2['gate']['success']} "
              f"{r2['gate']['penalty_before']:.1f} -> {r2['gate']['penalty_after']:.1f}")
        check("run 2 merged", r2["gate"]["success"] and r2["gate"]["merged"],
              r2["gate"].get("reason", ""))
        check("RUN 2 BASELINE == RUN 1 BASELINE (no contamination)",
              abs(r2["start_penalty"] - r1["start_penalty"]) < 1e-9,
              f"run1={r1['start_penalty']:.2f} run2={r2['start_penalty']:.2f}")
        check("run 2 did NOT inherit run 1's refactoring",
              "alphaB0" not in r2["start_content"]
              and "alphaB0" not in git(["show", "refactor/run2:src/alpha.cpp"], repo).stdout)
        check("run 2 branched from the same commit",
              r2["baseline_commit"] == r1["baseline_commit"])
        check("gate saw identical penalty_before in both runs",
              abs(r1["gate"]["penalty_before"] - r2["gate"]["penalty_before"]) < 1e-9,
              f"{r1['gate']['penalty_before']:.2f} vs {r2['gate']['penalty_before']:.2f}")

        print("\n[5] both run branches survive as permanent records")
        check("run 1 branch still exists", ref_exists(repo, "refactor/run1"))
        check("run 2 branch still exists", ref_exists(repo, "refactor/run2"))
        check("the two branches diverge (independent runs)",
              resolve_ref(repo, "refactor/run1") != resolve_ref(repo, "refactor/run2"))
        check("each branch is one commit past the shared baseline",
              git(["rev-list", "--count", f"{baseline_commit}..refactor/run1"], repo
                  ).stdout.strip() == "1"
              and git(["rev-list", "--count", f"{baseline_commit}..refactor/run2"], repo
                      ).stdout.strip() == "1")
        check("history on the run branches is linear",
              git(["rev-list", "--merges", "refactor/run1"], repo).stdout.strip() == ""
              and git(["rev-list", "--merges", "refactor/run2"], repo).stdout.strip() == "")

        print("\n[6] resume preserves the accepted integration-branch commits")
        run1_head = resolve_ref(repo, r1["branch"])
        resumed_baseline = resume_run_branch(
            repo, r1["branch"], "main", fetch=False,
        )
        check("resume returns the original baseline",
              resumed_baseline == r1["baseline_commit"])
        check("resume checks out the existing run head",
              resolve_ref(repo, "HEAD") == run1_head)
        check("resume does not recreate the branch at baseline",
              run1_head != resumed_baseline)
        check("primary branch can still be restored",
              restore_baseline(repo, "main", resumed_baseline))

        print("\n[7] pushing the run branch (thesis §4.4 archival step)")
        pushed = push_branch(repo, "refactor/run1")
        check("push reports success against a real remote", pushed is True)
        remote_branches = git(["branch", "-r"], repo).stdout
        check("branch is present on origin", "origin/refactor/run1" in remote_branches,
              remote_branches.strip().replace("\n", " "))
        check("origin/main untouched by the run",
              resolve_ref(repo, "origin/main") == baseline_commit)


    print("\n[8] detached checkouts and explicit local artifacts are preserved")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = setup_repo(root)
        baseline = resolve_ref(repo, "HEAD")
        git(["checkout", "--detach", baseline], repo)
        restore_ref, restore_commit, detached = capture_checkout(repo)
        check("detached checkout identity is captured",
              detached and restore_ref == baseline and restore_commit == baseline)

        local_cache = repo / "local-cache"
        local_cache.mkdir()
        (local_cache / "artifact.txt").write_text("keep\n")
        check("unknown untracked paths still make the repository dirty",
              repo_is_dirty(repo))
        check("one exact allowed directory does not hide other changes",
              not repo_is_dirty(repo, ("local-cache",)))

        prepare_run_branch(
            repo, "refactor/detached", "HEAD",
            allowed_untracked_paths=("local-cache",),
        )
        check("exact detached commit is restored after a run",
              restore_checkout(repo, restore_ref, restore_commit, detached)
              and resolve_ref(repo, "HEAD") == baseline)
        check("allowed local artifact survives branch lifecycle",
              (local_cache / "artifact.txt").read_text() == "keep\n")

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
