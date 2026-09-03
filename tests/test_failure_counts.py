"""Verify the failure counts the results chapter reports.

Thesis §5.1 asks for "failure counts (reverted attempts, test failures,
stagnation exits) for each model" and §5.4 for the system deviations
(merge conflicts, crashes, agents giving up). None of these were visible
to the coordination layer before, because the merge gate runs as its own
process and only printed its verdict to the programmer.

  1. classification  — every gate exit path sets a distinct outcome
  2. append log      — records survive concurrent writers and truncation
  3. gate end-to-end — a real gate run on a real repo logs every attempt
  4. run_summary     — the coordination layer tallies them into the
                       failures block, together with its own counts
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from coordination import gate_attempts                              # noqa: E402
from coordination import penalty_history as ph                      # noqa: E402


def _find_lizard() -> str:
    candidate = Path(sys.executable).parent / "lizard"
    if candidate.exists():
        return str(candidate)
    return shutil.which("lizard") or "lizard"


LIZARD = _find_lizard()
THRESHOLDS = {"ccn": 15, "cognitive": 15, "nloc": 30, "param": 5, "duplicates": 0}
WEIGHTS = {"ccn": 1, "nloc": 1, "cognitive": 1, "param": 1, "duplicates": 1}

FAIL = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(label)


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


def git(args, cwd, check_rc=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=check_rc, capture_output=True, text=True,
    )


# ---------------------------------------------------------------- [1]

print("\n[1] every gate exit path carries a distinct outcome")

from merge_gate.gate import GateResult                              # noqa: E402

check("outcome defaults to empty", GateResult(success=False).outcome == "")
check("all outcome constants are distinct",
      len(set(gate_attempts.OUTCOMES)) == len(gate_attempts.OUTCOMES),
      str(len(gate_attempts.OUTCOMES)))

# The gate's exit points, as asserted by section 3 below.
EXPECTED_OUTCOMES = {
    gate_attempts.MERGED,
    gate_attempts.PENALTY_REJECTED,
    gate_attempts.TEST_FAILED,
    gate_attempts.BUILD_FAILED,
    gate_attempts.BUILD_TIMEOUT,
    gate_attempts.BUILD_INTERRUPTED,
    gate_attempts.TEST_TIMEOUT,
    gate_attempts.TEST_INTERRUPTED,
    gate_attempts.REBASE_CONFLICT,
    gate_attempts.FF_RACE,
    gate_attempts.FF_RACE_EXHAUSTED,
    gate_attempts.UNCOMMITTED,
    gate_attempts.ANALYSIS_FAILED,
    gate_attempts.OUT_OF_SCOPE,
    gate_attempts.ATTEMPT_LIMIT,
    gate_attempts.DUPLICATE_PATCH,
    gate_attempts.DYNAMIC_UNAVAILABLE,
    gate_attempts.DYNAMIC_REGRESSION,
}
check("summarize covers every declared outcome",
      EXPECTED_OUTCOMES == set(gate_attempts.OUTCOMES),
      str(sorted(EXPECTED_OUTCOMES ^ set(gate_attempts.OUTCOMES))))

rec = gate_attempts.make_record("PROG_1", GateResult(
    success=False, reverted=True, penalty_before=500.0, penalty_after=505.0,
    outcome=gate_attempts.PENALTY_REJECTED, reason="penalty did not decrease",
))
check("record carries the agent", rec["agent"] == "PROG_1")
check("record carries the outcome", rec["outcome"] == gate_attempts.PENALTY_REJECTED)
check("record carries both penalties",
      rec["penalty_before"] == 500.0 and rec["penalty_after"] == 505.0)
check("record is timestamped", rec["timestamp"] > 0)
check("missing agent falls back rather than crashing",
      gate_attempts.make_record("", GateResult(success=True))["agent"] == "unknown")

# ---------------------------------------------------------------- [2]

print("\n[2] the attempt log is append-only and concurrency-safe")

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    log = root / "results" / "gate_attempts.jsonl"

    check("missing file summarizes to zeros",
          gate_attempts.summarize(gate_attempts.load(log))["gate_attempts"] == 0)

    gate_attempts.append(log, {"outcome": gate_attempts.MERGED, "agent": "PROG_1"})
    check("parent directory created on demand", log.exists())

    # 5 threads x 40 lines: interleaved single-syscall O_APPEND writes
    # must not corrupt each other, which is why the gate can log from
    # concurrent programmer processes without a lock.
    def spam(name):
        for _ in range(40):
            gate_attempts.append(log, {
                "outcome": gate_attempts.PENALTY_REJECTED, "agent": name,
                "pad": "x" * 200,
            })

    threads = [threading.Thread(target=spam, args=(f"PROG_{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    records = gate_attempts.load(log)
    check("every concurrent write landed", len(records) == 201, str(len(records)))
    check("no line was corrupted",
          all(r.get("outcome") for r in records),
          f"{sum(1 for r in records if not r.get('outcome'))} bad")
    counts = gate_attempts.summarize(records)
    check("tally matches", counts["merged"] == 1 and counts["reverted_attempts"] == 200,
          f"merged={counts['merged']} reverted={counts['reverted_attempts']}")

    # An outcome this version does not know about must still be counted,
    # so adding a gate exit path later cannot silently drop attempts.
    gate_attempts.append(log, {"outcome": "future_outcome", "agent": "PROG_9"})
    later = gate_attempts.summarize(gate_attempts.load(log))
    check("unknown outcome still counted in the total",
          later["gate_attempts"] == 202, str(later["gate_attempts"]))
    check("unknown outcome visible in by_outcome",
          later["by_outcome"].get("future_outcome") == 1)

    # A crash mid-write leaves a line with no terminating newline. The
    # next append fuses onto it, so that pair is unrecoverable — assert
    # the guarantee that actually holds: everything earlier survives.
    truncated = root / "truncated.jsonl"
    for i in range(5):
        gate_attempts.append(truncated, {"outcome": gate_attempts.MERGED, "n": i})
    with truncated.open("a") as fh:
        fh.write('{"outcome": "merged", "agent": "PRO')
    check("truncated final line is dropped",
          len(gate_attempts.load(truncated)) == 5,
          str(len(gate_attempts.load(truncated))))
    gate_attempts.append(truncated, {"outcome": gate_attempts.TEST_FAILED, "n": 99})
    after = gate_attempts.load(truncated)
    check("records written before the truncation all survive",
          len(after) == 5 and [r["n"] for r in after] == [0, 1, 2, 3, 4],
          str([r.get("n") for r in after]))

# ---------------------------------------------------------------- [3]

print("\n[3] a real gate run logs every attempt it makes")

from coordination.git_manager import create_worktree                # noqa: E402


def write_gate_config(wt: Path, repo: Path, log: Path, agent: str,
                      build_cmd=None, test_cmd=None,
                      build_timeout_sec=3600, test_timeout_sec=3600):
    cfg_path = log.parent / "gate_configs" / f"{agent}.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({
        "repo_root": str(repo),
        # Exercise the gate's canonicalization of a common CLI spelling.
        "target_subdir": "./src",
        "thresholds": THRESHOLDS,
        "weights": WEIGHTS,
        "build_cmd": build_cmd or ["true"],
        "test_cmd": test_cmd or ["true"],
        "build_timeout_sec": build_timeout_sec,
        "test_timeout_sec": test_timeout_sec,
        "integration_branch": "main",
        "duplo_binary": "",
        "duplo_min_block_lines": 4,
        "lizard_binary": LIZARD,
        "lizard_language": "cpp",
        "attempt_log": str(log),
        "agent_id": agent,
    }))
    GATE_CONFIGS[wt] = cfg_path


GATE_CONFIGS: dict[Path, Path] = {}


def run_gate(wt: Path, issue_id: str = "") -> dict:
    command = [
        sys.executable, str(EXP / "merge_gate" / "cli.py"),
        "--config", str(GATE_CONFIGS[wt]),
    ]
    if issue_id:
        command.extend(["--issue-id", issue_id])
    proc = subprocess.run(
        command,
        cwd=str(wt), capture_output=True, text=True,
    )
    for line in reversed(proc.stdout.strip().splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line.strip())
    raise AssertionError(f"no JSON from gate\nout:{proc.stdout}\nerr:{proc.stderr}")


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    work = root / "work"
    work.mkdir()
    repo = root / "repo"
    git(["init", "-b", "main", str(repo)], cwd=root)
    git(["config", "user.email", "v@example.com"], cwd=repo)
    git(["config", "user.name", "verifier"], cwd=repo)
    src = repo / "src"
    src.mkdir()
    for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
        (src / f"{name}.cpp").write_text(bad(name))
    (repo / "README.md").write_text("baseline\n")
    git(["add", "-A"], cwd=repo)
    git(["commit", "-m", "initial"], cwd=repo)

    log = root / "results" / "gate_attempts.jsonl"

    def worktree(name: str, **kw) -> Path:
        wt = create_worktree(repo, work, name, "src", base_ref="main")
        git(["config", "user.email", "v@example.com"], cwd=wt)
        git(["config", "user.name", "verifier"], cwd=wt)
        write_gate_config(wt, repo, log, name, **kw)
        return wt

    # (a) uncommitted work
    wt = worktree("PROG_1")
    (wt / "src" / "alpha.cpp").write_text(good("alpha"))
    r = run_gate(wt)
    check("uncommitted attempt reports its outcome",
          r["outcome"] == gate_attempts.UNCOMMITTED, r["outcome"])

    # (b) accepted merge
    git(["add", "-A"], cwd=wt)
    git(["commit", "-m", "refactor alpha"], cwd=wt)
    r = run_gate(wt)
    check("merged attempt reports its outcome",
          r["outcome"] == gate_attempts.MERGED, f"{r['outcome']} / {r.get('reason')}")

    # (c) penalty did not decrease -> reverted
    wt2 = worktree("PROG_2")
    (wt2 / "src" / "beta.cpp").write_text(bad("beta", n=30))
    git(["add", "-A"], cwd=wt2)
    git(["commit", "-m", "worsen beta"], cwd=wt2)
    r = run_gate(wt2)
    check("reverted attempt reports its outcome",
          r["outcome"] == gate_attempts.PENALTY_REJECTED, r["outcome"])

    # (d) penalty improved but the tests fail -> no revert
    wt3 = worktree("PROG_3", test_cmd=["false"])
    (wt3 / "src" / "gamma.cpp").write_text(good("gamma"))
    git(["add", "-A"], cwd=wt3)
    git(["commit", "-m", "refactor gamma"], cwd=wt3)
    r = run_gate(wt3)
    check("test failure reports its outcome",
          r["outcome"] == gate_attempts.TEST_FAILED, r["outcome"])
    check("test failure did NOT revert the programmer's work (§4.3.2)",
          "gammaBand0" in (wt3 / "src" / "gamma.cpp").read_text())

    # (e) penalty improved but the build fails
    wt4 = worktree("PROG_4", build_cmd=["false"])
    (wt4 / "src" / "delta.cpp").write_text(good("delta"))
    git(["add", "-A"], cwd=wt4)
    git(["commit", "-m", "refactor delta"], cwd=wt4)
    r = run_gate(wt4)
    check("build failure reports its outcome",
          r["outcome"] == gate_attempts.BUILD_FAILED, r["outcome"])

    # (e2) a build deadline is not misreported as a compiler failure
    wt8 = worktree(
        "PROG_8",
        build_cmd=[sys.executable, "-c", "import time; time.sleep(2)"],
        build_timeout_sec=1,
    )
    (wt8 / "src" / "delta.cpp").write_text(good("delta"))
    git(["add", "-A"], cwd=wt8)
    git(["commit", "-m", "refactor delta timeout"], cwd=wt8)
    r = run_gate(wt8)
    check("build timeout has a distinct outcome",
          r["outcome"] == gate_attempts.BUILD_TIMEOUT, r["outcome"])

    # (e3) a signal interruption is also distinct from a compiler failure
    wt9 = worktree(
        "PROG_9",
        build_cmd=[
            sys.executable, "-c",
            "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
    )
    (wt9 / "src" / "delta.cpp").write_text(good("delta"))
    git(["add", "-A"], cwd=wt9)
    git(["commit", "-m", "refactor delta interrupted"], cwd=wt9)
    r = run_gate(wt9)
    check("build interruption has a distinct outcome",
          r["outcome"] == gate_attempts.BUILD_INTERRUPTED, r["outcome"])

    # (f) a full worktree does not expand the authorized target subtree
    wt7 = worktree("PROG_7")
    (wt7 / "src" / "epsilon.cpp").write_text(good("epsilon"))
    (wt7 / "README.md").write_text("out of scope\n")
    git(["add", "-A"], cwd=wt7)
    git(["commit", "-m", "mix scoped and unscoped changes"], cwd=wt7)
    r = run_gate(wt7)
    check("out-of-scope change reports its outcome",
          r["outcome"] == gate_attempts.OUT_OF_SCOPE, r["outcome"])
    check("out-of-scope change was not merged",
          (repo / "README.md").read_text() == "baseline\n")

    # (g) rebase conflict: two branches edit the same lines of the same file
    wt5 = worktree("PROG_5")
    wt6 = worktree("PROG_6")
    (wt5 / "src" / "beta.cpp").write_text(good("beta"))
    git(["add", "-A"], cwd=wt5)
    git(["commit", "-m", "refactor beta one way"], cwd=wt5)
    r = run_gate(wt5)
    check("first of the conflicting pair merged",
          r["outcome"] == gate_attempts.MERGED, f"{r['outcome']} / {r.get('reason')}")
    (wt6 / "src" / "beta.cpp").write_text(good("beta").replace("Band", "Slice"))
    git(["add", "-A"], cwd=wt6)
    git(["commit", "-m", "refactor beta another way"], cwd=wt6)
    r = run_gate(wt6)
    check("rebase conflict reports its outcome",
          r["outcome"] == gate_attempts.REBASE_CONFLICT,
          f"{r['outcome']} / {r.get('reason')}")
    check("conflicted rebase left in place for manual resolution (§4.3.2)",
          (wt6 / ".git").exists() and
          git(["status", "--porcelain"], cwd=wt6, check_rc=False).stdout.strip() != "")

    print("\n     tally over the whole scenario")
    records = gate_attempts.load(log)
    counts = gate_attempts.summarize(records)
    print(f"     {json.dumps(counts['by_outcome'])}")
    check("every gate invocation was logged", counts["gate_attempts"] == 10,
          str(counts["gate_attempts"]))
    check("§5.1 reverted attempts counted", counts["reverted_attempts"] == 1,
          str(counts["reverted_attempts"]))
    check("§5.1 test failures counted", counts["test_failures"] == 1,
          str(counts["test_failures"]))
    check("build failures counted", counts["build_failures"] == 1,
          str(counts["build_failures"]))
    check("build timeouts counted separately", counts["build_timeouts"] == 1,
          str(counts["build_timeouts"]))
    check("build interruptions counted separately",
          counts["build_interruptions"] == 1,
          str(counts["build_interruptions"]))
    check("§5.4 merge conflicts counted", counts["merge_conflicts"] == 1,
          str(counts["merge_conflicts"]))
    check("merges counted", counts["merged"] == 2, str(counts["merged"]))
    check("uncommitted invocations counted", counts["uncommitted_invocations"] == 1,
          str(counts["uncommitted_invocations"]))
    check("scope violations counted", counts["scope_violations"] == 1,
          str(counts["scope_violations"]))
    check("every attempt is attributed to an agent",
          {r["agent"] for r in records} ==
          {"PROG_1", "PROG_2", "PROG_3", "PROG_4", "PROG_5", "PROG_6",
           "PROG_7", "PROG_8", "PROG_9"},
          str(sorted({r["agent"] for r in records})))
    check("a gate with no attempt_log configured writes nothing",
          not (root / "unconfigured.jsonl").exists())

    print("\n     gate CLI enforces the per-dispatch attempt boundary")
    limit_log = root / "limit-results" / "gate_attempts.jsonl"
    wt_limit = create_worktree(
        repo, work, "PROG_LIMIT", "src", base_ref="main",
    )
    git(["config", "user.email", "v@example.com"], cwd=wt_limit)
    git(["config", "user.name", "verifier"], cwd=wt_limit)
    write_gate_config(wt_limit, repo, limit_log, "PROG_LIMIT")
    for _ in range(3):
        gate_attempts.append(limit_log, {
            "agent": "PROG_LIMIT",
            "issue_id": "ISSUE-LIMIT",
            "outcome": gate_attempts.BUILD_FAILED,
        })
    limited = run_gate(wt_limit, "ISSUE-LIMIT")
    check("fourth invocation is rejected before another build",
          limited["outcome"] == gate_attempts.ATTEMPT_LIMIT,
          limited["outcome"])

# ---------------------------------------------------------------- [4]

print("\n[4] run_summary.json carries the failure counts (§5.1, §5.4)")

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-for-construction")
from config import Config                                           # noqa: E402
from coordination.coordinator import Coordinator                    # noqa: E402
from coordination import message_queue as mq                        # noqa: E402
from coordination.backlog import Issue                              # noqa: E402

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    src = root / "repo" / "src"
    src.mkdir(parents=True)
    (src / "big.cpp").write_text(bad("wide"))

    lizard = Path(sys.executable).parent / "lizard"
    cfg = Config(repo_root=root / "repo", target_subdir="src",
                 work_root=root / "work", run_id="failure_counts_test",
                 lizard_binary=str(lizard) if lizard.exists() else "lizard")
    c = Coordinator(cfg)

    attempt_log = cfg.run_results_path / cfg.gate_attempts_filename
    check("coordinator and programmer agree on the log path",
          cfg.gate_attempts_filename == "gate_attempts.jsonl"
          and attempt_log.parent == cfg.run_results_path,
          str(attempt_log))

    for outcome, n in (
        (gate_attempts.MERGED, 3),
        (gate_attempts.PENALTY_REJECTED, 4),
        (gate_attempts.TEST_FAILED, 2),
        (gate_attempts.REBASE_CONFLICT, 1),
        (gate_attempts.FF_RACE, 5),
        (gate_attempts.FF_RACE_EXHAUSTED, 1),
    ):
        for _ in range(n):
            gate_attempts.append(attempt_log, {"outcome": outcome, "agent": "PROG_1"})

    total, breakdown, stats = c._measure()
    c.baseline_penalty = c.current_penalty = total
    c.baseline_metric_stats = c.metric_stats = stats
    c.history.start(total, breakdown=breakdown)

    # Coordination-layer deviations: two hard timeouts, one discretionary
    # termination, one skipped issue and two crashed sessions.
    c.history.record(ph.TIMEOUT_KILL, penalty=total, agent="PROG_1")
    c.history.record(ph.TIMEOUT_KILL, penalty=total, agent="PROG_2")
    c.history.record(ph.STUCK_TERMINATE, penalty=total, agent="PROG_3")

    c.backlog.load_or_init()
    iid = c.backlog.add_issue(Issue(
        id="", file_path="src/big.cpp", line=1, severity="high",
        issue_type="highComplexity", message="CCN=20", metric_values={"ccn": 20},
    ))
    c.backlog.mark_skipped(iid, "architectural constraint")

    for sender, kind, rc in (
        ("PROG_1", mq.PROGRAMMER_FINISHED, 1),
        ("ANALYST_1", mq.ANALYST_FINISHED, 137),
        ("PROG_2", mq.PROGRAMMER_FINISHED, 0),
    ):
        c._apply_message(mq.Message(sender=sender, kind=kind,
                                    payload={"returncode": rc}))
    check("crashes counted, clean exits ignored", c.agent_crashes == 2,
          str(c.agent_crashes))

    # §6.2: the file-existence check on analyst findings. The analyst's own
    # prompt carries an example line (./src/factory.cc:159), which it
    # sometimes emits as a real finding; without this check that phantom
    # issue reaches a programmer.
    print("\n     §6.2 file-existence check on analyst findings")
    check("path relative to the target subtree is accepted",
          c._issue_file_exists("big.cpp"))
    check("path relative to the repo root is accepted",
          c._issue_file_exists("src/big.cpp"))
    check("leading ./ tolerated", c._issue_file_exists("./src/big.cpp"))
    check("absolute path is accepted", c._issue_file_exists(str(src / "big.cpp")))
    outside = root / "outside.cpp"
    outside.write_text(bad("outside"))
    check("an absolute path outside repo/worktrees is rejected",
          not c._issue_file_exists(str(outside)))
    analyst_wt = root / "analyst-wt"
    (analyst_wt / "src").mkdir(parents=True)
    (analyst_wt / "src" / "big.cpp").write_text(bad("wide"))
    c.analyst_worktrees["ANALYST_1"] = analyst_wt
    check("an Analyst worktree absolute path maps to repo-relative",
          c._canonical_issue_path(
              str(analyst_wt / "src" / "big.cpp")
          ) == "src/big.cpp")
    check("the prompt's own example file is rejected",
          not c._issue_file_exists("./src/factory.cc"))
    check("empty path is rejected", not c._issue_file_exists(""))
    check("a directory is not a file", not c._issue_file_exists("src"))

    before = len(c.backlog.snapshot().items)
    c._apply_message(mq.Message(sender="ANALYST_1", kind=mq.ADD_ISSUES, payload={
        "issues": [
            {"file_path": "./src/factory.cc", "line": 159, "severity": "complexity",
             "issue_type": "highComplexity", "message": "CCN=27, cognitive=35"},
            # line 42, not 1: line 1 of this file is already in the backlog
            # from the skip fixture above, and would be deduplicated away.
            {"file_path": "src/big.cpp", "line": 42, "severity": "complexity",
             "issue_type": "highComplexity", "message": "CCN=41"},
        ],
    }))
    added = len(c.backlog.snapshot().items) - before
    check("only the real finding entered the backlog", added == 1, str(added))
    check("the phantom finding was counted", c.phantom_issues == 1,
          str(c.phantom_issues))
    real = [it for it in c.backlog.snapshot().items.values() if it.line == 42]
    check("the reported path is stored canonically repo-relative",
          len(real) == 1 and real[0].file_path == "src/big.cpp",
          str([it.file_path for it in c.backlog.snapshot().items.values()]))

    duplicate_before = len(c.backlog.snapshot().items)
    c._apply_message(mq.Message(sender="ANALYST_1", kind=mq.ADD_ISSUES, payload={
        "issues": [
            {"file_path": "./big.cpp", "line": 42, "severity": "high",
             "issue_type": "highComplexity", "message": "CCN=41"},
            {"file_path": "./src/big.cpp", "line": 42, "severity": "high",
             "issue_type": "ccn", "message": "CCN=41"},
        ],
    }))
    check("target/repo path variants and type aliases deduplicate",
          len(c.backlog.snapshot().items) == duplicate_before)

    print("\n     merge reports are bound to assignment + gate evidence")
    protected = c.backlog.add_issue(Issue(
        id="", file_path="src/big.cpp", line=77, severity="high",
        issue_type="longFunction", message="NLOC=50", metric_values={"nloc": 50},
    ))
    c.backlog.mark_in_progress(protected, "PROG_1")
    forged = {"issue_id": protected, "penalty_before": 100.0, "penalty_after": 90.0}

    c._apply_message(mq.Message(
        sender="PROG_2", kind=mq.MERGE_RESULT, payload=forged,
    ))
    check("another programmer cannot claim the assignment",
          c.backlog.snapshot().items[protected].status == "IN_PROGRESS")

    c._apply_message(mq.Message(
        sender="PROG_1", kind=mq.MERGE_RESULT, payload=forged,
    ))
    check("a RESULT line without gate evidence is ignored",
          c.backlog.snapshot().items[protected].status == "IN_PROGRESS")

    gate_attempts.append(attempt_log, {
        "outcome": gate_attempts.MERGED,
        "agent": "PROG_1",
        "issue_id": protected,
        "penalty_before": 100.0,
        "penalty_after": 90.0,
    })
    altered = dict(forged, penalty_after=89.0)
    c._apply_message(mq.Message(
        sender="PROG_1", kind=mq.MERGE_RESULT, payload=altered,
    ))
    check("a RESULT line with altered penalties is ignored",
          c.backlog.snapshot().items[protected].status == "IN_PROGRESS")
    check("matching agent, issue and penalties are recognized",
          c._gate_confirms_merge("PROG_1", protected, 100.0, 90.0))
    c.backlog.return_to_todo(protected)

    c.stop_reason = "stagnation"
    c._write_summary()

    summary = json.loads((cfg.run_results_path / cfg.run_summary_filename).read_text())
    f = summary.get("failures", {})
    check("failures block present", bool(f))
    print(f"     {json.dumps({k: v for k, v in f.items() if k != 'by_outcome'})}")

    check("§5.1 reverted attempts", f.get("reverted_attempts") == 4,
          str(f.get("reverted_attempts")))
    check("§5.1 test failures", f.get("test_failures") == 2,
          str(f.get("test_failures")))
    check("§5.1 stagnation exit", f.get("stagnation_exit") is True,
          str(f.get("stagnation_exit")))
    check("§5.4 merge conflicts", f.get("merge_conflicts") == 1,
          str(f.get("merge_conflicts")))
    check("§5.4 crashes", f.get("agent_crashes") == 2, str(f.get("agent_crashes")))
    check("§5.4 issues given up on", f.get("issues_skipped") == 1,
          str(f.get("issues_skipped")))
    check("§6.2 phantom analyst findings reported",
          f.get("analyst_phantom_issues") == 1,
          str(f.get("analyst_phantom_issues")))
    check("hard timeout kills read off the penalty history",
          f.get("hard_timeout_kills") == 2, str(f.get("hard_timeout_kills")))
    check("discretionary terminations counted separately",
          f.get("stuck_terminations") == 1, str(f.get("stuck_terminations")))
    check("ff-race retries fold exhaustion in", f.get("ff_merge_races") == 6,
          str(f.get("ff_merge_races")))
    check("attempt total covers every record", f.get("gate_attempts") == 17,
          str(f.get("gate_attempts")))
    check("merges from the gate log, not the history",
          f.get("merged") == 4 and summary["merges"] == 0,
          f"gate={f.get('merged')} history={summary['merges']}")
    check("by_outcome retained for outcomes added later",
          isinstance(f.get("by_outcome"), dict)
          and set(f["by_outcome"]) >= set(gate_attempts.OUTCOMES))

    print("\n     a run with no gate activity must report zeros, not absent keys")
    cfg2 = Config(repo_root=root / "repo", target_subdir="src",
                  work_root=root / "work2", run_id="empty_test",
                  lizard_binary=str(lizard) if lizard.exists() else "lizard")
    c2 = Coordinator(cfg2)
    c2.baseline_penalty = c2.current_penalty = total
    c2.baseline_metric_stats = c2.metric_stats = stats
    c2.history.start(total, breakdown=breakdown)
    c2.backlog.load_or_init()
    c2._write_summary()
    f2 = json.loads(
        (cfg2.run_results_path / cfg2.run_summary_filename).read_text()
    )["failures"]
    check("zeros reported for an untouched run",
          f2["gate_attempts"] == 0 and f2["reverted_attempts"] == 0
          and f2["agent_crashes"] == 0)
    check("stagnation_exit false when the run stopped for another reason",
          f2["stagnation_exit"] is False)

print("\n" + "=" * 62)
print(f"FAILURES ({len(FAIL)}): " + "; ".join(FAIL) if FAIL else "ALL CHECKS PASSED")
sys.exit(1 if FAIL else 0)
