"""Main coordination loop.

Owns all shared state. Runs on the main thread. Drains the message
queue in a single-writer loop, persists the backlog atomically, and
dispatches agents based on orchestrator decisions.

Per the thesis the orchestrator is consulted at two decision points:
task assignment, and stuck-agent evaluation once a programmer passes
the ten-minute-per-issue mark. The thirty-minute hard timeout is
enforced by this layer without asking the orchestrator, and is the only
termination path that increments the stagnation counter.
"""

import hashlib
import json
import os
import posixpath
import shutil
import signal
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from config import Config
from coordination import gate_attempts
from coordination import issue_history
from coordination import message_queue as mq
from coordination import penalty_history as ph
from coordination.backlog import (
    BacklogStore, Issue, TODO, IN_PROGRESS, DONE, SKIPPED,
)
from coordination.git_manager import (
    capture_checkout,
    create_worktree,
    prepare_run_branch,
    push_branch,
    remove_worktree,
    resume_run_branch,
    reset_worktree,
    resolve_baseline_ref,
    restore_checkout,
)
from coordination.penalty_history import PenaltyHistory
from coordination.run_state import RunState
from coordination.stagnation import StagnationTracker
from coordination.token_usage import (
    budget_trigger,
    build_token_artifacts,
    collect_token_usage,
)
from coordination.model_pricing import pricing_snapshot
from agents.orchestrator import (
    Orchestrator, AssignmentDecision, SubscriptionQuotaExhausted,
)
from agents.analyst import AnalystSession
from agents.programmer import ProgrammerSession
from analysis.candidates import (
    LocalLead, build_local_leads, lead_matches_focus,
)
from analysis.tools import run_static_analysis
from analysis.metrics import (
    compute_metric_stats,
    format_metric_stats,
    mean_change_pct,
    PERCENTILE_METHOD,
)
from analysis.penalty import (
    compute_total_penalty,
    compute_penalty_breakdown,
    estimate_reduction_from_message,
    format_breakdown,
)


_SAFE_SUBSCRIPTION_SWITCH_REASONS = frozenset({
    "subscription_quota_exhausted",
    "wall_timeout",
})


class Coordinator:
    def __init__(self, cfg: Config, orchestrator=None):
        self.cfg = cfg
        self.queue = mq.make_queue()
        self.backlog = BacklogStore(cfg.backlog_file_path)
        self.stagnation = StagnationTracker(
            min_merge_gain=cfg.min_merge_gain,
            limit=cfg.stagnation_limit,
        )

        self.baseline_penalty: float = 0.0
        self.current_penalty: float = 0.0
        self.metric_breakdown: str = ""
        self.baseline_commit: str = ""
        self.original_checkout_ref: str = ""
        self.original_checkout_commit: str = ""
        self.original_checkout_detached: bool = False
        self.run_branch_pushed: bool = False
        # Agent sessions that exited non-zero without being killed
        # (thesis §5.4 counts these as crashes).
        self.agent_crashes: int = 0
        # Analyst findings rejected because the file does not exist
        # (thesis §6.2's file-existence check).
        self.phantom_issues: int = 0
        # Metric distribution statistics at the start and end of the run
        # (thesis §4.4: "the baseline and final quality metrics values").
        self.baseline_metric_stats: dict = {}
        self.metric_stats: dict = {}
        # Non-authoritative Lizard leads. Analysts still decide whether an
        # ISSUE enters the existing backlog.
        self.local_candidate_leads: list[LocalLead] = []
        self.analyst_lead_seen_keys: set[str] = set()
        self.analyst_lead_inflight: dict[str, set[str]] = {}
        # Token ceilings close future dispatches but do not kill in-flight
        # work. Usage is reported after turns complete, so overshoot is
        # measured rather than hidden.
        self.budget_dispatch_closed: bool = False
        self.budget_trigger: str = ""
        self.budget_usage_at_close: dict = {}
        # A subscription limit closes dispatch immediately but, like token
        # ceilings, lets already-running workers finish before shutdown.
        self.subscription_quota_closed: bool = False

        self.orchestrator = orchestrator if orchestrator is not None else Orchestrator(cfg)
        self.empty_analyst_scans: int = 0
        self.empty_assignment_decisions: int = 0

        self.programmer_worktrees: dict[str, Path] = {}
        self.analyst_worktrees: dict[str, Path] = {}
        self.programmer_sessions: dict[str, ProgrammerSession] = {}
        self.analyst_sessions: dict[str, AnalystSession] = {}

        self.programmer_pool: Optional[ThreadPoolExecutor] = None
        self.analyst_pool: Optional[ThreadPoolExecutor] = None

        self.programmer_futures: dict[str, Future] = {}
        self.analyst_futures: dict[str, Future] = {}
        # Killed workers remain in these maps until their threads exit.
        # Explicit termination state makes cleanup idempotent across ticks.
        self.terminating_programmers: set[str] = set()
        self.terminating_analysts: set[str] = set()
        # Throttles the orchestrator's stuck-agent evaluation per agent.
        self.last_stuck_eval: dict[str, float] = {}

        self.started_at: float = time.time()
        self.history = PenaltyHistory(
            path=cfg.run_results_path / cfg.penalty_history_filename
        )
        self.state = RunState(run_id=cfg.run_id, started_at=self.started_at)

        self.stop_reason: str = ""
        self._lifecycle_started: bool = False
        signal.signal(signal.SIGTERM, self._on_sigterm)

    # -- lifecycle -----------------------------------------------------

    def run(self) -> None:
        self.cfg.validate_for_run()
        if isinstance(self.orchestrator, Orchestrator):
            self.orchestrator.validate_transport()
        try:
            self._initialize()
            self._main_loop()
        finally:
            if self._lifecycle_started:
                self._shutdown()

    def _initialize(self) -> None:
        self.cfg.work_root.mkdir(parents=True, exist_ok=True)
        self.cfg.run_results_path.mkdir(parents=True, exist_ok=True)
        if not self.cfg.resume:
            existing = [
                path for path in (
                    self.cfg.state_path,
                    self.cfg.backlog_file_path,
                    self.cfg.run_results_path / self.cfg.penalty_history_filename,
                    self.cfg.run_results_path / self.cfg.gate_attempts_filename,
                    self.cfg.run_results_path / self.cfg.orchestrator_usage_filename,
                    self.cfg.run_results_path / self.cfg.orchestrator_raw_responses_filename,
                    self.cfg.run_results_path / self.cfg.token_usage_filename,
                )
                if path.exists()
            ]
            if existing:
                raise RuntimeError(
                    f"run {self.cfg.run_id!r} already has state; use a new "
                    f"--run-id or pass --resume ({existing[0]})"
                )
        self.cfg.agent_log_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.gate_config_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.gate_status_dir.mkdir(parents=True, exist_ok=True)

        restored = RunState.load(self.cfg.state_path) if self.cfg.resume else None
        if self.cfg.resume:
            if restored is None:
                raise RuntimeError(
                    f"cannot resume: no valid state at {self.cfg.state_path}"
                )
            if restored.run_id != self.cfg.run_id:
                raise RuntimeError(
                    f"cannot resume run {self.cfg.run_id!r} from state for "
                    f"{restored.run_id!r}"
                )
            provider_switched = self._validate_resume_configuration(restored)
            if provider_switched:
                restored.provider_transitions.append({
                    "timestamp": time.time(),
                    "reason": restored.stop_reason,
                    "from_provider": restored.api_provider,
                    "to_provider": self.cfg.api_provider,
                    "from_orchestrator_model": restored.orchestrator_model,
                    "to_orchestrator_model": self.cfg.orchestrator_model,
                    "from_agent_model": restored.agent_model,
                    "to_agent_model": self.cfg.agent_model,
                })
                print(
                    "[coordinator] approved safe resume transport switch: "
                    f"{restored.api_provider} -> {self.cfg.api_provider} "
                    f"after {restored.stop_reason}"
                )
            if (
                restored.analyst_lead_seen_keys
                and not restored.optimization_fingerprint
            ):
                raise RuntimeError(
                    "cannot safely resume legacy candidate progress without "
                    "an optimization fingerprint; use a new --run-id"
                )

        # Thesis §4.4: the run gets its own branch, created from a fixed
        # baseline so consecutive runs all start from the same state. main
        # is never advanced, so no destructive reset is needed.
        # A symbolic profile baseline such as HEAD is correct only on a fresh
        # run. After a crash HEAD may be the advanced integration branch, so
        # resume must prefer the immutable commit persisted in run_state.
        configured_baseline = (
            restored.baseline_commit
            if restored is not None and restored.baseline_commit
            else self.cfg.baseline_ref
        )
        baseline_ref = resolve_baseline_ref(
            self.cfg.repo_root, configured_baseline, self.cfg.main_branch,
        )
        if restored is not None:
            self.original_checkout_ref = restored.original_checkout_ref
            self.original_checkout_commit = restored.original_checkout_commit
            self.original_checkout_detached = restored.original_checkout_detached
            if not self.original_checkout_ref or not self.original_checkout_commit:
                raise RuntimeError(
                    "cannot resume state without original checkout metadata; "
                    "use a new --run-id"
                )
        else:
            (
                self.original_checkout_ref,
                self.original_checkout_commit,
                self.original_checkout_detached,
            ) = capture_checkout(self.cfg.repo_root)
        self._lifecycle_started = True
        if restored is not None:
            self.baseline_commit = resume_run_branch(
                self.cfg.repo_root, self.cfg.integration_branch, baseline_ref,
                allowed_untracked_paths=self.cfg.repo_allowed_untracked_paths,
            )
        else:
            self.baseline_commit = prepare_run_branch(
                self.cfg.repo_root, self.cfg.integration_branch, baseline_ref,
                allowed_untracked_paths=self.cfg.repo_allowed_untracked_paths,
            )
        duplicate_setting = (
            f"dupl -t = {self.cfg.dupl_threshold_tokens}"
            if self.cfg.lizard_language.lower() == "go"
            else f"Duplo -ml = {self.cfg.duplo_min_block_lines}"
        )
        print(
            f"[coordinator] baseline {baseline_ref} = {self.baseline_commit[:10]}\n"
            f"[coordinator] integration branch = {self.cfg.integration_branch}"
        )

        checkout_subdir = (
            self.cfg.target_subdir if self.cfg.sparse_worktrees else "."
        )
        for i in range(self.cfg.num_programmers):
            pid = f"PROG_{i+1}"
            wt = create_worktree(
                self.cfg.repo_root,
                self.cfg.agent_work_root,
                pid,
                checkout_subdir,
                base_ref=self.cfg.integration_branch,
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
                self.cfg.agent_work_root,
                aid,
                checkout_subdir,
                base_ref=self.cfg.integration_branch,
                detached=True,
            )
            self.analyst_worktrees[aid] = wt
            self.analyst_sessions[aid] = AnalystSession(
                analyst_id=aid,
                cfg=self.cfg,
                queue=self.queue,
                worktree=wt,
            )

        self.backlog.load_or_init()
        if restored is not None:
            recovered = self.backlog.recover_in_progress()
            if recovered:
                self.backlog.persist()
                print(
                    f"[coordinator] returned {recovered} crash-interrupted "
                    "issue(s) to TODO"
                )

        measured, breakdown, stats = self._measure()
        self.metric_breakdown = format_breakdown(breakdown)
        self.metric_stats = stats

        if restored is not None:
            # Keep the original baseline so improvement stays comparable
            # across the whole run; the current penalty is re-measured
            # because the integration branch may have moved before the crash.
            self.baseline_penalty = restored.baseline_penalty
            self.current_penalty = measured
            self.stagnation.counter = restored.stagnation_counter
            self.state.merges = restored.merges
            self.state.started_at = restored.started_at
            self.started_at = restored.started_at or self.started_at
            self.agent_crashes = restored.agent_crashes
            self.phantom_issues = restored.phantom_issues
            self.analyst_lead_seen_keys = set(
                restored.analyst_lead_seen_keys
            )
            self.budget_dispatch_closed = restored.budget_dispatch_closed
            self.budget_trigger = restored.budget_trigger
            self.budget_usage_at_close = restored.budget_usage_at_close
            self.state.baseline_build_status = restored.baseline_build_status
            self.state.baseline_build_elapsed_sec = (
                restored.baseline_build_elapsed_sec
            )
            self.state.baseline_build_log = restored.baseline_build_log
            self.baseline_metric_stats = restored.baseline_metric_stats or stats
            loaded_history = PenaltyHistory.load(
                self.cfg.run_results_path / self.cfg.penalty_history_filename
            )
            if loaded_history is None:
                raise RuntimeError(
                    "cannot resume: penalty history is missing or corrupt"
                )
            self.history = loaded_history
            print(
                f"[coordinator] resumed run {restored.run_id}: "
                f"baseline={self.baseline_penalty:.1f} "
                f"current={self.current_penalty:.1f} "
                f"stagnation={self.stagnation.counter} "
                f"merges={self.state.merges}"
            )
        else:
            self.baseline_penalty = measured
            self.current_penalty = measured
            self.baseline_metric_stats = stats
            self.history.start(self.baseline_penalty, breakdown=breakdown)

        # The baseline distribution is the reference every result table is
        # expressed against, so it is captured once and never overwritten.
        if not self.baseline_metric_stats:
            self.baseline_metric_stats = stats

        self.state.baseline_penalty = self.baseline_penalty
        self.state.current_penalty = self.current_penalty
        self.state.stop_reason = ""
        self._save_state()

        # Both of these change measured penalty and neither is pinned by
        # the thesis, so echo them next to the baseline they produced.
        print(
            f"[coordinator] baseline penalty = {self.baseline_penalty:.2f}\n"
            f"{self.metric_breakdown}\n"
            f"[coordinator] excluded dirs = "
            f"{', '.join(self.cfg.exclude_dirs) or '(none)'}\n"
            f"[coordinator] duplication setting: {duplicate_setting}\n"
            f"[coordinator] baseline metric distribution:\n"
            f"{format_metric_stats(stats)}"
        )

        # MongoDB's Query profile uses a shared Bazel disk cache.  Populate
        # it on the immutable baseline before any paid model dispatch, and
        # fail fast if the repository cannot build in this environment.
        self._prewarm_baseline_build(restored)

        self.programmer_pool = ThreadPoolExecutor(max_workers=self.cfg.num_programmers)
        self.analyst_pool = ThreadPoolExecutor(max_workers=self.cfg.num_analysts)

    def _measure(self) -> tuple[float, dict, dict]:
        """Measure total penalty, per-metric penalty split, and the metric
        distribution statistics the thesis reports in Tables 4.1 / 5.1."""
        lizard_records, cognitive_records, duplication = run_static_analysis(
            self.cfg.target_path, self.cfg.thresholds,
            duplo_binary=self.cfg.duplo_binary,
            duplo_min_block_lines=self.cfg.duplo_min_block_lines,
            dupl_binary=self.cfg.dupl_binary,
            dupl_threshold_tokens=self.cfg.dupl_threshold_tokens,
            lizard_binary=self.cfg.lizard_binary,
            gocognit_binary=self.cfg.gocognit_binary,
            language=self.cfg.lizard_language,
            exclude_dirs=self.cfg.exclude_dirs,
        )
        self.local_candidate_leads = build_local_leads(
            lizard_records,
            cognitive_records,
            target=self.cfg.target_path,
            thresholds=self.cfg.thresholds,
            weights=self.cfg.weights,
        )
        total = compute_total_penalty(
            lizard_records, cognitive_records,
            self.cfg.thresholds, duplication.ratio, self.cfg.weights,
        )
        breakdown = compute_penalty_breakdown(
            lizard_records, cognitive_records,
            self.cfg.thresholds, duplication.ratio, self.cfg.weights,
        )
        stats = compute_metric_stats(
            lizard_records, cognitive_records, duplication, self.cfg.thresholds,
        )
        return total, breakdown, stats

    def _prewarm_baseline_build(self, restored: RunState | None) -> None:
        output_base = self.cfg.baseline_build_output_base_path
        expected_parent = self.cfg.run_results_path.resolve()
        if (
            output_base.resolve().parent != expected_parent
            or output_base.name != "baseline-bazel-output"
        ):
            raise RuntimeError("unsafe baseline Bazel output-base path")
        # A prior crash may have left this run-scoped scratch directory. It
        # contains no experiment evidence; the durable log and shared disk
        # cache live elsewhere.
        if output_base.exists():
            shutil.rmtree(output_base)

        if not self.cfg.prewarm_build_cache:
            self.state.baseline_build_status = "not_requested"
            self._save_state()
            return
        if (
            restored is not None
            and restored.baseline_build_status == "passed"
        ):
            print(
                "[coordinator] baseline build cache already warmed; "
                "resume skips duplicate prewarm"
            )
            return

        log_path = self.cfg.baseline_build_log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.state.baseline_build_status = "running"
        self.state.baseline_build_log = str(log_path)
        self._save_state()
        started = time.monotonic()
        print(
            "[coordinator] prewarming baseline build cache before model "
            f"dispatch (log: {log_path})"
        )
        try:
            output_base.mkdir(parents=True, exist_ok=False)
            build_env = os.environ.copy()
            build_env["EXPERIMENT_BAZEL_OUTPUT_BASE"] = str(output_base)
            with log_path.open("w") as log:
                log.write("+ " + " ".join(self.cfg.build_cmd) + "\n")
                log.flush()
                completed = subprocess.run(
                    self.cfg.build_cmd,
                    cwd=str(self.cfg.repo_root),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=self.cfg.build_timeout_sec,
                    env=build_env,
                )
            status = "passed" if completed.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            status = "timeout"
        except OSError as exc:
            status = "failed"
            with log_path.open("a") as log:
                log.write(f"\nfailed to start baseline build: {exc}\n")
        finally:
            try:
                if output_base.exists():
                    shutil.rmtree(output_base)
            except OSError as exc:
                with log_path.open("a") as log:
                    log.write(
                        "\nwarning: failed to remove ephemeral baseline "
                        f"Bazel output base: {exc}\n"
                    )

        elapsed = round(time.monotonic() - started, 2)
        self.state.baseline_build_status = status
        self.state.baseline_build_elapsed_sec = elapsed
        self._save_state()
        if status != "passed":
            self.stop_reason = f"baseline_build_{status}"
            self._save_state()
            raise RuntimeError(
                f"baseline build {status} after {elapsed:.2f}s; "
                f"no model was dispatched (see {log_path})"
            )
        print(
            f"[coordinator] baseline build passed in {elapsed:.2f}s; "
            "shared build cache is warm"
        )

    def _on_sigterm(self, signum, frame) -> None:
        """SIGTERM handler: stop the loop and reap the agent processes.

        Worktrees are intentionally left as-is so in-progress changes can
        be inspected post-mortem.
        """
        self.stop_reason = "wall_timeout"
        for pid, session in self.programmer_sessions.items():
            if pid in self.programmer_futures:
                try:
                    session.kill()
                except Exception:
                    pass
        for aid, session in self.analyst_sessions.items():
            if aid in self.analyst_futures:
                try:
                    session.kill()
                except Exception:
                    pass

    def _shutdown(self) -> None:
        if self.stop_reason == "" and self.stagnation.should_stop():
            self.stop_reason = "stagnation"

        # Stop child process groups before removing their worktrees. This is
        # required for analysts too; otherwise a running analyst may continue
        # reading a directory that shutdown has already removed.
        for pid, session in self.programmer_sessions.items():
            if pid in self.programmer_futures:
                try:
                    session.kill()
                except Exception:
                    pass
        for aid, session in self.analyst_sessions.items():
            if aid in self.analyst_futures:
                try:
                    session.kill()
                except Exception:
                    pass

        if self.programmer_pool is not None:
            self.programmer_pool.shutdown(wait=True, cancel_futures=True)
        if self.analyst_pool is not None:
            self.analyst_pool.shutdown(wait=True, cancel_futures=True)
        for wt in (*self.programmer_worktrees.values(), *self.analyst_worktrees.values()):
            try:
                remove_worktree(self.cfg.repo_root, wt)
            except Exception:
                pass

        self._finalize_run_branch()
        self._save_state()

        try:
            self.history.record(
                ph.STOP, penalty=self.current_penalty,
                stop_reason=self.stop_reason or "unknown",
            )
            self._write_summary()
            plotted = self.history.plot(
                self.cfg.run_results_path / self.cfg.penalty_plot_filename
            )
            if not plotted:
                print("[coordinator] matplotlib unavailable; skipped penalty plot")
        except Exception as exc:
            print(f"[coordinator] failed to write run artefacts: {exc}")

    def _finalize_run_branch(self) -> None:
        """Thesis §4.4: push the run branch, then restore the baseline.

        The worktrees must already be removed, otherwise git refuses to
        check the integration branch out elsewhere.
        """
        if not self.baseline_commit:
            return

        if self.cfg.push_run_branch:
            try:
                self.run_branch_pushed = push_branch(
                    self.cfg.repo_root, self.cfg.integration_branch,
                )
            except Exception as exc:
                print(f"[coordinator] failed to push run branch: {exc}")
            print(
                f"[coordinator] run branch {self.cfg.integration_branch} "
                f"{'pushed to origin' if self.run_branch_pushed else 'NOT pushed'}"
            )
        else:
            print(
                f"[coordinator] run branch {self.cfg.integration_branch} kept locally "
                f"(pass --push-run-branch to publish it)"
            )

        try:
            restored = restore_checkout(
                self.cfg.repo_root,
                self.original_checkout_ref,
                self.original_checkout_commit,
                self.original_checkout_detached,
            )
        except Exception as exc:
            print(f"[coordinator] failed to restore baseline: {exc}")
            return
        if restored:
            print(
                f"[coordinator] working directory restored to baseline "
                f"{self.original_checkout_commit[:10]} on "
                f"{'detached HEAD' if self.original_checkout_detached else self.original_checkout_ref}"
            )
        else:
            print(
                f"[coordinator] WARNING: failed to restore original checkout "
                f"{self.original_checkout_ref} at "
                f"{self.original_checkout_commit[:10]}"
            )

    def _save_state(self) -> None:
        self.state.run_id = self.cfg.run_id
        self.state.baseline_commit = self.baseline_commit
        self.state.baseline_penalty = self.baseline_penalty
        self.state.current_penalty = self.current_penalty
        self.state.stagnation_counter = self.stagnation.counter
        self.state.stop_reason = self.stop_reason
        self.state.baseline_metric_stats = self.baseline_metric_stats
        self.state.agent_crashes = self.agent_crashes
        self.state.phantom_issues = self.phantom_issues
        self.state.analyst_lead_seen_keys = sorted(
            self.analyst_lead_seen_keys
        )
        self.state.budget_dispatch_closed = self.budget_dispatch_closed
        self.state.budget_trigger = self.budget_trigger
        self.state.budget_usage_at_close = self.budget_usage_at_close
        self.state.api_provider = self.cfg.api_provider
        self.state.orchestrator_model = self.cfg.orchestrator_model
        self.state.agent_model = self.cfg.agent_model
        self.state.optimization_fingerprint = (
            self._optimization_fingerprint()
        )
        self.state.optimization_core_fingerprint = (
            self._optimization_core_fingerprint()
        )
        self.state.original_checkout_ref = self.original_checkout_ref
        self.state.original_checkout_commit = self.original_checkout_commit
        self.state.original_checkout_detached = self.original_checkout_detached
        try:
            self.state.save(self.cfg.state_path)
        except Exception as exc:
            print(f"[coordinator] failed to save state: {exc}")

    def _failure_counts(self, skipped: int) -> dict:
        """The failure counts the results chapter reports.

        Thesis §5.1 asks for "reverted attempts, test failures,
        stagnation exits"; §5.4 for the deviations — merge conflicts,
        crashes, and agents giving up. The gate-side counts come from the
        attempt log its subprocesses append to, the rest from this
        layer's own bookkeeping.
        """
        records = gate_attempts.load(
            self.cfg.run_results_path / self.cfg.gate_attempts_filename
        )
        counts = gate_attempts.summarize(records)
        counts.update({
            "stagnation_exit": self.stop_reason == "stagnation",
            "hard_timeout_kills": self.history.event_count(ph.TIMEOUT_KILL),
            "analyst_timeout_kills": self.history.event_count(
                ph.ANALYST_TIMEOUT
            ),
            "stuck_terminations": self.history.event_count(ph.STUCK_TERMINATE),
            # Issues the programmers or the orchestrator declared
            # infeasible — §5.4's "agents give up".
            "issues_skipped": skipped,
            "agent_crashes": self.agent_crashes,
            # §6.2: analyst findings naming a file that does not exist.
            "analyst_phantom_issues": self.phantom_issues,
        })
        return counts

    def _write_summary(self) -> None:
        snapshot = self.backlog.snapshot()
        counts = {TODO: 0, IN_PROGRESS: 0, DONE: 0, SKIPPED: 0}
        for item in snapshot.items.values():
            counts[item.status] = counts.get(item.status, 0) + 1

        reduction = self.baseline_penalty - self.current_penalty
        pct = (reduction / self.baseline_penalty * 100.0) if self.baseline_penalty else 0.0
        merges = self.history.merge_count()
        token_summary = build_token_artifacts(
            agent_log_dir=self.cfg.agent_log_dir,
            orchestrator_log=(
                self.cfg.run_results_path / self.cfg.orchestrator_usage_filename
            ),
            detail_path=(
                self.cfg.run_results_path / self.cfg.token_usage_filename
            ),
            provider=self.cfg.api_provider,
            agent_model=self.cfg.agent_model,
            successful_merges=merges,
        )
        final_totals = token_summary["totals"]
        budget_limit = (
            self.cfg.max_run_input_tokens
            if self.budget_trigger == "input_tokens"
            else self.cfg.max_run_output_tokens
            if self.budget_trigger == "output_tokens"
            else self.cfg.max_run_cost_usd
            if self.budget_trigger == "cost_usd"
            else self.cfg.max_run_cost_cny
            if self.budget_trigger == "cost_cny"
            else 0
        )
        budget_final_value = (
            final_totals.get(
                "effective_cost_usd"
                if self.budget_trigger == "cost_usd"
                else "effective_cost_cny"
                if self.budget_trigger == "cost_cny"
                else self.budget_trigger,
                0,
            )
            if self.budget_trigger else 0
        )
        summary = {
            "run_id": self.cfg.run_id,
            "stop_reason": self.stop_reason or "unknown",
            "baseline_commit": self.baseline_commit,
            "original_checkout": {
                "ref": self.original_checkout_ref,
                "commit": self.original_checkout_commit,
                "detached": self.original_checkout_detached,
            },
            "integration_branch": self.cfg.integration_branch,
            "run_branch_pushed": self.run_branch_pushed,
            "provider_transitions": list(self.state.provider_transitions),
            "elapsed_sec": round(time.time() - self.started_at, 2),
            "baseline_penalty": self.baseline_penalty,
            "final_penalty": self.current_penalty,
            "total_reduction": round(reduction, 4),
            "reduction_pct": round(pct, 2),
            "merges": merges,
            "stagnation_counter": self.stagnation.counter,
            "baseline_build": {
                "enabled": self.cfg.prewarm_build_cache,
                "status": self.state.baseline_build_status,
                "elapsed_sec": self.state.baseline_build_elapsed_sec,
                "log": self.state.baseline_build_log or None,
            },
            "final_metric_breakdown": self.metric_breakdown,
            # Thesis §4.4 / Tables 4.1 and 5.1: the metric distributions
            # before and after the run, plus the change in each mean.
            "metrics": {
                "percentile_method": PERCENTILE_METHOD,
                "baseline": self.baseline_metric_stats,
                "final": self.metric_stats,
                "mean_change_pct": mean_change_pct(
                    self.baseline_metric_stats, self.metric_stats,
                ),
                "cleared": [
                    key for key, s in self.metric_stats.items() if s.get("cleared")
                ],
            },
            # Thesis §5.1 ("failure counts ... for each model") and §5.4
            # (system deviations).
            "failures": self._failure_counts(counts.get(SKIPPED, 0)),
            "backlog": {
                "total": len(snapshot.items),
                "todo": counts.get(TODO, 0),
                "in_progress": counts.get(IN_PROGRESS, 0),
                "done": counts.get(DONE, 0),
                "skipped": counts.get(SKIPPED, 0),
            },
            "tokens": token_summary,
            "token_budget": {
                "dispatch_closed": (
                    self.budget_dispatch_closed
                    and self.budget_trigger in ("input_tokens", "output_tokens")
                ),
                "trigger": (
                    self.budget_trigger
                    if self.budget_trigger in ("input_tokens", "output_tokens")
                    else None
                ),
                "usage_at_close": self.budget_usage_at_close,
                "final_value": (
                    budget_final_value
                    if self.budget_trigger in ("input_tokens", "output_tokens")
                    else 0
                ),
                "limit": (
                    budget_limit
                    if self.budget_trigger in ("input_tokens", "output_tokens")
                    else 0
                ),
                "overshoot": max(0, budget_final_value - budget_limit)
                if (
                    budget_limit
                    and self.budget_trigger in ("input_tokens", "output_tokens")
                ) else 0,
                "semantics": (
                    "reported-usage dispatch ceiling; in-flight agents finish"
                ),
            },
            "cost_budget": {
                "enabled": (
                    self.cfg.max_run_cost_usd > 0
                    or self.cfg.max_run_cost_cny > 0
                ),
                "dispatch_closed": (
                    self.budget_dispatch_closed
                    and self.budget_trigger in ("cost_usd", "cost_cny")
                ),
                "trigger": (
                    f"effective_{self.budget_trigger}"
                    if self.budget_trigger in ("cost_usd", "cost_cny") else None
                ),
                "usage_at_close": self.budget_usage_at_close,
                "final_value_usd": final_totals["effective_cost_usd"],
                "limit_usd": self.cfg.max_run_cost_usd,
                "overshoot_usd": max(
                    0.0,
                    final_totals["effective_cost_usd"]
                    - self.cfg.max_run_cost_usd,
                ) if self.cfg.max_run_cost_usd else 0.0,
                "final_value_cny": final_totals["effective_cost_cny"],
                "limit_cny": self.cfg.max_run_cost_cny,
                "overshoot_cny": max(
                    0.0,
                    final_totals["effective_cost_cny"]
                    - self.cfg.max_run_cost_cny,
                ) if self.cfg.max_run_cost_cny else 0.0,
                "semantics": (
                    "pinned-price native-currency dispatch ceiling; "
                    "in-flight agents finish"
                ),
            },
            "token_optimization": {
                "orchestrator_todo_window": (
                    self.cfg.orchestrator_backlog_top_k
                ),
                "analyst_lead_page_size": self.cfg.analyst_lead_page_size,
                "analyst_leads_reviewed": len(
                    self.analyst_lead_seen_keys
                ),
                "current_local_static_leads": len(
                    self.local_candidate_leads
                ),
                "lead_scope": ["ccn", "nloc", "param", "cognitive"],
                "analyst_remains_final_issue_decider": True,
            },
            "config": {
                "production_profile": self.cfg.production_profile or None,
                "repo_root": str(self.cfg.repo_root),
                "target_subdir": self.cfg.target_subdir,
                "sparse_worktrees": self.cfg.sparse_worktrees,
                "num_analysts": self.cfg.num_analysts,
                "num_programmers": self.cfg.num_programmers,
                "orchestrator_backlog_top_k": (
                    self.cfg.orchestrator_backlog_top_k
                ),
                "analyst_lead_page_size": self.cfg.analyst_lead_page_size,
                "orchestrator_no_progress_limit": (
                    self.cfg.orchestrator_no_progress_limit
                ),
                "max_run_input_tokens": self.cfg.max_run_input_tokens,
                "max_run_output_tokens": self.cfg.max_run_output_tokens,
                "max_run_cost_usd": self.cfg.max_run_cost_usd,
                "max_run_cost_cny": self.cfg.max_run_cost_cny,
                "api_provider": self.cfg.api_provider,
                "api_base_url": self._safe_api_base_url(
                    self.cfg.effective_api_base_url
                ),
                "api_key_env": self.cfg.effective_api_key_env,
                "subscription_type": self.cfg.subscription_type or None,
                "claude_cli_version": self.cfg.claude_cli_version or None,
                "orchestrator_model": self.cfg.orchestrator_model,
                "agent_model": self.cfg.agent_model,
                "deepseek_effort": self.cfg.deepseek_effort,
                "thresholds": self.cfg.thresholds,
                "weights": self.cfg.weights,
                "min_merge_gain": self.cfg.min_merge_gain,
                "stagnation_limit": self.cfg.stagnation_limit,
                "programmer_timeout_sec": self.cfg.programmer_timeout_sec,
                "build_timeout_sec": self.cfg.build_timeout_sec,
                "test_timeout_sec": self.cfg.test_timeout_sec,
                "gate_timeout_sec": self.cfg.gate_timeout_sec,
                "prewarm_build_cache": self.cfg.prewarm_build_cache,
                "analyst_timeout_sec": self.cfg.analyst_timeout_sec,
                "issue_timeout_sec": self.cfg.issue_timeout_sec,
                "max_gate_attempts_per_issue": (
                    self.cfg.max_gate_attempts_per_issue
                ),
                "max_issue_dispatches": self.cfg.max_issue_dispatches,
                "gate_allowed_untracked_paths": list(
                    self.cfg.gate_allowed_untracked_paths
                ),
                "repo_allowed_untracked_paths": list(
                    self.cfg.repo_allowed_untracked_paths
                ),
                "empty_scan_limit": self.cfg.empty_scan_limit,
                "agent_allowed_tools": list(self.cfg.agent_allowed_tools),
                "agent_bare_mode": self.cfg.agent_bare_mode,
                "lizard_language": self.cfg.lizard_language,
                "gocognit_binary": self.cfg.gocognit_binary,
                "dupl_binary": self.cfg.dupl_binary,
                "dupl_threshold_tokens": self.cfg.dupl_threshold_tokens,
                "duplo_min_block_lines": self.cfg.duplo_min_block_lines,
                "exclude_dirs": list(self.cfg.exclude_dirs),
                "build_cmd": self.cfg.build_cmd,
                "test_cmd": self.cfg.test_cmd,
                "main_branch": self.cfg.main_branch,
                "baseline_ref": self.cfg.baseline_ref or "(auto)",
                "push_run_branch": self.cfg.push_run_branch,
            },
        }
        path = self.cfg.run_results_path / self.cfg.run_summary_filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2))
        f = summary["failures"]
        token_totals = token_summary["totals"]
        token_coverage = token_summary["coverage"]
        print(
            f"[coordinator] stop_reason={summary['stop_reason']} "
            f"penalty {self.baseline_penalty:.2f} -> {self.current_penalty:.2f} "
            f"({pct:.1f}% reduction) over {summary['merges']} merges\n"
            f"[coordinator] gate attempts={f['gate_attempts']} "
            f"(merged {f['merged']}, reverted {f['reverted_attempts']}, "
            f"test-fail {f['test_failures']}, build-fail {f['build_failures']}, "
            f"analysis-fail {f['analysis_failures']}, "
            f"conflict {f['merge_conflicts']}, ff-race {f['ff_merge_races']})\n"
            f"[coordinator] deviations: timeout kills={f['hard_timeout_kills']}, "
            f"analyst timeouts={f['analyst_timeout_kills']}, "
            f"stuck terminations={f['stuck_terminations']}, "
            f"skipped issues={f['issues_skipped']}, "
            f"crashes={f['agent_crashes']}, "
            f"phantom issues={f['analyst_phantom_issues']}\n"
            f"[coordinator] final metric distribution:\n"
            f"{format_metric_stats(self.metric_stats)}\n"
            f"[coordinator] cleared metrics: "
            f"{', '.join(summary['metrics']['cleared']) or '(none)'}\n"
            f"[coordinator] tokens: input={token_totals['input_tokens']} "
            f"(uncached={token_totals['uncached_input_tokens']}, "
            f"cache-create={token_totals['cache_creation_input_tokens']}, "
            f"cache-read={token_totals['cache_read_input_tokens']}), "
            f"output={token_totals['output_tokens']}, "
            f"total={token_totals['total_tokens']}; "
            f"{'API-equivalent' if self.cfg.api_provider == 'subscription' else 'effective'} "
            f"cost=${token_totals['effective_cost_usd']:.6f}; "
            f"DeepSeek cost=¥{token_totals['effective_cost_cny']:.6f}; "
            f"usage coverage={token_coverage['records_with_usage']}/"
            f"{token_coverage['records']}\n"
            f"[coordinator] artefacts in {self.cfg.run_results_path}"
        )

    # -- main loop -----------------------------------------------------

    def _main_loop(self) -> None:
        if self.current_penalty <= 1e-9:
            self.stop_reason = "penalty_zero"
            return
        while not self.stagnation.should_stop() and self.stop_reason == "":
            self._drain_queue()
            # The gate log is durable evidence written after the integration
            # branch advances. Reconcile it before reaping or timing out a
            # worker so a completed merge cannot be lost in a queue race.
            self._recover_active_gate_merges()
            self._reap_finished_futures()
            budget_closed = self._check_token_budget()
            if self.stop_reason:
                break
            if self._settle_subscription_quota():
                self.stop_reason = "subscription_quota_exhausted"
                break
            self._check_no_actionable_stop()
            if self.stop_reason:
                break
            if not budget_closed and not self.subscription_quota_closed:
                self._dispatch_if_needed()
            if not self.subscription_quota_closed:
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
            added = 0
            for raw in msg.payload.get("issues", []):
                if not self._issue_file_exists(raw.get("file_path", "")):
                    self.phantom_issues += 1
                    print(
                        f"[coordinator] dropped issue from {msg.sender}: "
                        f"no such file {raw.get('file_path', '')!r}"
                    )
                    continue
                canonical_path = self._canonical_issue_path(raw["file_path"])
                reduction, parsed = estimate_reduction_from_message(
                    raw.get("message", ""), self.cfg.thresholds, self.cfg.weights,
                )
                impact = "high" if reduction >= self.cfg.min_merge_gain else "low"
                issue = Issue(
                    id="",
                    file_path=canonical_path,
                    line=int(raw["line"]),
                    severity=raw.get("severity", "info"),
                    issue_type=self._canonical_issue_type(
                        raw.get("issue_type", "unknown")
                    ),
                    message=raw.get("message", ""),
                    metric_values=parsed,
                    estimated_penalty_reduction=reduction,
                    impact=impact,
                )
                if self.backlog.add_issue(issue) is not None:
                    added += 1
            if added:
                self.empty_analyst_scans = 0
            else:
                self.empty_analyst_scans += 1
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
            self._apply_merge_result(msg)
            return

        if msg.kind in (mq.PROGRAMMER_FINISHED, mq.ANALYST_FINISHED):
            if bool(msg.payload.get("subscription_quota_exhausted")):
                # Leave unresolved programmer work retryable and preserve all
                # run state. `--resume` can continue after the shared Claude
                # subscription window resets.
                self.subscription_quota_closed = True
                print(
                    f"[coordinator] {msg.sender} reached the Claude Code "
                    "subscription limit; stopping safely for later --resume"
                )
            # Sessions the coordination layer killed never post these, so
            # a non-zero code here means the CLI itself died.
            if (
                int(msg.payload.get("returncode", 0)) != 0
                and not bool(msg.payload.get("subscription_quota_exhausted"))
            ):
                self.agent_crashes += 1
                print(
                    f"[coordinator] {msg.sender} exited "
                    f"{msg.payload['returncode']} (counted as a crash)"
                )
            if msg.kind == mq.PROGRAMMER_FINISHED:
                # A crash, context exhaustion, or malformed/missing RESULT
                # line must not strand assignments in IN_PROGRESS forever.
                session = self.programmer_sessions.get(msg.sender)
                if (
                    session is not None
                    and not bool(msg.payload.get("subscription_quota_exhausted"))
                    and not (
                        self.subscription_quota_closed
                        and session.gate_active()
                    )
                ):
                    snapshot = self.backlog.snapshot()
                    for iid in session.assigned_issues:
                        item = snapshot.items.get(iid)
                        if (
                            item is not None
                            and item.status == IN_PROGRESS
                            and item.assigned_to == msg.sender
                        ):
                            self.backlog.return_to_todo(iid)
            else:
                reviewed = self.analyst_lead_inflight.pop(msg.sender, set())
                if (
                    int(msg.payload.get("returncode", 0)) == 0
                    and bool(msg.payload.get("review_completed"))
                ):
                    self.analyst_lead_seen_keys.update(reviewed)
                    if reviewed:
                        self._save_state()
            return

        if msg.kind == mq.PROGRAMMER_HEARTBEAT:
            return

    def _check_no_actionable_stop(self) -> None:
        """Stop after repeated empty discovery scans.

        Without this guard, a zero-TODO backlog causes the orchestrator to
        dispatch analysts forever while no merge/timeout event can advance
        the stagnation counter.
        """
        if self.empty_analyst_scans < self.cfg.empty_scan_limit:
            return
        snapshot = self.backlog.snapshot()
        if any(
            item.status in (TODO, IN_PROGRESS)
            for item in snapshot.items.values()
        ):
            return
        if self.analyst_futures:
            return
        if self._has_unseen_local_leads():
            return
        self.stop_reason = "no_actionable_work"

    def _has_unseen_local_leads(self) -> bool:
        inflight = set().union(
            *self.analyst_lead_inflight.values()
        ) if self.analyst_lead_inflight else set()
        return any(
            lead.key not in self.analyst_lead_seen_keys
            and lead.key not in inflight
            for lead in self.local_candidate_leads
        )

    def _token_snapshot(self) -> dict:
        return collect_token_usage(
            agent_log_dir=self.cfg.agent_log_dir,
            orchestrator_log=(
                self.cfg.run_results_path / self.cfg.orchestrator_usage_filename
            ),
            provider=self.cfg.api_provider,
            agent_model=self.cfg.agent_model,
            successful_merges=self.history.merge_count(),
        )

    def _check_token_budget(self) -> bool:
        """Close new dispatches at a reported-usage ceiling.

        Existing agents are allowed to finish. This is deliberately not
        described as a hard token cap because providers report usage only
        after model turns complete.
        """
        if not (
            self.cfg.max_run_input_tokens
            or self.cfg.max_run_output_tokens
            or self.cfg.max_run_cost_usd
            or self.cfg.max_run_cost_cny
            or self.budget_dispatch_closed
        ):
            return False

        usage = self._token_snapshot()
        if not self.budget_dispatch_closed:
            trigger = budget_trigger(
                usage["totals"],
                max_input_tokens=self.cfg.max_run_input_tokens,
                max_output_tokens=self.cfg.max_run_output_tokens,
                max_cost_usd=self.cfg.max_run_cost_usd,
                max_cost_cny=self.cfg.max_run_cost_cny,
            )
            if trigger:
                self.budget_dispatch_closed = True
                self.budget_trigger = trigger
                self.budget_usage_at_close = dict(usage["totals"])
                self._save_state()
                trigger_value = usage["totals"].get(
                    "effective_cost_usd"
                    if trigger == "cost_usd" else trigger,
                    0,
                ) if trigger != "cost_cny" else usage["totals"].get(
                    "effective_cost_cny",
                    0,
                )
                print(
                    f"[coordinator] resource dispatch ceiling reached: "
                    f"{trigger}={trigger_value}"
                )

        if not self.budget_dispatch_closed:
            return False
        if (
            not self.programmer_futures
            and not self.analyst_futures
            and self.queue.empty()
        ):
            self.stop_reason = (
                f"cost_budget_{self.budget_trigger.removeprefix('cost_')}"
                if self.budget_trigger in ("cost_usd", "cost_cny")
                else f"token_budget_{self.budget_trigger}"
            )
        return True

    def _optimization_fingerprint_payload(self) -> dict:
        """Return the complete resume identity, including model transport."""
        return {
            # Schema 8 also binds production validation, gate scheduling, and
            # pricing. Refuse cross-version
            # resume rather than silently changing build/test or tool scope.
            "schema": 8,
            "repo_root": str(self.cfg.repo_root.resolve()),
            "production_profile": self.cfg.production_profile,
            "target_subdir": self.cfg.target_subdir,
            "sparse_worktrees": self.cfg.sparse_worktrees,
            "language": self.cfg.lizard_language,
            "build_cmd": self.cfg.build_cmd,
            "test_cmd": self.cfg.test_cmd,
            "serialize_merge_gate": self.cfg.serialize_merge_gate,
            "lizard_binary": self.cfg.lizard_binary,
            "gocognit_binary": self.cfg.gocognit_binary,
            "duplo_binary": self.cfg.duplo_binary,
            "duplo_min_block_lines": self.cfg.duplo_min_block_lines,
            "dupl_binary": self.cfg.dupl_binary,
            "dupl_threshold_tokens": self.cfg.dupl_threshold_tokens,
            "gate_allowed_untracked_paths": list(
                self.cfg.gate_allowed_untracked_paths
            ),
            "repo_allowed_untracked_paths": list(
                self.cfg.repo_allowed_untracked_paths
            ),
            "thresholds": self.cfg.thresholds,
            "weights": self.cfg.weights,
            "exclude_dirs": list(self.cfg.exclude_dirs),
            "orchestrator_backlog_top_k": (
                self.cfg.orchestrator_backlog_top_k
            ),
            "analyst_lead_page_size": self.cfg.analyst_lead_page_size,
            "max_run_input_tokens": self.cfg.max_run_input_tokens,
            "max_run_output_tokens": self.cfg.max_run_output_tokens,
            "max_run_cost_usd": self.cfg.max_run_cost_usd,
            "max_run_cost_cny": self.cfg.max_run_cost_cny,
            "api_provider": self.cfg.api_provider,
            "subscription_type": self.cfg.subscription_type,
            "claude_cli_version": self.cfg.claude_cli_version,
            "orchestrator_model": self.cfg.orchestrator_model,
            "agent_model": self.cfg.agent_model,
            "pricing_snapshot": pricing_snapshot(),
        }
    @staticmethod
    def _hash_fingerprint_payload(payload: dict) -> str:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _optimization_fingerprint(self) -> str:
        """Identify settings that affect the run, including its transport."""
        return self._hash_fingerprint_payload(
            self._optimization_fingerprint_payload()
        )

    def _optimization_core_fingerprint(self) -> str:
        """Identify every run setting except the model transport identity."""
        payload = self._optimization_fingerprint_payload()
        payload["schema"] = "transport-neutral-v1"
        for field in (
            "api_provider",
            "subscription_type",
            "claude_cli_version",
            "orchestrator_model",
            "agent_model",
        ):
            payload.pop(field)
        return self._hash_fingerprint_payload(payload)

    def _validate_resume_configuration(self, restored: RunState) -> bool:
        """Validate strict resume, or one safe subscription -> OR transition."""
        provider_switched = (
            restored.api_provider == "subscription"
            and self.cfg.api_provider == "openrouter"
        )
        if provider_switched:
            if restored.stop_reason not in _SAFE_SUBSCRIPTION_SWITCH_REASONS:
                raise RuntimeError(
                    "cannot switch subscription to OpenRouter after stop "
                    f"reason {restored.stop_reason!r}; allowed safe pause "
                    "reasons are subscription_quota_exhausted and wall_timeout"
                )
            if not restored.optimization_core_fingerprint:
                raise RuntimeError(
                    "cannot switch a legacy subscription run to OpenRouter "
                    "without a transport-neutral optimization fingerprint"
                )
            if (
                restored.optimization_core_fingerprint
                != self._optimization_core_fingerprint()
            ):
                raise RuntimeError(
                    "cannot switch subscription to OpenRouter after core "
                    "experiment configuration changed"
                )
            for field in ("orchestrator_model", "agent_model"):
                previous = getattr(restored, field, "")
                expected = f"anthropic/{previous}"
                current = getattr(self.cfg, field)
                if not previous or current != expected:
                    raise RuntimeError(
                        "cannot switch subscription to OpenRouter with a "
                        f"non-equivalent {field}: {previous!r} -> {current!r}"
                    )
            return True

        for field, current in (
            ("api_provider", self.cfg.api_provider),
            ("orchestrator_model", self.cfg.orchestrator_model),
            ("agent_model", self.cfg.agent_model),
        ):
            previous = getattr(restored, field, "")
            if previous and previous != current:
                raise RuntimeError(
                    f"cannot resume with a different {field}: "
                    f"{previous!r} -> {current!r}"
                )
        fingerprint = self._optimization_fingerprint()
        if (
            restored.optimization_fingerprint
            and restored.optimization_fingerprint != fingerprint
        ):
            raise RuntimeError(
                "cannot resume after token-optimization configuration "
                "changed; use a new --run-id"
            )
        return False

    @staticmethod
    def _safe_api_base_url(raw: str) -> str:
        """Remove credentials, query strings and fragments from summaries."""
        if not raw:
            return "(default)"
        try:
            parsed = urlsplit(raw)
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
            return urlunsplit((
                parsed.scheme, host, parsed.path, "", "",
            )) or "(custom endpoint)"
        except (TypeError, ValueError):
            return "(custom endpoint)"

    def _conflict_file_key(self, file_path: str) -> str:
        """Canonical repo-relative key for same-file dispatch conflicts."""
        raw = Path(file_path)
        repo = self.cfg.repo_root.resolve()
        target = self.cfg.target_path.resolve()
        candidates = [raw] if raw.is_absolute() else [
            target / raw,
            repo / raw,
        ]
        for candidate in candidates:
            resolved = candidate.resolve()
            if not resolved.exists():
                continue
            try:
                return resolved.relative_to(repo).as_posix()
            except ValueError:
                continue
        # Still normalize redundant "./" and ".." segments when an Analyst
        # reported a path that disappeared before dispatch.
        return posixpath.normpath(file_path.replace("\\", "/"))

    def _issue_file_exists(self, file_path: str) -> bool:
        """Thesis §6.2: analysts sometimes emit the example issue line from
        their own prompt as a real finding, putting a non-existent file in
        the backlog. The paper's mitigation is a file-existence check at
        the point issues are accepted, which is here.

        The analyst may report a path relative to its cwd (the target
        subtree) or to the repository root, so both bases are tried and an
        issue is rejected only when none of them resolves.
        """
        return self._resolve_issue_repo_path(file_path) is not None

    def _resolve_issue_repo_path(self, file_path: str) -> Optional[str]:
        """Resolve an issue spelling to an existing repo-relative file.

        Absolute paths are accepted only inside the coordinator checkout or
        one of its known agent worktrees. This maps an Analyst's absolute
        worktree path safely to the same relative file in all worktrees and
        rejects unrelated host files.
        """
        raw_text = (file_path or "").strip()
        if not raw_text:
            return None
        raw = Path(raw_text)
        repo = self.cfg.repo_root.resolve()

        if raw.is_absolute():
            resolved = raw.resolve()
            roots = [
                repo,
                *(path.resolve() for path in self.analyst_worktrees.values()),
                *(path.resolve() for path in self.programmer_worktrees.values()),
            ]
            for root in roots:
                try:
                    relative = resolved.relative_to(root)
                except ValueError:
                    continue
                if (repo / relative).is_file():
                    return relative.as_posix()
            return None

        target_relative = (self.cfg.target_path / raw).resolve()
        repo_relative = (repo / raw).resolve()
        for candidate in (target_relative, repo_relative):
            if not candidate.is_file():
                continue
            try:
                return candidate.relative_to(repo).as_posix()
            except ValueError:
                continue
        return None

    def _canonical_issue_path(self, file_path: str) -> str:
        """Store an existing issue file as a stable repo-relative path.

        Analysts run from the target subtree but may report target-relative,
        repo-relative, absolute, or leading-``./`` spellings of the same
        file. Canonical repo-relative storage prevents those spellings from
        becoming duplicate backlog items and remains valid in every agent
        worktree.
        """
        resolved = self._resolve_issue_repo_path(file_path)
        if resolved is not None:
            return resolved
        return posixpath.normpath(str(file_path).replace("\\", "/"))

    @staticmethod
    def _canonical_issue_type(issue_type: str) -> str:
        """Collapse common analyst aliases without changing issue meaning."""
        compact = "".join(ch for ch in str(issue_type).lower() if ch.isalnum())
        aliases = {
            "ccn": "complexity",
            "complexity": "complexity",
            "highcomplexity": "complexity",
            "cyclomaticcomplexity": "complexity",
            "nloc": "longFunction",
            "longfunction": "longFunction",
            "highnloc": "longFunction",
        }
        return aliases.get(compact, issue_type or "unknown")

    def _apply_merge_result(self, msg: mq.Message) -> None:
        """Record a merge: stagnation from the gate's delta, penalty and
        breakdown from a fresh measurement of the merged main."""
        issue_id = msg.payload.get("issue_id", "")
        snapshot = self.backlog.snapshot()
        item = snapshot.items.get(issue_id)
        if (
            item is None
            or item.status != IN_PROGRESS
            or item.assigned_to != msg.sender
        ):
            print(
                f"[coordinator] ignored unassigned merge report from "
                f"{msg.sender} for {issue_id!r}"
            )
            return

        before = float(msg.payload["penalty_before"])
        after = float(msg.payload["penalty_after"])
        if not self._gate_confirms_merge(msg.sender, issue_id, before, after):
            print(
                f"[coordinator] ignored unverified merge report from "
                f"{msg.sender} for {issue_id}"
            )
            return

        gate_reduction = before - after

        # The stagnation criterion is defined on the penalty reduction
        # the merge itself achieved, so it uses the gate's own delta.
        self.stagnation.record_merge(gate_reduction)

        measured, breakdown, stats = self._measure()
        self.current_penalty = measured
        self.metric_breakdown = format_breakdown(breakdown)
        self.metric_stats = stats
        self.state.merges += 1
        self.backlog.mark_done(issue_id)
        self.empty_analyst_scans = 0

        self.history.record(
            ph.MERGE,
            penalty=measured,
            issue_id=msg.payload.get("issue_id"),
            agent=msg.sender,
            gate_penalty_before=before,
            gate_penalty_after=after,
            gate_reduction=round(gate_reduction, 4),
            stagnation_counter=self.stagnation.counter,
            breakdown=breakdown,
        )
        self._save_state()
        print(
            f"[coordinator] merge by {msg.sender} "
            f"({msg.payload.get('issue_id', '?')}): "
            f"gate delta {gate_reduction:+.2f}, penalty now {measured:.2f}, "
            f"stagnation={self.stagnation.counter}"
        )

    def _gate_confirms_merge(
        self,
        agent: str,
        issue_id: str,
        before: float,
        after: float,
    ) -> bool:
        """Cross-check an LLM RESULT line against the gate's own record."""
        records = gate_attempts.load(
            self.cfg.run_results_path / self.cfg.gate_attempts_filename
        )
        for record in reversed(records):
            if record.get("agent") != agent:
                continue
            if record.get("issue_id") != issue_id:
                continue
            if record.get("outcome") != gate_attempts.MERGED:
                continue
            try:
                same_before = abs(float(record["penalty_before"]) - before) <= 0.05
                same_after = abs(float(record["penalty_after"]) - after) <= 0.05
            except (KeyError, TypeError, ValueError):
                return False
            return same_before and same_after
        return False

    def _reap_finished_futures(self) -> None:
        for name in list(self.programmer_futures):
            if self.programmer_futures[name].done():
                self.programmer_futures.pop(name, None)
                self.terminating_programmers.discard(name)
                self.last_stuck_eval.pop(name, None)
        for name in list(self.analyst_futures):
            if self.analyst_futures[name].done():
                self.analyst_futures.pop(name, None)
                self.terminating_analysts.discard(name)
                self.last_stuck_eval.pop(name, None)

    def _recover_active_gate_merges(self) -> int:
        """Apply durable gate merges before an agent RESULT is available."""
        records = gate_attempts.load(
            self.cfg.run_results_path / self.cfg.gate_attempts_filename
        )
        recovered = 0
        for pid, session in list(self.programmer_sessions.items()):
            # Terminating programmers remain eligible: a gate may persist a
            # successful merge in the narrow window between the pre-kill
            # reconciliation and process-group termination.
            if (
                pid not in self.programmer_futures
                and not self.subscription_quota_closed
            ):
                continue
            assigned = set(session.assigned_issues)
            for record in records[session.gate_record_start:]:
                issue_id = str(record.get("issue_id", ""))
                if (
                    issue_id not in assigned
                    or record.get("agent") != pid
                    or record.get("outcome") != gate_attempts.MERGED
                ):
                    continue
                item = self.backlog.snapshot().items.get(issue_id)
                if (
                    item is None
                    or item.status != IN_PROGRESS
                    or item.assigned_to != pid
                ):
                    continue
                try:
                    before = float(record["penalty_before"])
                    after = float(record["penalty_after"])
                except (KeyError, TypeError, ValueError):
                    continue
                self._apply_merge_result(mq.Message(
                    sender=pid,
                    kind=mq.MERGE_RESULT,
                    payload={
                        "issue_id": issue_id,
                        "penalty_before": before,
                        "penalty_after": after,
                    },
                ))
                if self.backlog.snapshot().items[issue_id].status == DONE:
                    recovered += 1
                    print(
                        f"[coordinator] recovered gate merge for {pid} "
                        f"({issue_id}) before agent completion"
                    )
        return recovered

    def _settle_subscription_quota(self) -> bool:
        """Wait for detached gates, then release their unresolved work."""
        if (
            not self.subscription_quota_closed
            or self.programmer_futures
            or self.analyst_futures
            or not self.queue.empty()
        ):
            return False
        if any(session.gate_active() for session in self.programmer_sessions.values()):
            return False

        dirty = False
        snapshot = self.backlog.snapshot()
        for pid, session in self.programmer_sessions.items():
            for iid in session.assigned_issues:
                item = snapshot.items.get(iid)
                if (
                    item is not None
                    and item.status == IN_PROGRESS
                    and item.assigned_to == pid
                ):
                    self.backlog.return_to_todo(iid)
                    dirty = True
        if dirty:
            self.backlog.persist()
        return True

    @staticmethod
    def _idle(sessions: dict, futures: dict) -> list[str]:
        return [name for name in sessions if name not in futures]

    # -- dispatch ------------------------------------------------------

    def _dispatch_if_needed(self) -> None:
        idle_programmers = self._idle(self.programmer_sessions, self.programmer_futures)
        idle_analysts = self._idle(self.analyst_sessions, self.analyst_futures)
        if not idle_programmers and not idle_analysts:
            return

        self._sync_issue_attempt_state()
        snapshot = self.backlog.snapshot()
        if not self._has_actionable_work(snapshot, idle_programmers, idle_analysts):
            return

        try:
            decision = self.orchestrator.assign(
                current_penalty=self.current_penalty,
                baseline_penalty=self.baseline_penalty,
                backlog=snapshot,
                idle_programmers=idle_programmers,
                idle_analysts=idle_analysts,
                stagnation=self.stagnation.counter,
                metric_breakdown=self.metric_breakdown,
            )
        except SubscriptionQuotaExhausted as exc:
            self.subscription_quota_closed = True
            print(f"[coordinator] {exc}")
            return
        # The synchronous orchestrator call itself may cross a ceiling.
        if self._check_token_budget():
            return
        submitted = self._apply_dispatch(
            decision, idle_programmers, idle_analysts,
        )
        self._record_assignment_progress(submitted)

    def _record_assignment_progress(self, submitted: bool) -> None:
        if submitted:
            self.empty_assignment_decisions = 0
            return
        # Do not stop and kill valid work merely because another idle slot
        # received an empty assignment while agents are still active.
        if self.programmer_futures or self.analyst_futures:
            return
        self.empty_assignment_decisions += 1
        if (
            self.empty_assignment_decisions
            >= self.cfg.orchestrator_no_progress_limit
        ):
            self.stop_reason = "orchestrator_no_progress"

    def _has_actionable_work(
        self,
        snapshot,
        idle_programmers: list[str],
        idle_analysts: list[str],
    ) -> bool:
        # Skip the orchestrator LLM call entirely when there is nothing
        # it could usefully assign — this is the per-tick hot path.
        has_todo = any(it.status == TODO for it in snapshot.items.values())
        has_in_progress = any(
            it.status == IN_PROGRESS for it in snapshot.items.values()
        )
        if idle_programmers and has_todo:
            return True
        nearing_stagnation = self.stagnation.counter >= self.cfg.stagnation_limit - 1
        if (
            idle_analysts
            and not self.analyst_futures
            and self.empty_analyst_scans < self.cfg.empty_scan_limit
            and (
                (not has_todo and not has_in_progress)
                or nearing_stagnation
            )
        ):
            return True
        return False

    def _apply_dispatch(
        self,
        decision: AssignmentDecision,
        idle_programmers: list[str],
        idle_analysts: list[str],
    ) -> bool:
        snapshot = self.backlog.snapshot()
        dirty = False
        submitted = False
        claimed_issue_ids: set[str] = set()
        claimed_files: set[str] = {
            self._conflict_file_key(item.file_path)
            for item in snapshot.items.values()
            if item.status == IN_PROGRESS
        }
        # Analysts are dispatched in waves. Do not refill a slot merely
        # because one Analyst in the current wave finished before its peers;
        # that timing-dependent rolling wave can exceed empty_scan_limit and
        # consume extra model calls. Multiple Analysts selected in this same
        # decision are still submitted together below.
        analyst_wave_active = bool(self.analyst_futures)

        for pid, issue_ids in decision.programmer_assignments.items():
            if pid not in idle_programmers:
                continue
            specs = []
            local_ids: set[str] = set()
            for spec in self._collect_specs(issue_ids, snapshot):
                if spec["id"] in claimed_issue_ids or spec["id"] in local_ids:
                    continue
                file_key = self._conflict_file_key(spec["file_path"])
                if file_key in claimed_files:
                    continue
                specs.append(spec)
                local_ids.add(spec["id"])
                if len(specs) == 2:
                    break
            if not specs:
                continue
            session = self.programmer_sessions[pid]
            try:
                future = self.programmer_pool.submit(session.run, specs)
            except RuntimeError:
                continue
            self.programmer_futures[pid] = future
            submitted = True
            self.last_stuck_eval.pop(pid, None)
            for spec in specs:
                self.backlog.mark_dispatched(
                    spec["id"], pid, self.cfg.max_issue_dispatches,
                )
                claimed_issue_ids.add(spec["id"])
                claimed_files.add(
                    self._conflict_file_key(spec["file_path"])
                )
            dirty = True

        for aid in decision.dispatch_analysts:
            # Do not refill Analyst slots after discovery exhaustion. With
            # staggered completions, waiting for all in-flight scans while
            # redispatching each newly idle slot creates an endless rolling
            # wave and unbounded token use.
            if self.empty_analyst_scans >= self.cfg.empty_scan_limit:
                continue
            if analyst_wave_active:
                continue
            if aid not in idle_analysts:
                continue
            focus = decision.analyst_targets.get(aid, "")
            focus_metrics = [t.strip() for t in focus.split(",") if t.strip()]
            session = self.analyst_sessions[aid]
            leads = self._reserve_local_leads(aid, focus_metrics)
            try:
                future = self.analyst_pool.submit(
                    session.run, focus_metrics, self.metric_breakdown, leads,
                )
            except RuntimeError:
                self.analyst_lead_inflight.pop(aid, None)
                continue
            self.analyst_futures[aid] = future
            submitted = True

        if dirty:
            self.backlog.persist()
        return submitted

    def _reserve_local_leads(
        self,
        analyst_id: str,
        focus: list[str],
    ) -> list[LocalLead]:
        inflight = set().union(
            *self.analyst_lead_inflight.values()
        ) if self.analyst_lead_inflight else set()
        available = [
            lead for lead in self.local_candidate_leads
            if lead.key not in self.analyst_lead_seen_keys
            and lead.key not in inflight
        ]
        focused = [
            lead for lead in available if lead_matches_focus(lead, focus)
        ]
        # A model-supplied directory/metric hint may not match Lizard's path
        # spelling. Fall back to the ranked local page so an unmatched hint
        # cannot strand valid leads and create an endless empty-scan loop.
        page = (focused or available)[:self.cfg.analyst_lead_page_size]
        if page:
            self.analyst_lead_inflight[analyst_id] = {
                lead.key for lead in page
            }
        return page

    def _collect_specs(self, issue_ids: list[str], snapshot) -> list[dict]:
        specs: list[dict] = []
        for iid in issue_ids:
            item = snapshot.items.get(iid)
            if (
                item is None
                or item.status != TODO
                or item.dispatch_count >= self.cfg.max_issue_dispatches
            ):
                continue
            specs.append({
                "id": iid,
                "file_path": item.file_path,
                "line": item.line,
                "issue_type": item.issue_type,
                "message": item.message,
                "dispatch_number": item.dispatch_count + 1,
                "dispatch_limit": self.cfg.max_issue_dispatches,
                "attempt_history_path": str(
                    self.cfg.run_results_path / item.attempt_history_path
                ) if item.attempt_history_path else "",
                "attempt_feedback": item.attempt_feedback,
            })
        return specs

    def _sync_issue_attempt_state(self) -> None:
        """Refresh bounded feedback and deterministically skip exhausted work."""
        snapshot = self.backlog.snapshot()
        dirty = False
        for issue_id, item in snapshot.items.items():
            path = issue_history.history_path(
                self.cfg.issue_history_dir, issue_id,
            )
            relative = str(path.relative_to(self.cfg.run_results_path))
            summaries = issue_history.feedback(issue_history.load(path))
            dirty = self.backlog.update_attempt_state(
                issue_id, relative, summaries,
            ) or dirty
            if (
                item.status == TODO
                and item.dispatch_count >= self.cfg.max_issue_dispatches
            ):
                self.backlog.mark_skipped(
                    issue_id,
                    f"issue dispatch limit reached "
                    f"({self.cfg.max_issue_dispatches})",
                )
                dirty = True
        if dirty:
            self.backlog.persist()

    # -- stuck-agent monitoring ---------------------------------------

    def _check_stuck_agents(self) -> None:
        """Two decision points, as specified in the thesis.

        Past `issue_timeout_sec` (the ten-minute per-issue limit) the
        orchestrator is asked whether to terminate or keep each agent.
        Past `programmer_timeout_sec` (thirty minutes) this layer kills
        unconditionally. Only the hard timeout ticks stagnation.
        """
        now = time.time()
        hard: list[tuple[str, ProgrammerSession]] = []
        soft: list[tuple[str, ProgrammerSession]] = []

        for pid, session in list(self.programmer_sessions.items()):
            if pid not in self.programmer_futures:
                continue
            # Build and test have independent deadlines inside the merge
            # gate. Do not spend the model's hard-timeout budget while
            # repository validation is active.
            if session.gate_active():
                if session.gate_runtime_sec() > self.cfg.gate_timeout_sec:
                    hard.append((pid, session))
                continue
            runtime = session.runtime_sec()
            if runtime > self.cfg.programmer_timeout_sec:
                hard.append((pid, session))
            elif runtime > self.cfg.issue_timeout_sec:
                last = self.last_stuck_eval.get(pid, 0.0)
                if now - last >= self.cfg.stuck_eval_interval_sec:
                    soft.append((pid, session))

        for pid, session in hard:
            self._terminate_programmer(pid, session, hard_timeout=True)

        if soft:
            self._evaluate_stuck(soft, now)
        self._check_analyst_timeouts()

    def _check_analyst_timeouts(self) -> None:
        """Bound Analyst tool calls so discovery cannot hang a run."""
        for aid, session in list(self.analyst_sessions.items()):
            if (
                aid not in self.analyst_futures
                or aid in self.terminating_analysts
                or session.runtime_sec() <= self.cfg.analyst_timeout_sec
            ):
                continue
            self.terminating_analysts.add(aid)
            session.kill()
            self.analyst_lead_inflight.pop(aid, None)
            self.empty_analyst_scans += 1
            self.history.record(
                ph.ANALYST_TIMEOUT,
                penalty=self.current_penalty,
                agent=aid,
                runtime_sec=round(session.runtime_sec(), 1),
            )
            self._save_state()
            print(
                f"[coordinator] terminated {aid} (analyst hard timeout)"
            )

    def _evaluate_stuck(
        self,
        soft: list[tuple[str, "ProgrammerSession"]],
        now: float,
    ) -> None:
        payload = [
            {
                "programmer_id": pid,
                "runtime_sec": session.runtime_sec(),
                "edits_made": session.edits_made(),
                "gate_invocations": session.gate_invocations(),
                "assigned_issues": list(session.assigned_issues),
                "recent_log": session.tail_log(),
            }
            for pid, session in soft
        ]
        # Stamp before the call so a failure cannot cause a retry storm.
        for pid, _ in soft:
            self.last_stuck_eval[pid] = now

        try:
            decision = self.orchestrator.evaluate_stuck(
                stuck=payload,
                stagnation=self.stagnation.counter,
                hard_timeout_sec=self.cfg.programmer_timeout_sec,
            )
        except SubscriptionQuotaExhausted as exc:
            self.subscription_quota_closed = True
            print(f"[coordinator] {exc}")
            return
        except Exception as exc:
            print(f"[coordinator] stuck-agent evaluation failed: {exc}")
            return

        dirty = False
        for pid in decision.terminate:
            session = self.programmer_sessions.get(pid)
            if session is None or pid not in self.programmer_futures:
                continue
            # Discretionary termination: no stagnation tick, per the
            # thesis end-condition definition.
            self._terminate_programmer(pid, session, hard_timeout=False)
            dirty = True

        for iid in decision.infeasible_issues:
            self.backlog.mark_skipped(iid, "orchestrator: marked infeasible")
            dirty = True

        if decision.reasoning:
            print(f"[coordinator] stuck-agent verdict: {decision.reasoning}")
        if dirty:
            self.backlog.persist()

    def _terminate_programmer(
        self,
        pid: str,
        session: "ProgrammerSession",
        hard_timeout: bool,
    ) -> None:
        if pid in self.terminating_programmers:
            return
        # Close the race where the gate fast-forwarded and persisted its
        # result immediately before this timeout check.
        self._recover_active_gate_merges()
        self.terminating_programmers.add(pid)
        session.kill()
        self._recover_active_gate_merges()
        snapshot = self.backlog.snapshot()
        unresolved = [
            iid for iid in session.assigned_issues
            if (
                (item := snapshot.items.get(iid)) is not None
                and item.status == IN_PROGRESS
                and item.assigned_to == pid
            )
        ]
        for iid in unresolved:
            self.backlog.return_to_todo(iid)
        if hard_timeout and unresolved:
            self.stagnation.record_timeout()
        worktree = self.programmer_worktrees.get(pid)
        if worktree is not None:
            try:
                reset_worktree(worktree, self.cfg.integration_branch)
            except Exception:
                pass
        self.last_stuck_eval.pop(pid, None)
        if unresolved:
            self.history.record(
                ph.TIMEOUT_KILL if hard_timeout else ph.STUCK_TERMINATE,
                penalty=self.current_penalty,
                agent=pid,
                runtime_sec=round(session.runtime_sec(), 1),
                stagnation_counter=self.stagnation.counter,
            )
        self.backlog.persist()
        self._save_state()
        if unresolved:
            print(
                f"[coordinator] terminated {pid} "
                f"({'hard timeout' if hard_timeout else 'orchestrator verdict'}), "
                f"stagnation={self.stagnation.counter}"
            )
        else:
            print(
                f"[coordinator] stopped {pid} after recovering all assigned "
                "gate merges"
            )
