"""Crash-recovery state for the coordination layer.

Written after every merge and on shutdown. On startup with --resume the
coordinator restores the baseline penalty, the current penalty and the
stagnation counter instead of re-deriving them, so a run that died
mid-way can be continued rather than restarted from scratch.

The backlog itself already persists separately (backlog.json); this file
carries only the scalar orchestration state that would otherwise be lost.
"""

import json
import os
import tempfile
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional


@dataclass
class RunState:
    run_id: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    baseline_penalty: float = 0.0
    current_penalty: float = 0.0
    stagnation_counter: int = 0
    merges: int = 0
    stop_reason: str = ""
    baseline_metric_stats: dict = field(default_factory=dict)
    agent_crashes: int = 0
    phantom_issues: int = 0
    analyst_lead_seen_keys: list[str] = field(default_factory=list)
    budget_dispatch_closed: bool = False
    budget_trigger: str = ""
    budget_usage_at_close: dict = field(default_factory=dict)
    api_provider: str = ""
    orchestrator_model: str = ""
    agent_model: str = ""
    optimization_fingerprint: str = ""
    # This excludes only transport identity. It permits an explicitly audited
    # subscription -> OpenRouter resume while preserving all experiment,
    # measurement, scope, budget, and validation settings.
    optimization_core_fingerprint: str = ""
    provider_transitions: list[dict] = field(default_factory=list)
    # Immutable commit from which the run branch was created. Persist it
    # separately from a symbolic baseline such as HEAD: after a crash, HEAD
    # may point at the already-advanced integration branch.
    baseline_commit: str = ""
    original_checkout_ref: str = ""
    original_checkout_commit: str = ""
    original_checkout_detached: bool = False
    baseline_build_status: str = "not_requested"
    baseline_build_elapsed_sec: float = 0.0
    baseline_build_log: str = ""

    def save(self, path: Path) -> None:
        self.updated_at = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=".state-", suffix=".json", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(asdict(self), fh, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def load(cls, path: Path) -> Optional["RunState"]:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
