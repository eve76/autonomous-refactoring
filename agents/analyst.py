"""Analyst session — wraps a `claude` CLI subprocess that scans the
accepted (main-branch) source tree and reports quality issues.
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Optional

from config import Config
from agents import PROMPT_DIR
from agents.agent_runner import AgentProcess, spawn_claude_agent
from agents.log_parser import extract_assistant_text
from coordination import message_queue as mq
from coordination.git_manager import reset_worktree


_ISSUE_RE = re.compile(
    r"^\s*ISSUE:\s*(?P<file>[^:]+):(?P<line>\d+)\s*-\s*(?P<severity>[^-]+?)\s*-\s*(?P<type>[^-]+?)\s*-\s*(?P<msg>.+)$",
    re.MULTILINE,
)


@dataclass
class AnalystSession:
    analyst_id: str
    cfg: Config
    queue: Queue
    worktree: Path
    process: Optional[AgentProcess] = None
    started_at: Optional[float] = None
    log_path: Optional[Path] = None
    focus_metrics: list[str] = field(default_factory=list)

    def run(self, focus_metrics: list[str]) -> None:
        self.focus_metrics = focus_metrics
        # Reset to accepted main code so the analyst never sees a
        # half-merged or in-progress programmer state.
        reset_worktree(self.worktree, self.cfg.main_branch)
        self._start()
        self._wait_and_collect()

    def _start(self) -> None:
        system_prompt = (PROMPT_DIR / "analyst.txt").read_text()
        task_prompt = self._build_task_prompt(self.focus_metrics)

        self.log_path = self.cfg.work_root / self.cfg.log_dir / f"{self.analyst_id}.log"
        target = self.worktree / self.cfg.target_subdir if self.cfg.target_subdir not in (".", "") else self.worktree
        self.process = spawn_claude_agent(
            agent_id=self.analyst_id,
            cli_path=self.cfg.claude_cli,
            cwd=target,
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
        issues = self._parse_issues_from_log()
        self.queue.put(mq.Message(
            sender=self.analyst_id,
            kind=mq.ADD_ISSUES,
            payload={"issues": issues},
        ))
        self.queue.put(mq.Message(
            sender=self.analyst_id,
            kind=mq.ANALYST_FINISHED,
            payload={"analyst_id": self.analyst_id},
        ))

    def _build_task_prompt(self, focus_metrics: list[str]) -> str:
        focus = ", ".join(focus_metrics) if focus_metrics else "any metric"
        target = self.worktree / self.cfg.target_subdir if self.cfg.target_subdir not in (".", "") else self.worktree
        return (
            f"Scan the source tree under {target} for quality "
            f"violations. Focus on: {focus}. Run Lizard or other relevant "
            f"tools as needed, then emit one ISSUE: line per violation in "
            f"the format defined in your system prompt. Group multiple "
            f"violations in the same function into one line. After all "
            f"issues are emitted, stop."
        )

    def _parse_issues_from_log(self) -> list[dict]:
        text = extract_assistant_text(self.log_path) if self.log_path else ""
        issues: list[dict] = []
        for m in _ISSUE_RE.finditer(text):
            issues.append({
                "file_path": m.group("file").strip(),
                "line": int(m.group("line")),
                "severity": m.group("severity").strip(),
                "issue_type": m.group("type").strip(),
                "message": m.group("msg").strip(),
            })
        return issues
