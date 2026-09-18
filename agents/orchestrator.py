"""Orchestrator: synchronous LLM calls (NOT a persistent process).

Per the thesis, the coordination layer calls the orchestrator
synchronously at two decision points:

  1. Task assignment — given the penalty scores, a per-metric breakdown,
     every active item, a high-impact TODO window and the idle agents, it
     returns an assignment plan. The complete backlog remains local.
  2. Stuck-agent evaluation — given each stuck programmer's runtime,
     recent log output and whether it has made edits or run the merge
     gate, it decides whether to terminate or keep each agent, and may
     additionally mark specific issues as infeasible.

Subscription mode uses one tool-free, schema-constrained Claude Code CLI turn.
Explicit Anthropic/DeepSeek API modes retain the Messages API transport;
DeepSeek forces schema-backed tool results.
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from agents import PROMPT_DIR
from agents.agent_runner import run_claude_once
from agents.log_parser import is_subscription_quota_error
from agents.provider import (
    agent_subprocess_environment,
    make_orchestrator_client,
    validate_subscription_auth,
)
from config import Config
from coordination.backlog import (
    Backlog, DONE, IN_PROGRESS, SKIPPED, TODO,
)
from coordination.token_usage import (
    append_orchestrator_raw_response,
    append_orchestrator_usage,
)


_ASSIGNMENT_TOOL = {
    "name": "submit_assignment",
    "description": (
        "Submit the complete assignment decision for this coordinator tick. "
        "Use only idle agent IDs and TODO issue IDs present in the prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "programmer_assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "programmer_id": {"type": "string"},
                        "issue_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "maxItems": 2,
                        },
                    },
                    "required": ["programmer_id", "issue_ids"],
                    "additionalProperties": False,
                },
            },
            "analyst_assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "analyst_id": {"type": "string"},
                        "focus": {"type": "string"},
                    },
                    "required": ["analyst_id", "focus"],
                    "additionalProperties": False,
                },
            },
            "reasoning": {"type": "string"},
        },
        "required": [
            "programmer_assignments", "analyst_assignments", "reasoning",
        ],
        "additionalProperties": False,
    },
}

_STUCK_TOOL = {
    "name": "submit_stuck_evaluation",
    "description": "Submit verdicts for all stuck programmers.",
    "input_schema": {
        "type": "object",
        "properties": {
            "terminate": {"type": "array", "items": {"type": "string"}},
            "keep": {"type": "array", "items": {"type": "string"}},
            "infeasible_issues": {
                "type": "array", "items": {"type": "string"},
            },
            "reasoning": {"type": "string"},
        },
        "required": ["terminate", "keep", "infeasible_issues", "reasoning"],
        "additionalProperties": False,
    },
}


class SubscriptionQuotaExhausted(RuntimeError):
    """Claude Code reported that the shared subscription limit was reached."""


def compact_backlog_view(backlog: Backlog, todo_limit: int) -> dict:
    """Return the deterministic assignment-only view sent to the LLM.

    All IN_PROGRESS records remain visible for file-conflict checks. Only the
    highest-impact TODO records are useful because one turn can assign at most
    six issues. DONE/SKIPPED details remain in the local persisted backlog.
    """
    counts = {TODO: 0, IN_PROGRESS: 0, DONE: 0, SKIPPED: 0}
    active = []
    todo = []
    for issue_id, item in backlog.items.items():
        counts[item.status] = counts.get(item.status, 0) + 1
        pair = (issue_id, item)
        if item.status == IN_PROGRESS:
            active.append(pair)
        elif item.status == TODO:
            todo.append(pair)

    active.sort(key=lambda pair: pair[0])
    todo.sort(key=lambda pair: (
        -float(pair[1].estimated_penalty_reduction),
        pair[0],
    ))
    selected_todo = todo[:todo_limit]
    selected = [*active, *selected_todo]
    return {
        "status_counts": {
            "total": len(backlog.items),
            "todo": counts[TODO],
            "in_progress": counts[IN_PROGRESS],
            "done": counts[DONE],
            "skipped": counts[SKIPPED],
        },
        "selection": {
            "todo_limit": todo_limit,
            "todo_shown": len(selected_todo),
            "todo_omitted": max(0, len(todo) - len(selected_todo)),
            "all_in_progress_included": True,
            "order": "estimated_penalty_reduction_desc_then_issue_id",
        },
        "items": {
            issue_id: asdict(item) for issue_id, item in selected
        },
    }


@dataclass
class AssignmentDecision:
    programmer_assignments: dict[str, list[str]] = field(default_factory=dict)
    dispatch_analysts: list[str] = field(default_factory=list)
    analyst_targets: dict[str, str] = field(default_factory=dict)
    reasoning: str = ""


@dataclass
class StuckDecision:
    """Verdicts for stuck programmers, keyed by programmer id."""
    terminate: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    infeasible_issues: list[str] = field(default_factory=list)
    reasoning: str = ""


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if cfg.api_provider == "subscription":
            self.client = None
            self._subscription_auth_validated = False
        else:
            self.client = make_orchestrator_client(cfg)
            self._subscription_auth_validated = True

    def validate_transport(self) -> None:
        """Validate subscription auth before experimental state is created."""
        if self.cfg.api_provider != "subscription":
            return
        auth = validate_subscription_auth(self.cfg)
        self.cfg.subscription_type = auth["subscription_type"]
        self.cfg.claude_cli_version = auth["claude_cli_version"]
        self._subscription_auth_validated = True

    # -- decision point 1: task assignment ----------------------------

    def assign(
        self,
        current_penalty: float,
        baseline_penalty: float,
        backlog: Backlog,
        idle_programmers: list[str],
        idle_analysts: list[str],
        stagnation: int,
        metric_breakdown: str = "",
    ) -> AssignmentDecision:
        template = (PROMPT_DIR / "orchestrator_assignment.txt").read_text()
        user_msg = template.format(
            current_penalty=current_penalty,
            baseline_penalty=baseline_penalty,
            metric_breakdown=metric_breakdown or "  (not measured)",
            backlog_json=json.dumps(
                compact_backlog_view(
                    backlog, self.cfg.orchestrator_backlog_top_k,
                ),
                separators=(",", ":"),
                sort_keys=True,
            ),
            idle_programmers=", ".join(idle_programmers) or "(none)",
            idle_analysts=", ".join(idle_analysts) or "(none)",
            stagnation=stagnation,
        )
        return self._parse_assignment(self._call(user_msg, "assignment"))

    # -- decision point 2: stuck-agent evaluation ---------------------

    def evaluate_stuck(
        self,
        stuck: list[dict],
        stagnation: int,
        hard_timeout_sec: int,
    ) -> StuckDecision:
        """Judge stuck programmers.

        `stuck` carries one dict per programmer with keys: programmer_id,
        runtime_sec, edits_made, gate_invocations, assigned_issues,
        recent_log.
        """
        template = (PROMPT_DIR / "orchestrator_stuck.txt").read_text()
        blocks = []
        for s in stuck:
            blocks.append(
                f"--- {s['programmer_id']} ---\n"
                f"runtime: {s['runtime_sec']:.0f}s "
                f"(hard timeout at {hard_timeout_sec}s)\n"
                f"assigned issues: {', '.join(s['assigned_issues']) or '(none)'}\n"
                f"has made file edits: {'yes' if s['edits_made'] else 'no'}\n"
                f"merge-gate invocations: {s['gate_invocations']}\n"
                f"recent log output:\n{s['recent_log'] or '(no output captured)'}"
            )
        user_msg = template.format(
            stuck_agents="\n\n".join(blocks),
            stagnation=stagnation,
        )
        return self._parse_stuck(self._call(user_msg, "stuck_evaluation"))

    # -- transport -----------------------------------------------------

    def _call(self, user_msg: str, call_type: str = "unknown") -> str | dict:
        if self.cfg.api_provider == "subscription":
            return self._call_subscription(user_msg, call_type)
        kwargs = dict(
            model=self.cfg.orchestrator_model,
            max_tokens=4096,
            messages=[{"role": "user", "content": user_msg}],
        )
        if self.cfg.api_provider == "deepseek":
            # This role only selects work from a bounded snapshot. Disable
            # DeepSeek thinking so the final decision cannot be crowded out
            # by reasoning tokens, and force a typed tool call instead of
            # relying on free-form text protocol compliance.
            kwargs["thinking"] = {"type": "disabled"}
            tool = (
                _ASSIGNMENT_TOOL
                if call_type == "assignment"
                else _STUCK_TOOL if call_type == "stuck_evaluation" else None
            )
            if tool is not None:
                kwargs["tools"] = [tool]
                kwargs["tool_choice"] = {
                    "type": "tool", "name": tool["name"],
                }
        resp = self.client.messages.create(**kwargs)
        # Concatenate only text blocks for the existing parser, but retain
        # the complete provider response (including non-text blocks) for
        # post-run diagnosis of malformed or truncated assignment output.
        parts = []
        structured_input = None
        expected_tool = (
            _ASSIGNMENT_TOOL["name"]
            if call_type == "assignment"
            else _STUCK_TOOL["name"]
            if call_type == "stuck_evaluation"
            else None
        )
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            elif (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == expected_tool
                and isinstance(getattr(block, "input", None), Mapping)
            ):
                structured_input = dict(block.input)
        text = "".join(parts)
        try:
            append_orchestrator_raw_response(
                self.cfg.run_results_path
                / self.cfg.orchestrator_raw_responses_filename,
                response=resp,
                parsed_text=text,
                structured_input=structured_input,
                provider=self.cfg.api_provider,
                model=self.cfg.orchestrator_model,
                call_type=call_type,
            )
        except Exception as exc:
            # Capture is diagnostic only; a filesystem problem must not
            # change the model's assignment decision.
            print(f"[orchestrator] failed to record raw response: {exc}")
        try:
            append_orchestrator_usage(
                self.cfg.run_results_path / self.cfg.orchestrator_usage_filename,
                usage=getattr(resp, "usage", None),
                provider=self.cfg.api_provider,
                model=self.cfg.orchestrator_model,
                call_type=call_type,
                source="orchestrator_cli",
            )
        except Exception as exc:
            # Accounting must never change an assignment decision.
            print(f"[orchestrator] failed to record token usage: {exc}")
        return structured_input if structured_input is not None else text

    def _call_subscription(
        self,
        user_msg: str,
        call_type: str,
    ) -> str | dict:
        if not getattr(self, "_subscription_auth_validated", False):
            self.validate_transport()
        schema = (
            _ASSIGNMENT_TOOL["input_schema"]
            if call_type == "assignment"
            else _STUCK_TOOL["input_schema"]
            if call_type == "stuck_evaluation"
            else {"type": "object"}
        )
        result = run_claude_once(
            cli_path=self.cfg.claude_cli,
            cwd=PROMPT_DIR.parent,
            system_prompt=(
                "You are the deterministic decision component inside a "
                "code-quality experiment. Follow the user prompt exactly and "
                "return only an object matching the supplied JSON schema."
            ),
            task_prompt=user_msg,
            model=self.cfg.orchestrator_model,
            json_schema=schema,
            extra_args=[
                "--safe-mode",
                "--no-session-persistence",
                "--disable-slash-commands",
            ],
            env=agent_subprocess_environment(self.cfg),
        )
        structured_input = result.structured_output
        if not isinstance(structured_input, Mapping) and result.result_text:
            try:
                candidate = json.loads(result.result_text)
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, Mapping):
                structured_input = dict(candidate)

        raw_response = {
            "returncode": result.returncode,
            "events": result.events,
            "stderr": result.stderr,
        }
        parsed_text = result.result_text
        try:
            append_orchestrator_raw_response(
                self.cfg.run_results_path
                / self.cfg.orchestrator_raw_responses_filename,
                response=raw_response,
                parsed_text=parsed_text,
                structured_input=structured_input,
                provider=self.cfg.api_provider,
                model=self.cfg.orchestrator_model,
                call_type=call_type,
                source="orchestrator_cli",
            )
        except Exception as exc:
            print(f"[orchestrator] failed to record raw response: {exc}")

        terminal = result.terminal
        try:
            append_orchestrator_usage(
                self.cfg.run_results_path
                / self.cfg.orchestrator_usage_filename,
                usage=terminal.get("usage"),
                provider=self.cfg.api_provider,
                model=self.cfg.orchestrator_model,
                call_type=call_type,
                reported_cost_usd=terminal.get("total_cost_usd"),
                turns_or_calls=terminal.get("num_turns", 1),
                model_usage=terminal.get("modelUsage"),
                source="orchestrator_cli",
            )
        except Exception as exc:
            print(f"[orchestrator] failed to record token usage: {exc}")

        if is_subscription_quota_error({
            "terminal": terminal,
            "stderr": result.stderr,
        }):
            raise SubscriptionQuotaExhausted(
                "Claude Code subscription usage limit reached; resume this "
                "run after the subscription window resets"
            )
        # Match the DeepSeek API path's tolerant tool-input semantics. Claude
        # Code may reject a StructuredOutput envelope for an extra property
        # and then hit the one-turn ceiling, even though the first typed
        # decision is usable by our parser. In that exact case, accept the
        # recovered tool input instead of failing the whole experiment.
        recovered_first_decision = (
            isinstance(structured_input, Mapping)
            and not isinstance(terminal.get("structured_output"), Mapping)
            and terminal.get("subtype") == "error_max_turns"
            and terminal.get("stop_reason") == "tool_use"
        )
        if not recovered_first_decision and (
            result.returncode != 0
            or terminal.get("is_error") is True
            or not isinstance(structured_input, Mapping)
        ):
            raise RuntimeError(
                "Claude Code subscription orchestrator failed or omitted "
                f"structured output (exit {result.returncode})"
            )
        return dict(structured_input)

    # -- parsing -------------------------------------------------------

    def _parse_assignment(self, payload: str | Mapping[str, Any]) -> AssignmentDecision:
        decision = AssignmentDecision()
        if isinstance(payload, Mapping):
            for item in payload.get("programmer_assignments", []):
                if not isinstance(item, Mapping):
                    continue
                programmer_id = str(item.get("programmer_id", "")).upper()
                issue_ids = item.get("issue_ids", [])
                if programmer_id.startswith("PROG_") and isinstance(issue_ids, list):
                    decision.programmer_assignments[programmer_id] = [
                        str(issue_id) for issue_id in issue_ids if issue_id
                    ][:2]
            for item in payload.get("analyst_assignments", []):
                if not isinstance(item, Mapping):
                    continue
                analyst_id = str(item.get("analyst_id", "")).upper()
                if not analyst_id.startswith("ANALYST_"):
                    continue
                decision.dispatch_analysts.append(analyst_id)
                focus = str(item.get("focus", ""))
                if focus:
                    decision.analyst_targets[analyst_id] = focus
            decision.reasoning = str(payload.get("reasoning", ""))
            return decision
        text = payload
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

    def _parse_stuck(self, payload: str | Mapping[str, Any]) -> StuckDecision:
        decision = StuckDecision()
        if isinstance(payload, Mapping):
            decision.terminate = [
                str(value).upper() for value in payload.get("terminate", [])
                if value
            ]
            decision.keep = [
                str(value).upper() for value in payload.get("keep", [])
                if value
            ]
            decision.infeasible_issues = [
                str(value) for value in payload.get("infeasible_issues", [])
                if value
            ]
            decision.reasoning = str(payload.get("reasoning", ""))
            return decision
        text = payload
        for raw in text.splitlines():
            line = raw.strip()
            if not line or ":" not in line:
                continue
            head, rest = line.split(":", 1)
            head = head.strip().upper()
            rest = rest.strip()
            if head.startswith("PROG_"):
                verdict = rest.lower()
                if verdict.startswith("terminate"):
                    decision.terminate.append(head)
                elif verdict.startswith("keep"):
                    decision.keep.append(head)
            elif head == "INFEASIBLE":
                decision.infeasible_issues.extend(
                    tok.strip() for tok in rest.split(",")
                    if tok.strip() and tok.strip().lower() != "none"
                )
            elif head == "REASONING":
                decision.reasoning = rest
        return decision
