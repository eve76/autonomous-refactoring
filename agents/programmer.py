"""Programmer session — wraps a `claude` CLI subprocess that performs
non-functional refactoring inside its own git worktree.
"""

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Optional

from config import Config
from agents import GATE_CLI, PROMPT_DIR
from agents.agent_runner import AgentProcess, spawn_claude_agent
from agents.log_parser import extract_assistant_text
from coordination import message_queue as mq


_RESULT_DONE_RE = re.compile(
    r"^\s*RESULT:\s*(ISSUE-\S+)\s*-\s*done\s*-\s*merged at penalty\s+([\d.]+)\s*->\s*([\d.]+)\s*$",
    re.MULTILINE,
)
_RESULT_SKIP_RE = re.compile(
    r"^\s*RESULT:\s*(ISSUE-\S+)\s*-\s*skipped\s*-\s*(.+)$",
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

    def run(self, issue_specs: list[dict]) -> None:
        self.aborted = False
        self.issue_specs = issue_specs
        self.assigned_issues = [s["id"] for s in issue_specs]
        self._write_gate_config()
        self._start()
        self._wait_and_collect()

    def runtime_sec(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    def edits_made(self) -> bool:
        return self.process.edits_made if self.process is not None else False

    def gate_invocations(self) -> int:
        return self.process.gate_invocations if self.process is not None else 0

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
        cfg_payload = {
            "repo_root": str(self.cfg.repo_root),
            "target_subdir": self.cfg.target_subdir,
            "thresholds": self.cfg.thresholds,
            "build_cmd": self.cfg.build_cmd,
            "test_cmd": self.cfg.test_cmd,
            "main_branch": self.cfg.main_branch,
            "duplo_binary": self.cfg.duplo_binary,
            "duplo_min_block_lines": self.cfg.duplo_min_block_lines,
        }
        (self.worktree / self.cfg.gate_config_filename).write_text(
            json.dumps(cfg_payload, indent=2)
        )

    def _start(self) -> None:
        system_prompt = (PROMPT_DIR / "programmer.txt").read_text()
        task_prompt = self._build_task_prompt(self.issue_specs)

        self.log_path = self.cfg.work_root / self.cfg.log_dir / f"{self.programmer_id}.log"
        self.process = spawn_claude_agent(
            agent_id=self.programmer_id,
            cli_path=self.cfg.claude_cli,
            cwd=self.worktree,
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            log_path=self.log_path,
            model=self.cfg.agent_model,
        )
        self.started_at = time.time()

    def _wait_and_collect(self) -> None:
        if self.process is None:
            return
        self.process.process.wait()
        self.process.stdout_reader.join(timeout=5)
        if self.aborted:
            return
        results = self._parse_results_from_log()

        for r in results:
            if r["status"] == "done":
                self.queue.put(mq.Message(
                    sender=self.programmer_id,
                    kind=mq.MARK_DONE,
                    payload={"issue_id": r["id"]},
                ))
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

        self.queue.put(mq.Message(
            sender=self.programmer_id,
            kind=mq.PROGRAMMER_FINISHED,
            payload={"programmer_id": self.programmer_id},
        ))

    def _build_task_prompt(self, issue_specs: list[dict]) -> str:
        lines = [
            "You have been assigned the following backlog issues:",
            "",
        ]
        for s in issue_specs:
            lines.append(
                f"  {s['id']}  ({s.get('issue_type', '?')})  "
                f"{s['file_path']}:{s['line']}  -  {s.get('message', '')}"
            )
        lines.extend([
            "",
            "Process them ONE AT A TIME, in order. For each issue:",
            "  1. Reset your worktree clean and pull latest main.",
            "  2. Read the flagged file, refactor per your strategy guide.",
            "  3. Commit on your feature branch.",
            "  4. Invoke the merge gate from the worktree root:",
            f"        python {GATE_CLI} --penalty-before <current penalty>",
            "     The gate prints a JSON line with success/penalty/merged.",
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
        for m in _RESULT_DONE_RE.finditer(text):
            iid = m.group(1)
            if iid in seen:
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
            if iid in seen:
                continue
            seen.add(iid)
            results.append({
                "id": iid,
                "status": "skipped",
                "reason": m.group(2).strip(),
            })
        return results
