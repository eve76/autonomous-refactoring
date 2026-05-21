# Comparison: autonomous-refactoring vs gnomad-kiro

What this repo (`autonomous-refactoring`) does differently compared to the original (`gnomad-kiro/multi_agent_refactoring/` + `gnomad-kiro/tools/`). All claims below are verified directly from source code.

---

## Verified Differences

### 1. Merge Gate Baseline: Stale vs Fresh

**autonomous-refactoring (`merge_gate/gate.py`):** Uses `penalty_before` passed in from the coordinator (computed at task assignment time). Never re-measures main.

**gnomad-kiro (`tools/programmer_gate.py`):** After rebasing, re-measures main's current penalty:
```python
main_target = target_dir.replace(worktree_path, repo_root)
_, main_raw = measure(main_target)
penalty_before = compute_penalty(main_raw, weights).get("total", 0)
```
Then compares the worktree's penalty against this fresh value.

**Impact:** If another programmer merged between assignment and gate invocation, AR uses the old (higher) baseline — making it easier to pass. GK always compares against the true current state.

### 2. Duplo Minimum Block Size

**autonomous-refactoring:** `duplo_min_block_lines=6` (in `config.py`)

**gnomad-kiro:** `-ml 10` hardcoded in `run_duplo.py`

**Impact:** AR catches smaller duplicate blocks (6+ lines vs 10+ lines), inflating the duplicate-line ratio.

### 3. Cognitive Complexity: Joined vs Separate

The two repos handle cognitive complexity scores differently when computing the total penalty.

**autonomous-refactoring** runs Lizard (which gives CCN, NLOC, params per function) and cognitive complexity (which gives cognitive score per function), then **merges them into one record per function**. In `analysis/tools.py`, `run_static_analysis` first builds the Lizard records list, then tries to attach cognitive scores:

```python
def run_static_analysis(target, thresholds, duplo_binary="", duplo_min_block_lines=6):
    records = run_lizard(target)       # list of dicts: [{file, line, name, ccn, nloc, param}, ...]
    cog = run_cognitive(target)        # dict: {(file, name): score, ...}

    if cog:
        for rec in records:                          # iterates LIZARD records only
            for nm in _function_match_keys(rec["name"]):
                if (rec["file"], nm) in cog:
                    rec["cognitive"] = cog[(rec["file"], nm)]   # attaches to existing record
                    break
                # if no match found, rec has no "cognitive" key

    dup_ratio = run_duplo(target, duplo_binary, duplo_min_block_lines)
    return records, dup_ratio          # returns only Lizard records (with cognitive attached where matched)
```

Then in `analysis/penalty.py`, `compute_total_penalty` iterates those same records:

```python
for m in metrics:                    # only Lizard records
    for key in PER_FUNCTION_METRICS: # ("ccn", "cognitive", "nloc", "param")
        value = m.get(key)
        if value is None:            # cognitive is None if no match was found
            continue
        total += function_penalty(float(value), float(thresholds[key]))
```

So any function that the cognitive tool finds but Lizard doesn't → no Lizard record exists → cognitive score is **silently dropped** and never penalized.

**gnomad-kiro** keeps them as **two separate lists** in `tools/metrics/compute_penalty_default.py`:

```python
for fn in lizard:          # penalize CCN, NLOC, params
    for metric, key in [("ccn", "ccn"), ("nloc", "nloc"), ("params", "param")]:
        pen = P_FN[metric](fn.get(key, 0))
        ...
for fn in cognitive:       # penalize cognitive separately
    pen = P_FN["cognitive"](fn.get("cognitive", 0))
    ...
```

Every function in the cognitive list gets penalized regardless of whether Lizard also found it.

**Impact:** The penalty totals will differ for the same codebase. The cognitive tool and Lizard use different parsers and may detect different function boundaries. Functions that appear in one list but not the other are handled differently — AR drops unmatched cognitive scores, GK always penalizes them.

### 4. Metric Weights System

**autonomous-refactoring:** No weights. All metrics always contribute equally. `penalty.py` has no weight parameter.

**gnomad-kiro:** `DEFAULT_WEIGHTS = {"ccn": 1, "nloc": 1, "cognitive": 1, "params": 1, "duplicates": 1}`. Weights are passed through the entire pipeline (gate CLI `--weights`, `compute_penalty(raw, weights)`). Setting a weight to 0 disables that metric.

**Impact:** Cannot replicate ablation studies or single-metric experiments without adding weight support.

### 5. Pre-Pull Penalty Check

**autonomous-refactoring:** No early bail-out. Goes straight to rebase → penalty → build → test.

**gnomad-kiro (`programmer_gate.py`):** Measures penalty BEFORE pulling main. If changes are already worse, bails immediately:
```python
_, wt_raw_pre = measure(target_dir)
penalty_pre = compute_penalty(wt_raw_pre, weights).get("total", 0)
if penalty_pre >= penalty_before:
    ...
    return 1
```

**Impact:** GK avoids expensive rebase/test cycles when changes are clearly bad. AR wastes time on doomed attempts.

### 6. Gate Commits vs Assumes Pre-Committed

**autonomous-refactoring:** Assumes the programmer already committed. Gate starts with rebase.

**gnomad-kiro:** Gate does `git add -A && git commit` as its first step:
```python
_git(worktree_path, "add", "-A")
r = _git(worktree_path, "commit", "-m", f"Fix from programmer {worktree_idx}")
```

**Impact:** If the AR programmer forgets to commit or commits partially, the gate operates on incomplete changes. GK guarantees all edits are captured before evaluation.

### 7. Revert on Failure

**autonomous-refactoring:** On penalty/build/test failure, reverts worktree:
```python
self._git(["reset", "--hard", f"origin/{self.main_branch}"], cwd=self.worktree)
```

**gnomad-kiro:** Does NOT revert the worktree on failure. Instead saves a `.patch` file to `GNOMAD_REVERTED_DIR` for post-mortem analysis.

**Impact:** In AR, a failed gate wipes the programmer's work — the agent must start from scratch. In GK, the failed state persists, allowing the programmer to iterate on the same changes.

### 8. File Exclusion Patterns

**autonomous-refactoring:**
```python
_DEFAULT_EXCLUDE = ("test", "tests", "__pycache__", "build", "node_modules", ".git")
```

**gnomad-kiro:**
```python
dirs[:] = [d for d in dirs if d.lower() != 'test']
```
Only excludes `test` directory.

**Impact:** AR excludes more directories (e.g. a folder named `tests` or `build`), potentially missing functions that GK would analyze and penalize.

### 9. Duplo Output Parsing

**autonomous-refactoring:** Uses `-json` flag, parses JSON output, counts `LineCount * 2` per block, divides by total lines counted from files.

**gnomad-kiro:** Parses Duplo's text output with regex, reads the `Duplicate lines of code: N` and `Lines of code: N` summary lines directly from Duplo's output.

**Impact:** The two approaches may compute different duplicate-line ratios for the same codebase, since AR manually counts `LineCount*2` per block while GK uses Duplo's own summary which may account for overlapping blocks differently.

### 10. Two-Tier Stuck Handling vs Single-Tier

**autonomous-refactoring:** Has both hard timeout (30 min kill) AND a discretionary stuck-eval at `issue_timeout_sec` (10 min) where the orchestrator LLM decides terminate/keep/mark_infeasible.

**gnomad-kiro:** Only has hard timeout (30 min kill). No discretionary evaluation.

**Impact:** AR may terminate programmers earlier (at 10 min) if the LLM judges them stuck, potentially reducing wasted time but also killing agents that would have succeeded given more time.

### 11. Penalty History & Results Archiving

**gnomad-kiro has, autonomous-refactoring lacks:**
- `penalty_tracking.py` logging every merge attempt to `penalty_history.json`
- Penalty plot generation (`plot_penalty()`)
- Timestamped results directories
- Feature branch creation per run with auto-push to origin
- Reverted patches directory

**Impact:** Without penalty history and archiving, AR cannot produce the time-series data needed for analysis plots or compare runs systematically.

### 12. Crash Recovery / Resume

**gnomad-kiro:** `save_state(state)` after each merge, `load_state()` on startup. `STATE_FILE` persists full orchestration state.

**autonomous-refactoring:** Backlog is persisted atomically, but no full state resume capability.

**Impact:** If AR crashes mid-run, all progress (merge count, penalty state, agent assignments) is lost. GK can resume from the last successful merge.

### 13. SIGTERM Handling

**gnomad-kiro:** Catches SIGTERM, sets `stop_reason = "wall_timeout"`, kills all agents gracefully.

**autonomous-refactoring:** No SIGTERM handler.

**Impact:** When AR is killed externally (e.g. by a timeout wrapper or system shutdown), agent subprocesses may be orphaned and worktrees left in inconsistent states.

### 14. Kill Mechanism

**autonomous-refactoring:** `process.kill()` (kills only the direct child process).

**gnomad-kiro:** `os.killpg(os.getpgid(proc.pid), SIGKILL)` (kills entire process group including children).

**Impact:** AR may leave orphaned grandchild processes (e.g. build tools, git commands spawned by the agent) running after a kill, consuming resources.

### 15. Lizard Invocation Method

**autonomous-refactoring:** Uses Lizard Python API directly (`lizard.analyze()`).

**gnomad-kiro:** Calls Lizard as a CLI subprocess (`~/.local/bin/lizard -l cpp -f filelist`), parses regex from stdout.

**Impact:** The Python API and CLI may produce slightly different results due to language detection and file filtering differences. The API auto-detects language; the CLI is forced to `-l cpp`.

---

## Prompt Differences

### Orchestrator

| Aspect | autonomous-refactoring | gnomad-kiro |
|--------|------------------------|-------------|
| Penalty guide injected | No — only raw penalty numbers | Yes — explains thresholds, active metrics, "below threshold = 0" insight |
| Per-metric breakdown | Not provided | Ranked by reduction potential with function counts |
| Stuck issue warnings | Not provided | "STUCK ISSUES (failed 3+ times, consider skipping): ..." |
| Decision framework | Rules only | 5-step "How to Think" section |
| Batching guidance | "Keep assignments small (1-2 issues)" | Same, plus: batch multiple LOW issues together (3+) |
| Analyst partitioning | "Dispatch when backlog empty or stagnation approaching" | Same, plus: "Partition by directory — no overlap" |

### Programmer

| Aspect | autonomous-refactoring | gnomad-kiro |
|--------|------------------------|-------------|
| Gate command delivery | Programmer must know the CLI invocation | Gate command injected as ready-to-run string |
| Retry feedback | None | "PREVIOUS ATTEMPT REJECTED (attempt N): reason" |
| Skip criteria | 3 failed attempts, private API, architectural | Same, plus: 10-min limit per issue |
| Rebase conflict handling | Not mentioned | Explicit: resolve, max 3 attempts, then skip |
| Penalty context | Implicit (gate handles it) | penalty_guide() injected with thresholds and strategy |
| Output format | RESULT: ISSUE-X - done/skipped | What is FIXED + where it MODIFIED, or if SKIPPED issue |

### Analyst

| Aspect | autonomous-refactoring | gnomad-kiro |
|--------|------------------------|-------------|
| Metric prioritization | "Focus on functions most exceeding threshold" | "Analyst ranks potential penalty improvement" |
| Grouping nuance | "Multiple violations in same function = SINGLE issue" | Same, plus: "unrelated findings = keep separate" |
| Tool invocation | Generic: "You may run Lizard, cognitive, Duplo" | Specific commands with paths provided |
| Anti-patterns | Not mentioned | "NEVER report: copyright, license, includes, comments" |

### Prompt Takeaway

The gnomad-kiro prompts are more **operationally specific** — they inject runtime context (gate commands, penalty guides, metric breakdowns, retry feedback) directly rather than expecting the agent to figure things out. The autonomous-refactoring prompts are more **abstract/generic** and rely on the agent having implicit knowledge about how the system works.

---
