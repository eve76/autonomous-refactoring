"""Verify conservative Stage 2/3 token-saving behavior and budget semantics."""

import copy
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from agents.analyst import AnalystSession                              # noqa: E402
from agents import analyst as analyst_module, programmer as programmer_module  # noqa: E402
from agents.log_parser import has_successful_terminal_result           # noqa: E402
from agents.orchestrator import Orchestrator, compact_backlog_view     # noqa: E402
from analysis.candidates import build_local_leads, lead_matches_focus  # noqa: E402
from config import Config                                               # noqa: E402
from coordination import message_queue as mq                            # noqa: E402
from coordination.backlog import (                                     # noqa: E402
    Backlog, DONE, IN_PROGRESS, Issue, SKIPPED, TODO,
)
from coordination.coordinator import Coordinator                       # noqa: E402
from coordination.run_state import RunState                            # noqa: E402
from tests.test_real_repo_deepseek_three_issue import (                 # noqa: E402
    ThreeIssueCoordinator,
)


FAILURES = []


def check(label, condition, detail=""):
    print(
        f"  {'PASS' if condition else 'FAIL'}  {label}"
        + (f"  [{detail}]" if detail else "")
    )
    if not condition:
        FAILURES.append(label)


def issue(issue_id, status=TODO, reduction=0.0, file_path="src/a.cc"):
    return Issue(
        id=issue_id, file_path=file_path, line=1, severity="high",
        issue_type="complexity", message="CCN=21", metric_values={"ccn": 21},
        estimated_penalty_reduction=reduction, status=status,
        assigned_to="PROG_1" if status == IN_PROGRESS else None,
    )


print("\n[1] Orchestrator sees all active work and only Top-24 TODO")
items = {
    f"ISSUE-{index:04d}": issue(
        f"ISSUE-{index:04d}", reduction=float(index),
        file_path=f"src/f{index}.cc",
    )
    for index in range(1, 31)
}
items["ISSUE-0100"] = issue(
    "ISSUE-0100", IN_PROGRESS, 1, "src/active.cc",
)
items["ISSUE-0101"] = issue("ISSUE-0101", DONE)
items["ISSUE-0102"] = issue("ISSUE-0102", SKIPPED)
backlog = Backlog(items=items)
before = copy.deepcopy(backlog.to_dict())
view = compact_backlog_view(backlog, 24)
visible = set(view["items"])
check("exactly Top-24 TODO plus every IN_PROGRESS are visible",
      len(visible) == 25
      and "ISSUE-0100" in visible
      and "ISSUE-0030" in visible
      and "ISSUE-0007" in visible
      and "ISSUE-0006" not in visible)
check("DONE/SKIPPED bodies omitted but complete counts retained",
      "ISSUE-0101" not in visible and "ISSUE-0102" not in visible
      and view["status_counts"] == {
          "total": 33, "todo": 30, "in_progress": 1,
          "done": 1, "skipped": 1,
      }, str(view["status_counts"]))
check("selection explains omitted data",
      view["selection"]["todo_omitted"] == 6
      and view["selection"]["all_in_progress_included"] is True)
check("compact serialization is deterministic",
      json.dumps(view, sort_keys=True)
      == json.dumps(compact_backlog_view(backlog, 24), sort_keys=True))
check("view generation does not mutate persisted backlog",
      backlog.to_dict() == before)

large = Backlog(items={
    f"ISSUE-{index:04d}": issue(
        f"ISSUE-{index:04d}", reduction=float(index),
        file_path=f"src/f{index}.cc",
    )
    for index in range(1, 201)
})
full_json = json.dumps(large.to_dict(), separators=(",", ":"))
compact_json = json.dumps(
    compact_backlog_view(large, 24), separators=(",", ":"),
)
check("Top-24 view materially shrinks a large prompt fixture",
      len(compact_json) < len(full_json) * 0.2,
      f"{len(full_json)} -> {len(compact_json)} chars")

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = Config(
        repo_root=root / "repo", target_subdir="src",
        work_root=root / "work", run_id="orchestrator_view",
    )
    captured = {}

    class FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(
                    type="text", text="PROG_1: ISSUE-0030",
                )],
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
            )

    orchestrator = object.__new__(Orchestrator)
    orchestrator.cfg = cfg
    orchestrator.client = SimpleNamespace(messages=FakeMessages())
    decision = orchestrator.assign(
        current_penalty=10, baseline_penalty=20, backlog=backlog,
        idle_programmers=["PROG_1"], idle_analysts=[],
        stagnation=0, metric_breakdown="ccn",
    )
    sent_prompt = captured["messages"][0]["content"]
    check("real assignment call uses the compact view",
          "ISSUE-0030" in sent_prompt
          and "ISSUE-0100" in sent_prompt
          and "ISSUE-0006" not in sent_prompt)
    check("existing assignment parser remains unchanged",
          decision.programmer_assignments == {
              "PROG_1": ["ISSUE-0030"],
          })


print("\n[2] local leads are filtered, merged and ranked locally")
with tempfile.TemporaryDirectory() as td:
    target = Path(td)
    records = [
        {"file": str(target / "a.cc"), "line": 10, "name": "wide",
         "ccn": 21, "nloc": 60, "param": 2},
        {"file": str(target / "b.cc"), "line": 20, "name": "params",
         "ccn": 3, "nloc": 10, "param": 8},
        {"file": str(target / "c.cc"), "line": 30, "name": "clean",
         "ccn": 15, "nloc": 30, "param": 5},
    ]
    leads = build_local_leads(
        records, target=target,
        thresholds={"ccn": 15, "nloc": 30, "param": 5},
        weights={"ccn": 1, "nloc": 1, "param": 0},
    )
    check("strict threshold and zero weight leave one lead",
          len(leads) == 1, str([lead.to_dict() for lead in leads]))
    check("same function's metric violations are merged",
          set(leads[0].metric_values) == {"ccn", "nloc"})
    check("local reduction is used only as a deterministic rank",
          leads[0].estimated_reduction > 0)
    check("metric and path focus both work",
          lead_matches_focus(leads[0], ["ccn"])
          and lead_matches_focus(leads[0], ["a.cc"])
          and lead_matches_focus(leads[0], ["wide"])
          and not lead_matches_focus(leads[0], ["param"]))

    cognitive_leads = build_local_leads(
        records,
        [{"file": str(target / "a.cc"), "line": 10, "name": "wide",
          "cognitive": 19}],
        target=target,
        thresholds={"ccn": 15, "nloc": 30, "param": 5, "cognitive": 15},
        weights={"ccn": 1, "nloc": 1, "param": 0, "cognitive": 1},
    )
    check("cognitive violation merges into the same local function lead",
          set(cognitive_leads[0].metric_values) == {"ccn", "nloc", "cognitive"})
    check("cognitive focus selects gocognit-backed leads",
          lead_matches_focus(cognitive_leads[0], ["cognitive"]))

    session = object.__new__(AnalystSession)
    session.analyst_id = "ANALYST_1"
    session.worktree = target
    session.cfg = SimpleNamespace(
        target_subdir=".",
        weights={
            "ccn": 1, "nloc": 1, "param": 0,
            "cognitive": 0, "duplicates": 0,
        },
        duplo_binary="",
    )
    session.metric_breakdown = "  ccn penalty=1"
    prompt = session._build_task_prompt(["ccn"], leads)
    check("prompt labels local leads as non-authoritative",
          "not confirmed issues" in prompt
          and "Independently decide" in prompt
          and "never copy" in prompt)
    check("existing ISSUE output contract remains requested",
          "ISSUE: line" in prompt)
    check("prompt constrains disabled tools and lead-page scope",
          "Do not search for, install, or run tools for disabled metrics"
          in prompt
          and "functions only for this dispatch" in prompt)
    bold_issue = analyst_module._ISSUE_RE.search(
        "**ISSUE: src/a.cc:7 - complexity - highComplexity - CCN=19**"
    )
    bold_result = programmer_module._RESULT_DONE_RE.search(
        "**RESULT: ISSUE-0001 - done - merged at penalty 21.05 -> 0.0**"
    )
    check("Markdown-wrapped DeepSeek ISSUE/RESULT lines remain parseable",
          bold_issue is not None
          and bold_issue.group("msg") == "CCN=19"
          and bold_result is not None
          and bold_result.group(1) == "ISSUE-0001")

    recovery_log = target / "confirmed.log"
    recovery_log.write_text(json.dumps({
        "type": "result",
        "result": (
            "I inspected wide at a.cc:10. I confirm this lead. "
            "The function should be refactored."
        ),
        "is_error": False,
    }) + "\n")
    session.log_path = recovery_log
    session.candidate_leads = leads
    recovered = session._parse_issues_from_log()
    check("one explicitly confirmed lead survives a missing ISSUE line",
          len(recovered) == 1
          and recovered[0]["file_path"] == "a.cc"
          and recovered[0]["line"] == 10,
          str(recovered))
    session.candidate_leads = leads * 2
    check("prose recovery is disabled when more than one lead is in scope",
          session._parse_issues_from_log() == [])


print("\n[3] Analyst pages are disjoint and only success commits seen keys")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = Config(
        repo_root=root / "repo", target_subdir="src",
        work_root=root / "work", run_id="pages",
        analyst_lead_page_size=2,
    )
    coordinator = Coordinator(cfg, orchestrator=SimpleNamespace())
    records = [
        {"file": str(root / "repo" / "src" / f"f{i}.cc"),
         "line": i, "name": f"fn{i}", "ccn": 20 + i,
         "nloc": 10, "param": 1}
        for i in range(1, 6)
    ]
    coordinator.local_candidate_leads = build_local_leads(
        records, target=root / "repo" / "src",
        thresholds=cfg.thresholds, weights=cfg.weights,
    )
    first = coordinator._reserve_local_leads("ANALYST_1", ["ccn"])
    second = coordinator._reserve_local_leads("ANALYST_2", ["ccn"])
    check("page size is enforced", len(first) == 2 and len(second) == 2)
    check("concurrent Analyst pages do not overlap",
          {lead.key for lead in first}.isdisjoint(
              {lead.key for lead in second}
          ))

    coordinator._apply_message(mq.Message(
        sender="ANALYST_1", kind=mq.ANALYST_FINISHED,
        payload={
            "analyst_id": "ANALYST_1", "returncode": 0,
            "review_completed": True,
        },
    ))
    check("successful review commits its page to seen",
          {lead.key for lead in first}
          <= coordinator.analyst_lead_seen_keys)
    coordinator._apply_message(mq.Message(
        sender="ANALYST_2", kind=mq.ANALYST_FINISHED,
        payload={
            "analyst_id": "ANALYST_2", "returncode": 0,
            "review_completed": False,
        },
    ))
    retry = coordinator._reserve_local_leads("ANALYST_3", ["ccn"])
    check("zero exit without a terminal result releases leads for retry",
          bool({lead.key for lead in second}
               & {lead.key for lead in retry}))
    restored = RunState.load(cfg.state_path)
    check("seen keys survive resume state",
          restored is not None
          and set(restored.analyst_lead_seen_keys)
          == {lead.key for lead in first})

    success_log = root / "success.log"
    success_log.write_text(json.dumps({
        "type": "result", "result": "", "is_error": False,
    }) + "\n")
    truncated_log = root / "truncated.log"
    truncated_log.write_text(json.dumps({
        "type": "assistant", "message": {"content": []},
    }) + "\n")
    error_log = root / "error.log"
    error_log.write_text(json.dumps({
        "type": "result", "result": "failed", "is_error": True,
    }) + "\n")
    check("terminal result validation distinguishes complete logs",
          has_successful_terminal_result(success_log)
          and not has_successful_terminal_result(truncated_log)
          and not has_successful_terminal_result(error_log))


print("\n[4] token ceilings close dispatch, wait for in-flight work, then stop")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = Config(
        repo_root=root / "repo", target_subdir="src",
        work_root=root / "work", run_id="budget",
        max_run_input_tokens=100,
    )
    coordinator = Coordinator(cfg, orchestrator=SimpleNamespace())
    cfg.agent_log_dir.mkdir(parents=True)
    (cfg.agent_log_dir / "ANALYST_1_001.log").write_text(json.dumps({
        "type": "result",
        "usage": {"input_tokens": 100, "output_tokens": 5},
    }) + "\n")
    coordinator.analyst_futures["ANALYST_1"] = SimpleNamespace()
    closed = coordinator._check_token_budget()
    check("ceiling closes new dispatch at equality",
          closed and coordinator.budget_dispatch_closed
          and coordinator.budget_trigger == "input_tokens")
    check("in-flight work is not converted into an immediate stop",
          coordinator.stop_reason == "")
    coordinator.analyst_futures.clear()
    coordinator.queue.put(mq.Message(
        sender="ANALYST_1", kind=mq.ANALYST_FINISHED,
        payload={"returncode": 0, "review_completed": True},
    ))
    coordinator._check_token_budget()
    check("queued terminal messages are drained before budget stop",
          coordinator.stop_reason == "")
    coordinator.queue.get_nowait()
    coordinator._check_token_budget()
    check("run stops only after in-flight work is gone",
          coordinator.stop_reason == "token_budget_input_tokens")

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = Config(
        repo_root=root / "repo", target_subdir="src",
        work_root=root / "work", run_id="cost-budget",
        max_run_cost_usd=0.25,
    )
    coordinator = Coordinator(cfg, orchestrator=SimpleNamespace())
    cfg.agent_log_dir.mkdir(parents=True)
    (cfg.agent_log_dir / "PROG_1_001.log").write_text(json.dumps({
        "type": "result",
        "total_cost_usd": 0.25,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }) + "\n")
    check("USD ceiling closes dispatch at equality",
          coordinator._check_token_budget()
          and coordinator.budget_trigger == "cost_usd")
    check("cost ceiling has a distinct terminal reason",
          coordinator.stop_reason == "cost_budget_usd")

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    cfg = Config(
        repo_root=root / "repo", target_subdir="src",
        work_root=root / "work", run_id="cny-cost-budget",
        api_provider="deepseek", max_run_cost_cny=300.0,
    )
    coordinator = Coordinator(cfg, orchestrator=SimpleNamespace())
    cfg.agent_log_dir.mkdir(parents=True)
    (cfg.agent_log_dir / "PROG_1_001.log").write_text(json.dumps({
        "type": "result",
        "usage": {"input_tokens": 0, "output_tokens": 50_000_000},
        "modelUsage": {
            "deepseek-v4-pro[1m]": {
                "inputTokens": 0,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
                "outputTokens": 50_000_000,
            },
        },
    }) + "\n")
    check("RMB ceiling closes dispatch at equality",
          coordinator._check_token_budget()
          and coordinator.budget_trigger == "cost_cny")
    check("RMB ceiling has a distinct terminal reason",
          coordinator.stop_reason == "cost_budget_cny")


print("\n[5] no-progress, resume and path-safety guards are bounded")
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    repo = root / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.cc").write_text("int a;\n")
    cfg = Config(
        repo_root=repo, target_subdir="src", work_root=root / "work",
        run_id="guards", orchestrator_no_progress_limit=3,
    )
    coordinator = Coordinator(cfg, orchestrator=SimpleNamespace())
    coordinator._record_assignment_progress(False)
    coordinator._record_assignment_progress(False)
    check("empty assignment responses do not stop too early",
          coordinator.stop_reason == "")
    coordinator._record_assignment_progress(False)
    check("repeated unusable responses stop boundedly",
          coordinator.stop_reason == "orchestrator_no_progress")
    coordinator.stop_reason = ""
    coordinator._record_assignment_progress(True)
    check("a usable dispatch resets the no-progress counter",
          coordinator.empty_assignment_decisions == 0)
    coordinator.empty_analyst_scans = cfg.empty_scan_limit
    check("empty-scan exhaustion prevents rolling Analyst redispatch",
          not coordinator._has_actionable_work(
              coordinator.backlog.snapshot(), [], ["ANALYST_1"],
          ))
    coordinator.empty_analyst_scans = 1
    coordinator.analyst_futures["ANALYST_2"] = SimpleNamespace()
    check("an active verification wave is not refilled slot by slot",
          not coordinator._has_actionable_work(
              coordinator.backlog.snapshot(), [], ["ANALYST_1"],
          ))
    coordinator.analyst_futures.clear()

    bounded = object.__new__(ThreeIssueCoordinator)
    bounded.cfg = SimpleNamespace(empty_scan_limit=2)
    bounded._accepted_issue_count = 1
    bounded._expected_issues = (object(),)
    bounded.empty_analyst_scans = 0
    original_apply_merge = Coordinator._apply_merge_result
    try:
        Coordinator._apply_merge_result = (
            lambda self, msg: setattr(self, "empty_analyst_scans", 0)
        )
        bounded._apply_merge_result(None)
    finally:
        Coordinator._apply_merge_result = original_apply_merge
    check("bounded three-issue merge preserves discovery exhaustion",
          bounded.empty_analyst_scans == bounded.cfg.empty_scan_limit)

    check("equivalent file spellings share one conflict key",
          coordinator._conflict_file_key("a.cc")
          == coordinator._conflict_file_key("src/a.cc")
          == coordinator._conflict_file_key("./src/a.cc"))

    original_fingerprint = coordinator._optimization_fingerprint()
    cfg.analyst_lead_page_size += 1
    check("candidate-affecting configuration changes the fingerprint",
          coordinator._optimization_fingerprint() != original_fingerprint)

    check("summary URL strips credentials, query and fragment",
          coordinator._safe_api_base_url(
              "https://user:secret@example.test/anthropic?key=secret#x"
          ) == "https://example.test/anthropic")


print("\n" + "=" * 62)
print(
    f"FAILURES ({len(FAILURES)}): " + "; ".join(FAILURES)
    if FAILURES else "ALL CHECKS PASSED"
)
raise SystemExit(1 if FAILURES else 0)
