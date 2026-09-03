"""Per-issue refactoring attempt history and saved patches."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


_SAFE_ISSUE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def issue_dir(root: Path, issue_id: str) -> Path:
    safe_id = _SAFE_ISSUE_ID.sub("_", issue_id) or "unknown"
    return root / safe_id


def history_path(root: Path, issue_id: str) -> Path:
    return issue_dir(root, issue_id) / "history.jsonl"


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open() as fh:
        for raw in fh:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records


def append(path: Path, record: dict) -> None:
    """Append one complete record atomically for concurrent gate writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    value = dict(record)
    value.setdefault("timestamp", round(time.time(), 3))
    value.setdefault("attempt_number", len(load(path)) + 1)
    line = json.dumps(value, sort_keys=True) + "\n"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def feedback(records: list[dict], limit: int = 3) -> list[dict]:
    """Return a bounded prompt-friendly summary of the latest attempts."""
    keys = (
        "attempt_number", "outcome", "strategy", "penalty_before",
        "penalty_after", "reason", "patch_id", "patch_file",
    )
    return [
        {key: record[key] for key in keys if record.get(key) not in (None, "")}
        for record in records[-limit:]
    ]
