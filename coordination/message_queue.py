"""Message types posted by agents to the coordination layer.

The coordination layer is the single writer of shared state. Agents
post change requests onto this queue and proceed without waiting.
"""

from dataclasses import dataclass
from queue import Queue
from typing import Any


@dataclass
class Message:
    sender: str
    kind: str
    payload: dict[str, Any]


ADD_ISSUES = "add_issues"
MARK_DONE = "mark_done"
MARK_SKIPPED = "mark_skipped"
PROGRAMMER_FINISHED = "programmer_finished"
ANALYST_FINISHED = "analyst_finished"
PROGRAMMER_HEARTBEAT = "programmer_heartbeat"
MERGE_RESULT = "merge_result"


def make_queue() -> Queue:
    return Queue()
