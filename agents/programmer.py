"""Programmer session — wraps a `claude` CLI subprocess that performs
non-functional refactoring inside its own git worktree.
"""

import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Optional

from config import Config
from agents import GATE_CLI, PROMPT_DIR
from agents.agent_runner import AgentProcess, spawn_claude_agent
from agents.log_parser import extract_assistant_text
from agents.provider import agent_subprocess_environment
from coordination import gate_attempts
from coordination import message_queue as mq


_RESULT_DONE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?RESULT:\s*(ISSUE-\S+)\s*-\s*"
    r"done\s*-\s*merged at penalty\s+([\d.]+)\s*->\s*([\d.]+)"
    r"(?:\*\*)?\s*$",
    re.MULTILINE,
)
_RESULT_SKIP_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?RESULT:\s*(ISSUE-\S+)\s*-\s*"
    r"skipped\s*-\s*(.*?)(?:\*\*)?\s*$",
    re.MULTILINE,
)


@dataclass
class ProgrammerSession:
    programmer_id: str
    worktree: Path
    cfg: Config
    queue: Queue
    process: Optional[AgentProcess] = None
    started_at: Optional[float] = None
    log_path: Optional[Path] = None
    assigned_issues: list[str] = field(default_factory=list)
    issue_specs: list[dict] = field(default_factory=list)
    aborted: bool = False
    dispatch: int = 0
    gate_record_start: int = 0

    def run(self, issue_specs: list[dict]) -> None:
        self.aborted = False
        self.issue_specs = issue_specs
        self.assigned_issues = [s["id"] for s in issue_specs]
        self.gate_record_start = len(gate_attempts.load(
            self.cfg.run_results_path / self.cfg.gate_attempts_filename
        ))
        self._clear_gate_status()
        self._write_gate_config()
        self._start()
        self._wait_and_collect()

    def runtime_sec(self) -> float:
        if self.started_at is None:
            return 0.0
        wall = time.time() - self.started_at
        status = self._read_gate_status()
        if status is None:
            return wall
        validation = max(0.0, float(status.get("accumulated_sec", 0.0)))
        if self._status_is_live(status):
            validation += max(
                0.0, time.time() - float(status.get("started_at", 0.0))
            )
        return max(0.0, wall - validation)

    def edits_made(self) -> bool:
        return self.process.edits_made if self.process is not None else False

    def gate_invocations(self) -> int:
        return self.process.gate_invocations if self.process is not None else 0

    def gate_status(self) -> dict | None:
        """Return a live merge-gate marker, ignoring stale crash debris."""
        payload = self._read_gate_status()
        return (
            payload
            if payload is not None and self._status_is_live(payload)
            else None
        )

    def _read_gate_status(self) -> dict | None:
        try:
            payload = json.loads(self._gate_status_path().read_text())
            return payload if isinstance(payload, dict) else None
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def _status_is_live(payload: dict) -> bool:
        if not payload.get("active", False):
            return False
        try:
            pid = int(payload.get("pid", 0))
            if pid <= 0:
                return False
            os.kill(pid, 0)
            return True
        except (ValueError, TypeError, ProcessLookupError,
                PermissionError, OSError):
            return False

    def gate_active(self) -> bool:
        return self.gate_status() is not None

    def gate_runtime_sec(self) -> float:
        status = self.gate_status()
        if status is None:
            return 0.0
        return max(0.0, time.time() - float(status.get("started_at", 0.0)))

    def tail_log(self, max_chars: int = 4000) -> str:
        """Return the last `max_chars` characters of assistant text."""
        if self.log_path is None or not self.log_path.exists():
            return ""
        try:
            text = extract_assistant_text(self.log_path)
        except Exception:
            return ""
        return text[-max_chars:]

    def kill(self) -> None:
        # Set before killing the process so the worker thread, when it
        # unblocks from process.wait(), sees the flag and skips posting
        # results that the coordinator has already returned to TODO.
        self.aborted = True
        if self.process is not None:
            self.process.kill()

    # -- internals -----------------------------------------------------

    def _write_gate_config(self) -> None:
        cfg_path = self._gate_config_path()
        cfg_payload = {
            "repo_root": str(self.cfg.repo_root),
            "target_subdir": self.cfg.target_subdir,
            "thresholds": self.cfg.thresholds,
            "weights": self.cfg.weights,
            "build_cmd": self.cfg.build_cmd,
            "test_cmd": self.cfg.test_cmd,
            "build_timeout_sec": self.cfg.build_timeout_sec,
            "test_timeout_sec": self.cfg.test_timeout_sec,
            "integration_branch": self.cfg.integration_branch,
            "duplo_binary": self.cfg.duplo_binary,
            "duplo_min_block_lines": self.cfg.duplo_min_block_lines,
            "dupl_binary": self.cfg.dupl_binary,
            "dupl_threshold_tokens": self.cfg.dupl_threshold_tokens,
            "lizard_binary": self.cfg.lizard_binary,
            "gocognit_binary": self.cfg.gocognit_binary,
            "lizard_language": self.cfg.lizard_language,
            "exclude_dirs": list(self.cfg.exclude_dirs),
            "allowed_untracked_paths": list(
                self.cfg.gate_allowed_untracked_paths
            ),
            "max_gate_attempts_per_issue": (
                self.cfg.max_gate_attempts_per_issue
            ),
            "gate_record_start": self.gate_record_start,
            # The gate runs as its own process and cannot reach the
            # message queue, so it appends every attempt here instead.
            "agent_id": self.programmer_id,
            "attempt_log": str(
                self.cfg.run_results_path / self.cfg.gate_attempts_filename
            ),
            "issue_history_dir": str(self.cfg.issue_history_dir),
            "gate_python": sys.executable,
            "gate_cli": str(GATE_CLI),
            "gate_status_file": str(self._gate_status_path()),
        }
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(cfg_payload, indent=2))

    def _gate_config_path(self) -> Path:
        return self.cfg.gate_config_dir / f"{self.programmer_id}.json"

    def _gate_status_path(self) -> Path:
        return self.cfg.gate_status_dir / f"{self.programmer_id}.json"

    def _clear_gate_status(self) -> None:
        try:
            self._gate_status_path().unlink()
        except FileNotFoundError:
            pass

    def _next_log_path(self) -> Path:
        """One log file per dispatch (thesis §4.4: "the full agent logs").

        A single file per agent would not do: the runner opens the log
        with "w", so each re-dispatch would erase the previous one. Nor
        can it be opened for append, because _parse_results_from_log
        scans the whole file and would re-post an earlier dispatch's
        RESULT lines as fresh merges.
        """
        if self.dispatch == 0 and self.cfg.agent_log_dir.exists():
            prefix = f"{self.programmer_id}_"
            existing = []
            for path in self.cfg.agent_log_dir.glob(f"{prefix}*.log"):
                try:
                    existing.append(int(path.stem.removeprefix(prefix)))
                except ValueError:
                    continue
            self.dispatch = max(existing, default=0)
        self.dispatch += 1
        return (
            self.cfg.agent_log_dir / f"{self.programmer_id}_{self.dispatch:03d}.log"
        )

    def _start(self) -> None:
        system_prompt = (PROMPT_DIR / "programmer.txt").read_text()
        task_prompt = self._build_task_prompt(self.issue_specs)

        self.log_path = self._next_log_path()
        self.process = spawn_claude_agent(
            agent_id=self.programmer_id,
            cli_path=self.cfg.claude_cli,
            cwd=self.worktree,
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            log_path=self.log_path,
            model=self.cfg.agent_model,
            extra_args=self.cfg.agent_cli_extra_args,
            env=agent_subprocess_environment(self.cfg),
        )
        self.started_at = time.time()

    def _wait_and_collect(self) -> None:
        if self.process is None:
            return
        returncode = self.process.process.wait()
        self.process.stdout_reader.join(timeout=5)
        if self.aborted:
            return
        results = self._parse_results_from_log()
        recovered = self._recover_merged_gate_results({
            result["id"] for result in results
        })
        if recovered:
            print(
                f"[{self.programmer_id}] recovered {len(recovered)} successful "
                "result(s) from authoritative gate evidence"
            )
            results.extend(recovered)

        for r in results:
            if r["status"] == "done":
                self.queue.put(mq.Message(
                    sender=self.programmer_id,
                    kind=mq.MERGE_RESULT,
                    payload={
                        "issue_id": r["id"],
                        "penalty_before": r["penalty_before"],
                        "penalty_after": r["penalty_after"],
                    },
                ))
            else:
                self.queue.put(mq.Message(
                    sender=self.programmer_id,
                    kind=mq.MARK_SKIPPED,
                    payload={"issue_id": r["id"], "reason": r.get("reason", "")},
                ))

        # A non-zero exit from a session that was NOT killed is a crash of
        # the CLI itself (thesis §5.4 counts these; there they are KIRO
        # crashes). Killed sessions return above, so they cannot be
        # miscounted here.
        self.queue.put(mq.Message(
            sender=self.programmer_id,
            kind=mq.PROGRAMMER_FINISHED,
            payload={
                "programmer_id": self.programmer_id,
                "returncode": returncode,
            },
        ))

    def _build_task_prompt(self, issue_specs: list[dict]) -> str:
        # The programmer commits its own edits before invoking the gate;
        # the gate re-measures the integration branch's penalty itself, so
        # no baseline is passed on the command line.
        gate_cmd = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(GATE_CLI))} "
            f"--config {shlex.quote(str(self._gate_config_path()))} "
            "--issue-id <ISSUE-ID>"
        )
        branch = self.cfg.integration_branch
        lines = [
            "You have been assigned the following backlog issues:",
            "",
        ]
        for s in issue_specs:
            lines.append(
                f"  {s['id']}  ({s.get('issue_type', '?')})  "
                f"{s['file_path']}:{s['line']}  -  {s.get('message', '')}"
            )
            lines.append(
                f"    Dispatch {s.get('dispatch_number', 1)}/"
                f"{s.get('dispatch_limit', self.cfg.max_issue_dispatches)}."
            )
            feedback = s.get("attempt_feedback", [])
            if feedback:
                lines.append("    Previous failed/evaluated attempts:")
                for attempt in feedback:
                    lines.append(
                        "      - "
                        + json.dumps(attempt, sort_keys=True, separators=(",", ":"))
                    )
                history_path = s.get("attempt_history_path", "")
                if history_path:
                    lines.append(
                        f"    Read {history_path} and its referenced patches "
                        "before editing. Do not repeat a recorded strategy "
                        "without a materially different design."
                    )
        lines.extend([
            "",
            f"The integration branch for this run is `{branch}`. That is the",
            "branch every accepted change is merged into; use it wherever your",
            "system prompt refers to the integration branch.",
            "",
            "Process them ONE AT A TIME, in order. For each issue:",
            "  1. Sync onto the latest integration branch and reset clean:",
            f"        git reset --hard {branch} && git clean -fd",
            "  2. Read the flagged file, refactor per your strategy guide.",
            "  3. Commit your edits to your feature branch.",
            "  4. Invoke the merge gate from the worktree root:",
            f"        {gate_cmd}",
            f"     The gate rebases onto the latest {branch}, re-measures the",
            "     penalty, builds, tests, and (on success) fast-forwards",
            f"     {branch}. It prints a JSON line with success/penalty/",
            "     merged/reverted/reason — read it and react per your",
            "     system prompt.",
            "     Do not run a separate build or test before the gate; the",
            "     gate owns those commands. Do not pipe/filter gate output or",
            "     invoke it more than three times for one issue.",
            "  5. If the gate succeeded, emit:",
            "        RESULT: <ISSUE-ID> - done - merged at penalty <before> -> <after>",
            "     If it could not be made to pass, emit:",
            "        RESULT: <ISSUE-ID> - skipped - <short reason>",
            "",
            "Stop after all assigned issues have been resolved (done or skipped).",
        ])
        return "\n".join(lines)

    def _parse_results_from_log(self) -> list[dict]:
        text = extract_assistant_text(self.log_path) if self.log_path else ""
        results: list[dict] = []
        seen: set[str] = set()
        assigned = set(self.assigned_issues)
        for m in _RESULT_DONE_RE.finditer(text):
            iid = m.group(1)
            if iid in seen or iid not in assigned:
                continue
            seen.add(iid)
            results.append({
                "id": iid,
                "status": "done",
                "penalty_before": float(m.group(2)),
                "penalty_after": float(m.group(3)),
            })
        for m in _RESULT_SKIP_RE.finditer(text):
            iid = m.group(1)
            if iid in seen or iid not in assigned:
                continue
            seen.add(iid)
            results.append({
                "id": iid,
                "status": "skipped",
                "reason": m.group(2).strip(),
            })
        return results

    def _recover_merged_gate_results(self, already_reported: set[str]) -> list[dict]:
        """Recover a successful gate result when the CLI omits RESULT.

        The gate log is the authoritative record already used by the
        coordinator to verify model-reported penalties. A model can finish
        immediately after a successful long-running gate without emitting
        the requested final protocol line; returning that issue to TODO would
        duplicate paid work and can re-run builds after the change is already
        merged. Only assigned, otherwise-unreported successful records for
        this programmer are eligible.
        """
        assigned = set(self.assigned_issues) - already_reported
        if not assigned:
            return []
        records = gate_attempts.load(
            self.cfg.run_results_path / self.cfg.gate_attempts_filename
        )[self.gate_record_start:]
        recovered: dict[str, dict] = {}
        for record in records:
            issue_id = str(record.get("issue_id", ""))
            if (
                issue_id not in assigned
                or record.get("agent") != self.programmer_id
                or record.get("outcome") != gate_attempts.MERGED
            ):
                continue
            try:
                before = float(record["penalty_before"])
                after = float(record["penalty_after"])
            except (KeyError, TypeError, ValueError):
                continue
            recovered[issue_id] = {
                "id": issue_id,
                "status": "done",
                "penalty_before": before,
                "penalty_after": after,
            }
        return [
            recovered[issue_id]
            for issue_id in self.assigned_issues
            if issue_id in recovered
        ]
