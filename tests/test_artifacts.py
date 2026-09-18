"""Verify the reproduction artefacts: penalty history, plot, run state."""
import json, sys, tempfile
from pathlib import Path

EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from coordination import penalty_history as ph
from coordination.penalty_history import PenaltyHistory
from coordination.run_state import RunState

FAIL = []
def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not cond: FAIL.append(label)

with tempfile.TemporaryDirectory() as td:
    root = Path(td)

    print("\n[1] penalty history records a run timeline")
    h = PenaltyHistory(path=root / "results" / "penalty_history.json")
    h.start(500.0, breakdown={"ccn": {"penalty": 500.0, "violations": 9, "worst": 41}})
    h.record(ph.MERGE, penalty=470.0, issue_id="ISSUE-0001", agent="PROG_1",
             gate_reduction=30.0, stagnation_counter=0)
    h.record(ph.MERGE, penalty=465.0, issue_id="ISSUE-0002", agent="PROG_2",
             gate_reduction=5.0, stagnation_counter=1)
    h.record(ph.TIMEOUT_KILL, penalty=465.0, agent="PROG_3", runtime_sec=1801.0,
             stagnation_counter=2)
    h.record(ph.STOP, penalty=465.0, stop_reason="stagnation")

    check("history file written", (root / "results" / "penalty_history.json").exists())
    data = json.loads((root / "results" / "penalty_history.json").read_text())
    check("baseline recorded", data["baseline_penalty"] == 500.0)
    check("merge count correct", data["merges"] == 2, str(data["merges"]))
    check("all 5 events present", len(data["events"]) == 5, str(len(data["events"])))
    check("total_reduction tracked", data["events"][2]["total_reduction"] == 35.0,
          str(data["events"][2]["total_reduction"]))
    check("elapsed_sec present on every event",
          all("elapsed_sec" in e for e in data["events"]))
    check("merge carries issue + agent",
          data["events"][1]["issue_id"] == "ISSUE-0001" and data["events"][1]["agent"] == "PROG_1")
    check("stop_reason captured", data["events"][-1]["stop_reason"] == "stagnation")
    loaded = PenaltyHistory.load(root / "results" / "penalty_history.json")
    check("history reload preserves all prior events",
          loaded is not None and loaded.merge_count() == 2
          and len(loaded.events) == 5)
    loaded.record(ph.MERGE, penalty=460.0, issue_id="ISSUE-0003")
    check("resumed history appends instead of overwriting",
          len(PenaltyHistory.load(loaded.path).events) == 6)

    print("\n[2] penalty plot generation")
    ok = h.plot(root / "results" / "penalty.png")
    check("plot reported success", ok is True)
    png = root / "results" / "penalty.png"
    check("png exists and is non-trivial", png.exists() and png.stat().st_size > 5000,
          f"{png.stat().st_size if png.exists() else 0} bytes")
    check("png magic bytes", png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n")

    print("\n[3] crash-recovery state round-trip")
    s = RunState(run_id="20260729_101500", started_at=1.0,
                 baseline_commit="0123456789abcdef", baseline_penalty=500.0,
                 current_penalty=465.0, stagnation_counter=2, merges=2,
                 optimization_core_fingerprint="core-fingerprint",
                 provider_transitions=[{
                     "from_provider": "subscription",
                     "to_provider": "openrouter",
                 }])
    s.save(root / "run_state.json")
    check("state file written", (root / "run_state.json").exists())
    back = RunState.load(root / "run_state.json")
    check("baseline survives round-trip", back.baseline_penalty == 500.0)
    check("immutable baseline commit survives round-trip",
          back.baseline_commit == "0123456789abcdef")
    check("stagnation survives round-trip", back.stagnation_counter == 2)
    check("merges survive round-trip", back.merges == 2)
    check("run_id survives round-trip", back.run_id == "20260729_101500")
    check("core fingerprint survives round-trip",
          back.optimization_core_fingerprint == "core-fingerprint")
    check("provider transition survives round-trip",
          back.provider_transitions[0]["to_provider"] == "openrouter")
    check("updated_at stamped", back.updated_at > 0)
    check("missing file returns None", RunState.load(root / "nope.json") is None)
    (root / "corrupt.json").write_text("{not json")
    check("corrupt file returns None", RunState.load(root / "corrupt.json") is None)
    (root / "extra.json").write_text(json.dumps({"baseline_penalty": 1.0, "bogus_field": 2}))
    check("unknown fields ignored on load",
          RunState.load(root / "extra.json").baseline_penalty == 1.0)

print("\n[4] run_summary.json carries the baseline and final metric values (§4.4)")
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-for-construction")
from config import Config
from coordination.coordinator import Coordinator

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    src = root / "repo" / "src"
    src.mkdir(parents=True)
    body = "\n".join(f"    if (a > {i}) r += {i};" for i in range(1, 21))
    (src / "big.cpp").write_text(
        f"int wide(int a) {{\n    int r = 0;\n{body}\n    return r;\n}}\n")

    lizard = Path(sys.executable).parent / "lizard"
    cfg = Config(repo_root=root / "repo", target_subdir="src",
                 work_root=root / "work", run_id="metrics_test",
                 lizard_binary=str(lizard) if lizard.exists() else "lizard")
    c = Coordinator(cfg)

    total, breakdown, stats = c._measure()
    c.baseline_penalty = c.current_penalty = total
    c.baseline_metric_stats = stats
    c.metric_stats = stats
    c.history.start(total, breakdown=breakdown)
    c._write_summary()

    summary = json.loads((cfg.run_results_path / cfg.run_summary_filename).read_text())
    check("summary written", bool(summary))
    m = summary.get("metrics", {})
    check("metrics block present", bool(m))
    check("percentile method recorded", bool(m.get("percentile_method")),
          str(m.get("percentile_method")))
    check("baseline metric values present", set(m.get("baseline", {})) >=
          {"ccn", "nloc", "cognitive", "param", "duplicates"},
          str(sorted(m.get("baseline", {}))))
    check("final metric values present", bool(m.get("final")))
    check("mean_change_pct present for all five metrics",
          set(m.get("mean_change_pct", {})) ==
          {"ccn", "nloc", "cognitive", "param", "duplicates"},
          str(sorted(m.get("mean_change_pct", {}))))
    check("cleared list present", isinstance(m.get("cleared"), list),
          str(m.get("cleared")))
    ccn = m.get("baseline", {}).get("ccn", {})
    check("per-metric row has the Table 4.1 columns",
          {"threshold", "functions", "mean", "median", "p90", "p95", "p99",
           "max", "over_threshold", "cleared"} <= set(ccn),
          str(sorted(ccn)))
    check("the single wide function is counted once, over threshold",
          ccn.get("functions") == 1 and ccn.get("over_threshold") == 1
          and ccn.get("max", 0) > 15,
          f"functions={ccn.get('functions')} max={ccn.get('max')} "
          f"over={ccn.get('over_threshold')}")
    check("baseline commit / integration branch recorded",
          "baseline_commit" in summary and "integration_branch" in summary,
          summary.get("integration_branch", ""))
    summary_cfg = summary.get("config", {})
    check("subscription transport records no API credential variable",
          summary_cfg.get("api_provider") == "subscription"
          and summary_cfg.get("api_key_env") == "")
    check("API key value is never written to run_summary",
          "dummy-for-construction" not in json.dumps(summary))
    tokens = summary.get("tokens", {})
    check("token summary block is present", bool(tokens))
    check("empty run has explicit zero token totals",
          tokens.get("totals", {}).get("total_tokens") == 0,
          str(tokens.get("totals")))
    token_detail = cfg.run_results_path / cfg.token_usage_filename
    check("detailed token artifact is written", token_detail.exists(),
          str(token_detail))
    check("token detail never contains the API key value",
          "dummy-for-construction" not in token_detail.read_text())
    budget = summary.get("token_budget", {})
    check("disabled token ceiling is explicit",
          budget.get("dispatch_closed") is False
          and budget.get("trigger") is None)

    print("\n[5] agent logs live in the run's results directory (§4.4)")
    from agents.programmer import ProgrammerSession
    from agents.analyst import AnalystSession
    from queue import Queue

    check("agent_log_dir is inside the results directory",
          cfg.agent_log_dir.parent == cfg.run_results_path, str(cfg.agent_log_dir))

    prog = ProgrammerSession(programmer_id="PROG_1", worktree=root / "wt",
                             cfg=cfg, queue=Queue())
    first, second = prog._next_log_path(), prog._next_log_path()
    check("each dispatch gets its own log file", first != second,
          f"{first.name} vs {second.name}")
    check("programmer logs sit under the run's results dir",
          first.parent == cfg.agent_log_dir, str(first))
    check("dispatch numbering is zero-padded so files sort in order",
          first.name == "PROG_1_001.log" and second.name == "PROG_1_002.log",
          f"{first.name}, {second.name}")

    # A real provider can finish after the long gate tool call without
    # emitting the requested final RESULT line. The authoritative gate
    # record must recover that success rather than cause paid redispatch.
    from coordination import gate_attempts
    prog.assigned_issues = ["ISSUE-0042"]
    gate_log = cfg.run_results_path / cfg.gate_attempts_filename
    gate_attempts.append(
        gate_log,
        {
            "agent": "PROG_1",
            "issue_id": "ISSUE-OLD",
            "outcome": gate_attempts.MERGED,
            "penalty_before": 99.0,
            "penalty_after": 1.0,
        },
    )
    prog.gate_record_start = len(gate_attempts.load(gate_log))
    gate_attempts.append(gate_log, {
        "agent": "PROG_1",
        "issue_id": "ISSUE-0042",
        "outcome": gate_attempts.MERGED,
        "penalty_before": 12.5,
        "penalty_after": 2.0,
    })
    recovered = prog._recover_merged_gate_results(set())
    check("missing RESULT is recovered from matching successful gate evidence",
          recovered == [{
              "id": "ISSUE-0042", "status": "done",
              "penalty_before": 12.5, "penalty_after": 2.0,
          }], str(recovered))
    check("an already reported RESULT is never duplicated",
          prog._recover_merged_gate_results({"ISSUE-0042"}) == [])

    analyst = AnalystSession(analyst_id="ANALYST_1", cfg=cfg, queue=Queue(),
                             worktree=root / "wt")
    check("analyst logs use the same directory and scheme",
          analyst._next_log_path() == cfg.agent_log_dir / "ANALYST_1_001.log",
          str(analyst._next_log_path()))

    # Two runs sharing a work_root must not overwrite each other's logs.
    other = Config(repo_root=cfg.repo_root, target_subdir="src",
                   work_root=cfg.work_root, run_id="second_run")
    check("a second run's logs land in a different directory",
          other.agent_log_dir != cfg.agent_log_dir, str(other.agent_log_dir))
    check("independent runs use different backlog files",
          other.backlog_file_path != cfg.backlog_file_path,
          f"{cfg.backlog_file_path} vs {other.backlog_file_path}")
    check("independent runs use different state files",
          other.state_path != cfg.state_path,
          f"{cfg.state_path} vs {other.state_path}")
    check("independent runs use different worktree roots",
          other.agent_work_root != cfg.agent_work_root,
          f"{cfg.agent_work_root} vs {other.agent_work_root}")

print("\n" + "="*62)
print(f"FAILURES ({len(FAIL)}): " + "; ".join(FAIL) if FAIL else "ALL CHECKS PASSED")
sys.exit(1 if FAIL else 0)
