"""Analyst session — wraps a `claude` CLI subprocess that scans the
accepted (integration-branch) source tree and reports quality issues.
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
from agents.log_parser import (
    extract_assistant_text,
    has_subscription_quota_error,
    has_successful_terminal_result,
)
from agents.provider import agent_subprocess_environment
from analysis.candidates import LocalLead, format_local_leads
from coordination import message_queue as mq
from coordination.git_manager import reset_worktree


_ISSUE_RE = re.compile(
    r"^\s*(?:[-*]\s+)?(?:\*\*)?ISSUE:\s*"
    r"(?P<file>[^:]+):(?P<line>\d+)\s*-\s*"
    r"(?P<severity>[^-]+?)\s*-\s*(?P<type>[^-]+?)\s*-\s*"
    r"(?P<msg>.*?)(?:\*\*)?\s*$",
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
    metric_breakdown: str = ""
    candidate_leads: list[LocalLead] = field(default_factory=list)
    dispatch: int = 0
    aborted: bool = False

    def run(
        self,
        focus_metrics: list[str],
        metric_breakdown: str = "",
        candidate_leads: Optional[list[LocalLead]] = None,
    ) -> None:
        self.aborted = False
        self.focus_metrics = focus_metrics
        self.metric_breakdown = metric_breakdown
        self.candidate_leads = list(candidate_leads or [])
        # Reset to the accepted (integration-branch) code so the analyst
        # never sees a half-merged or in-progress programmer state.
        reset_worktree(self.worktree, self.cfg.integration_branch)
        self._start()
        self._wait_and_collect()

    def kill(self) -> None:
        self.aborted = True
        if self.process is not None:
            self.process.kill()

    def runtime_sec(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    def _next_log_path(self) -> Path:
        """One log file per dispatch — see ProgrammerSession._next_log_path."""
        if self.dispatch == 0 and self.cfg.agent_log_dir.exists():
            prefix = f"{self.analyst_id}_"
            existing = []
            for path in self.cfg.agent_log_dir.glob(f"{prefix}*.log"):
                try:
                    existing.append(int(path.stem.removeprefix(prefix)))
                except ValueError:
                    continue
            self.dispatch = max(existing, default=0)
        self.dispatch += 1
        return self.cfg.agent_log_dir / f"{self.analyst_id}_{self.dispatch:03d}.log"

    def _start(self) -> None:
        system_prompt = (PROMPT_DIR / "analyst.txt").read_text()
        task_prompt = self._build_task_prompt(
            self.focus_metrics, self.candidate_leads,
        )

        self.log_path = self._next_log_path()
        target = self.worktree / self.cfg.target_subdir if self.cfg.target_subdir not in (".", "") else self.worktree
        self.process = spawn_claude_agent(
            agent_id=self.analyst_id,
            cli_path=self.cfg.claude_cli,
            cwd=target,
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
        quota_exhausted = bool(
            self.cfg.api_provider == "subscription"
            and self.log_path
            and has_subscription_quota_error(self.log_path)
        )
        if quota_exhausted:
            self.queue.put(mq.Message(
                sender=self.analyst_id,
                kind=mq.ANALYST_FINISHED,
                payload={
                    "analyst_id": self.analyst_id,
                    "returncode": returncode,
                    "review_completed": False,
                    "subscription_quota_exhausted": True,
                },
            ))
            return
        issues = self._parse_issues_from_log()
        self.queue.put(mq.Message(
            sender=self.analyst_id,
            kind=mq.ADD_ISSUES,
            payload={"issues": issues},
        ))
        # Analysts are never killed by the coordination layer, so any
        # non-zero exit is a crash of the CLI (counted for §5.4).
        self.queue.put(mq.Message(
            sender=self.analyst_id,
            kind=mq.ANALYST_FINISHED,
            payload={
                "analyst_id": self.analyst_id,
                "returncode": returncode,
                "review_completed": bool(
                    self.log_path
                    and has_successful_terminal_result(self.log_path)
                ),
            },
        ))

    def _build_task_prompt(
        self,
        focus_metrics: list[str],
        candidate_leads: Optional[list[LocalLead]] = None,
    ) -> str:
        focus = ", ".join(focus_metrics) if focus_metrics else "any metric"
        target = self.worktree / self.cfg.target_subdir if self.cfg.target_subdir not in (".", "") else self.worktree
        breakdown = self.metric_breakdown or "  (not measured)"
        active_metrics = [
            name for name, weight in self.cfg.weights.items()
            if weight > 0
        ]
        disabled_metrics = [
            name for name, weight in self.cfg.weights.items()
            if weight <= 0
        ]
        duplicate_binary = (
            getattr(self.cfg, "dupl_binary", "")
            if getattr(self.cfg, "lizard_language", "cpp").lower() == "go"
            else getattr(self.cfg, "duplo_binary", "")
        )
        tool_scope = (
            f"\nACTIVE METRICS: {', '.join(active_metrics) or '(none)'}\n"
            f"DISABLED METRICS: {', '.join(disabled_metrics) or '(none)'}\n"
            f"DUPLICATION TOOL: "
            f"{'configured' if duplicate_binary else 'not configured'}\n"
            "Do not search for, install, or run tools for disabled metrics. "
            "Do not search the host filesystem for analysis binaries; use "
            "only the commands and local leads already provided."
        )
        leads = list(candidate_leads or [])
        lead_block = ""
        if leads:
            lead_block = (
                "\n\nLOCAL STATIC-ANALYSIS LEADS (not confirmed issues)\n"
                f"{format_local_leads(leads)}\n\n"
                "These are non-authoritative static-analysis leads. Inspect the named "
                "functions only for this dispatch and do not repeat a "
                "whole-tree Lizard scan. Do not inspect or report unrelated "
                "functions. Independently decide "
                "whether each function is suitable for refactoring. Emit an "
                "ISSUE line only for a lead you confirm; never copy a lead "
                "automatically into the backlog."
            )
        return (
            f"Scan the source tree under {target} for quality "
            f"violations. Focus on: {focus}.\n\n"
            f"CURRENT PENALTY BREAKDOWN (ranked by remaining potential)\n"
            f"{breakdown}"
            f"{tool_scope}"
            f"{lead_block}\n\n"
            f"Target the metrics that still hold the most penalty rather "
            f"than scanning indiscriminately. Run Lizard or other relevant "
            f"tools as needed, then emit one ISSUE: line per violation in "
            f"the format defined in your system prompt. Group multiple "
            f"violations in the same function into one line. Your final "
            f"response MUST contain the ISSUE lines themselves; a prose "
            f"statement that a lead is confirmed is not sufficient. After "
            f"all issues are emitted, stop."
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
        if not issues:
            recovered = self._recover_explicitly_confirmed_lead(text)
            if recovered is not None:
                print(
                    f"[{self.analyst_id}] recovered one explicitly confirmed "
                    "local lead after a missing ISSUE protocol line"
                )
                issues.append(recovered)
        return issues

    def _recover_explicitly_confirmed_lead(self, text: str) -> Optional[dict]:
        """Recover only an unambiguous one-lead protocol omission.

        Local leads never enter the backlog on their own. Recovery requires
        one and only one lead, a successful terminal provider result, the
        function name in the response, and explicit confirmation language.
        This preserves the Analyst as final decision-maker while tolerating
        providers that complete the analysis but omit the requested ISSUE
        line.
        """
        if (
            len(self.candidate_leads) != 1
            or self.log_path is None
            or not has_successful_terminal_result(self.log_path)
        ):
            return None
        lead = self.candidate_leads[0]
        lowered = text.lower()
        if lead.function.lower() not in lowered:
            return None
        confirmations = (
            "i confirm this lead",
            "i confirm the lead",
            "this lead is confirmed",
            "confirmed local lead",
        )
        if not any(phrase in lowered for phrase in confirmations):
            return None

        metrics = ", ".join(
            f"{name.upper()}={value:g}"
            for name, value in sorted(lead.metric_values.items())
        )
        if "ccn" in lead.metric_values:
            issue_type = "complexity"
        elif "nloc" in lead.metric_values:
            issue_type = "longFunction"
        else:
            issue_type = "parameters"
        return {
            "file_path": lead.file_path,
            "line": lead.line,
            "severity": "complexity",
            "issue_type": issue_type,
            "message": (
                f"Function {lead.function} explicitly confirmed by Analyst; "
                f"{metrics}"
            ),
        }
