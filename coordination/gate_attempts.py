"""Per-attempt record of every merge-gate evaluation.

The thesis needs counts the gate is the only component that can see:

  - §5.1 "failure counts (reverted attempts, test failures, stagnation
    exits) for each model", reported alongside Table 5.1.
  - §5.4 System deviations: "results for when the systems do not
    complete their designated tasks. Like merge conflicts, kiro-crashes,
    agents give up and so on."

The gate runs as a separate process inside each programmer's worktree,
so it cannot post onto the coordination layer's message queue. It
instead appends one line per attempt to a run-level JSONL file in the
run's results directory, and the coordination layer tallies the file
when it writes run_summary.json.

Append-only JSONL rather than a JSON document because several
programmers gate concurrently from separate processes: a single
O_APPEND write of one short line is atomic on POSIX, so no locking is
needed and concurrent writers cannot interleave.

Durability limit, stated because failure counts are experimental data: a
crash during one of those writes can leave a line with no terminating
newline, and the next record appended fuses onto it, so BOTH are lost.
Every record written before that point survives. Recovering the fused
pair is not attempted — one lost attempt out of a run's hundreds does
not change a count that is reported per run, and guarding against it
would mean either locking or a blank line between every record.

"Attempt" means one full evaluation pass (rebase -> penalty -> build ->
test -> merge), not one gate invocation. The two differ only when the
gate loses the fast-forward race and retries, which §4.3.2 defines as
re-entering the evaluation stage — a genuinely separate attempt.
"""

import json
import os
import time
from pathlib import Path

# Outcome of an attempt. Set by the gate at each of its exit points, so
# nothing here has to be inferred from the human-readable reason string.
MERGED = "merged"
PENALTY_REJECTED = "penalty_rejected"
TEST_FAILED = "test_failed"
BUILD_FAILED = "build_failed"
BUILD_TIMEOUT = "build_timeout"
BUILD_INTERRUPTED = "build_interrupted"
TEST_TIMEOUT = "test_timeout"
TEST_INTERRUPTED = "test_interrupted"
REBASE_CONFLICT = "rebase_conflict"
FF_RACE = "ff_merge_race"
FF_RACE_EXHAUSTED = "ff_race_exhausted"
UNCOMMITTED = "uncommitted"
ANALYSIS_FAILED = "analysis_failed"
OUT_OF_SCOPE = "out_of_scope"
ATTEMPT_LIMIT = "attempt_limit"
DUPLICATE_PATCH = "duplicate_patch"

OUTCOMES = (
    MERGED,
    PENALTY_REJECTED,
    TEST_FAILED,
    BUILD_FAILED,
    BUILD_TIMEOUT,
    BUILD_INTERRUPTED,
    TEST_TIMEOUT,
    TEST_INTERRUPTED,
    REBASE_CONFLICT,
    FF_RACE,
    FF_RACE_EXHAUSTED,
    UNCOMMITTED,
    ANALYSIS_FAILED,
    OUT_OF_SCOPE,
    ATTEMPT_LIMIT,
    DUPLICATE_PATCH,
)


def append(path: Path, record: dict) -> None:
    """Append one attempt record as a single line.

    Uses a bare O_APPEND write instead of open("a") so the whole line
    lands in one syscall; concurrent programmers therefore cannot
    interleave halves of their records.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def make_record(agent_id: str, result, issue_id: str = "") -> dict:
    """Flatten a GateResult into the record shape written to disk."""
    record = {
        "timestamp": round(time.time(), 3),
        "agent": agent_id or "unknown",
        "issue_id": issue_id,
        "outcome": result.outcome,
        "success": bool(result.success),
        "penalty_before": round(float(result.penalty_before), 4),
        "penalty_after": round(float(result.penalty_after), 4),
        "reason": result.reason,
    }
    for key in (
        "patch_id", "patch_file", "base_commit", "commit_hash",
        "strategy", "changed_files",
    ):
        value = getattr(result, key, None)
        if value not in (None, "", []):
            record[key] = value
    return record


def load(path: Path) -> list[dict]:
    """Read the attempt log, tolerating a truncated final line.

    A run killed mid-write leaves a partial last line; dropping it is
    right, because the alternative is losing every earlier record.
    """
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open() as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def summarize(records: list[dict]) -> dict:
    """Tally attempts into the counts the results chapter reports.

    by_outcome keeps the raw tally so an outcome added later is still
    visible in the summary without changing this function.
    """
    by_outcome = {name: 0 for name in OUTCOMES}
    for rec in records:
        outcome = rec.get("outcome") or "unknown"
        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1

    return {
        "gate_attempts": len(records),
        "merged": by_outcome[MERGED],
        # §5.1: attempts whose penalty did not decrease, which the gate
        # reverted to the pre-refactoring state.
        "reverted_attempts": by_outcome[PENALTY_REJECTED],
        # §5.1: attempts that reduced the penalty but broke the tests.
        "test_failures": by_outcome[TEST_FAILED],
        "build_failures": by_outcome[BUILD_FAILED],
        "build_timeouts": by_outcome[BUILD_TIMEOUT],
        "build_interruptions": by_outcome[BUILD_INTERRUPTED],
        "test_timeouts": by_outcome[TEST_TIMEOUT],
        "test_interruptions": by_outcome[TEST_INTERRUPTED],
        # §5.4: rebases the programmer had to resolve by hand.
        "merge_conflicts": by_outcome[REBASE_CONFLICT],
        "ff_merge_races": by_outcome[FF_RACE] + by_outcome[FF_RACE_EXHAUSTED],
        "uncommitted_invocations": by_outcome[UNCOMMITTED],
        "analysis_failures": by_outcome[ANALYSIS_FAILED],
        "scope_violations": by_outcome[OUT_OF_SCOPE],
        "attempt_limit_rejections": by_outcome[ATTEMPT_LIMIT],
        "duplicate_patch_rejections": by_outcome[DUPLICATE_PATCH],
        "by_outcome": by_outcome,
    }
