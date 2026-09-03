"""Verify run-wide issue dispatch limits and cross-session feedback."""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("ANTHROPIC_API_KEY", "dummy-for-construction")
EXP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(EXP))

from agents.programmer import ProgrammerSession  # noqa: E402
from config import Config  # noqa: E402
from coordination import issue_history  # noqa: E402
from coordination.backlog import Backlog, BacklogStore, Issue, SKIPPED, TODO  # noqa: E402
from coordination.coordinator import Coordinator  # noqa: E402


FAIL = []


def check(label, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not condition:
        FAIL.append(label)


def make_issue(issue_id="ISSUE-0001"):
    return Issue(
        id=issue_id, file_path="src/a.cc", line=10, severity="high",
        issue_type="complexity", message="CCN=30", metric_values={"ccn": 30},
    )


with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    print("\n[1] Backlog persists run-wide dispatch count")
    store = BacklogStore(root / "backlog.json")
    store.add_issue(make_issue())
    for number in range(1, 4):
        check(f"dispatch {number} accepted",
              store.mark_dispatched("ISSUE-0001", "PROG_1", 3))
        store.return_to_todo("ISSUE-0001")
    check("fourth dispatch rejected",
          not store.mark_dispatched("ISSUE-0001", "PROG_1", 3))
    store.persist()
    restored = BacklogStore(root / "backlog.json")
    restored.load_or_init()
    check("dispatch count survives reload",
          restored.snapshot().items["ISSUE-0001"].dispatch_count == 3)

    print("\n[2] Coordinator skips exhausted issues before orchestration")
    cfg = Config(
        repo_root=root / "repo", target_subdir="src",
        work_root=root / "work", run_id="attempt_policy",
        max_issue_dispatches=3,
    )
    coordinator = Coordinator(cfg)
    cfg.run_results_path.mkdir(parents=True, exist_ok=True)
    exhausted = make_issue("ISSUE-0002")
    exhausted.dispatch_count = 3
    coordinator.backlog.add_issue(exhausted)
    coordinator._sync_issue_attempt_state()
    exhausted_after = coordinator.backlog.snapshot().items["ISSUE-0002"]
    check("exhausted issue deterministically skipped",
          exhausted_after.status == SKIPPED)
    check("skip reason records configured limit",
          exhausted_after.skip_reason == "issue dispatch limit reached (3)")

    print("\n[3] Prior gate feedback reaches the next Programmer prompt")
    active = make_issue("ISSUE-0003")
    active.file_path = "src/b.cc"
    active.dispatch_count = 1
    coordinator.backlog.add_issue(active)
    history = issue_history.history_path(cfg.issue_history_dir, "ISSUE-0003")
    issue_history.append(history, {
        "outcome": "penalty_rejected",
        "strategy": "extract one large helper",
        "penalty_before": 100.0,
        "penalty_after": 110.0,
        "reason": "penalty did not decrease",
        "patch_id": "abc123",
        "patch_file": "patches/abc123.patch",
    })
    coordinator._sync_issue_attempt_state()
    snapshot = coordinator.backlog.snapshot()
    specs = coordinator._collect_specs(["ISSUE-0003"], snapshot)
    check("next spec identifies dispatch 2 of 3",
          specs[0]["dispatch_number"] == 2 and specs[0]["dispatch_limit"] == 3)
    check("spec carries bounded prior feedback",
          specs[0]["attempt_feedback"][0]["patch_id"] == "abc123")
    session = ProgrammerSession(
        programmer_id="PROG_1", worktree=root / "worktree",
        cfg=cfg, queue=None,
    )
    prompt = session._build_task_prompt(specs)
    check("prompt contains prior strategy and history path",
          "extract one large helper" in prompt
          and str(history) in prompt
          and "Dispatch 2/3" in prompt)

    print("\n[4] Old backlog JSON remains loadable")
    legacy = root / "legacy.json"
    body = make_issue("ISSUE-0099").__dict__
    for key in ("dispatch_count", "attempt_history_path", "attempt_feedback"):
        body.pop(key)
    legacy.write_text(json.dumps({"items": {"ISSUE-0099": body}}))
    legacy_store = BacklogStore(legacy)
    legacy_store.load_or_init()
    legacy_issue = legacy_store.snapshot().items["ISSUE-0099"]
    check("new fields receive backward-compatible defaults",
          legacy_issue.dispatch_count == 0
          and legacy_issue.attempt_feedback == [])

print("\n" + "=" * 62)
if FAIL:
    print(f"FAILURES ({len(FAIL)}): " + "; ".join(FAIL))
    raise SystemExit(1)
print("ALL CHECKS PASSED")
