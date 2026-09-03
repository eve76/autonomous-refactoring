"""Product backlog: thread-safe via single-writer protocol.

Agents read deep-copy snapshots and post change requests to a queue.
The coordination layer drains the queue, mutates in-memory state,
and atomically rewrites the JSON file via mkstemp + os.replace.
"""

import copy
import json
import os
import tempfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from threading import Lock
from typing import Optional


TODO = "TODO"
IN_PROGRESS = "IN_PROGRESS"
DONE = "DONE"
SKIPPED = "SKIPPED"


@dataclass
class Issue:
    id: str
    file_path: str
    line: int
    severity: str
    issue_type: str
    message: str
    metric_values: dict
    estimated_penalty_reduction: float = 0.0
    impact: str = "low"
    status: str = TODO
    assigned_to: Optional[str] = None
    skip_reason: Optional[str] = None
    dispatch_count: int = 0
    attempt_history_path: str = ""
    attempt_feedback: list[dict] = field(default_factory=list)


@dataclass
class Backlog:
    items: dict[str, Issue] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"items": {iid: asdict(it) for iid, it in self.items.items()}}

    @classmethod
    def from_dict(cls, data: dict) -> "Backlog":
        items = {iid: Issue(**body) for iid, body in data.get("items", {}).items()}
        return cls(items=items)


class BacklogStore:
    """Owns the in-memory backlog and persists it atomically.

    Only the coordination layer's drain loop calls the mutating
    methods. The snapshot() method is the one safe read path for
    agent threads.
    """

    def __init__(self, path: Path):
        self.path = path
        self._backlog = Backlog()
        self._lock = Lock()
        self._next_id = 1

    def load_or_init(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self._backlog = Backlog.from_dict(data)
            self._next_id = self._compute_next_id()

    def _compute_next_id(self) -> int:
        max_n = 0
        for iid in self._backlog.items:
            try:
                n = int(iid.split("-")[-1])
                max_n = max(max_n, n)
            except ValueError:
                continue
        return max_n + 1

    def snapshot(self) -> Backlog:
        with self._lock:
            return copy.deepcopy(self._backlog)

    def add_issue(self, issue: Issue) -> Optional[str]:
        if self._is_duplicate(issue):
            return None
        if not issue.id:
            issue.id = f"ISSUE-{self._next_id:04d}"
            self._next_id += 1
        self._backlog.items[issue.id] = issue
        return issue.id

    def _is_duplicate(self, issue: Issue) -> bool:
        for existing in self._backlog.items.values():
            if (existing.file_path == issue.file_path
                    and existing.line == issue.line
                    and existing.issue_type == issue.issue_type):
                return True
        return False

    def mark_in_progress(self, issue_id: str, agent: str) -> None:
        if issue_id in self._backlog.items:
            self._backlog.items[issue_id].status = IN_PROGRESS
            self._backlog.items[issue_id].assigned_to = agent

    def mark_dispatched(self, issue_id: str, agent: str, limit: int) -> bool:
        """Claim one TODO issue and consume one run-wide dispatch chance."""
        item = self._backlog.items.get(issue_id)
        if item is None or item.status != TODO or item.dispatch_count >= limit:
            return False
        item.dispatch_count += 1
        item.status = IN_PROGRESS
        item.assigned_to = agent
        return True

    def update_attempt_state(
        self, issue_id: str, history_path: str, feedback: list[dict],
    ) -> bool:
        item = self._backlog.items.get(issue_id)
        if item is None:
            return False
        changed = (
            item.attempt_history_path != history_path
            or item.attempt_feedback != feedback
        )
        item.attempt_history_path = history_path
        item.attempt_feedback = copy.deepcopy(feedback)
        return changed

    def mark_done(self, issue_id: str) -> None:
        if issue_id in self._backlog.items:
            self._backlog.items[issue_id].status = DONE

    def mark_skipped(self, issue_id: str, reason: str) -> None:
        if issue_id in self._backlog.items:
            self._backlog.items[issue_id].status = SKIPPED
            self._backlog.items[issue_id].skip_reason = reason

    def return_to_todo(self, issue_id: str) -> None:
        if issue_id in self._backlog.items:
            self._backlog.items[issue_id].status = TODO
            self._backlog.items[issue_id].assigned_to = None

    def recover_in_progress(self) -> int:
        """Return crash-interrupted assignments to TODO on resume."""
        recovered = 0
        for item in self._backlog.items.values():
            if item.status == IN_PROGRESS:
                item.status = TODO
                item.assigned_to = None
                recovered += 1
        return recovered

    def persist(self) -> None:
        """Atomic write: tmp file in same dir, then os.replace."""
        data = json.dumps(self._backlog.to_dict(), indent=2)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".backlog-",
            suffix=".json",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
