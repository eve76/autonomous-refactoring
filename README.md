# Multi-Agent Refactoring Experiment

A reproduction of the multi-agent refactoring system described in the
thesis Method chapter (§4.3.2 and §4.5.2). The system runs an
analyst → backlog → orchestrator → programmer → merge-gate loop on a
target git repository, driving the total static-analysis penalty score
downward until a stagnation criterion stops it.

**The only intended deviations from the thesis are the agent runtime and
the model.** The thesis system was built on the KIRO CLI with Claude
Opus 4.6; this reproduction uses OAuth-authenticated Claude Code CLI
sessions for the orchestrator, analysts, and programmers. The default
`subscription` transport consumes the logged-in Claude Pro/Max allocation,
not Anthropic API credits. Anthropic and DeepSeek APIs remain explicit
comparison modes.
Everything else — the architecture, the penalty mathematics, the merge
gate pipeline, the two orchestrator decision points, the stopping
criterion and the prompt content — follows the thesis text.

Because the runtime differs, the KIRO-specific settings described in
§4.3 (chain-of-thought toggle, long-term memory, Tangent Mode, Todo
Lists, Checkpointing, Context Usage Indicator) have no counterpart here.

***This repository was reproduced with the help of AI.***

---

## Architecture

- **Coordination layer** (deterministic Python). Owns all shared state
  on the main thread, drains a thread-safe message queue, persists the
  product backlog atomically, and dispatches agents via thread pools.
- **Agent layer** (LLM-based judgment). Three roles:
  - **Orchestrator** — one synchronous, tool-free, JSON-Schema-constrained
    Claude Code CLI turn at each of the thesis's *two* decision points:
    task assignment and stuck-agent evaluation. API transports retain their
    previous SDK implementation.
  - **Analyst** — Claude CLI subprocess. Scans the accepted
    (main-branch) source tree in its own detached worktree and emits
    `ISSUE:` lines.
  - **Programmer** — Claude CLI subprocess. Refactors flagged code
    inside its own feature-branch worktree, commits, and merges through
    the merge gate.

Default population: 1 orchestrator, 3 analysts, 3 programmers.

```
┌─────────────────────────────────────────────────────────────────┐
│                     Coordination layer (main thread)            │
│   ┌──────────┐   ┌──────────┐   ┌──────────────┐   ┌─────────┐  │
│   │ backlog  │   │ message  │   │ stagnation   │   │ git mgr │  │
│   │  store   │   │  queue   │   │   tracker    │   │worktrees│  │
│   └──────────┘   └──────────┘   └──────────────┘   └─────────┘  │
│         │             ▲                                         │
│         │  drain      │ post                                    │
│         ▼             │                                         │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │            ThreadPoolExecutor (programmers)             │   │
│   │            ThreadPoolExecutor (analysts)                │   │
│   └─────────────────────────────────────────────────────────┘   │
│                          │                                      │
│                          │ subprocess (claude CLI)              │
│                          ▼                                      │
│           ┌──────────────────────────────────┐                  │
│           │ ANALYST_n / PROG_n  (per process)│                  │
│           └──────────────────────────────────┘                  │
└─────────────────────────────────────────────────────────────────┘

Synchronous Claude Code subscription calls (default):
   coordinator ──► Orchestrator.assign()         ──► AssignmentDecision
   coordinator ──► Orchestrator.evaluate_stuck() ──► StuckDecision
```

---

## Directory layout

```
experiment_token_save/
├── main.py                    # CLI entry: parses args -> Coordinator.run
├── config.py                  # Central configuration dataclass
├── production_profiles.py     # Locked FerretDB / MongoDB-query production scopes
├── DYNAMIC_BENCHMARK_EXTENSION_PLAN.md # Deferred performance experiment plan
├── scripts/
│   └── production_validation.py # Repository-native build/test entrypoint
├── requirements.txt
├── bin/duplo                  # Duplo binary
│
├── coordination/              # Deterministic Python (single writer)
│   ├── coordinator.py         #   main loop, dispatch, stuck handling
│   ├── backlog.py             #   thread-safe JSON-backed product backlog
│   ├── git_manager.py         #   worktree provisioning + sparse-checkout
│   ├── message_queue.py       #   typed agent → coordinator messages
│   ├── stagnation.py          #   stopping-criterion tracker
│   ├── penalty_history.py     #   penalty time series + plot
│   ├── gate_attempts.py       #   gate attempt log + failure counts (§5.1/§5.4)
│   ├── run_state.py           #   crash-recovery state file
│   └── token_usage.py         #   provider-neutral token accounting
│
├── agents/                    # LLM-driven roles
│   ├── orchestrator.py        #   schema-constrained CLI/SDK decisions
│   ├── analyst.py             #   claude-CLI subprocess; emits ISSUE: lines
│   ├── programmer.py          #   claude-CLI subprocess; refactor + gate
│   ├── agent_runner.py        #   shared subprocess + log streamer
│   ├── provider.py            #   subscription auth/env + optional API clients
│   └── log_parser.py          #   stream-json -> assistant text extractor
│
├── analysis/                  # Quality measurement
│   ├── penalty.py             #   hyperbolic penalty + per-metric breakdown
│   ├── metrics.py             #   metric distributions (Tables 4.1 / 5.1)
│   ├── candidates.py          #   non-authoritative Lizard leads for Analysts
│   └── tools.py               #   Lizard / cognitive-complexity / Duplo
│
├── merge_gate/                # Per-issue evaluation pipeline
│   ├── gate.py                #   rebase → penalty → build → test → ff-merge
│   └── cli.py                 #   entry programmers invoke from worktree
│
├── prompts/
│   ├── orchestrator_assignment.txt
│   ├── orchestrator_stuck.txt
│   ├── analyst.txt
│   └── programmer.txt
│
└── tests/                     # Verification suites (no API key needed)
    ├── run_all.py
    ├── test_merge_gate.py     #   end-to-end on a throwaway git repo
    ├── test_run_isolation.py  #   per-run branch protocol (§4.4)
    ├── test_metric_stats.py   #   distributions + Lizard/Duplo accounting
    ├── test_artifacts.py      #   penalty history / plot / state / summary
    ├── test_stuck_policy.py   #   two-tier stuck-agent policy
    ├── test_failure_counts.py #   gate attempt log + failure counts
    ├── test_provider_config.py #  Anthropic/DeepSeek provider isolation
    ├── test_token_usage.py    #   provider/cache accounting + recovery
    ├── test_go_cognitive.py   #   gocognit scope/parser/penalty checks
    └── test_token_optimizations.py # Top-24, local leads, token ceilings
```

---

## Quality metrics and penalty

All five metrics from the thesis are wired:

| Metric                | Tool                              | Threshold | Notes                                       |
|-----------------------|-----------------------------------|-----------|---------------------------------------------|
| Cyclomatic complexity | Lizard (`--csv`)                  | 15        | per function                                |
| Logical lines of code | Lizard (`--csv`, `nloc` column)   | 30        | per function                                |
| Parameter count       | Lizard (`--csv`)                  | 5         | per function                                |
| Cognitive complexity  | `modified_cognitive_complexity` / `gocognit` | 15 | per function, C/C++ / Go — independent list |
| Duplicate-line ratio  | Duplo (C/C++) / mibk/dupl (Go)   | —         | codebase-level                              |

**Lizard is invoked with `--csv`, not its default tabular output.** The
tabular output repeats every function that trips Lizard's *own* warning
threshold (`cyclomatic_complexity > 15`) in a trailing
`!!!! Warnings !!!!` section. Scanning it line by line therefore counted
those functions twice — precisely the functions that carry penalty — so
the penalty and every distribution statistic were inflated for exactly
the code the system is meant to fix. `--csv` emits one row per function
and quotes paths, so it also survives filenames containing spaces.
`tests/test_metric_stats.py` guards against a regression.

**Duplo's text summary is parsed, not its `-json` output.** §4.2.2 notes
Duplo was chosen partly for its JSON output, but that output contains
only the block list: it carries no line totals, so the duplicate-line
*ratio* cannot be derived from it. Duplo's own `Lines of code` figure is
a post-filter count (it drops preprocessor directives under `-ip` and
lines below the `-mc` minimum) that cannot be reconstructed externally,
and the two modes are mutually exclusive — passing `-json` suppresses the
summary entirely. Parsing the summary is therefore the only way to
reproduce the ratio Duplo itself reports, and hence the 2.41% → 19.4
calibration in §4.5.1. The block count that Table 4.1 reports alongside
the ratio is in the summary too. Duplo is always handed absolute paths,
because it silently reports zero lines for relative ones.

For FerretDB, the profile instead uses `mibk/dupl`, whose detector parses
Go syntax into an AST-derived token stream. Its plumbing output reports
clone pairs in both directions, so the adapter canonicalizes reverse pairs
and merges overlapping ranges per file. The duplicate-line numerator and
denominator are physical source lines from the same production-only Go file
list; this keeps the ratio bounded. The resulting ratio enters the existing
Eq. 4.5 penalty unchanged.

`-ml` (minimum duplicate block size) defaults to **4**, which is Duplo's
own default. The thesis never states one — §4.2.2 only requires that
preprocessor directives and comments be filtered — so the tool default is
the reconstruction that assumes least, and every other Duplo knob is left
alone for the same reason. Raising it suppresses short duplicate blocks,
lowering both the ratio and the block count, so it is worth checking
against Table 4.1 (**7 blocks at a 2.41% ratio**) on the real target
codebase; that pair is the only published handle on this setting.

### Excluded directories

§4.3.2: "Test directories are excluded since they do not contribute to
the penalty score." The thesis says *directories*, plural, and never
enumerates them, so the list is a parameter of the experiment
(`--exclude-dirs`), not something the thesis pins down.

Matching only a directory named exactly `test` was too narrow to be a
safe default. A repository laid out with `tests/` would have its test
files measured, so the analyst would file issues against them — issues
the programmer prompt forbids acting on, and which could therefore only
ever be skipped. The default list is limited to names that are test
artifacts by convention and never production code:

```
test  tests  testing  unittest  unittests  unit_test  unit_tests
gtest  googletest  gmock  mocks
```

Matching is on whole path components and is case-insensitive, so
`Testing/` is excluded while `latest/` is not. All three tools receive
the same file list, so the duplication ratio cannot end up measured over
a different population than the per-function metrics. Omitting the flag
keeps the default; passing `--exclude-dirs ''` measures everything.

Penalty function:

- Per-function: `p_m(x) = 100 · (1 − T_m / max(T_m, x))`
- Duplicates: `p_dup(r) = 100 · r / (r + 0.1)`

Total penalty is the **weighted** sum of all per-function penalties
across all functions and metrics, plus the duplicate-ratio penalty.
Lizard records (CCN/NLOC/param) and cognitive-complexity records are
kept as **two separate lists**: a function that the cognitive tool
detects but Lizard doesn't (or vice versa) still contributes its own
metrics independently. Setting any weight to 0 in `config.weights`
disables that metric entirely, which is how single-metric and ablation
runs are configured.

`compute_penalty_breakdown()` splits the same total per metric and is
what feeds the "per-metric breakdown ranked by improvement potential"
that the thesis requires in the orchestrator and analyst prompts. Its
per-metric penalties sum exactly to `compute_total_penalty()` on the
same inputs (asserted in `tests/test_merge_gate.py`).

The same implementation is shared by the merge gate and the backlog
impact estimator (`estimate_reduction_from_message`), and both honor the
same weight dict, so they cannot drift.

### Metric distributions

The penalty is the agents' optimisation signal, but the thesis *reports*
quality through each metric's distribution — §4.2.3: "the quality is
evaluated using the upper percentiles of the distribution". `analysis/
metrics.py` computes, per metric, the threshold, population size, mean,
median, p90, p95, p99, max, the number of functions over the threshold,
and whether the metric is **cleared** (§4.5.1: every function below its
threshold). Duplicates carry the line ratio (as a fraction and a percent),
the duplicate/total line counts, and the block count.

Statistics are taken over **all** functions the tools report, not only
violating ones — Table 4.1's mean CCN of 3.21 against a threshold of 15
is only meaningful over the whole population. Percentiles use linear
interpolation between closest ranks (numpy's default); the method is
recorded in `run_summary.json` so the thesis can state it.

The penalty implementation is validated against the numbers the thesis
publishes: Eq 4.5 yields 19.42 at a duplication ratio of 2.41% (thesis:
19.4), and Eq 4.4 yields 27.30 for cognitive scores {19, 16} (thesis:
27.3 total).

---

## Lifecycle

1. **Init.** The coordinator
   - refuses to start if the target repository has uncommitted changes,
   - resolves the baseline ref (`--baseline-ref`, else `origin/<main>`,
     else `<main>`) and records its commit,
   - for a fresh run, creates `refactor/<run-id>` at that baseline and
     refuses to overwrite evidence for an existing run id; for
     `--resume`, checks out the existing run branch without moving it,
   - creates one feature-branch worktree per programmer off that branch
     (sparse-checkout by default, or a full worktree with
     `--full-worktree` when native tests need repository-wide dependencies),
   - creates one detached-HEAD worktree per analyst pinned to it,
   - runs Lizard + cognitive-complexity + Duplo to compute the
     baseline penalty, its per-metric breakdown, and the baseline metric
     distribution,
   - opens `penalty_history.json` and writes the baseline event,
   - registers a SIGTERM handler so external timeouts produce a clean
     `stop_reason = "wall_timeout"` exit.

2. **Loop tick (~1 s).**
   - Drain the message queue: apply backlog mutations, record merges,
     advance the stagnation counter.
   - Reap finished thread-pool futures.
   - If there is *actionable* work (idle programmer + TODO items, or
     idle analyst when backlog empty / stagnation looming), call the
     orchestrator for an assignment plan.
   - Submit `session.run(...)` to the appropriate pool.
   - Check stuck agents (two tiers, see below).

3. **Programmer subprocess** (per assigned issue):
   - reset the worktree onto the latest integration branch → read the
     flagged file → edit → **commit**,
   - the coordination layer writes a config under
     `<results>/<run-id>/gate_configs/`, outside the worktree so neither
     `git clean` nor the clean-tree check can invalidate it,
   - run the absolute gate path with the coordinator's Python interpreter,
     `--config <path>` and `--issue-id <id>`. The gate:
       1. refuses to proceed if the worktree has uncommitted changes,
       2. rebases onto the latest integration branch — leaving any
          conflict in place for the programmer to resolve by hand,
       3. rejects any committed path outside `target_subdir`; a full
          worktree expands test visibility, not refactoring authority,
       4. re-measures the integration branch's current penalty as the
          baseline (never trusts a caller-supplied value — another
          programmer may have merged since the issue was assigned),
       5. **if the penalty did not decrease, reverts the worktree to
          the pre-refactoring state**,
       6. builds and tests; on failure it reports without reverting, so
          the programmer can fix its refactored code and re-run,
       7. fast-forward merges the integration branch, retrying the whole
          cycle up to 3 times if another programmer wins the race.
   - emits `RESULT: ISSUE-X done|skipped ...` so the coordinator can
     parse the outcome from the stream-json log.

4. **Analyst subprocess** (per dispatch):
   - the coordinator resets the analyst's worktree to the integration
     branch so it sees only accepted code, then spawns the CLI session
     with the current metric breakdown and at most 15 local Lizard leads,
   - leads are search hints only; the Analyst must inspect the code and
     independently decide whether to emit the unchanged `ISSUE:` format,
   - the analyst optionally runs the static-analysis tools and emits
     one `ISSUE:` line per violation; the coordinator parses, dedupes,
     and adds them to the backlog.

5. **Stop** when the stagnation counter reaches its limit, the penalty is
   zero, repeated analyst scans find no actionable work, or on external
   SIGTERM. Either way the run's artefacts are written, the
   worktrees are removed, the run branch is optionally pushed, and the
   working directory is restored to the baseline.

### The per-run integration branch

Thesis §4.4 asks for two things that pull in the same direction: the
codebase must be *reset to its original state before each run*, across
10 independent runs, and each run must *create a dedicated git branch,
record all its commits there, push it at the end, and leave the working
directory back at the original baseline*.

Both fall out of never advancing the primary branch. Each run creates
`refactor/<run-id>` at the baseline ref and the merge gate fast-forwards
**that** branch. `main` is therefore still sitting on the baseline when
the run ends, so:

- the next run starts from an identical state with **no destructive
  reset** — nothing has to be thrown away, so a stray local commit on
  `main` can never be lost;
- the run branch survives as the permanent record §4.4 asks for;
- restoring the baseline is a plain `git checkout main`, verified against
  the recorded baseline commit rather than assumed.

Within a run, every rebase, reset and revert targets the **local**
integration branch. All worktrees share one object database, so that ref
reflects each fast-forward merge the instant it lands. A remote-tracking
ref would stay frozen at the run's starting commit, because the branch is
not pushed until the run ends — rebasing there would pin every programmer
to the original code and make the second concurrent merge impossible.
`origin` is touched exactly twice: a best-effort `fetch` at startup to
resolve the baseline, and the optional push at the end.

The run refuses to start if the target repository has uncommitted
changes, rather than stashing or discarding them.

---

## Stopping criterion

| Trigger                                                              | Stagnation tick? |
|----------------------------------------------------------------------|------------------|
| A merge yields penalty reduction below `min_merge_gain` (10 units)   | yes              |
| A programmer is killed for exceeding `programmer_timeout_sec` (30 m) | yes              |
| Orchestrator discretionary terminate (stuck, but before 30 m)         | no               |
| A gate attempt is reverted because the penalty did not decrease      | no — see below   |

The system halts once the counter reaches `stagnation_limit`
(default 3) without an intervening high-gain merge. A merge whose
reduction is *exactly* `min_merge_gain` counts as low-gain, since the
thesis condition for resetting the counter is a strict `> 10`.

**Reverted attempts deliberately do not tick the counter.** This looks
wrong against Tables 4.5/4.6, whose caption reads "entries marked r
denote reverted attempts … counting as zero improvement toward the
stagnation counter" — a zero-improvement cycle would tick. But those
tables validate the *single-agent* system, which counts sequential
cycles. §4.3.2 defines the multi-agent counter explicitly and lists only
two triggers: the reduction "from a merge falls below 10 units", or a
programmer "terminated for exceeding its 30-minute time limit". Reverted
attempts are not among them. They are counted and reported in
`run_summary.json` (§5.1) but do not drive the stop condition.

---

## Stuck-agent handling (two tiers)

The thesis has the orchestrator judge stuck agents, so this
reproduction implements both tiers:

- **Past `issue_timeout_sec` (10 min, the thesis's per-issue limit):**
  the coordinator hands the orchestrator each stuck programmer's
  runtime, recent log output, whether it has made file edits, and how
  many times it has invoked the merge gate. The orchestrator replies
  `terminate` or `keep` per agent, and may additionally list issues to
  mark infeasible. Re-evaluation of the same agent is throttled by
  `stuck_eval_interval_sec` (5 min). A discretionary termination does
  **not** tick the stagnation counter.
- **Past `programmer_timeout_sec` (30 min):** the coordinator kills
  unconditionally without consulting the orchestrator, returns the
  issues to TODO, resets the worktree, and ticks stagnation.

Kills use `os.killpg(getpgid(pid), SIGKILL)` against the process group
of the agent subprocess (launched with `start_new_session=True`), so
grandchildren spawned by Claude (build tools, git, etc.) are reaped
along with the agent — no orphaned processes.

---

## Run artefacts

Each run writes to `<work-root>/results/<run-id>/`:

| File                   | Contents                                                              |
|------------------------|-----------------------------------------------------------------------|
| `penalty_history.json` | Every penalty-changing event with `elapsed_sec`, the per-metric breakdown at each merge, the gate's before/after values, and the stagnation counter |
| `penalty.png`          | Penalty-vs-time step plot with merge and termination markers          |
| `run_summary.json`     | Baseline/final penalty, total reduction and %, merge count, stop reason, backlog tallies, the baseline commit and integration branch, the **baseline and final metric distributions**, per-metric mean change, the cleared-metric list, the **failure counts**, and the full parameter set used |
| `gate_attempts.jsonl`  | One line per merge-gate attempt: agent, outcome, before/after penalty, reason |
| `issues/<ISSUE-ID>/history.jsonl` | Run-wide feedback for one issue, including strategy, outcome and patch fingerprint |
| `issues/<ISSUE-ID>/patches/*.patch` | Committed candidate patches saved before a rejection can reset the worktree |
| `logs/<AGENT>_<nnn>.log` | Full Claude CLI stream-json log, one file per agent **per dispatch** |
| `orchestrator_usage.jsonl` | Append-only token event per orchestrator CLI/API call; retained across `--resume` |
| `orchestrator_raw_responses.jsonl` | Full raw CLI/API response and the exact text passed to the Orchestrator parser for every call; contains no credential |
| `token_usage.json`     | Cache-aware totals by role, agent and dispatch, coverage, and tokens per successful merge |

The `metrics` block in `run_summary.json` is what Tables 4.1 and 5.1 are
built from:

```json
"metrics": {
  "percentile_method": "linear interpolation between closest ranks",
  "baseline": { "ccn": { "threshold": 15, "functions": 412, "mean": 3.21,
                         "median": 1.0, "p90": 6.0, "p95": 9.0, "p99": 28.0,
                         "max": 47.0, "over_threshold": 9, "cleared": false },
                "duplicates": { "line_ratio": 0.0241, "line_ratio_pct": 2.41,
                                "duplicate_lines": 241, "total_lines": 10000,
                                "block_count": 7, "cleared": false } },
  "final": { "...": "same shape" },
  "mean_change_pct": { "ccn": -28.5, "...": 0 },
  "cleared": ["cognitive", "param"]
}
```

### Conservative token-saving stages 1–3

This directory is the optimized variant; `writing/experiment` remains the
control. The first three changes deliberately preserve the 3+3 roles,
`ISSUE:` protocol, backlog schema, 1–2 issue assignment, merge gate and
stagnation logic:

1. **Accounting and optional dispatch ceilings.** `run_summary.json`
   contains cache-aware totals and `token_usage.json` retains dispatch
   detail. `--max-run-input-tokens`, `--max-run-output-tokens`, and
   `--max-run-cost-usd` defaults to 0; DeepSeek runs default
   `--max-run-cost-cny` to 300 RMB. They stop new dispatches and
   let in-flight agents finish, so final usage/cost may overshoot and the
   overshoot is reported.
2. **Compact Orchestrator view.** The LLM receives every `IN_PROGRESS`
   issue, full status counts, and only the highest-impact 24 TODO issues.
   DONE/SKIPPED bodies and lower-ranked TODO remain in the local backlog.
3. **Conservative Analyst leads.** Existing Lizard records produce
   non-authoritative CCN/NLOC/parameter leads. Up to 15 disjoint leads are
   sent to each Analyst. Only a successful Analyst review marks a lead as
   seen; crashes release it for retry. Cognitive complexity and Duplo keep
   the original Analyst-driven path.

The compact view and lead pages reduce LLM context; they do not delete
experimental evidence or directly insert static-analysis output into the
backlog.

The `failures` block is what §5.1's "failure counts (reverted attempts,
test failures, stagnation exits)" and §5.4's system deviations are built
from. The merge gate runs as its own process inside each programmer's
worktree, so it cannot post onto the coordination layer's message queue;
it appends one line per attempt to `gate_attempts.jsonl` instead, and the
coordination layer tallies that file when it writes the summary:

```json
"failures": {
  "gate_attempts": 41, "merged": 12,
  "reverted_attempts": 9,        // §5.1  penalty did not decrease
  "test_failures": 4,            // §5.1  broke the tests
  "build_failures": 2,
  "merge_conflicts": 3,          // §5.4  rebase needed manual resolution
  "ff_merge_races": 11,          // §4.3.2 integration retries
  "uncommitted_invocations": 0,
  "scope_violations": 0,         // committed outside target_subdir
  "stagnation_exit": true,       // §5.1
  "hard_timeout_kills": 2,       // §5.4  30-minute kills
  "stuck_terminations": 1,       // §5.4  orchestrator's verdict
  "issues_skipped": 5,           // §5.4  agents gave up
  "agent_crashes": 1,            // §5.4  CLI exited non-zero unkilled
  "analyst_phantom_issues": 2,   // §6.2  finding named a non-existent file
  "by_outcome": { "...": 0 }
}
```

`analyst_phantom_issues` counts §6.2's failure mode: "analysts occasionally
hardcoded the example format from their prompt as an actual issue in the
backlog, reporting non-existent problems." The paper's mitigation is a
file-existence check where findings are accepted, which is implemented —
the analyst prompt still carries the example line the thesis blames
(`./src/factory.cc:159`), so without the check that phantom issue would be
assigned to a programmer. A rejected finding is dropped and counted rather
than silently discarded. The reported path is validated against both the
target subtree and the repository root, and is stored exactly as the
analyst wrote it: programmers act on it inside their own worktrees, so
rewriting it to an absolute path here would aim them at the wrong tree.

`failures.merged` and the top-level `merges` count the same events by
independent routes — the former from the gate's own log, the latter from
the `RESULT:` lines the programmer reported and the coordinator acted on.
They should agree; a gap means an agent merged without reporting it, so
the penalty history is missing a point.

An *attempt* is one full evaluation pass (rebase → penalty → build →
test → merge), not one gate invocation. The two differ only when the
gate loses the fast-forward race and retries, which §4.3.2 defines as
re-entering the evaluation stage — a genuinely separate attempt. Every
gate exit point sets an explicit `outcome` field, so these counts never
depend on parsing the human-readable `reason`.

Logging is strictly observational: if writing the attempt log fails, the
gate still returns its verdict, so a disk problem cannot turn an accepted
refactoring into a rejected one. The counts are only as durable as an
append-only file: a crash mid-write can lose the truncated line and the
next record appended after it, but nothing earlier.

Agent logs get one file per **dispatch** (`PROG_1_001.log`,
`PROG_1_002.log`, …) rather than one per agent. The runner opens each log
with `"w"`, so a single file per agent would have left only the last
dispatch, and opening for append instead would make the programmer
re-read an earlier dispatch's `RESULT:` lines and post them as fresh
merges.

`<work-root>/results/<run-id>/` also holds that run's `backlog.json`,
`run_state.json`, external gate configs and logs. Per-agent worktrees are
under `<work-root>/worktrees/<run-id>/`, so concurrent or consecutive runs
cannot share issue state or worktree paths.

`run_state.json` is the crash-recovery file. `--resume` preserves the
existing integration branch and penalty history, restores the original
baseline penalty, stagnation counter and merge count, resumes log
numbering, returns abandoned `IN_PROGRESS` issues to `TODO`, and then
re-measures the current branch. Keeping the original baseline and history
makes the improvement figure comparable across the whole run.
Resume also verifies the provider/models and the token-optimization
fingerprint (target, language, thresholds, weights, exclusions and compact
view/page sizes), so stale reviewed-lead keys cannot silently hide candidates
after a configuration change.

Plotting needs `matplotlib`; if it is missing the run still completes
and logs that the plot was skipped.

---

## Key design choices

- **Single-writer protocol.** Only the coordination layer mutates
  shared state (backlog, penalty, stagnation, history, futures dicts).
  Agent threads communicate by posting messages and never wait.
- **Atomic persistence.** The backlog, penalty history and run state
  all write through `tempfile.mkstemp` + `os.replace`, so a crash never
  leaves partial JSON.
- **Worktree isolation per agent.** Each programmer commits on its own
  feature branch and only touches the integration branch via
  fast-forward through the merge gate; never via merge commits. Each
  analyst sits on a detached HEAD reset to it before every run.
- **The primary branch is read-only.** Nothing in a run advances `main`,
  which is what makes repeated runs independent without a reset.
- **Hot-path discipline.** The coordinator skips the orchestrator LLM
  call entirely on ticks where no useful assignment could result, so
  the per-tick cost is dominated by `queue.empty()` checks.
- **Gate owns the baseline, programmer owns the commit.** The gate
  re-measures the integration branch's penalty on every run, avoiding
  stale-baseline races when programmers merge concurrently; the
  programmer commits its own work, and the gate refuses to run on a
  dirty worktree rather than silently committing on its behalf.
- **Gate results are cross-checked.** A `RESULT` is accepted only for an
  issue currently assigned to that programmer and only when the
  append-only gate log has a matching successful issue id and penalties.
- **Analysis fails closed.** A configured static-analysis tool that is
  missing, times out, exits unsuccessfully without valid output, or
  produces unparseable output records `analysis_failed`; it cannot
  silently lower the measured penalty.
- **Machine-readable tool output.** Lizard is read via `--csv` and Duplo
  via its summary counters, so no number the thesis reports depends on
  scraping a human-formatted table.
- **Bounded race recovery.** On ff-merge contention the gate retries up
  to 3 full rebase-build-test cycles, then surfaces the failure rather
  than recursing.
- **Backlog deduplication.** New issues are matched against existing
  backlog and completed items by `(file_path, line, issue_type)`.

---

## Known deviations and judgment calls

| Item | Status |
|------|--------|
| Agent runtime | Claude Code CLI instead of KIRO CLI — the intended deviation. `subscription` is the default and covers all roles; explicit `anthropic`/`deepseek` modes retain API transports |
| System-prompt delivery | `--append-system-prompt`, **decided by the author**. §4.3.2 writes "the full system prompt to the process's standard input"; the CLI's `--system-prompt` would replace Claude Code's built-in prompt wholesale and match that wording more literally, but it also strips the built-in tool-use guidance and risks degrading agent behaviour. Appending layers the role prompt on top instead, so agents additionally carry the harness defaults |
| Model | Subscription and Anthropic modes default to `claude-opus-5`; optional DeepSeek mode uses `deepseek-v4-pro` for the orchestrator and `deepseek-v4-pro[1m]` for every Claude Code role |
| Stuck-eval trigger | The thesis states a ten-minute *per-issue* limit but gives no explicit stuck-eval threshold; `issue_timeout_sec` reuses that 10 minutes |
| Excluded directories | §4.3.2 excludes "test directories" without enumerating them, so the list is a **configurable parameter** (`--exclude-dirs`). The default covers names that are test artifacts by convention: `test`, `tests`, `testing`, `unittest(s)`, `unit_test(s)`, `gtest`, `googletest`, `gmock`, `mocks`. Set it to match the target repository |
| Duplication backend | MongoDB Query retains Duplo `-ml 4 -ip`; FerretDB uses the Go-AST-aware `mibk/dupl -t 100`. The penalty formula and production-file exclusions are identical; only the language-specific detector changes |
| Duplo output format | §4.2.2 mentions Duplo's JSON output; the summary counters are parsed instead, because the JSON carries no line totals and the two modes are mutually exclusive. See "Quality metrics and penalty" |
| Auto-push of the run branch | Implemented, but **opt-in** via `--push-run-branch`. §4.4 pushes the branch at the end of every run; it is off by default because it writes to the target repository's remote |
| `z_m` selector | Eq 4.3 defines `z_m ∈ {0,1}`; `config.weights` accepts any real value. A superset — using 0/1 reproduces the thesis exactly, and §4.4's three experiments only need 0/1 |
| Analyst worktrees | §4.3.2 creates one worktree per *programmer*; analysts additionally get a detached worktree. Without it an analyst would read the repository root while the gate is mid-merge, breaking §4.3.2's requirement that it "only reports issues that exist in the accepted version of the code" |
| Backlog `SKIPPED` status | §4.3.2 lists `TODO`/`IN_PROGRESS`/`DONE` and describes skipped issues as removed. A fourth `SKIPPED` status is kept instead so the deduplication key still matches and an analyst cannot re-report the same infeasible issue forever |
| Single-agent system | **Deliberately out of scope**, per the author. This repository ports the multi-agent system of §4.3.2 only. The thesis runs every experiment on both systems, so the cross-system comparisons (Table 5.1 single-vs-multi, §5.2.1/5.2.2, §5.3.1/5.3.2) fall outside this repository's remit. This is a scope decision, not a missing feature — do not implement it |

---

## Configuration (`config.py`)

| Field | Default | Purpose |
|-------|---------|---------|
| `production_profile` | `""` | Locked `ferretdb` or `mongodb-query` scope/toolchain selected by `--profile` |
| `repo_root` | required | Target git repository |
| `target_subdir` | `.` | Subtree measured and refactored |
| `sparse_worktrees` | `True` | Sparse-checkout `target_subdir`; set false with `--full-worktree` when repository-native tests need other directories |
| `work_root` | required | Hosts run-scoped worktrees and results; each results directory also holds backlog/state/configs/logs |
| `num_analysts`, `num_programmers` | 3, 3 | Pool sizes |
| `orchestrator_backlog_top_k` | 24 | Highest-impact TODO records sent to the Orchestrator; all IN_PROGRESS remain visible |
| `analyst_lead_page_size` | 15 | Maximum non-authoritative Lizard leads per Analyst dispatch |
| `max_run_input_tokens` / `max_run_output_tokens` | 0 / 0 | Reported-usage dispatch ceilings; 0 disables them and in-flight agents are never killed |
| `max_run_cost_usd` | 0 | API modes only. Subscription mode rejects billed-cost ceilings; use token ceilings |
| `max_run_cost_cny` | DeepSeek: 300; otherwise 0 | Native RMB dispatch ceiling using official DeepSeek prices; in-flight agents finish |
| `dupl_threshold_tokens` | 100 | FerretDB's mibk/dupl minimum Go syntax-token clone size |
| `orchestrator_no_progress_limit` | 3 | Consecutive unusable assignment responses before `orchestrator_no_progress`; prevents token-burning retry loops |
| `claude_cli` | `claude` | Path / name of the Claude Code CLI |
| `api_provider` | `subscription` | `subscription`, `anthropic`, `openrouter`, or `deepseek`; subscription routes every role through Claude Code OAuth |
| `api_base_url` | provider default | API modes only; subscription mode rejects overrides |
| `api_key_env` | provider default | API modes only; subscription children explicitly remove API credential variables |
| `subscription_type` | detected | Claude Code auth preflight result recorded without account identity |
| `claude_cli_version` | detected | Pinned into the run fingerprint for resume safety |
| `orchestrator_model` / `agent_model` | provider-specific | Subscription/Anthropic: `claude-opus-5`; OpenRouter: `anthropic/claude-opus-5`; DeepSeek: `deepseek-v4-pro` / `deepseek-v4-pro[1m]` |
| `deepseek_effort` | `max` | DeepSeek thinking effort for Analyst/Programmer Claude Code sessions only; the Orchestrator runs without thinking |
| `min_merge_gain` | 10.0 | Stagnation threshold (penalty units) |
| `stagnation_limit` | 3 | Consecutive low-gain merges before stop |
| `programmer_timeout_sec` | 1800 | Hard kill timeout per programmer |
| `analyst_timeout_sec` | 900 | Hard kill timeout per Analyst; releases any reserved local leads so an unattended run cannot wait forever |
| `issue_timeout_sec` | 600 | Soft threshold that triggers orchestrator stuck-eval |
| `stuck_eval_interval_sec` | 300 | Minimum gap between stuck-evals of one agent |
| `empty_scan_limit` | 3 | Empty analyst scans before `no_actionable_work` |
| `max_gate_attempts_per_issue` | 3 | Maximum gate invocations for one issue in one Programmer dispatch; enforced by the gate CLI before build/test |
| `max_issue_dispatches` | 3 | Run-wide maximum Programmer dispatches for one issue; exhausted issues are deterministically marked `SKIPPED` |
| `gate_allowed_untracked_paths` | `()` | Exact repository-relative generated paths ignored by the clean-worktree guard; tracked modifications are never ignored |
| `serialize_merge_gate` | `False` | Run at most one complete merge gate at a time; enabled by the MongoDB profile to bound Bazel memory use |
| `repo_allowed_untracked_paths` | `()` | Exact pre-existing untracked paths allowed in the source checkout; production profiles keep this list repository-specific and tracked changes always fail |
| `thresholds` | `{ccn:15, cognitive:15, nloc:30, param:5, duplicates:0}` | Penalty thresholds (§4.5.1) |
| `weights` | `{ccn:1, nloc:1, cognitive:1, param:1, duplicates:1}` | Per-metric weights (0 disables) |
| `duplo_binary` | `""` | Path to the Duplo binary (empty disables duplicates) |
| `duplo_min_block_lines` | 4 | Duplo `-ml` (its own default). **Changes measured penalty** — calibrate against Table 4.1 |
| `dupl_binary` | `""` | Path to mibk/dupl, used only when `lizard_language=go` |
| `dupl_threshold_tokens` | 100 | mibk/dupl `-t`; **changes measured penalty** |
| `exclude_dirs` | test-dir names | Directories excluded from every measurement (§4.3.2). **Changes measured penalty** |
| `lizard_binary` | `lizard` | Lizard CLI on PATH |
| `gocognit_binary` | `gocognit` | Go cognitive-complexity CLI on PATH; missing/invalid output fails the gate closed |
| `lizard_language` | `cpp` | `cpp` / `go` / `java` / `python`. C/C++ uses `modified_cognitive_complexity`; Go uses `gocognit`; other languages currently have an empty cognitive population |
| `build_cmd` / `test_cmd` | empty (startup error) | A production profile supplies real native commands; custom runs must explicitly supply both. Empty, `true`, and `:` fail closed |
| `main_branch` | `main` | Fallback branch name used when resolving a custom run's default baseline |
| `baseline_ref` | `""` | Ref defining the run's starting state. Empty → `origin/<main>`, else `<main>` |
| `run_branch_prefix` | `refactor` | Integration branch is `<prefix>/<run_id>` — this is what the gate fast-forwards |
| `push_run_branch` | `False` | Push the run branch to `origin` at the end (§4.4) |
| `run_id` | timestamp | Names the results directory **and** the integration branch |
| `resume` | `False` | Continue the existing run branch, backlog, state, history and log sequence |
| `log_dir` | `logs` | Agent-log subdirectory **inside** the run's results directory (§4.4) |
| `gate_attempts_filename` | `gate_attempts.jsonl` | Per-attempt gate log the failure counts are tallied from |
| `token_usage_filename` | `token_usage.json` | Rebuilt detailed token accounting |
| `orchestrator_usage_filename` | `orchestrator_usage.jsonl` | Append-only CLI/API usage events |

---

## Setup and running

Python 3.13+ is required (`modified_cognitive_complexity` enforces it).

```bash
# 1. Virtualenv + Python deps
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Go cognitive complexity (fixed version used by this project)
GOBIN="$PWD/.venv/bin" go install github.com/uudashr/gocognit/cmd/gocognit@v1.2.1

# Go duplicate detector used by the FerretDB profile (fixed version)
GOBIN="$PWD/.venv/bin" go install github.com/mibk/dupl@v1.1.0

# 2. Duplo binary (download the matching release for your OS)
mkdir -p bin
curl -sL https://github.com/dlidstrom/Duplo/releases/latest/download/duplo-macos.zip \
  -o /tmp/duplo.zip
unzip -o /tmp/duplo.zip -d bin/
chmod +x bin/duplo

# 3. Verify the installation (no API key or target repo needed)
python tests/run_all.py

# Optional full fake-CLI test against a temporary clone of local FerretDB
python tests/test_fake_cli_e2e.py --repo ../../ferret-dev/FerretDB

# Same flow plus DeepSeek endpoint/key/model child-environment mapping
python tests/test_fake_cli_e2e.py --provider deepseek \
    --repo ../../ferret-dev/FerretDB

# Optional real-repository smoke tests (fake CLI, no paid API).
# MongoDB is strictly limited to src/mongo/db/query/bson; FerretDB is
# limited to internal/util/telemetry. Both use focused native tests.
./.venv/bin/python tests/test_real_repo_fake_cli_e2e.py
# Or run one:
./.venv/bin/python tests/test_real_repo_fake_cli_e2e.py \
    --scenario mongo_query_multikey
./.venv/bin/python tests/test_real_repo_fake_cli_e2e.py \
    --scenario ferret_telemetry

# 4. Authenticate Claude Code with Claude.ai Pro/Max (one-time)
claude auth login
claude auth status --json   # must show loggedIn=true and subscriptionType

# 5a. FerretDB: full repository, real Go build + short unit-test suite
./.venv/bin/python main.py --profile ferretdb

# 5b. MongoDB: edits/metrics restricted to src/mongo/db/query,
#     real Bazel build + tests restricted to //src/mongo/db/query/...
./.venv/bin/python main.py --profile mongodb-query
```

These two commands are the production entrypoints for the local checkouts at
`../../ferret-dev/FerretDB` and `../../dev/mongo`. A profile locks language,
edit/measurement scope, full-worktree mode, build, test, and tool paths so an
unattended run cannot silently fall back to a smoke-test command. `--repo` and
`--work-root` may still relocate the checkout/results; scope and validation
cannot be overridden while a profile is active. FerretDB runs
`go test -run=^$ -race -tags=ferretdb_dev ./...` before
`go test -short -count=1 -shuffle=on -timeout=35m -race -tags=ferretdb_dev ./...`.
MongoDB runs Bazel `build` and `test` over `//src/mongo/db/query/...` only.
Its Programmer model sessions remain parallel, but their complete merge gates
are serialized with a run-scoped advisory lock. Each worktree keeps its own
Bazel server/output base while all gates continue sharing the profile's
`--disk_cache`, so completed compilation actions remain reusable without
running multiple memory-heavy link waves concurrently.

The subscription preflight fails before creating run state unless Claude Code
reports Claude.ai OAuth plus a subscription type. Every child environment
removes `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, and
`ANTHROPIC_BASE_URL`, preventing an ambient Console credential from silently
changing the billing surface. Subscription workers use `--safe-mode` rather
than `--bare`: in Claude Code 2.1.209, `--bare` deliberately refuses OAuth and
keychain credentials. Safe mode still disables CLAUDE.md discovery, plugins,
hooks, MCP servers, custom agents, and other project/user customizations.

Claude Code `-p` is subscription-authenticated rather than billed through the
Console API when Claude.ai OAuth is active and no API credential overrides it.
As of 2026-08-27, Anthropic has paused its previously announced separate Agent
SDK monthly-credit change: Agent SDK and `claude -p` usage still draw from the
signed-in subscription's usage limits. See Anthropic's current
[Claude-plan Agent SDK notice](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan).

If the shared subscription limit is reached, new dispatches close, in-flight
workers finish, and the run stops as `subscription_quota_exhausted`. Resume the
same run with `--resume` after the allocation resets. This event is not counted
as an agent crash.

A run created by the current code also stores a transport-neutral optimization
fingerprint. After a safe subscription pause (`subscription_quota_exhausted` or
an explicit `wall_timeout`), that run may be resumed through OpenRouter without
changing its repository scope, metrics, validation commands, budgets, or model
family:

```bash
cd writing/experiment
source ./activate_project.sh
export OPENROUTER_API_KEY=...
python main.py --profile mongodb-query --provider openrouter \
    --run-id <existing-run-id> --resume
```

OpenRouter defaults to its native Anthropic-compatible endpoint
`https://openrouter.ai/api` and the exact equivalent model slug
`anthropic/claude-opus-5`. The key is privately mapped to
`ANTHROPIC_AUTH_TOKEN` for Claude Code children, while `ANTHROPIC_API_KEY` is
explicitly blanked to prevent cached/ambient Anthropic API authentication from
winning. The transition and both model IDs are persisted in `run_state.json`
and `run_summary.json`. Switching after a completed run, using a different
model, changing core experiment settings, or switching any other provider pair
is rejected.

Before any agent is dispatched, the coordinator runs both locked validation
commands on the production baseline and fails closed if either command fails.
FerretDB validation first runs `go generate ./build/version`, as its version
package tests require the repository's ignored version/commit/branch files.

The source checkout may start on a branch or detached HEAD. Its exact ref and
commit are persisted in `run_state.json` and restored at shutdown; the
`refactor/<run_id>` integration branch remains as the run record. Startup
refuses tracked changes and unknown untracked files. The MongoDB profile only
allows the already-present `.venv`, `experiment_logs`, and
`MODULE.bazel.lock`; the gate separately allows Bazel to generate the lockfile.

Performance benchmarks are deliberately not part of the current production
gate. The deferred before/after measurement design is recorded in
[DYNAMIC_BENCHMARK_EXTENSION_PLAN.md](DYNAMIC_BENCHMARK_EXTENSION_PLAN.md);
until that plan is explicitly activated, the profiles retain only the current
repository-native functional build and correctness tests.

As an alternative to exporting the key, copy `.env.example` to `.env` and
fill in the selected provider:

```bash
cp .env.example .env
# Edit .env, then run main.py normally; no `source .env` is needed.
```

`main.py` automatically reads the project-root `.env` before parsing the
run configuration. Existing shell/CI environment variables take precedence.
The real `.env` and `.env.*` files are ignored by Git, while
`.env.example` remains tracked. Secret values are never copied into argv,
`Config`, gate configuration, or result artefacts.

The real-repository smoke test clones the exact local HEADs from
`../../dev/mongo` and `../../ferret-dev/FerretDB` into a temporary directory,
removes the temporary clone's remote, runs one deterministic helper
extraction through the normal coordinator and merge gate, and then checks
that the source checkouts are unchanged. It uses full worktrees only because
the native build graph crosses the analysis subtree. MongoDB builds
`//src/mongo/db/query/bson:multikey_dotted_path_support` and runs only the
`ExtractAllElementsAlongPath` suite; FerretDB runs only telemetry package
compilation and `TestState`. A cold MongoDB Bazel run can be lengthy and may
need dependency/cache network access. This smoke test is intentionally not
part of `tests/run_all.py`.

The paid DeepSeek micro-smoke has a separate hard boundary: it accepts only
the named function, dispatches at most one Analyst and one Programmer, never
pushes, and counts the native commands. Each repository gate compiles one
focused test binary and runs two focused test cases:

```bash
python tests/test_real_repo_deepseek_smoke.py \
    --env-file .env \
    --scenario ferret
```

Use `--scenario mongo` for the MongoDB query/bson target. If a successful
DeepSeek Analyst result explicitly confirms the sole local lead but omits the
required `ISSUE:` line, it may be reused without paying for the same analysis
again:

```bash
python tests/test_real_repo_deepseek_smoke.py \
    --env-file .env \
    --scenario ferret \
    --analyst-evidence /path/to/ANALYST_1_001.log
```

That recovery is intentionally narrow: the log must have a successful terminal
event, exactly one local lead must be in scope, and the response must name the
function and explicitly confirm the lead. The new run records the provenance in
`analyst_recovery.json`.

For an unattended multi-issue validation, one command runs the fixed MongoDB
query/bson issue followed by two sequential FerretDB telemetry issues:

```bash
python tests/test_real_repo_deepseek_three_issue.py \
    --env-file .env
```

This route does not import logs or require a manually prepared backlog. It
allows at most two automatic Analyst attempts and two Programmer dispatches
per issue, rejects findings outside the three named functions, and writes an
aggregate `three_issue_summary.json` beside the normal per-repository result
directories. Once the third fixed issue is merged, the harness preserves its
bounded discovery-exhausted state and requires the normal
`no_actionable_work` stop reason; it does not spend additional orchestrator
calls probing for a fourth issue.

The coordinator also reconciles a successful merge directly from the durable
`gate_attempts.jsonl` record before reaping or timing out a Programmer. This
prevents a completed merge from being lost if the gate exits just before the
agent's final `RESULT:` message reaches the queue. Termination is idempotent,
and a recovered merge is not counted as a timeout or returned to TODO.

MongoDB's pinned Bazel starts a local gRPC/Netty server even for this focused
test. Run the smoke command in an environment that permits loopback listeners;
a managed sandbox that rejects `bind(127.0.0.1, 0)` will fail before any C++
build begins. This is an execution permission requirement, not a request to
edit the target checkout. The harness still uses a unique output base, removes
the temporary clone's remote, and verifies that the source checkout is
unchanged.

DeepSeek uses its official Anthropic-compatible API for both the SDK
orchestrator and the existing Claude Code CLI harness:

```bash
export DEEPSEEK_API_KEY=...
./.venv/bin/python main.py --profile ferretdb --provider deepseek
```

With `--provider deepseek`, the coordinator privately maps the key and
`https://api.deepseek.com/anthropic` into each CLI subprocess environment.
It does not mutate the parent shell and never puts the key in argv,
`run_summary.json`, or a gate config. Defaults are
`deepseek-v4-pro` for the direct orchestrator call and
`deepseek-v4-pro[1m]` for analyst/programmer Claude Code sessions and all Claude Code subagents, with
agent thinking effort `max`. The direct Orchestrator call explicitly disables thinking
and forces a schema-backed `submit_assignment` or
`submit_stuck_evaluation` tool result.

Provider and role overrides:

```bash
# Read the secret from a differently named environment variable.
export MY_DEEPSEEK_KEY=...
python main.py ... --provider deepseek --api-key-env MY_DEEPSEEK_KEY

# Custom compatible endpoint and per-role models.
python main.py ... --provider deepseek \
    --api-base-url https://example.invalid/anthropic \
    --orchestrator-model deepseek-v4-pro \
    --agent-model 'deepseek-v4-pro[1m]' \
    --deepseek-effort high
```

`--model` remains a shorthand that sets both role models; the two
role-specific flags take precedence.

For subscription runs, `token_usage.json` still records uncached, cache-write,
cache-read, output, per-model, per-role, and per-dispatch usage. Its USD value
is explicitly an **API-equivalent estimate**, not an incremental subscription
charge. Subscription mode therefore forbids `--max-run-cost-usd` and
`--max-run-cost-cny`; use input/output token ceilings when a deterministic
dispatch budget is needed.

### Reproducible cost ceiling

DeepSeek runs now default to a native RMB ceiling of ¥300. It can also be
specified explicitly:

```bash
./.venv/bin/python main.py --profile mongodb-query --provider deepseek \
    --max-run-cost-cny 300
```

Pass `--max-run-cost-cny 0` to disable it. `--max-run-cost-usd` remains
available as a mutually exclusive alternative.

The coordinator closes new dispatches when the completed-turn estimate reaches
the boundary; already-running agents finish, so a bounded overshoot is expected
and recorded in `run_summary.json`. Prices are a dated, auditable snapshot in
`coordination/model_pricing.py`, not a live web lookup, so rerunning an archived
experiment does not silently adopt a later provider price.

The 2026-08-13 snapshot uses official per-million-token prices. DeepSeek V4
Pro's native RMB rates are ¥3 cache-miss input, ¥0.025 cache-hit input, and ¥6
output; no exchange rate is involved in the ¥300 limit. The USD table retained
for cross-provider reporting is:

| Model | Uncached/cache-miss input | Cache read/hit | Output | 5m / 1h cache write |
|---|---:|---:|---:|---:|
| Claude Opus 5 | 5.00 | 0.50 | 25.00 | 6.25 / 10.00 |
| DeepSeek V4 Pro | 0.435 | 0.003625 | 0.87 | billed as cache miss |
| DeepSeek V4 Flash | 0.14 | 0.0028 | 0.28 | billed as cache miss |

Anthropic CLI-reported cost is preferred when present; missing SDK cost is
estimated from token classes. Historical DeepSeek logs place a CNY-denominated
value under the misleading `total_cost_usd`/`costUSD` field names, so the USD
ceiling always recomputes DeepSeek cost from official prices and per-model
`modelUsage`. `token_usage.json` preserves reported, estimated, and effective
cost separately. The price sources are the official
[Claude pricing page](https://platform.claude.com/docs/en/about-claude/pricing)
and [DeepSeek pricing page](https://api-docs.deepseek.com/quick_start/pricing).

`--build-cmd` / `--test-cmd` are split with `shlex`, so they take
arguments but not shell operators; wrap a pipeline in a script if you
need one. They default to `true`, which makes the gate's build and test
stages vacuous — set them for any real target, or the gate will accept
changes on the penalty check alone.

When `--subdir` is narrower than the files required by the repository's
native build or tests, add `--full-worktree`. The static-analysis penalty
and agent target remain scoped to `--subdir`; only worktree materialization
changes. The merge gate rejects commits outside that subtree. This avoids
silently testing an incomplete sparse checkout without widening the
authorized refactoring scope.

The FerretDB profile selects `--dupl-binary .venv/bin/dupl`; MongoDB Query
selects `--duplo-binary bin/duplo`. For Go, reverse clone-pair reports and
overlapping physical ranges are canonicalized before computing the ratio,
so each source line contributes at most once.

`--language` defaults to `cpp`. C/C++ cognitive complexity uses
`modified_cognitive_complexity`; `--language go` uses `gocognit -json`.
The exact production file list is passed in bounded batches, so configured
test directories and colocated `*_test.go` files are excluded consistently.
Use `--gocognit-binary /path/to/gocognit` when it is not on `PATH`.
Missing binaries, non-zero exits, and malformed JSON fail the gate closed.

Ablation and parameter sweeps:

```bash
# Disable a metric entirely (weight 0)
python main.py ... --weights '{"duplicates":0,"param":0}'

# Change thresholds
python main.py ... --thresholds '{"ccn":20,"nloc":40}'

# Vary the stopping criterion
python main.py ... --min-merge-gain 5 --stagnation-limit 5

# Match the target repository's own test layout, and calibrate Duplo
# against Table 4.1 (expect 7 blocks at a 2.41% ratio)
python main.py ... --exclude-dirs 'test,tests,third_party'
python main.py ... --duplo-min-block-lines 4

# Name the run, then resume it after a crash
python main.py ... --run-id run_baseline
python main.py ... --run-id run_baseline --resume

# The 10-run protocol of thesis 4.4. Every run branches from the same
# baseline, so no reset between runs is needed; main is never advanced.
for i in $(seq 1 10); do
    python main.py ... --run-id complete_$i --push-run-branch
done

# Pin the baseline to an explicit commit instead of origin/main
python main.py ... --baseline-ref 6f3a9c1
```
