"""Orchestrator: synchronous LLM calls (NOT a persistent process).

The orchestrator is invoked at two decision points:
  1. Task assignment — given backlog state and idle agents, return an
     assignment plan (programmer -> issue ids, analysts to dispatch).
  2. Stuck-agent evaluation — given heartbeat snapshots, decide which
     programmers to terminate or which issues to mark infeasible.

Implemented via the Anthropic Python SDK rather than a CLI subprocess,
since the orchestrator does not need file editing or tool use.
"""

import json
from dataclasses import dataclass, field
from typing import Optional

from anthropic import Anthropic

from agents import PROMPT_DIR
from config import Config
from coordination.backlog import Backlog


@dataclass
class AssignmentDecision:
    programmer_assignments: dict[str, list[str]] = field(default_factory=dict)
    dispatch_analysts: list[str] = field(default_factory=list)
    analyst_targets: dict[str, str] = field(default_factory=dict)
    reasoning: str = ""


@dataclass
class StuckAgentReport:
    agent_id: str
    runtime_sec: float
    recent_log: str
    edits_made: bool
    ran_gate: bool
    assigned_issues: list[str]


@dataclass
class StuckDecision:
    terminate: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    mark_infeasible: dict[str, str] = field(default_factory=dict)
    reasoning: str = ""


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = Anthropic()

    def assign(
        self,
        current_penalty: float,
        baseline_penalty: float,
        backlog: Backlog,
        idle_programmers: list[str],
        idle_analysts: list[str],
        stagnation: int,
    ) -> AssignmentDecision:
        template = (PROMPT_DIR / "orchestrator_assignment.txt").read_text()
        user_msg = template.format(
            current_penalty=current_penalty,
            baseline_penalty=baseline_penalty,
            backlog_json=json.dumps(backlog.to_dict(), indent=2),
            idle_programmers=", ".join(idle_programmers) or "(none)",
            idle_analysts=", ".join(idle_analysts) or "(none)",
            stagnation=stagnation,
        )
        text = self._call(user_msg)
        return self._parse_assignment(text)

    def evaluate_stuck(
        self, reports: list[StuckAgentReport]
    ) -> StuckDecision:
        template = (PROMPT_DIR / "orchestrator_stuck.txt").read_text()
        user_msg = template.format(
            reports_json=json.dumps([r.__dict__ for r in reports], indent=2),
        )
        text = self._call(user_msg)
        return self._parse_stuck(text)

    def _call(self, user_msg: str) -> str:
        resp = self.client.messages.create(
            model=self.cfg.orchestrator_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_msg}],
        )
        # Concatenate all text blocks from the assistant message.
        parts = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts)

    def _parse_assignment(self, text: str) -> AssignmentDecision:
        decision = AssignmentDecision()
        for raw in text.splitlines():
            line = raw.strip()
            if not line or ":" not in line:
                continue
            head, rest = line.split(":", 1)
            head = head.strip().upper()
            rest = rest.strip()
            if head.startswith("PROG_"):
                ids = [tok.strip() for tok in rest.split(",") if tok.strip()]
                decision.programmer_assignments[head] = ids
            elif head.startswith("ANALYST_"):
                decision.dispatch_analysts.append(head)
                if rest:
                    decision.analyst_targets[head] = rest
            elif head == "REASONING":
                decision.reasoning = rest
        return decision

    def _parse_stuck(self, text: str) -> StuckDecision:
        decision = StuckDecision()
        for raw in text.splitlines():
            line = raw.strip()
            if not line or ":" not in line:
                continue
            head, rest = line.split(":", 1)
            head = head.strip().upper()
            rest = rest.strip()
            if head == "TERMINATE":
                decision.terminate = [t.strip() for t in rest.split(",") if t.strip()]
            elif head == "KEEP":
                decision.keep = [t.strip() for t in rest.split(",") if t.strip()]
            elif head == "INFEASIBLE":
                for entry in rest.split(";"):
                    entry = entry.strip()
                    if " - " in entry:
                        iid, reason = entry.split(" - ", 1)
                        decision.mark_infeasible[iid.strip()] = reason.strip()
            elif head == "REASONING":
                decision.reasoning = rest
        return decision
