"""Verify the two-tier stuck-agent policy the thesis specifies.

Paper contract:
  - past the 10-minute per-issue mark the ORCHESTRATOR decides
    terminate/keep and may mark issues infeasible;
  - past the 30-minute hard timeout the coordination layer kills
    unconditionally, WITHOUT asking;
  - only the hard timeout increments the stagnation counter.
"""
import json, os, sys, tempfile, time
from pathlib import Path
from queue import Queue

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-for-construction")
EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from config import Config
from coordination.coordinator import Coordinator
from coordination.backlog import Issue, TODO, IN_PROGRESS, SKIPPED
from agents.orchestrator import StuckDecision

FAIL = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond: FAIL.append(label)

class FakeSession:
    def __init__(self, runtime, issues, gate_active=False, gate_runtime=0):
        self._rt, self.assigned_issues = runtime, list(issues)
        self._gate_active, self._gate_runtime = gate_active, gate_runtime
        self.killed = False
        self.gate_record_start = 0
    def runtime_sec(self): return self._rt
    def edits_made(self): return True
    def gate_invocations(self): return 2
    def gate_active(self): return self._gate_active
    def gate_runtime_sec(self): return self._gate_runtime
    def tail_log(self, max_chars=4000): return "…looping on the same edit…"
    def kill(self): self.killed = True

class FakeOrch:
    def __init__(self, decision): self.decision, self.calls = decision, 0
    def assign(self, **kw): raise AssertionError("assign must not be called here")
    def evaluate_stuck(self, stuck, stagnation, hard_timeout_sec):
        self.calls += 1
        self.seen = stuck
        return self.decision

def make_coord(tmp, sessions, decision):
    cfg = Config(repo_root=Path(tmp)/"repo", target_subdir=".", work_root=Path(tmp)/"work",
                 run_id="test")
    c = Coordinator(cfg)
    c.orchestrator = FakeOrch(decision)
    c.programmer_sessions = dict(sessions)
    c.programmer_futures = {k: object() for k in sessions}
    c.programmer_worktrees = {k: Path(tmp)/"nonexistent" for k in sessions}
    c.current_penalty = 500.0
    line_no = 0
    for pid, s in sessions.items():
        for iid in s.assigned_issues:
            line_no += 10   # distinct (file, line, type) so dedup keeps both
            c.backlog.add_issue(Issue(id=iid, file_path="a.cc", line=line_no, severity="s",
                                      issue_type="t", message="m", metric_values={},
                                      status=IN_PROGRESS, assigned_to=pid))
    return c, cfg

with tempfile.TemporaryDirectory() as td:
    print("\n[1] below the 10-minute mark: orchestrator must NOT be consulted")
    s = {"PROG_1": FakeSession(300, ["ISSUE-0001"])}
    c, cfg = make_coord(td, s, StuckDecision())
    c._check_stuck_agents()
    check("orchestrator not called", c.orchestrator.calls == 0)
    check("agent alive", s["PROG_1"].killed is False)
    check("stagnation untouched", c.stagnation.counter == 0)

    print("\n[2] past 10 min: orchestrator consulted; 'keep' verdict spares the agent")
    s = {"PROG_1": FakeSession(700, ["ISSUE-0001"])}
    c, _ = make_coord(td, s, StuckDecision(keep=["PROG_1"]))
    c._check_stuck_agents()
    check("orchestrator consulted once", c.orchestrator.calls == 1)
    check("context included runtime + edits + gate count",
          c.orchestrator.seen[0]["runtime_sec"] == 700
          and c.orchestrator.seen[0]["edits_made"] is True
          and c.orchestrator.seen[0]["gate_invocations"] == 2)
    check("recent log passed to orchestrator", "looping" in c.orchestrator.seen[0]["recent_log"])
    check("agent kept alive", s["PROG_1"].killed is False)
    check("stagnation untouched by keep", c.stagnation.counter == 0)

    print("\n[3] past 10 min: 'terminate' verdict kills WITHOUT a stagnation tick")
    s = {"PROG_1": FakeSession(700, ["ISSUE-0001", "ISSUE-0002"])}
    c, _ = make_coord(td, s, StuckDecision(terminate=["PROG_1"],
                                           infeasible_issues=["ISSUE-0002"]))
    c._check_stuck_agents()
    check("agent killed", s["PROG_1"].killed is True)
    check("discretionary kill does NOT tick stagnation", c.stagnation.counter == 0,
          f"counter={c.stagnation.counter}")
    snap = c.backlog.snapshot()
    check("unfinished issue returned to TODO", snap.items["ISSUE-0001"].status == TODO)
    check("infeasible issue marked SKIPPED", snap.items["ISSUE-0002"].status == SKIPPED,
          snap.items["ISSUE-0002"].status)

    print("\n[4] past 30 min: killed unconditionally, orchestrator NOT consulted, stagnation ticks")
    s = {"PROG_1": FakeSession(1900, ["ISSUE-0001"])}
    c, _ = make_coord(td, s, StuckDecision(keep=["PROG_1"]))  # a 'keep' must be ignored
    c._check_stuck_agents()
    check("orchestrator not consulted at hard timeout", c.orchestrator.calls == 0)
    check("agent killed regardless of any keep verdict", s["PROG_1"].killed is True)
    check("hard timeout ticks stagnation", c.stagnation.counter == 1,
          f"counter={c.stagnation.counter}")
    check("issue returned to TODO", c.backlog.snapshot().items["ISSUE-0001"].status == TODO)
    c._check_stuck_agents()
    check("hard timeout termination is idempotent",
          c.stagnation.counter == 1
          and c.history.event_count("timeout_kill") == 1)

    print("\n[4b] an active merge gate has an independent timeout budget")
    s = {"PROG_1": FakeSession(
        1900, ["ISSUE-0001"], gate_active=True, gate_runtime=300,
    )}
    c, _ = make_coord(td, s, StuckDecision(terminate=["PROG_1"]))
    c._check_stuck_agents()
    check("programmer hard timeout pauses during active gate",
          s["PROG_1"].killed is False)
    check("stuck orchestrator does not terminate active validation",
          c.orchestrator.calls == 0)
    check("active validation does not tick stagnation",
          c.stagnation.counter == 0)

    s = {"PROG_1": FakeSession(
        1900, ["ISSUE-0001"], gate_active=True,
        gate_runtime=0,
    )}
    c, _ = make_coord(td, s, StuckDecision())
    s["PROG_1"]._gate_runtime = c.cfg.gate_timeout_sec + 1
    c._check_stuck_agents()
    check("overall gate safety timeout still terminates",
          s["PROG_1"].killed is True)

    print("\n[5] evaluation is throttled per agent")
    s = {"PROG_1": FakeSession(700, ["ISSUE-0001"])}
    c, _ = make_coord(td, s, StuckDecision(keep=["PROG_1"]))
    c._check_stuck_agents(); c._check_stuck_agents(); c._check_stuck_agents()
    check("consulted only once across rapid ticks", c.orchestrator.calls == 1,
          f"calls={c.orchestrator.calls}")

    print("\n[6] stagnation semantics (paper: >10 resets, <=10 ticks, 3 stops)")
    from coordination.stagnation import StagnationTracker
    t = StagnationTracker(min_merge_gain=10.0, limit=3)
    t.record_merge(30.0);  check("high-gain merge keeps counter at 0", t.counter == 0)
    t.record_merge(5.0);   check("low-gain merge ticks", t.counter == 1)
    t.record_merge(10.0);  check("exactly 10 counts as low gain", t.counter == 2)
    check("not stopped at 2", t.should_stop() is False)
    t.record_merge(40.0);  check("high-gain merge resets counter", t.counter == 0)
    t.record_merge(1.0); t.record_timeout(); t.record_merge(2.0)
    check("three low-gain events stop the system", t.should_stop() is True, f"counter={t.counter}")

    print("\n[7] repeated empty analyst scans stop instead of looping forever")
    c, _ = make_coord(td, {}, StuckDecision())
    c.empty_analyst_scans = c.cfg.empty_scan_limit
    c._check_no_actionable_stop()
    check("empty discovery exhaustion sets a terminal reason",
          c.stop_reason == "no_actionable_work", c.stop_reason)

    print("\n[8] a durable gate merge is recovered before timeout cleanup")
    from coordination import gate_attempts
    s = {"PROG_1": FakeSession(1900, ["ISSUE-0001"])}
    c, cfg = make_coord(td, s, StuckDecision())
    gate_attempts.append(
        cfg.run_results_path / cfg.gate_attempts_filename,
        {
            "agent": "PROG_1",
            "issue_id": "ISSUE-0001",
            "outcome": gate_attempts.MERGED,
            "penalty_before": 100.0,
            "penalty_after": 80.0,
        },
    )
    c._apply_merge_result = lambda msg: c.backlog.mark_done(
        msg.payload["issue_id"]
    )
    c._check_stuck_agents()
    check("merged issue remains DONE",
          c.backlog.snapshot().items["ISSUE-0001"].status == "DONE")
    check("recovered merge does not count as a timeout",
          c.stagnation.counter == 0
          and c.history.event_count("timeout_kill") == 0)

    print("\n[8b] merge persisted between pre-kill check and kill is recovered")
    s = {"PROG_1": FakeSession(1900, ["ISSUE-0001"])}
    c, cfg = make_coord(td, s, StuckDecision())
    c._apply_merge_result = lambda msg: c.backlog.mark_done(
        msg.payload["issue_id"]
    )
    original_recover = c._recover_active_gate_merges
    recovery_calls = [0]
    def staged_recovery():
        recovery_calls[0] += 1
        if recovery_calls[0] == 2:
            gate_attempts.append(
                cfg.run_results_path / cfg.gate_attempts_filename,
                {
                    "agent": "PROG_1", "issue_id": "ISSUE-0001",
                    "outcome": gate_attempts.MERGED,
                    "penalty_before": 100.0, "penalty_after": 80.0,
                },
            )
        return original_recover()
    c._recover_active_gate_merges = staged_recovery
    c._check_stuck_agents()
    check("post-check durable merge remains DONE",
          c.backlog.snapshot().items["ISSUE-0001"].status == "DONE")
    check("post-check merge is not counted as timeout",
          c.stagnation.counter == 0
          and c.history.event_count("timeout_kill") == 0)

    print("\n[9] Analyst hard timeout is bounded and idempotent")
    class FakeAnalyst:
        def __init__(self):
            self.killed = False
        def runtime_sec(self): return 9999
        def kill(self): self.killed = True
    analyst = FakeAnalyst()
    c, cfg = make_coord(td, {}, StuckDecision())
    c.analyst_sessions = {"ANALYST_1": analyst}
    c.analyst_futures = {"ANALYST_1": object()}
    c.analyst_lead_inflight = {"ANALYST_1": {"lead"}}
    c._check_stuck_agents()
    c._check_stuck_agents()
    check("Analyst killed once and lead reservation released",
          analyst.killed and "ANALYST_1" not in c.analyst_lead_inflight)
    check("Analyst timeout counted once",
          c.history.event_count("analyst_timeout") == 1)

    print("\n[10] baseline build prewarms before dispatch and records evidence")
    repo = Path(td) / "prewarm-repo"
    repo.mkdir()
    cfg = Config(
        repo_root=repo, target_subdir=".", work_root=Path(td) / "prewarm-work",
        run_id="prewarm", prewarm_build_cache=True,
        build_cmd=[sys.executable, "-c", "print('baseline cache warm')"],
        test_cmd=[sys.executable, "-c", "pass"],
    )
    c = Coordinator(cfg)
    c._prewarm_baseline_build(None)
    check("baseline prewarm passed", c.state.baseline_build_status == "passed")
    check("baseline prewarm log is preserved",
          "baseline cache warm" in cfg.baseline_build_log_path.read_text())
    check("ephemeral baseline output base is removed",
          not cfg.baseline_build_output_base_path.exists())

    print("\n[11] completed gate time remains excluded from model timeout")
    from agents.programmer import ProgrammerSession
    cfg.gate_status_dir.mkdir(parents=True, exist_ok=True)
    session = ProgrammerSession("PROG_1", repo, cfg, Queue())
    session.started_at = time.time() - 2000
    (cfg.gate_status_dir / "PROG_1.json").write_text(json.dumps({
        "active": False,
        "pid": os.getpid(),
        "accumulated_sec": 500,
    }))
    check("completed validation is subtracted from effective runtime",
          1495 <= session.runtime_sec() <= 1505,
          f"runtime={session.runtime_sec():.1f}")

print("\n" + "="*62)
print(f"FAILURES ({len(FAIL)}): " + "; ".join(FAIL) if FAIL else "ALL CHECKS PASSED")
sys.exit(1 if FAIL else 0)
