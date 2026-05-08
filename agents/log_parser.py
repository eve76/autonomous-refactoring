"""Extract assistant-emitted text from Claude CLI stream-json logs.

Each line of the log is a JSON object. Assistant text appears either
in `assistant` messages (content blocks of type=text) or in the final
`result` message's `result` field. Tool-use / tool-result blocks are
ignored — those are noise for our purposes.
"""

import json
from pathlib import Path


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
