"""Penalty time series for post-run analysis.

Every event that changes the system's penalty state — a merge, a hard
timeout, a discretionary termination, the final stop — is appended here
with a wall-clock offset, so a run can be plotted and compared against
other runs after the fact.

Only the coordination layer writes to this file, so it inherits the
single-writer guarantee of the rest of the shared state. Writes are
atomic (mkstemp + os.replace) for the same reason the backlog is.
"""

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

MERGE = "merge"
TIMEOUT_KILL = "timeout_kill"
STUCK_TERMINATE = "stuck_terminate"
ANALYST_TIMEOUT = "analyst_timeout"
BASELINE = "baseline"
STOP = "stop"


@dataclass
class PenaltyHistory:
    path: Path
    baseline_penalty: float = 0.0
    started_at: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)

    def start(self, baseline_penalty: float, breakdown: Optional[dict] = None) -> None:
        """Open the timeline at the baseline measurement.

        The clock restarts here so elapsed_sec is measured from the
        baseline rather than from object construction, which happens
        before worktree provisioning and would otherwise offset every
        point by the setup time.
        """
        self.baseline_penalty = baseline_penalty
        self.started_at = time.time()
        self.record(BASELINE, penalty=baseline_penalty, breakdown=breakdown)

    @classmethod
    def load(cls, path: Path) -> Optional["PenaltyHistory"]:
        """Restore an existing timeline without overwriting prior events."""
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
            events = payload["events"]
            if not isinstance(events, list):
                return None
            return cls(
                path=path,
                baseline_penalty=float(payload["baseline_penalty"]),
                started_at=float(payload["started_at"]),
                events=events,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
            return None

    def record(self, event: str, penalty: float, **fields: Any) -> None:
        entry = {
            "event": event,
            "elapsed_sec": round(time.time() - self.started_at, 2),
            "penalty": penalty,
            "total_reduction": round(self.baseline_penalty - penalty, 4),
        }
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.events.append(entry)
        self.persist()

    def event_count(self, event: str) -> int:
        return sum(1 for e in self.events if e["event"] == event)

    def merge_count(self) -> int:
        return self.event_count(MERGE)

    def persist(self) -> None:
        payload = {
            "baseline_penalty": self.baseline_penalty,
            "started_at": self.started_at,
            "merges": self.merge_count(),
            "events": self.events,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".penalty-", suffix=".json", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- plotting ------------------------------------------------------

    def plot(self, out_path: Path) -> bool:
        """Render the penalty curve. Returns False if matplotlib is absent."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            return False

        if not self.events:
            return False

        xs = [e["elapsed_sec"] / 60.0 for e in self.events]
        ys = [e["penalty"] for e in self.events]

        fig, ax = plt.subplots(figsize=(8, 4.5))
        ax.step(xs, ys, where="post", color="#1f77b4", linewidth=1.6)

        merges = [
            (e["elapsed_sec"] / 60.0, e["penalty"])
            for e in self.events if e["event"] == MERGE
        ]
        if merges:
            ax.scatter(
                [m[0] for m in merges], [m[1] for m in merges],
                s=26, color="#2ca02c", zorder=3, label=f"merges ({len(merges)})",
            )
        kills = [
            (e["elapsed_sec"] / 60.0, e["penalty"])
            for e in self.events
            if e["event"] in (TIMEOUT_KILL, STUCK_TERMINATE, ANALYST_TIMEOUT)
        ]
        if kills:
            ax.scatter(
                [k[0] for k in kills], [k[1] for k in kills],
                s=34, marker="x", color="#d62728", zorder=3,
                label=f"terminations ({len(kills)})",
            )

        ax.axhline(
            self.baseline_penalty, linestyle="--", linewidth=1,
            color="#888888", label=f"baseline ({self.baseline_penalty:.1f})",
        )
        ax.set_xlabel("elapsed time (minutes)")
        ax.set_ylabel("total penalty")
        ax.set_title("Penalty over the course of the run")
        ax.legend(loc="upper right", fontsize=8)
        fig.tight_layout()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        return True
