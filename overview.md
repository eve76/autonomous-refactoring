# Implementation Overview

A reproduction reference for the multi-agent refactoring system: what every
module does, the exact formulas and their calibration anchors, the git and
concurrency protocols, and the commands that reproduce each experiment.

`README.md` covers how to run the system and why each design choice was
made. This document covers **how it is built** — read it when you need to
reproduce a result, port the system, or verify an implementation claim
against the thesis.

**Source of truth:** `../Master_Thesis 15-04-2026.pdf`. Method chapter
pp. 19–33, results pp. 34–40. Section references below (§4.3.2, Eq 4.4, …)
point there. Where code and thesis disagree, the thesis wins unless the
divergence is listed in *Deviations* at the end.

**Scope:** the multi-agent system of §4.3.2 only. The single-agent system
of §4.3.1 is deliberately not implemented (author's decision), so the
thesis's cross-system comparisons are outside this codebase.

---

## 1. Environment

| Component | Version used | Notes |
|---|---|---|
| Python | 3.13.13 | 3.13+ required; `modified_cognitive_complexity` enforces it |
| Lizard | 1.22.1 | CCN / NLOC / parameter count |
| Duplo | 2.3.10 | C/C++ duplicate-block detection; binary committed at `bin/duplo` |
| mibk/dupl | 1.1.0 | Go AST/token duplicate detection for FerretDB; installed at `.venv/bin/dupl` |
| `modified_cognitive_complexity` | git HEAD | SonarSource cognitive complexity for C/C++, Tree-sitter based |
| `gocognit` | 1.2.1 | SonarSource-style cognitive complexity for Go functions/methods |
| `anthropic` | ≥ 0.40.0 | orchestrator only |
| `matplotlib` | ≥ 3.8 | penalty plot; absent → plot skipped, run continues |

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
GOBIN="$PWD/.venv/bin" go install github.com/uudashr/gocognit/cmd/gocognit@v1.2.1
export ANTHROPIC_API_KEY=sk-...          # default Anthropic provider
./.venv/bin/python tests/run_all.py      # 10 fast suites; no API key or target repo needed
```

Alternatively, copy `.env.example` to `.env` and place
`ANTHROPIC_API_KEY` or `DEEPSEEK_API_KEY` there. `main.py` loads this file
automatically; an already exported environment variable takes precedence,
and the real `.env` is ignored by Git.

Agents run through the `claude` CLI, which authenticates separately from
`ANTHROPIC_API_KEY`. The CLI must be on `PATH` (override with
`Config.claude_cli`). Alternatively, `--provider deepseek` uses DeepSeek's
official Anthropic-compatible API for both the SDK orchestrator and the
Claude Code subprocesses; only `DEEPSEEK_API_KEY` is required.

---

## 2. Repository map

```
config.py                      every tunable; derived paths as properties
main.py                        argv -> Config; no logic beyond parsing

coordination/                  owns all shared state, runs on the main thread
  coordinator.py               the run loop: drain -> reap -> dispatch -> stuck-check
  backlog.py                   product backlog, atomic persistence, dedup
  message_queue.py             typed agent -> coordinator messages
  git_manager.py               integration branch, worktrees, baseline restore
  stagnation.py                stopping criterion
  penalty_history.py           penalty time series + plot
  gate_attempts.py             per-attempt gate log -> failure counts
  run_state.py                 crash-recovery state
  token_usage.py               provider-neutral token accounting

agents/
  orchestrator.py              two synchronous LLM decision points (SDK)
  provider.py                  Anthropic/DeepSeek client + child environment
  analyst.py                   claude-CLI session; emits ISSUE: lines
  programmer.py                claude-CLI session; refactors, invokes the gate
  agent_runner.py              subprocess spawn + stream-json log streamer
  log_parser.py                stream-json -> assistant text

analysis/
  penalty.py                   Eq 4.4 / Eq 4.5, breakdown, impact estimation
  metrics.py                   distribution statistics (Tables 4.1 / 5.1)
  candidates.py                conservative CCN/NLOC/param Analyst leads
  tools.py                     Lizard / cognitive / Duplo wrappers, file selection

merge_gate/
  gate.py                      rebase -> penalty -> build -> test -> ff-merge
  cli.py                       entry point programmers invoke from their worktree

prompts/                       orchestrator_assignment, orchestrator_stuck,
                               analyst, programmer
```

Two boundaries carry the design:

- **`coordination/` is the only writer of shared state.** Agents never
  mutate the backlog; they post messages and continue.
- **`merge_gate/` runs as a separate process** in the programmer's
  worktree, configured entirely through a JSON file. It therefore cannot
  reach the message queue, which is why it logs attempts to a file
  (§9).

---

## 3. Objective function

`analysis/penalty.py`. Per-function metrics use the hyperbolic penalty of
**Eq 4.4**; the duplicate-line ratio uses the saturation function of
**Eq 4.5**:

```
p_m(x)   = 100 · (1 − T_m / max(T_m, x))       zero at or below T_m
p_dup(r) = 100 · r / (r + k),   k = 0.1        no threshold
```

Thresholds (§4.5.1, adopted from the literature, not derived from the
codebase): `T_CCN = 15`, `T_LLOC = 30`, `T_Cog = 15`, `T_Param = 5`.

Total penalty = weighted sum of every function's per-metric penalty, plus
the duplicate-ratio penalty. Two implementation details matter:

- **Lizard records and cognitive records are kept as two separate
  lists**, not joined. A function the cognitive tool sees but Lizard does
  not still contributes its cognitive penalty. Joining them would silently
  drop penalty.
- **A weight of 0 removes a metric entirely.** This is the mechanism for
  `z_m` in Eq 4.3, and therefore how both ablation studies are configured.

### Calibration anchors

Any change to the penalty math must still reproduce these. All four are
published in the thesis and are checked by `tests/test_metric_stats.py`.

| Anchor | Thesis | Code |
|---|---|---|
| Eq 4.5 at r = 2.41 % | 19.4 | 19.42 |
| Eq 4.4, cognitive {19, 16} | 27.3 | 27.30 |
| Baseline split: LLOC 811.2 · CCN 292.7 · Cog 27.3 · Dup 19.4 · Param 16.7 | total 1167.28 | sums to 1167.3 |
| Table 4.1 cognitive row: mean 1.32 / median 0 / max 19 | — | reproduced from a synthetic population |

> **The duplicate ratio is a fraction, not a percentage.** `p_dup(0.0241)`
> = 19.4 as published; `p_dup(2.41)` = 96.02. This single unit confusion
> silently inflates the duplicate penalty ~5× and is the easiest way to
> fail reproduction.

### Backlog impact estimation

`estimate_reduction_from_message()` parses metric values out of an
analyst's free-text message and estimates the penalty a fix would remove,
assuming the programmer brings each metric down to exactly its threshold.
It accepts both `CCN=27` and `161 NLOC` because the analyst prompt's own
example mixes the two styles. The result labels each issue `high` impact
when it clears `min_merge_gain`, else `low` — that label is what the
orchestrator prioritises on.

---

## 4. Measurement pipeline

`analysis/tools.py`. `run_static_analysis()` returns
`(lizard_records, cognitive_records, DuplicationResult)`. All three tools
receive **the same file list**, so the duplication ratio can never be
measured over a different population than the per-function metrics.

**Lizard** — `lizard -l <lang> --csv -f <filelist>`, CSV columns
`nloc, ccn, token, param, length, location, file, name, long_name,
start_line, end_line`.

> `--csv` is load-bearing. Lizard's default tabular output **reprints every
> function that trips its own warning threshold (CCN > 15) in a trailing
> `!!!! Warnings !!!!` section.** Parsing that output double-counts
> precisely the functions that carry penalty, inflating the total, the
> per-metric split, backlog impact estimates, and every distribution
> statistic. Regression-guarded in `test_metric_stats.py` §7.

**Cognitive complexity** — C/C++ calls
`cognitive_complexity_for_file()` per file. Go calls `gocognit -json`
with the exact filtered production file list in bounded argument batches.
Both produce the same independent `{file, line, name, cognitive}` record
population consumed by the penalty function. Missing tools, non-zero exits,
and malformed JSON fail closed. Other languages currently produce an empty
cognitive population.

**Duplo** — `duplo -ml <n> -ip - -`, file list on stdin, output parsed
from the text summary:

```
Lines of code: N
Duplicate lines of code: N
Total N duplicate block(s) found.
```

Three non-obvious constraints:

- **Absolute paths are mandatory.** Duplo silently reports zero lines for
  relative paths, so `run_duplo` resolves every path.
- **The text summary is parsed rather than `-json`.** §4.2.2 mentions
  Duplo's JSON output, but that output carries only the block list — no
  line totals — so the duplicate *ratio* cannot be derived from it. Duplo's
  own `Lines of code` is a post-filter count (it drops preprocessor
  directives under `-ip` and short lines under `-mc`) that cannot be
  reconstructed externally, and `-json` suppresses the summary entirely.
  The two modes are mutually exclusive; only the summary reproduces the
  2.41 % → 19.4 calibration.
- **`-ml` defaults to 4, Duplo's own default.** The thesis never specifies
  a minimum block size, so the tool default is the reconstruction that
  assumes least. Raising it suppresses short duplicate blocks and lowers
  both the ratio and the block count. `-ip` is set because §4.2.2 requires
  preprocessor filtering; every other Duplo knob is left alone.

**mibk/dupl (Go)** — `dupl -files -plumbing -t 100`. The same filtered
production Go file list is supplied on stdin. Reverse pair reports are
canonicalized and overlapping physical ranges are merged per file; the
denominator is physical lines in that exact file list. This keeps the ratio
bounded while preserving the existing `DuplicationResult` and Eq. 4.5.

### File selection and exclusion

§4.3.2 excludes "test directories" without enumerating them, so the list
is a parameter (`--exclude-dirs`). Default:

```
test  tests  testing  unittest  unittests  unit_test  unit_tests
gtest  googletest  gmock  mocks
```

Matching is on **whole path components**, case-insensitively, and prunes
`os.walk` in place — so `Testing/` is excluded while `latest/` is not.
`None` means "use the default"; an explicitly empty list means "exclude
nothing" and must not collapse to the default.

Matching only a directory named exactly `test` was too narrow to be a safe
default: with a repository laid out as `tests/`, the analyst files issues
against test files that the programmer prompt forbids touching — work that
can only ever be skipped.

Language → suffix map: `cpp` → `.c .cc .cpp .cxx .h .hpp .hh .hxx`;
`go` → `.go`; `java` → `.java`; `python` → `.py`.

---

## 5. Distribution statistics

`analysis/metrics.py`. The penalty is the agents' optimisation signal, but
the thesis *reports* quality through each metric's distribution (§4.2.3,
Tables 4.1 and 5.1). Per metric: threshold, population size, mean, median,
p90, p95, p99, max, count over threshold, and a `cleared` flag.

- **Statistics cover all functions, not only violating ones.** Table 4.1's
  mean CCN of 3.21 against a threshold of 15 is only meaningful over the
  whole population.
- **Percentiles use linear interpolation between closest ranks** (numpy's
  default). The method string is written into `run_summary.json` as
  `metrics.percentile_method` so the thesis can cite it rather than leave
  it an implicit convention.
- `cleared` = no function above the threshold (§4.5.1). For duplicates,
  `cleared` = zero duplicate blocks.
- Duplicates carry both `line_ratio` (fraction) and `line_ratio_pct`,
  plus `duplicate_lines`, `total_lines`, `block_count` — Table 4.1 reports
  the block count next to the ratio.

`mean_change_pct()` produces Table 5.1's parenthesised percentage change,
using the line ratio for duplicates since they have no per-function mean.

---

## 6. Concurrency and state model

Per §4.3.2: one orchestrator, three analysts, three programmers. The
coordination layer runs on the main thread; agents run in two
`ThreadPoolExecutor`s (one per role, sized to the pool count).

**Single-writer protocol.** Agents post `Message(sender, kind, payload)`
onto a `queue.Queue` and continue without waiting. The coordinator drains
the queue in one loop and is the only writer of the backlog, the penalty
history, and the run state. Reads by agents go through
`BacklogStore.snapshot()`, which returns a deep copy.

Message kinds: `add_issues`, `mark_done`, `mark_skipped`, `merge_result`,
`programmer_finished`, `analyst_finished`, `programmer_heartbeat`.

**Main loop** (`Coordinator._main_loop`), one tick per
`backlog_drain_interval_sec` (1.0 s):

```
drain queue  ->  reap finished futures  ->  dispatch if needed  ->  check stuck agents
```

`_dispatch_if_needed` skips the orchestrator LLM call entirely when
nothing could usefully be assigned — idle programmers with no TODO items
and no reason to scan. This is the per-tick hot path.

**Atomic persistence.** The backlog, penalty history, and run state are
written with `mkstemp` + `os.replace`, so a crash never leaves a partial
file. The gate attempt log is the one exception and is append-only (§9).
The backlog, state, history, logs and gate configs are all scoped by
`run_id`; independent runs cannot inherit each other's issues or recovery
state.

---

## 7. Git protocol

`coordination/git_manager.py`. §4.4 requires that the codebase be reset to
its original state before each of 10 runs, and that each run record its
commits on a dedicated branch which is pushed at the end while the working
directory returns to baseline.

Both are satisfied by **never advancing the repository's primary branch**:

1. `resolve_baseline_ref` — explicit `--baseline-ref` wins; otherwise
   prefer `origin/<main>` so repeated runs pin to fetched upstream state;
   fall back to the local branch when there is no remote.
2. A fresh run calls `prepare_run_branch` — **refuses to start against a dirty repository**
   (no stashing, no discarding); best-effort `git fetch origin`; resolve
   the baseline commit; create `refactor/<run_id>` at that commit. Re-running
   the same run id without `--resume` is refused rather than overwriting its
   evidence. A resumed run calls `resume_run_branch` instead: it checks out
   the existing integration branch without moving it back to baseline.
3. `create_worktree` — one worktree per agent at
   `<work_root>/worktrees/<run_id>/<agent_id>`.
   Programmers get a branch `feature/<agent_id>`; **analysts get a detached
   HEAD**, because git refuses to check out a branch that is already
   checked out elsewhere. Sparse checkout (`init --cone` + `set <subdir>`)
   when the target is a subdirectory, so a large repository is not fully
   materialised per agent.
4. The merge gate fast-forwards the **integration branch**, never `main`.
5. Before branch creation, `capture_checkout` records the exact branch or
   detached-HEAD commit. `_finalize_run_branch` removes worktrees, optionally
   pushes, then `restore_checkout` restores that exact identity and verifies
   its SHA. These fields are persisted in `run_state.json`, so resume does not
   assume that a repository has a local `main` branch.

Consequences: consecutive runs start from an identical state with no
destructive reset; the run branch is the permanent record §4.4 asks for;
and a literal `git reset --hard origin/main` — which would destroy local
commits on the target repository's main — is never needed.

**Push is opt-in** (`--push-run-branch`). §4.4 pushes every run, but it
writes to the target repository's remote, so the run prints how to enable
it rather than doing it unasked.

**`reset_worktree` targets the local integration branch.** The gate
fast-forwards it on every accepted change and never pushes mid-run, so any
remote ref is stale for the entire run — resetting there would discard
every refactoring accepted so far.

---

## 8. The merge gate

`merge_gate/gate.py`, invoked by the programmer with the same Python
interpreter that launched the coordinator:

```
<venv-python> /absolute/path/merge_gate/cli.py \
  --config <results>/<run_id>/gate_configs/PROG_n.json \
  --issue-id ISSUE-nnnn
```

The config deliberately lives **outside** the target worktree: otherwise
`git clean -fd` deletes it, while leaving it in place makes the gate reject
the worktree as dirty. Configuration contains:
`repo_root`, `target_subdir`, `thresholds`, `weights`, `build_cmd`,
`test_cmd`, `integration_branch`, `duplo_binary`, `duplo_min_block_lines`,
`dupl_binary`, `dupl_threshold_tokens`,
`lizard_binary`, `gocognit_binary`, `lizard_language`, `exclude_dirs`, `agent_id`,
`attempt_log`.

Pipeline, following §4.3.2 ("Evaluation" and "Integration") literally:

| Step | Behaviour on failure | `outcome` |
|---|---|---|
| 0. Reject uncommitted work | the gate never stages or commits for the programmer | `uncommitted` |
| 1. Rebase onto the integration branch | **conflict left in place** — the programmer resolves it manually and re-runs | `rebase_conflict` |
| 2. Measure penalty before and after | integration-branch penalty is re-measured every attempt, so a concurrent merge cannot leave a stale baseline | — |
| 3. Penalty must decrease | **revert** to the pre-refactoring state; programmer moves on | `penalty_rejected` |
| 4. Build, then test | **no revert** — the programmer reads the output, fixes its own code, re-runs | `build_failed` / `test_failed` |
| 5. Fast-forward merge | lost race → retry the whole cycle, up to 3 times | `ff_merge_race` / `ff_race_exhausted` |
| success | — | `merged` |

The asymmetry in steps 3 and 4 is from the thesis: a penalty that did not
decrease means the attempt was pointless, so it is discarded; a broken
build or test means the work may still be salvageable, so it is left for
the programmer to fix.

**Every exit point sets an explicit `outcome` field.** Build failure and
test failure are otherwise indistinguishable on `GateResult` (both leave
`tests_passed` false), and classifying experimental data by pattern-matching
prose would be fragile.

> **The rebase and revert targets must be the local integration branch.**
> An earlier version pointed both at `origin/main` while only
> fast-forwarding the local branch and never pushing, so `origin/main` sat
> at the run's start commit forever. Two consequences: a revert rolled the
> worktree back to the *original baseline*, discarding all accepted work;
> and after the first merge, **no second concurrent merge could ever
> succeed** (rebase onto a stale ref → ff-merge fails → 3 retries →
> give up). That directly violated §4.3.2's "replaying the programmer's
> commits on top of any changes merged by other programmers in the
> meantime." Covered by `test_merge_gate.py` scenario 5.

---

## 9. Failure counting

§5.1 asks for "failure counts (reverted attempts, test failures,
stagnation exits) for each model"; §5.4 for the system deviations — merge
conflicts, crashes, agents giving up. The gate is the only component that
observes most of these, and it runs as a separate process, so it appends
one line per attempt to `<results>/gate_attempts.jsonl`:

```json
{"timestamp": 1.7e9, "agent": "PROG_1", "issue_id": "ISSUE-0001",
 "outcome": "penalty_rejected",
 "success": false, "penalty_before": 500.0, "penalty_after": 505.0, "reason": "..."}
```

Configured analysis tools fail closed: missing executables, timeouts,
unsuccessful runs without valid output, and unparseable output record
`analysis_failed`; they cannot silently become a smaller penalty and admit
a bad merge. (Duplo's documented duplicate-found exit is accepted only
when its summary counters parse successfully.) The coordinator
accepts a programmer's `RESULT` only when the issue is currently assigned
to that programmer and a matching successful gate-attempt record has the
same issue id and penalty values.

JSONL, not a JSON document, because several programmers gate concurrently
from separate processes: a single `O_APPEND` write of one short line is
atomic on POSIX, so no locking is needed.

- **An "attempt" is one full evaluation pass**, not one gate invocation.
  The two differ only when the gate loses the fast-forward race and
  retries, which §4.3.2 defines as re-entering the evaluation stage.
- **Logging is strictly observational.** Any error writing the log is
  swallowed, so a disk problem cannot turn an accepted refactoring into a
  rejected one.
- **Durability limit:** a crash mid-write can leave a line with no
  terminating newline, and the next record appended fuses onto it, so
  both are lost. Every record written earlier survives. Recovering the
  fused pair is not attempted — one lost attempt out of a run's hundreds
  does not change a per-run count, and guarding it would mean locking or a
  blank line between every record.

The coordination layer tallies that file and merges in its own counters
into `run_summary.json`:

| Key | Source | Thesis |
|---|---|---|
| `reverted_attempts` | gate log | §5.1 |
| `test_failures`, `build_failures` | gate log | §5.1 |
| `merge_conflicts` | gate log | §5.4 |
| `ff_merge_races` | gate log | §4.3.2 integration retry |
| `stagnation_exit` | `stop_reason == "stagnation"` | §5.1 |
| `hard_timeout_kills`, `stuck_terminations` | penalty history events | §5.4 |
| `issues_skipped` | backlog `SKIPPED` count | §5.4 "agents give up" |
| `agent_crashes` | non-zero exit from a session that was **not** killed | §5.4 "kiro-crashes" |
| `analyst_phantom_issues` | file-existence check | §6.2 |

`failures.merged` and the top-level `merges` count the same events by
independent routes — the gate's own log versus the `RESULT:` lines the
programmer reported. They should agree; a gap means an agent merged without
reporting it, so the penalty history is missing a point.

**The `analyst_phantom_issues` check is §6.2's mitigation:** "analysts
occasionally hardcoded the example format from their prompt as an actual
issue in the backlog, reporting non-existent problems." The analyst prompt
still carries the example the thesis blames (`./src/factory.cc:159`), so
without the check that phantom issue reaches a programmer. A finding whose
file resolves under neither the target subtree nor the repository root is
dropped and counted. The reported path is **not** rewritten to the one
that matched: programmers act on it inside their own worktrees, so storing
an absolute path from this checkout would aim them at the wrong tree.

---

## 10. Agent layer

### Orchestrator — `agents/orchestrator.py`

A Python program that makes **two synchronous LLM calls**, not a persistent
process (§4.3.2). Uses the Anthropic SDK/wire format rather than the CLI
because it needs no file editing or shell tools. The default provider is
Anthropic; DeepSeek uses `https://api.deepseek.com/anthropic`. One user
message, `max_tokens=4096`, no sampling parameters. DeepSeek Orchestrator
requests explicitly disable thinking and force a schema-backed tool result; the
configured `high`/`max` effort applies only to Analyst/Programmer Claude
Code sessions.

**Decision point 1 — task assignment.** The prompt carries current and
baseline penalty, the stagnation counter, idle programmer and analyst
lists, the per-metric breakdown ranked by improvement potential, and the
compact backlog view as JSON: all `IN_PROGRESS`, full status counts and
the highest-impact 24 TODO records. The complete backlog stays local. A
metric's "improvement potential" is exactly its
current penalty contribution, since driving every function below threshold
zeroes it. Assignment rules in the prompt, verbatim from §4.3.2: group
issues in the same file to one programmer; never assign two programmers to
the same file; prioritise high-impact issues; keep each assignment to 1–2
issues; dispatch analysts only when the backlog is empty or stagnation is
approaching.

Output is line-oriented and parsed by prefix:

```
PROG_1: ISSUE-0001, ISSUE-0003
ANALYST_1: <metric focus or directory>
REASONING: <one paragraph>
```

The coordination layer then **validates against current backlog state** —
`_collect_specs` drops any id that no longer exists or is no longer `TODO`
— before dispatching.

**Decision point 2 — stuck-agent evaluation.** Per stuck programmer:
runtime, assigned issues, whether it has made file edits, gate invocation
count, and the tail of its assistant text. The orchestrator returns
`PROG_n: terminate|keep`, optionally `INFEASIBLE: <ids>`, and `REASONING:`.
The evaluation is throttled per agent by `stuck_eval_interval_sec`, and the
timestamp is stamped **before** the call so a failure cannot cause a retry
storm.

### Analyst and programmer — `agents/agent_runner.py`

Both are `claude` CLI subprocesses. With the Anthropic provider they keep
the normal Claude Code authentication. With the DeepSeek provider, only
the child environment receives DeepSeek's documented `ANTHROPIC_BASE_URL`,
`ANTHROPIC_AUTH_TOKEN`, model and effort variables:

```
claude -p --append-system-prompt <role prompt> --permission-mode acceptEdits
       --output-format stream-json --verbose --model <model>
       --bare --no-session-persistence
       --allowedTools Bash,Edit,Write,Read,Glob,Grep
```

with the task prompt written to stdin and stdout streamed line-by-line to
a log file. Bare mode and disabled session persistence keep every dispatch
independent of project/user memory, hooks, plugins and prior sessions;
the explicit allowlist gives headless agents the Bash access needed for
git, analysis, build, test and the gate. `start_new_session=True` puts the child in its own process
group so `killpg` reaps the agent *and* any build tools or git processes it
spawned — a plain `process.kill()` would orphan them.

Two activity signals are scraped from the raw stream-json as it streams, to
feed the orchestrator's stuck evaluation: `edits_made` (an Edit/Write/
MultiEdit tool marker appeared) and `gate_invocations` (a Bash call
referencing `merge_gate/cli.py`).

**Analysts** reset their worktree to the integration branch before each
scan, so they only ever report issues that exist in the accepted version
of the code (§4.3.2). They emit one line per finding:

```
ISSUE: <file>:<line> - <severity> - <type> - <metric values>
```

**Programmers** work one issue at a time, commit their own edits, then
invoke the gate and react to its JSON verdict. They report:

```
RESULT: <ISSUE-ID> - done    - merged at penalty <before> -> <after>
RESULT: <ISSUE-ID> - skipped - <reason>
```

Results are parsed from the log after the process exits, deduplicated by
issue id and limited to the current assignment. A session killed by the coordination layer sets `aborted` and
posts nothing, so issues already returned to `TODO` are not double-reported.
If the CLI crashes or exits without resolving an assigned issue, the
coordinator returns that issue from `IN_PROGRESS` to `TODO`.

**One log file per dispatch** — `<AGENT>_<nnn>.log`. The runner opens logs
with `"w"`, so a single file per agent would leave only the last dispatch
(§4.4 asks for "the full agent logs"). Append mode is also wrong: the
result parser scans the whole file and would re-post an earlier dispatch's
`RESULT:` lines as fresh merges.

---

## 11. Product backlog

`coordination/backlog.py`. JSON on disk, ids `ISSUE-%04d`.

```python
Issue(id, file_path, line, severity, issue_type, message,
      metric_values, estimated_penalty_reduction, impact,
      status, assigned_to, skip_reason)
```

- **Dedup key: `(file_path, line, issue_type)`**, checked against every
  item including completed ones, so a re-scan cannot re-add finished work.
- **Status lifecycle:** `TODO → IN_PROGRESS → DONE`, plus `SKIPPED`.
  §4.3.2 lists only the first three and describes skipped issues as
  removed from the backlog; a fourth status is kept instead because true
  removal lets the dedup key go stale, so an analyst would re-report the
  same infeasible issue indefinitely.
- A terminated programmer's issues are returned to `TODO`.
- On `--resume`, any `IN_PROGRESS` item left by a crashed coordinator is
  likewise recovered to `TODO`.

---

## 12. Stopping criterion

`coordination/stagnation.py`, per §4.3.2 and §4.5.2.

| Trigger | Ticks the counter? |
|---|---|
| A merge whose reduction is ≤ `min_merge_gain` (10) | yes |
| A programmer killed at `programmer_timeout_sec` (30 min) | yes |
| Orchestrator discretionary termination (stuck, before 30 min) | no |
| A gate attempt reverted for not reducing penalty | no — see below |

A merge above the limit resets the counter to zero; the run stops when the
counter reaches `stagnation_limit` (3). A reduction exactly equal to
`min_merge_gain` counts as low-gain, since the thesis condition for
resetting is a strict `> 10`.

**Reverted attempts deliberately do not tick the counter.** This looks
wrong against Tables 4.5/4.6, whose caption reads "entries marked r denote
reverted attempts … counting as zero improvement toward the stagnation
counter" — a zero-improvement cycle would tick. But those tables validate
the **single-agent** system, which counts sequential cycles. §4.3.2 defines
the multi-agent counter explicitly and lists only two triggers: the
reduction "from a merge falls below 10 units", or a programmer "terminated
for exceeding its 30-minute time limit". Reverted attempts are counted and
reported (§9) but do not drive the stop condition.

**Two-tier stuck handling.** Past `issue_timeout_sec` (10 min, the
thesis's per-issue limit) the orchestrator is asked whether to terminate;
past `programmer_timeout_sec` (30 min) the coordination layer kills
unconditionally. Only the hard timeout ticks stagnation.

Two additional terminal conditions prevent an otherwise idle run from
looping forever: a measured penalty of zero yields `penalty_zero`, and
`empty_scan_limit` consecutive analyst scans that add no actionable issue
yield `no_actionable_work`.

---

## 13. Run artifacts

`<work_root>/results/<run_id>/`:

| File | Contents |
|---|---|
| `penalty_history.json` | every penalty-changing event with `elapsed_sec`, the per-metric breakdown at each merge, the gate's before/after values, the stagnation counter |
| `penalty.png` | penalty-vs-time step plot, merge and termination markers |
| `run_summary.json` | baseline/final penalty, reduction and %, merges, stop reason, baseline commit, integration branch, metrics/failures/tokens/budget/optimization summaries, backlog tallies, and the full parameter set |
| `gate_attempts.jsonl` | one line per gate attempt |
| `logs/<AGENT>_<nnn>.log` | full stream-json log, one file per agent per dispatch |
| `orchestrator_usage.jsonl` | append-only orchestrator SDK token events |
| `token_usage.json` | token/cache totals by role, agent and dispatch, plus coverage and tokens per merge |
| `gate_configs/PROG_n.json` | external gate configuration, outside git worktrees |
| `backlog.json` | run-scoped issue state |
| `run_state.json` | run-scoped crash-recovery state |

Event types in the history: `baseline`, `merge`, `timeout_kill`,
`stuck_terminate`, `stop`. The clock starts at the baseline measurement,
not at object construction, so setup time does not offset every point.

Per-agent worktrees live under
`<work_root>/worktrees/<run_id>/`. `--resume` checks out the existing run
branch without resetting it, reloads the run-scoped backlog, state,
penalty history and log numbering, recovers abandoned `IN_PROGRESS`
issues to `TODO`, and then re-measures the current branch. Keeping the
original baseline and prior history is what makes the improvement figure
comparable across the whole run.

`run_summary.json`'s `metrics` block is what Tables 4.1 and 5.1 are built
from: `percentile_method`, `baseline`, `final`, `mean_change_pct`, and the
`cleared` list.

### Token-saving variant: stages 1–3

`experiment_token_save` keeps `experiment` as its control and makes only
three conservative changes:

1. cache-aware token accounting plus optional, default-off input/output
   dispatch ceilings; already-running agents finish and overshoot is reported;
2. a Top-24 TODO Orchestrator view, while all active items and complete local
   backlog evidence are retained;
3. disjoint Top-15 local Lizard lead pages. Leads cover CCN, NLOC and
   parameters only and are explicitly non-authoritative. The Analyst still
   inspects code and is the only component that can emit the existing ISSUE
   format. Cognitive and duplicate discovery retain the original path.

No candidate database, accept/reject protocol, backlog schema change, or
merge-gate change is introduced. Successfully reviewed lead keys are saved
for resume only after a successful terminal CLI result; a crashed or truncated
Analyst page is retried. Resume rejects candidate-affecting configuration
drift, and three consecutive unusable Orchestrator assignment responses stop
the run instead of consuming tokens indefinitely.

### Locked production profiles

The prepared local repositories use fail-closed profiles rather than ad-hoc
smoke-test flags:

```bash
./.venv/bin/python main.py --profile ferretdb --provider deepseek
./.venv/bin/python main.py --profile mongodb-query --provider deepseek
```

`ferretdb` measures and authorizes edits across the complete FerretDB checkout,
then performs a race-enabled compile-only Go pass and the repository-wide
short unit-test suite. `mongodb-query` locks measurement and edits to
`src/mongo/db/query`, while full worktrees expose Bazel dependencies; build and
test targets remain `//src/mongo/db/query/...`. Profile scope, language,
worktree mode, and native commands cannot be overridden. Empty commands and
the no-op executables `true`/`:` are rejected before any run state is created.
Custom repositories remain supported, but must explicitly provide real
`--build-cmd` and `--test-cmd` values.

---

## 14. Reproducing the experiments

§4.4 runs each configuration **10 independent times**. Every run branches
from the same baseline ref, so no reset between runs is needed and the
primary branch is never advanced.

```bash
# Complete model experiment (§4.4.1) — all five metrics, z_m = 1
for i in $(seq 1 10); do
  ./.venv/bin/python main.py \
    --repo /path/to/target --subdir src --work-root ./work \
    --duplo-binary ./bin/duplo \
    --build-cmd 'make -j8' --test-cmd 'ctest --output-on-failure' \
    --run-id complete_$i --push-run-branch
done

# Ablation (§4.4.3) — remove one metric, keep the other four
for m in ccn nloc cognitive param duplicates; do
  for i in $(seq 1 10); do
    ./.venv/bin/python main.py ... --run-id abl_${m}_$i --weights "{\"$m\":0}"
  done
done

# Inverse ablation (§4.4.2) — target one metric in isolation
#   (spell out the four zeros; the targeted metric keeps its default of 1)
for i in $(seq 1 10); do
  ./.venv/bin/python main.py ... --run-id inv_ccn_$i \
    --weights '{"nloc":0,"cognitive":0,"param":0,"duplicates":0}'
done
```

`--weights` and `--thresholds` **merge into the defaults** rather than
replacing them, which is why an ablation run only names the metric it
removes. The five weight keys are `ccn`, `nloc`, `cognitive`, `param`,
`duplicates`.

> **The thesis's LLOC is the config's `nloc`.** Because both dicts merge
> into the defaults, an unknown key used to be accepted and then never
> read: `--weights '{"lloc":0}'` produced exactly the complete model's
> penalty, so the LLOC-ablation condition would have been run — and
> tabulated — as the reference condition, with nothing to indicate it.
> Unknown keys are now rejected at startup. The `weights` and
> `thresholds` actually used are also echoed in `run_summary.json`'s
> `config` block, which is the record to check when auditing a result.

Other knobs that matter for reproduction:

```bash
--exclude-dirs 'test,tests,third_party'   # match the target repo's layout
--duplo-min-block-lines 4                 # Duplo's own default
--dupl-threshold-tokens 100               # Go-only mibk/dupl default
--min-merge-gain 10 --stagnation-limit 3  # the prestudy's selection (§4.5.2)
--baseline-ref <sha>                      # pin the baseline explicitly
--resume                                  # continue after a crash
```

**Calibration step before trusting any result.** Run one baseline
measurement against the real target codebase and check it against the
published values: **7 duplicate blocks at a 2.41 % ratio** (Table 4.1),
mean CCN 3.21, and total baseline penalty 1167.28. If they disagree,
suspect `--exclude-dirs` first — it determines the file set — then
`--duplo-min-block-lines`.

---

## 15. Verification

```bash
./.venv/bin/python tests/run_all.py
```

No API key and no target codebase required: LLM calls are stubbed and git
repositories are synthesised in temporary directories.

| Suite | Covers |
|---|---|
| `test_merge_gate.py` | the gate end-to-end on a real git repo: accept, reject + revert, refuse uncommitted, concurrency |
| `test_run_isolation.py` | §4.4 per-run branch protocol, fresh-run refusal, resume without branch reset, push, baseline restore |
| `test_metric_stats.py` | distributions, Table 4.1 reproduction, Lizard/Duplo accounting, exclusion semantics, external gate config, interpreter/tool permissions |
| `test_artifacts.py` | penalty history, resumed-history append, crash-recovery state, run-scoped paths, the `metrics` block |
| `test_stuck_policy.py` | two-tier stuck policy, stagnation semantics, empty-scan termination |
| `test_failure_counts.py` | gate attempt log, concurrent appends, explicit analysis failure, failure counts, §6.2 check |
| `test_provider_config.py` | Anthropic/DeepSeek defaults, credential isolation, endpoint/model mapping, structured non-thinking Orchestrator request and CLI flags |
| `test_token_usage.py` | provider/cache normalization, partial-session recovery, append-only SDK events, detailed summaries |
| `test_go_cognitive.py` | gocognit JSON parsing, exact Go production-file scope, penalty integration, fail-closed errors |
| `test_token_optimizations.py` | Top-24 active-safe backlog view, conservative local leads, disjoint pages, terminal-result retry, resume fingerprint, queue-safe dispatch ceilings, bounded no-progress responses |
| `test_real_repo_fake_cli_e2e.py` | optional deterministic end-to-end smoke test on temporary clones of local MongoDB query/bson and FerretDB telemetry checkouts; focused native Bazel/Go tests and strict path-scope verification |

The full fake-CLI integration test uses a temporary local clone of
`../../ferret-dev/FerretDB`, adds three controlled Go quality violations,
runs the real coordinator with 3 analysts and 3 programmers, and exercises
the real Lizard, Duplo, Go build/test, concurrent rebase and merge paths:

```bash
./.venv/bin/python tests/test_fake_cli_e2e.py \
  --repo ../../ferret-dev/FerretDB

# Same full fake flow plus DeepSeek provider environment mapping:
./.venv/bin/python tests/test_fake_cli_e2e.py \
  --provider deepseek --repo ../../ferret-dev/FerretDB
```

It never changes the supplied FerretDB checkout: all fixture edits and run
branches exist only in the temporary clone.

A second optional smoke test uses the two unmodified real local repositories
and a deterministic fake CLI, without calling a paid model API:

```bash
# Both scenarios, or select one with --scenario.
./.venv/bin/python tests/test_real_repo_fake_cli_e2e.py
./.venv/bin/python tests/test_real_repo_fake_cli_e2e.py \
  --scenario mongo_query_multikey
./.venv/bin/python tests/test_real_repo_fake_cli_e2e.py \
  --scenario ferret_telemetry
```

The MongoDB scenario is deliberately restricted to
`src/mongo/db/query/bson`: only
`multikey_dotted_path_support.cpp` may change, and the native validation
builds that Bazel target and runs the `ExtractAllElementsAlongPath` suite.
The FerretDB scenario is restricted to `internal/util/telemetry` and runs
only telemetry compilation plus `TestState`. Full worktrees make
cross-directory build dependencies visible, while the merge gate rejects
committed paths outside the configured subtree. Each source HEAD is cloned
to a temporary repository with its remote removed, and its status is checked
before and after. A cold MongoDB Bazel run can be lengthy and may require
dependency/cache network access, so this test is intentionally excluded from
`run_all.py`.

Two failure modes these were written to catch, both of which had occurred:

- **Lizard double-counting** every function above CCN 15 — the numbers
  looked plausible and were uniformly inflated.
- **Tests silently measuring less than they claimed.** An earlier
  portability change used `shutil.which("lizard")`, which does not find a
  virtualenv binary that is not on `PATH`; two suites ran for a while with
  Lizard absent, exercising only cognitive complexity. The helpers now
  resolve from `Path(sys.executable).parent` first. A green suite that
  tests less than it says is worse than a red one.

---

## 16. Deviations from the thesis

Every item is either forced by the runtime, a documented technical
necessity, or an explicit author decision. `README.md` carries the full
table with reasoning; the load-bearing ones:

| Item | Status |
|---|---|
| Agent runtime | Claude CLI + Anthropic-compatible SDK instead of KIRO CLI — the intended deviation; provider may be Anthropic or DeepSeek |
| System-prompt delivery | `--append-system-prompt` (author's decision). §4.3.2 writes the full prompt to stdin; `--system-prompt` would match that more literally but replaces Claude Code's built-in prompt wholesale and strips its tool-use guidance |
| Model | Anthropic defaults to `claude-opus-4-7`; DeepSeek mode uses V4 Pro. Either differs from the thesis's Opus 4.6 and must be recorded as an experimental configuration |
| Single-agent system | Deliberately out of scope, per the author |
| Duplo output | text summary, not `-json` — the JSON carries no line totals and the modes are mutually exclusive (§4) |
| Duplo `-ml` | 4, Duplo's own default; the thesis does not specify one |
| Excluded directories | configurable list; §4.3.2 does not enumerate them |
| `z_m` | `config.weights` accepts any real value — a superset of Eq 4.3's `{0,1}`; 0/1 reproduces the thesis exactly |
| Analyst worktrees | §4.3.2 creates one worktree per *programmer*; analysts additionally get a detached one, else they would read the repository root mid-merge |
| Backlog `SKIPPED` | a fourth status instead of removal, so dedup cannot go stale (§11) |
| Empty discovery stop | `empty_scan_limit=3`; the thesis specifies stagnation but not how an issue-free multi-agent run terminates |

---

## 17. Open items

- The **Table 4.1 calibration** of §14 has not been run — it needs the real
  target codebase.
- The fake-CLI FerretDB integration test validates transport, coordination,
  gate concurrency, metrics and build/test behavior, but it does not
  validate the judgment quality of a real CLI/model; run a real-model smoke
  test before collecting thesis data.
- The thesis's own results TODOs (§5.2.2, §5.3.2, §5.4, §5.5, §5.6) are
  writing and experiment tasks. The code now collects the data §5.1 and
  §5.4 require, but the runs themselves have not been performed.
- `analysis/tools.py:_is_excluded()` and the `_iter_cpp_files` alias are
  pre-existing dead code, kept rather than removed.
