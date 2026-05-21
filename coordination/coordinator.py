"""Main coordination loop.

Owns all shared state. Runs on the main thread. Drains the message
queue in a single-writer loop, persists the backlog atomically, and
dispatches agents based on orchestrator decisions.
"""

import signal
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Optional

from config import Config
from coordination import message_queue as mq
from coordination.backlog import BacklogStore, Issue, TODO
from coordination.git_manager import (
    create_worktree,
    ensure_clean_main,
    remove_worktree,
    reset_worktree,
)
from coordination.stagnation import StagnationTracker
from agents.orchestrator import Orchestrator, AssignmentDecision
from agents.analyst import AnalystSession
from agents.programmer import ProgrammerSession
from analysis.tools import run_static_analysis
from analysis.penalty import (
    compute_total_penalty,
    estimate_reduction_from_message,
)


class Coordinator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.queue = mq.make_queue()
        self.backlog = BacklogStore(cfg.work_root / cfg.backlog_path)
        self.stagnation = StagnationTracker(
            min_merge_gain=cfg.min_merge_gain,
            limit=cfg.stagnation_limit,
        )

        self.baseline_penalty: float = 0.0
        self.current_penalty: float = 0.0

        self.orchestrator = Orchestrator(cfg)

        self.programmer_worktrees: dict[str, Path] = {}
        self.analyst_worktrees: dict[str, Path] = {}
        self.programmer_sessions: dict[str, ProgrammerSession] = {}
        self.analyst_sessions: dict[str, AnalystSession] = {}

        self.programmer_pool: Optional[ThreadPoolExecutor] = None
        self.analyst_pool: Optional[ThreadPoolExecutor] = None

        self.programmer_futures: dict[str, Future] = {}
        self.analyst_futures: dict[str, Future] = {}

        self.stop_reason: str = ""
        signal.signal(signal.SIGTERM, self._on_sigterm)

    # -- lifecycle -----------------------------------------------------

    def run(self) -> None:
        try:
            self._initialize()
            self._main_loop()
        finally:
            self._shutdown()

    def _initialize(self) -> None:
        self.cfg.work_root.mkdir(parents=True, exist_ok=True)
        (self.cfg.work_root / self.cfg.log_dir).mkdir(parents=True, exist_ok=True)

        ensure_clean_main(self.cfg.repo_root)

        for i in range(self.cfg.num_programmers):
            pid = f"PROG_{i+1}"
            wt = create_worktree(
                self.cfg.repo_root,
                self.cfg.work_root,
                pid,
                self.cfg.target_subdir,
            )
            self.programmer_worktrees[pid] = wt
            self.programmer_sessions[pid] = ProgrammerSession(
                programmer_id=pid,
                worktree=wt,
                cfg=self.cfg,
                queue=self.queue,
            )

        for i in range(self.cfg.num_analysts):
            aid = f"ANALYST_{i+1}"
            wt = create_worktree(
                self.cfg.repo_root,
                self.cfg.work_root,
                aid,
                self.cfg.target_subdir,
                detach_to_main=True,
            )
            self.analyst_worktrees[aid] = wt
            self.analyst_sessions[aid] = AnalystSession(
                analyst_id=aid,
                cfg=self.cfg,
                queue=self.queue,
                worktree=wt,
            )

        self.backlog.load_or_init()

        lizard_records, cognitive_records, dup_ratio = run_static_analysis(
            self.cfg.target_path, self.cfg.thresholds,
            duplo_binary=self.cfg.duplo_binary,
            duplo_min_block_lines=self.cfg.duplo_min_block_lines,
            lizard_binary=self.cfg.lizard_binary,
            language=self.cfg.lizard_language,
        )
        self.baseline_penalty = compute_total_penalty(
            lizard_records, cognitive_records,
            self.cfg.thresholds, dup_ratio, self.cfg.weights,
        )
        self.current_penalty = self.baseline_penalty

        self.programmer_pool = ThreadPoolExecutor(max_workers=self.cfg.num_programmers)
        self.analyst_pool = ThreadPoolExecutor(max_workers=self.cfg.num_analysts)

    def _on_sigterm(self, signum, frame) -> None:
        """SIGTERM handler (gnomad-kiro parity).

        Set wall_timeout so the main loop exits its tick. Active agents
        are killed; their worktrees are intentionally left as-is so any
        in-progress changes can be inspected post-mortem.
        """
        self.stop_reason = "wall_timeout"
        for pid, session in self.programmer_sessions.items():
            if pid in self.programmer_futures:
                try:
                    session.kill()
                except Exception:
                    pass

    def _shutdown(self) -> None:
        if self.programmer_pool is not None:
            self.programmer_pool.shutdown(wait=False, cancel_futures=True)
        if self.analyst_pool is not None:
            self.analyst_pool.shutdown(wait=False, cancel_futures=True)
        for wt in (*self.programmer_worktrees.values(), *self.analyst_worktrees.values()):
            try:
                remove_worktree(self.cfg.repo_root, wt)
            except Exception:
                pass

    # -- main loop -----------------------------------------------------

    def _main_loop(self) -> None:
        while not self.stagnation.should_stop() and self.stop_reason == "":
            self._drain_queue()
            self._reap_finished_futures()
            self._dispatch_if_needed()
            self._check_stuck_agents()
            time.sleep(self.cfg.backlog_drain_interval_sec)

    def _drain_queue(self) -> None:
        dirty = False
        while not self.queue.empty():
            msg: mq.Message = self.queue.get_nowait()
            self._apply_message(msg)
            dirty = True
        if dirty:
            self.backlog.persist()

    def _apply_message(self, msg: mq.Message) -> None:
        if msg.kind == mq.ADD_ISSUES:
            for raw in msg.payload.get("issues", []):
                reduction, parsed = estimate_reduction_from_message(
                    raw.get("message", ""), self.cfg.thresholds, self.cfg.weights,
                )
                impact = "high" if reduction >= self.cfg.min_merge_gain else "low"
                issue = Issue(
                    id="",
                    file_path=raw["file_path"],
                    line=int(raw["line"]),
                    severity=raw.get("severity", "info"),
                    issue_type=raw.get("issue_type", "unknown"),
                    message=raw.get("message", ""),
                    metric_values=parsed,
                    estimated_penalty_reduction=reduction,
                    impact=impact,
                )
                self.backlog.add_issue(issue)
            return

        if msg.kind == mq.MARK_DONE:
            self.backlog.mark_done(msg.payload["issue_id"])
            return

        if msg.kind == mq.MARK_SKIPPED:
            self.backlog.mark_skipped(
                msg.payload["issue_id"],
                msg.payload.get("reason", ""),
            )
            return

        if msg.kind == mq.MERGE_RESULT:
            before = float(msg.payload["penalty_before"])
            after = float(msg.payload["penalty_after"])
            reduction = before - after
            self.current_penalty = after
            self.stagnation.record_merge(reduction)
            return

        if msg.kind in (mq.PROGRAMMER_FINISHED, mq.ANALYST_FINISHED, mq.PROGRAMMER_HEARTBEAT):
            return

    def _reap_finished_futures(self) -> None:
        for futures in (self.programmer_futures, self.analyst_futures):
            for name in list(futures):
                if futures[name].done():
                    futures.pop(name, None)

    @staticmethod
    def _idle(sessions: dict, futures: dict) -> list[str]:
        return [name for name in sessions if name not in futures]

    # -- dispatch ------------------------------------------------------

    def _dispatch_if_needed(self) -> None:
        idle_programmers = self._idle(self.programmer_sessions, self.programmer_futures)
        idle_analysts = self._idle(self.analyst_sessions, self.analyst_futures)
        if not idle_programmers and not idle_analysts:
            return

        snapshot = self.backlog.snapshot()
        if not self._has_actionable_work(snapshot, idle_programmers, idle_analysts):
            return

        decision = self.orchestrator.assign(
            current_penalty=self.current_penalty,
            baseline_penalty=self.baseline_penalty,
            backlog=snapshot,
            idle_programmers=idle_programmers,
            idle_analysts=idle_analysts,
            stagnation=self.stagnation.counter,
        )
        self._apply_dispatch(decision, idle_programmers, idle_analysts)

    def _has_actionable_work(
        self,
        snapshot,
        idle_programmers: list[str],
        idle_analysts: list[str],
    ) -> bool:
        # Skip the orchestrator LLM call entirely when there is nothing
        # it could usefully assign — this is the per-tick hot path.
        has_todo = any(it.status == TODO for it in snapshot.items.values())
        if idle_programmers and has_todo:
            return True
        nearing_stagnation = self.stagnation.counter >= self.cfg.stagnation_limit - 1
        if idle_analysts and (not has_todo or nearing_stagnation):
            return True
        return False

    def _apply_dispatch(
        self,
        decision: AssignmentDecision,
        idle_programmers: list[str],
        idle_analysts: list[str],
    ) -> None:
        snapshot = self.backlog.snapshot()
        dirty = False

        for pid, issue_ids in decision.programmer_assignments.items():
            if pid not in idle_programmers:
                continue
            specs = self._collect_specs(issue_ids, snapshot)
            if not specs:
                continue
            session = self.programmer_sessions[pid]
            try:
                future = self.programmer_pool.submit(session.run, specs)
            except RuntimeError:
                continue
            self.programmer_futures[pid] = future
            for spec in specs:
                self.backlog.mark_in_progress(spec["id"], pid)
            dirty = True

        for aid in decision.dispatch_analysts:
            if aid not in idle_analysts:
                continue
            focus = decision.analyst_targets.get(aid, "")
            focus_metrics = [t.strip() for t in focus.split(",") if t.strip()]
            session = self.analyst_sessions[aid]
            try:
                future = self.analyst_pool.submit(session.run, focus_metrics)
            except RuntimeError:
                continue
            self.analyst_futures[aid] = future

        if dirty:
            self.backlog.persist()

    @staticmethod
    def _collect_specs(issue_ids: list[str], snapshot) -> list[dict]:
        specs: list[dict] = []
        for iid in issue_ids:
            item = snapshot.items.get(iid)
            if item is None or item.status != TODO:
                continue
            specs.append({
                "id": iid,
                "file_path": item.file_path,
                "line": item.line,
                "issue_type": item.issue_type,
                "message": item.message,
            })
        return specs

    # -- stuck-agent monitoring ---------------------------------------

    def _check_stuck_agents(self) -> None:
        """Hard-timeout only (gnomad-kiro parity).

        Any programmer past `programmer_timeout_sec` is killed, its
        issues are returned to TODO, its worktree is reset, and the
        stagnation counter ticks. No discretionary orchestrator
        evaluation — gnomad-kiro has a single-tier policy.
        """
        for pid, session in list(self.programmer_sessions.items()):
            if pid not in self.programmer_futures:
                continue
            if session.runtime_sec() > self.cfg.programmer_timeout_sec:
                self._terminate_programmer(pid, session)

    def _terminate_programmer(self, pid: str, session) -> None:
        session.kill()
        for iid in session.assigned_issues:
            self.backlog.return_to_todo(iid)
        self.stagnation.record_timeout()
        worktree = self.programmer_worktrees.get(pid)
        if worktree is not None:
            try:
                reset_worktree(worktree, self.cfg.main_branch)
            except Exception:
                pass
        self.backlog.persist()
