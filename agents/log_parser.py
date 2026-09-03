"""Extract assistant-emitted text from Claude CLI stream-json logs.

Each line of the log is a JSON object. Assistant text appears either
in `assistant` messages (content blocks of type=text) or in the final
`result` message's `result` field. Tool-use / tool-result blocks are
ignored — those are noise for our purposes.
"""

import json
from pathlib import Path


_SUBSCRIPTION_QUOTA_MARKERS = (
    "usage limit reached",
    "usage limit",
    "hit your limit",
    "you've hit your limit",
    "you have hit your limit",
    "rate limit reached",
    "resets at",
    "resets in",
)


def is_subscription_quota_error(value) -> bool:
    """Recognize Claude Code's terminal subscription-limit diagnostics."""
    try:
        text = json.dumps(value, sort_keys=True, default=str).lower()
    except (TypeError, ValueError):
        text = str(value).lower()
    return any(marker in text for marker in _SUBSCRIPTION_QUOTA_MARKERS)


def has_subscription_quota_error(log_path: Path) -> bool:
    if not log_path.exists():
        return False
    terminal = None
    raw_parts = []
    with log_path.open(errors="replace") as fh:
        for raw in fh:
            raw_parts.append(raw)
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                terminal = obj
    if (
        terminal is not None
        and terminal.get("is_error") is True
        and is_subscription_quota_error(terminal)
    ):
        return True
    return terminal is None and is_subscription_quota_error("".join(raw_parts))


def has_successful_terminal_result(log_path: Path) -> bool:
    """Return whether a stream-json log ended in a successful result event.

    A zero process exit alone is insufficient: a truncated or malformed log
    must not make the coordinator treat a local-lead page as reviewed.
    """
    if not log_path.exists():
        return False
    terminal = None
    with log_path.open() as fh:
        for raw in fh:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "result":
                terminal = obj
    return bool(
        terminal is not None
        and terminal.get("is_error") is not True
        and isinstance(terminal.get("result"), str)
    )


def extract_assistant_text(log_path: Path) -> str:
    if not log_path.exists():
        return ""
    parts: list[str] = []
    with log_path.open() as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = obj.get("type")
            if kind == "assistant":
                msg = obj.get("message") or {}
                for block in msg.get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        parts.append(block["text"])
            elif kind == "result":
                final = obj.get("result")
                if isinstance(final, str) and final:
                    parts.append(final)
    return "\n".join(parts)
