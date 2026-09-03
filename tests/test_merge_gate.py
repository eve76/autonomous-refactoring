"""End-to-end verification of the paper-aligned merge gate.

Builds a throwaway git repo (bare origin + clone) whose C++ sources
violate the CCN/NLOC thresholds, then drives the real merge gate through
every path the thesis specifies:

  1. baseline measurement + breakdown invariant
  2. ACCEPT  — a genuine refactoring is merged fast-forward into main
  3. REJECT  — a worsening change is reverted to the pre-refactoring state
  4. GUARD   — the gate refuses uncommitted work instead of committing it
  5. CONCURRENCY — a programmer whose branch predates another's merge can
     still rebase onto the new main and fast-forward in
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from analysis.tools import run_static_analysis                      # noqa: E402
from analysis.penalty import (                                       # noqa: E402
    compute_total_penalty, compute_penalty_breakdown, format_breakdown,
)
from coordination.git_manager import create_worktree  # noqa: E402

def _find_lizard() -> str:
    """Prefer the lizard next to the running interpreter (virtualenv), then PATH."""
    candidate = Path(sys.executable).parent / "lizard"
    if candidate.exists():
        return str(candidate)
    return shutil.which("lizard") or "lizard"


LIZARD = _find_lizard()


THRESHOLDS = {"ccn": 15, "cognitive": 15, "nloc": 30, "param": 5, "duplicates": 0}
WEIGHTS = {"ccn": 1, "nloc": 1, "cognitive": 1, "param": 1, "duplicates": 1}


def bad(fn: str, n: int = 20) -> str:
    body = "\n".join(f"    if (a > {i} && b < {i * 2}) r += {i};" for i in range(1, n + 1))
    return (
        "#include <cstdio>\n\n"
        f"int {fn}(int a, int b) {{\n    int r = 0;\n{body}\n    return r;\n}}\n"
    )


def good(fn: str) -> str:
    parts = []
    for band in range(4):
        lo = band * 5 + 1
        lines = "\n".join(
            f"    if (a > {i} && b < {i * 2}) r += {i};" for i in range(lo, lo + 5)
        )
        parts.append(
            f"static int {fn}Band{band}(int a, int b) {{\n"
            f"    int r = 0;\n{lines}\n    return r;\n}}\n"
        )
    call = " + ".join(f"{fn}Band{b}(a, b)" for b in range(4))
    return (
        "#include <cstdio>\n\n"
        + "\n".join(parts)
        + f"\nint {fn}(int a, int b) {{\n    return {call};\n}}\n"
    )


def git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check, capture_output=True, text=True,
    )


def measure(target):
    lz, cog, dup = run_static_analysis(
        target, THRESHOLDS, duplo_binary="",
        lizard_binary=LIZARD, language="cpp",
    )
    return (
        compute_total_penalty(lz, cog, THRESHOLDS, dup.ratio, WEIGHTS),
        compute_penalty_breakdown(lz, cog, THRESHOLDS, dup.ratio, WEIGHTS),
    )


def setup_repo(root: Path) -> Path:
    origin, repo = root / "origin.git", root / "repo"
    origin.mkdir()
    git(["init", "--bare", "-b", "main"], cwd=origin)
    git(["clone", str(origin), str(repo)], cwd=root)
    git(["config", "user.email", "v@example.com"], cwd=repo)
    git(["config", "user.name", "verifier"], cwd=repo)
    src = repo / "src"
    src.mkdir()
    (src / "alpha.cpp").write_text(bad("alpha"))
    (src / "beta.cpp").write_text(bad("beta"))
    (src / "gamma.cpp").write_text(bad("gamma"))
    git(["add", "-A"], cwd=repo)
    git(["commit", "-m", "initial"], cwd=repo)
    git(["push", "-u", "origin", "main"], cwd=repo)
    return repo


def new_worktree(repo: Path, work: Path, name: str) -> Path:
    # This suite exercises the gate against `main` directly; the per-run
    # integration-branch protocol is covered by test_run_isolation.py.
    wt = create_worktree(repo, work, name, "src", base_ref="main")
    git(["config", "user.email", "v@example.com"], cwd=wt)
    git(["config", "user.name", "verifier"], cwd=wt)
    cfg_path = work / "gate_configs" / f"{name}.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "repo_root": str(repo),
        "target_subdir": "src",
        "thresholds": THRESHOLDS,
        "weights": WEIGHTS,
        "build_cmd": ["true"],
        "test_cmd": ["true"],
        "integration_branch": "main",
        "duplo_binary": "",
        "duplo_min_block_lines": 4,
        "lizard_binary": LIZARD,
        "lizard_language": "cpp",
        "attempt_log": str(work / "gate_attempts.jsonl"),
        "agent_id": name,
        "issue_history_dir": str(work / "issues"),
    }))
    GATE_CONFIGS[wt] = cfg_path
    return wt


GATE_CONFIGS: dict[Path, Path] = {}


def run_gate(worktree: Path, issue_id: str = "") -> dict:
    command = [
        sys.executable, str(EXP / "merge_gate" / "cli.py"),
        "--config", str(GATE_CONFIGS[worktree]),
    ]
    if issue_id:
        command.extend(["--issue-id", issue_id])
    proc = subprocess.run(
        command,
        cwd=str(worktree), capture_output=True, text=True,
    )
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"no JSON from gate.\nout:\n{proc.stdout}\nerr:\n{proc.stderr}")


FAILURES = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        work = root / "work"
        work.mkdir()
        repo = setup_repo(root)
        print("\n[1] baseline measurement + breakdown invariant")
        baseline, breakdown = measure(repo / "src")
        print(format_breakdown(breakdown))
        check("baseline penalty > 0", baseline > 0, f"{baseline:.2f}")
        summed = sum(v["penalty"] for v in breakdown.values())
        check("breakdown sums to total", abs(summed - baseline) < 1e-6,
              f"sum={summed:.6f} total={baseline:.6f}")

        print("\n[2] ACCEPT: genuine refactoring must fast-forward into main")
        wt1 = new_worktree(repo, work, "PROG_1")
        (wt1 / "src" / "alpha.cpp").write_text(good("alpha"))
        git(["add", "-A"], cwd=wt1)
        git(["commit", "-m", "refactor alpha"], cwd=wt1)
        r = run_gate(wt1)
        print(f"      {json.dumps(r)}")
        check("success", r["success"] is True, r.get("reason", ""))
        check("merged", r["merged"] is True)
        check("tests ran", r["tests_passed"] is True)
        check("penalty decreased", r["penalty_after"] < r["penalty_before"],
              f"{r['penalty_before']:.2f} -> {r['penalty_after']:.2f}")
        check("main has the refactoring", "alphaBand0" in (repo / "src" / "alpha.cpp").read_text())
        after1, _ = measure(repo / "src")
        check("main's measured penalty dropped", after1 < baseline, f"{baseline:.2f} -> {after1:.2f}")
        check("history stayed linear (ff-merge)",
              git(["rev-list", "--merges", "HEAD"], cwd=repo).stdout.strip() == "")

        print("\n[3] REJECT: worsening change must be reverted to pre-refactoring state")
        wt2 = new_worktree(repo, work, "PROG_2")
        (wt2 / "src" / "beta.cpp").write_text(bad("beta", n=30))
        git(["add", "-A"], cwd=wt2)
        git(["commit", "-m", "make beta worse"], cwd=wt2)
        r = run_gate(wt2)
        print(f"      {json.dumps(r)}")
        check("rejected", r["success"] is False)
        check("reported reverted", r["reverted"] is True, r.get("reason", ""))
        check("not merged", r["merged"] is False)
        reverted = (wt2 / "src" / "beta.cpp").read_text()
        check("worktree back at pre-refactoring beta", "a > 30" not in reverted)
        check("revert kept previously accepted work",
              "alphaBand0" in (wt2 / "src" / "alpha.cpp").read_text())
        check("main untouched", "a > 30" not in (repo / "src" / "beta.cpp").read_text())

        print("\n[3b] DEDUPE: identical issue patch is rejected before evaluation")
        wt_dup = new_worktree(repo, work, "PROG_DUP")
        (wt_dup / "src" / "beta.cpp").write_text(bad("beta", n=30))
        git(["add", "-A"], cwd=wt_dup)
        git(["commit", "-m", "duplicate candidate"], cwd=wt_dup)
        first = run_gate(wt_dup, "ISSUE-DUP")
        check("first patch evaluated normally",
              first["outcome"] == "penalty_rejected", first["outcome"])
        history = work / "issues" / "ISSUE-DUP" / "history.jsonl"
        patches = list((history.parent / "patches").glob("*.patch"))
        check("pre-revert patch and issue history saved",
              history.exists() and len(patches) == 1)
        (wt_dup / "src" / "beta.cpp").write_text(bad("beta", n=30))
        git(["add", "-A"], cwd=wt_dup)
        git(["commit", "-m", "same patch again"], cwd=wt_dup)
        duplicate = run_gate(wt_dup, "ISSUE-DUP")
        check("second identical patch rejected as duplicate",
              duplicate["outcome"] == "duplicate_patch",
              duplicate["outcome"])
        saved = [json.loads(line) for line in history.read_text().splitlines()]
        check("duplicate history points at the same patch",
              len(saved) == 2
              and saved[0]["patch_id"] == saved[1]["patch_id"])

        print("\n[4] GUARD: gate must not commit on the programmer's behalf")
        wt3 = new_worktree(repo, work, "PROG_3")
        cfg3 = json.loads(GATE_CONFIGS[wt3].read_text())
        cfg3["allowed_untracked_paths"] = ["src/beta.cpp"]
        GATE_CONFIGS[wt3].write_text(json.dumps(cfg3))
        (wt3 / "src" / "beta.cpp").write_text(good("beta"))   # edited, NOT committed
        r = run_gate(wt3)
        print(f"      {json.dumps(r)}")
        check("rejected for uncommitted work", r["success"] is False)
        check("reason names the uncommitted edits", "uncommitted" in r["reason"].lower(),
              r["reason"])
        check("gate created no commit",
              "betaBand0" not in git(["log", "-1", "--stat"], cwd=wt3).stdout)
        check("edit still present for the programmer to commit",
              "betaBand0" in (wt3 / "src" / "beta.cpp").read_text())
        check("allowlisting a path never exempts a tracked modification",
              r["outcome"] == "uncommitted")

        print("\n[4b] GUARD: exact untracked build byproducts may be allowed")
        wt_allowed = new_worktree(repo, work, "PROG_ALLOWED")
        cfg_allowed = json.loads(GATE_CONFIGS[wt_allowed].read_text())
        cfg_allowed["allowed_untracked_paths"] = ["MODULE.bazel.lock"]
        GATE_CONFIGS[wt_allowed].write_text(json.dumps(cfg_allowed))
        (wt_allowed / "MODULE.bazel.lock").write_text("generated\n")
        r_allowed = run_gate(wt_allowed)
        print(f"      {json.dumps(r_allowed)}")
        check("allowed untracked byproduct passes the dirty guard",
              r_allowed["outcome"] == "penalty_rejected",
              r_allowed.get("reason", ""))

        print("\n[5] CONCURRENCY: branch predating another merge must still merge")
        # Both worktrees branch off main BEFORE either one merges.
        wt4 = new_worktree(repo, work, "PROG_4")
        wt5 = new_worktree(repo, work, "PROG_5")
        base4 = git(["rev-parse", "HEAD"], cwd=wt4).stdout.strip()
        base5 = git(["rev-parse", "HEAD"], cwd=wt5).stdout.strip()
        check("both branches start from the same main", base4 == base5, base4[:8])

        (wt4 / "src" / "beta.cpp").write_text(good("beta"))
        git(["add", "-A"], cwd=wt4)
        git(["commit", "-m", "refactor beta"], cwd=wt4)
        r4 = run_gate(wt4)
        print(f"      PROG_4 -> {json.dumps(r4)}")
        check("first of the two merges", r4["success"] is True and r4["merged"] is True,
              r4.get("reason", ""))

        # PROG_5's branch is now behind main. It must rebase onto the new
        # main and still fast-forward in. This is what a stale
        # origin/main target used to make impossible.
        (wt5 / "src" / "gamma.cpp").write_text(good("gamma"))
        git(["add", "-A"], cwd=wt5)
        git(["commit", "-m", "refactor gamma"], cwd=wt5)
        main_before = git(["rev-parse", "main"], cwd=repo).stdout.strip()
        check("PROG_5 branch is behind main now",
              git(["merge-base", "--is-ancestor", "HEAD", "main"], cwd=wt5,
                  check=False).returncode != 0)
        r5 = run_gate(wt5)
        print(f"      PROG_5 -> {json.dumps(r5)}")
        check("second merge succeeded from a stale branch",
              r5["success"] is True and r5["merged"] is True, r5.get("reason", ""))
        check("second merge reduced the penalty", r5["penalty_after"] < r5["penalty_before"],
              f"{r5['penalty_before']:.2f} -> {r5['penalty_after']:.2f}")
        check("PROG_5 rebased onto PROG_4's merged main",
              "betaBand0" in (wt5 / "src" / "beta.cpp").read_text())
        check("main advanced past PROG_4's commit",
              git(["rev-parse", "main"], cwd=repo).stdout.strip() != main_before)
        check("main carries BOTH programmers' work",
              "betaBand0" in (repo / "src" / "beta.cpp").read_text()
              and "gammaBand0" in (repo / "src" / "gamma.cpp").read_text())
        check("history still linear after both merges",
              git(["rev-list", "--merges", "main"], cwd=repo).stdout.strip() == "")
        final, _ = measure(repo / "src")
        check("final penalty is zero (all violations refactored away)",
              final == 0.0, f"{final:.2f}")

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAILURES ({len(FAILURES)}): " + "; ".join(FAILURES))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
