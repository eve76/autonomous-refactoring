"""CLI entry the programmer agent invokes from inside its worktree.

Usage (run from inside the worktree, after committing your edits):
    /path/to/venv/python /path/to/experiment/merge_gate/cli.py \
        --config /path/to/results/gate_configs/PROG_1.json \
        --issue-id ISSUE-0001

Reads the external config written by the coordination layer for
repo_root / target_subdir / thresholds / weights / build & test
commands. A worktree-local .gate_config.json remains a legacy fallback.
The gate re-measures the integration branch's penalty itself, so no
caller-supplied baseline is needed. Prints a JSON result line on stdout
and exits non-zero when the gate rejects the change.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path


def _load_config(cfg_path: Path) -> dict:
    if not cfg_path.exists():
        print(json.dumps({"success": False, "reason": f"missing gate config: {cfg_path}"}))
        sys.exit(2)
    return json.loads(cfg_path.read_text())


def _write_gate_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".gate-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_accumulated_gate_time(path: Path) -> float:
    try:
        payload = json.loads(path.read_text())
        return max(0.0, float(payload.get("accumulated_sec", 0.0)))
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError,
            OSError):
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the merge gate on the current worktree."
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Gate config path (default: legacy .gate_config.json in the worktree)",
    )
    parser.add_argument("--issue-id", default="", help="Backlog issue being evaluated")
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from merge_gate.gate import MergeGate
    from coordination import gate_attempts

    worktree = Path.cwd()
    cfg = _load_config(
        args.config.expanduser().resolve()
        if args.config is not None else worktree / ".gate_config.json"
    )
    attempt_log = Path(cfg["attempt_log"]) if cfg.get("attempt_log") else None
    max_attempts = int(cfg.get("max_gate_attempts_per_issue", 3))
    if attempt_log is not None and args.issue_id:
        gate_record_start = int(cfg.get("gate_record_start", 0))
        prior_attempts = sum(
            record.get("agent") == cfg.get("agent_id", "")
            and record.get("issue_id") == args.issue_id
            for record in gate_attempts.load(attempt_log)[gate_record_start:]
        )
        if prior_attempts >= max_attempts:
            payload = {
                "success": False,
                "rebase_conflict": False,
                "penalty_before": 0.0,
                "penalty_after": 0.0,
                "tests_passed": False,
                "merged": False,
                "merged_race": False,
                "reverted": False,
                "reason": (
                    f"gate attempt limit reached ({max_attempts}) - "
                    "report the issue as skipped"
                ),
                "outcome": gate_attempts.ATTEMPT_LIMIT,
            }
            print(json.dumps(payload))
            return 1

    gate = MergeGate(
        worktree=worktree,
        repo_root=Path(cfg["repo_root"]),
        target_subdir=cfg["target_subdir"],
        thresholds=cfg["thresholds"],
        build_cmd=cfg["build_cmd"],
        test_cmd=cfg["test_cmd"],
        integration_branch=cfg["integration_branch"],
        duplo_binary=cfg.get("duplo_binary", ""),
        duplo_min_block_lines=int(cfg.get("duplo_min_block_lines", 4)),
        dupl_binary=cfg.get("dupl_binary", ""),
        dupl_threshold_tokens=int(cfg.get("dupl_threshold_tokens", 100)),
        lizard_binary=cfg.get("lizard_binary", "lizard"),
        gocognit_binary=cfg.get("gocognit_binary", "gocognit"),
        lizard_language=cfg.get("lizard_language", "cpp"),
        weights=cfg.get("weights"),
        # Run-level attempt log, so the coordination layer can count the
        # rejections/failures it never sees directly (thesis §5.1, §5.4).
        attempt_log=attempt_log,
        agent_id=cfg.get("agent_id", ""),
        # Absent key -> the tools default. An explicitly empty list is
        # honoured as "exclude nothing", so `.get(..., None)` is required
        # rather than a falsy check.
        exclude_dirs=cfg.get("exclude_dirs"),
        issue_id=args.issue_id,
        allowed_untracked_paths=cfg.get("allowed_untracked_paths", ()),
        issue_history_dir=(
            Path(cfg["issue_history_dir"])
            if cfg.get("issue_history_dir") else None
        ),
        build_timeout_sec=int(cfg.get("build_timeout_sec", 60 * 60)),
        test_timeout_sec=int(cfg.get("test_timeout_sec", 60 * 60)),
    )
    status_file = (
        Path(cfg["gate_status_file"])
        if cfg.get("gate_status_file") else None
    )
    if status_file is not None:
        accumulated_sec = _read_accumulated_gate_time(status_file)
        gate_started_at = time.time()
        _write_gate_status(status_file, {
            "agent_id": cfg.get("agent_id", ""),
            "issue_id": args.issue_id,
            "pid": os.getpid(),
            "started_at": gate_started_at,
            "accumulated_sec": accumulated_sec,
            "active": True,
        })
    try:
        result = gate.run()
    finally:
        if status_file is not None:
            finished_at = time.time()
            _write_gate_status(status_file, {
                "agent_id": cfg.get("agent_id", ""),
                "issue_id": args.issue_id,
                "pid": os.getpid(),
                "started_at": gate_started_at,
                "finished_at": finished_at,
                "accumulated_sec": (
                    accumulated_sec + max(0.0, finished_at - gate_started_at)
                ),
                "active": False,
            })
    print(json.dumps(asdict(result)))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
