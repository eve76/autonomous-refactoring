# 3. Research Method

## 3.1. Research design

This study investigates whether a coordinated multi-agent system can autonomously improve the structural quality of an existing software system while preserving its externally observable behavior and controlling the computational cost of large-language-model (LLM) inference. The task is formulated as a constrained iterative optimization problem. At each iteration, one or more agents identify a candidate quality problem, implement a behavior-preserving refactoring, and submit the resulting change to a deterministic acceptance procedure. A candidate is accepted only if it reduces a predefined static-analysis objective and passes the target project's native build and correctness tests.

The method deliberately separates model-based judgment from deterministic control. LLM agents are used for activities that require contextual interpretation, including selecting refactoring opportunities, reasoning about source code, proposing structural changes, and assessing whether an agent has become unproductive. In contrast, state transitions, metric calculation, scope enforcement, version-control operations, acceptance decisions, stopping conditions, and experimental logging are implemented in conventional Python code. This separation prevents an agent from declaring its own change successful without independently verifiable evidence.

The system implements one multi-agent configuration consisting of one orchestrator, three analyst agents, and three programmer agents. The number of analysts and programmers is configurable, but the default configuration is used as the reference design throughout this section. The implementation supports both C/C++ and Go targets and includes locked execution profiles for MongoDB Query and FerretDB. It does not implement a single-agent baseline, and runtime-performance measurements are not part of the current acceptance criterion.

## 3.2. Problem formulation

Let \(S_i\) denote the accepted state of the target repository after the \(i\)-th successful integration, with \(S_0\) representing the fixed baseline revision. Each state is characterized by five static quality dimensions: cyclomatic complexity, cognitive complexity, logical lines of code, number of function parameters, and duplicate-line ratio. The first four are measured per function, whereas duplication is measured over the complete production-code scope.

The system searches for a sequence of behavior-preserving transformations

\[
S_0 \rightarrow S_1 \rightarrow \cdots \rightarrow S_n
\]

such that every accepted transition satisfies

\[
F(S_{i+1}) < F(S_i),
\]

where \(F\) is the weighted static-quality penalty defined in Section 3.4. In addition, each accepted state must satisfy the target repository's build and correctness-test procedures. The acceptance relation can therefore be expressed as

\[
\operatorname{accept}(S_i,S_{i+1}) =
\begin{cases}
1, & F(S_{i+1}) < F(S_i) \land B(S_{i+1}) \land T(S_{i+1}),\\
0, & \text{otherwise},
\end{cases}
\]

where \(B\) and \(T\) denote successful build and test outcomes, respectively. The formulation intentionally requires a strict reduction. Changes that preserve the penalty but improve an unmeasured property are not accepted by the current method, because their benefit cannot be established by the experiment's predefined objective.

The optimization is further constrained to a configured source subtree. A full repository checkout may be provided to satisfy transitive build dependencies, but neither measurement nor edit authorization is expanded beyond the target scope. Test sources are excluded from metric calculation and may not be modified as a means of making a candidate pass.

## 3.3. System architecture

The system consists of a deterministic coordination layer and an LLM-based agent layer. The coordination layer owns all shared state and executes on the main thread. Analyst and programmer sessions are launched as independent command-line subprocesses through two thread pools, while the orchestrator is invoked synchronously only when a coordination decision is required.

Table 1 summarizes the principal components.

**Table 1. Principal system components and responsibilities.**

| Component | Cardinality | Execution model | Primary responsibility |
|---|---:|---|---|
| Coordinator | 1 | Python main thread | Own state, schedule work, apply stopping rules, and produce experimental artifacts |
| Orchestrator | 1 | Synchronous LLM API call | Allocate work and evaluate potentially stuck programmers |
| Analyst | 3 | Independent CLI subprocesses | Inspect accepted code and report actionable refactoring opportunities |
| Programmer | 3 | Independent CLI subprocesses | Implement assigned refactorings in isolated Git worktrees |
| Merge gate | On demand | Independent Python process | Evaluate, test, and integrate committed candidates |
| Static-analysis layer | On demand | Deterministic external tools | Measure quality metrics and calculate the optimization objective |

The workflow begins with a baseline measurement of the configured production-code scope. If no actionable issue is already present, the orchestrator dispatches analysts to examine high-penalty functions. Confirmed findings are inserted into a persistent product backlog. The orchestrator then assigns backlog items to idle programmers. Each programmer modifies an isolated feature branch and invokes the merge gate. A successful gate advances the run-specific integration branch, after which the coordinator remeasures the complete target scope. This updated state forms the basis of subsequent analysis and scheduling decisions.

Communication from analysts and programmers to the coordinator uses a thread-safe message queue. Agents do not write the backlog or global run state directly. Instead, the coordinator drains queued messages in a single-writer loop and persists state using atomic file replacement. This design avoids concurrent modification of shared JSON documents while allowing the LLM subprocesses to operate asynchronously.

## 3.4. Quality objective

### 3.4.1. Selected metrics

The quality objective comprises the five metrics shown in Table 2. Thresholds are configurable; the values shown are the defaults used by the system.

**Table 2. Static quality metrics and default parameters.**

| Metric | Symbol | Default threshold | Measurement unit | Measurement backend |
|---|---|---:|---|---|
| Cyclomatic complexity | CCN | 15 | Per function | Lizard |
| Cognitive complexity | Cog | 15 | Per function | Cognitive-complexity tool for C/C++; gocognit for Go |
| Logical lines of code | NLOC | 30 | Per function | Lizard |
| Parameter count | Param | 5 | Per function | Lizard |
| Duplicate-line ratio | Dup | 0 | Target scope | Duplo for C/C++; mibk/dupl for Go |

The same production-file population is supplied to all applicable tools. Directory-level test artifacts are excluded by configurable path-component rules, and language-specific test files are excluded by filename conventions. This common selection procedure prevents the duplication denominator and the per-function metric populations from referring to different source scopes.

### 3.4.2. Per-function penalty

For a function-level metric \(m\), let \(x_{f,m}\) be the value measured for function \(f\), and let \(T_m\) be the corresponding threshold. The penalty contribution is defined as

\[
p_m(x_{f,m}) = 100\left(1-\frac{T_m}{\max(T_m,x_{f,m})}\right).
\]

The function assigns zero penalty to values at or below the threshold. Above the threshold, the penalty increases monotonically and approaches 100 asymptotically. Consequently, reducing an extreme violation produces a measurable improvement even when the resulting function remains above the threshold, while bringing a function into the acceptable region removes its contribution entirely.

### 3.4.3. Duplication penalty

Let \(r\in[0,1]\) denote the fraction of duplicate production lines. Because any non-zero duplication contributes to the objective, the duplication component uses

\[
p_{dup}(r) = 100\frac{r}{r+0.1}.
\]

The implementation represents \(r\) as a fraction rather than a percentage. The C/C++ adapter derives the numerator and denominator from Duplo's summary output. The Go adapter canonicalizes reverse clone-pair reports and merges overlapping physical line intervals before calculating the ratio, thereby preventing double counting and keeping the ratio bounded.

### 3.4.4. Aggregate objective and experimental selectors

The total penalty of state \(S\) is

\[
F(S) =
\sum_{m \in M_f} w_m \sum_{f \in \mathcal{F}_m} p_m(x_{f,m})
+ w_{dup}p_{dup}(r),
\]

where \(M_f=\{CCN,Cog,NLOC,Param\}\), \(\mathcal{F}_m\) is the function population reported for metric \(m\), and \(w_m\) is the configured weight. Lizard and cognitive-complexity records are retained as independent populations. This avoids dropping a valid metric contribution when two analysis backends identify functions differently.

All weights are one in the complete configuration. Setting a weight to zero excludes that metric from the acceptance objective, enabling both single-metric and metric-ablation treatments without modifying the gate. Although the implementation accepts real-valued weights, controlled experiments should use a predefined weighting scheme and record it in every run artifact.

### 3.4.5. Distributional outcome measures

The scalar penalty provides the feedback signal used during optimization, but it is insufficient for interpreting how the code changed. For this reason, the system records the complete baseline and final distributions of each function-level metric: population size, mean, median, 90th, 95th, and 99th percentiles, maximum, number of threshold violations, and whether all violations were cleared. Duplication is reported as a ratio and percentage together with duplicate lines, total lines, and clone-block count.

The objective and the reported outcome measures thus serve different purposes. The former determines whether a candidate can be accepted, whereas the latter reveals whether improvements affected a broad portion of the codebase or only a small number of outliers.

## 3.5. Static-analysis procedure

For each measurement event, the analysis layer first enumerates production files under the configured target scope. Lizard is executed in CSV mode to obtain one record per function for CCN, NLOC, and parameter count. CSV output is required because Lizard's default report may repeat high-complexity functions in a warning section, which would inflate precisely the observations that contribute to the objective.

Cognitive complexity is measured independently. C/C++ files are processed by a Tree-sitter-based cognitive-complexity implementation. Go files are supplied to `gocognit` in bounded batches and parsed from JSON. The implementation currently has no cognitive-complexity backend for Java or Python; experiments in those languages must therefore disable this dimension or supply an additional backend.

Duplicate-code measurement is language dependent. For C/C++, Duplo receives absolute paths and filters preprocessor directives. Its textual summary is used because it contains both duplicate lines and the post-filter total line count required to calculate \(r\). The default minimum duplicate block length is four lines. For Go, `mibk/dupl` operates on a syntax-token representation with a default minimum clone size of 100 tokens.

All analysis failures are treated conservatively. A missing executable, timeout, non-successful process, or unparseable result aborts the measurement and produces an explicit `analysis_failed` gate outcome. Missing data are never interpreted as zero violations.

## 3.6. Agent roles and interaction protocol

### 3.6.1. Orchestrator

The orchestrator is a stateless decision agent invoked at two points: work allocation and stuck-agent evaluation. During allocation, it receives the current and baseline penalties, the stagnation counter, a per-metric penalty breakdown, the identities of idle agents, and a bounded view of the product backlog. It may assign one or two issues to each programmer and may request new analyst scans when no actionable item is available or the run is approaching stagnation.

The coordination layer validates every proposed assignment against the current backlog. It rejects unknown or non-`TODO` issue identifiers, assignments to non-idle agents, duplicate assignments, exhausted issues, and simultaneous assignments that would allow different programmers to modify the same file. Thus, the orchestrator proposes a schedule but cannot violate deterministic concurrency constraints.

The second decision point is triggered when a programmer exceeds the soft per-issue time threshold. The orchestrator receives the elapsed model-work time, assigned issue identifiers, whether edits have occurred, the number of gate invocations, and a bounded tail of the recent assistant log. It may recommend continuation, termination, or classification of specified issues as infeasible.

When DeepSeek is used, the orchestrator is called without extended thinking and must return a schema-constrained tool invocation. The bounded decision task does not require a long reasoning trace, and structured output reduces protocol failures. Under the Anthropic provider, the implementation parses the equivalent line-oriented response format. Raw provider responses and the exact parser input are retained for post-run diagnosis.

### 3.6.2. Analyst

Analysts inspect only accepted code. Each analyst operates in a detached worktree synchronized with the current integration branch before a scan. This prevents findings from being based on unmerged changes in a programmer's feature branch.

The coordinator supplies the current per-metric penalty breakdown and a bounded page of local static-analysis leads. A lead combines all active CCN, NLOC, parameter-count, and cognitive-complexity violations associated with the same function and is ranked by the penalty that would be removed if those values were reduced to their thresholds. Leads are non-authoritative: the analyst must inspect the named function and determine whether a safe, behavior-preserving refactoring is plausible. Confirmed findings are emitted using a line-oriented `ISSUE` protocol containing the file, line, severity, type, metric values, and a short explanation.

The coordinator validates file existence and scope, canonicalizes paths and issue types, estimates potential penalty reduction, and deduplicates the finding before adding it to the backlog. Findings that refer to nonexistent or out-of-scope files are discarded and recorded as phantom issues.

### 3.6.3. Programmer

Each programmer works in an isolated feature branch. A dispatch contains at most two issues, which must be processed sequentially. Before handling an issue, the programmer synchronizes its worktree with the current integration branch and reads the target code and relevant dependencies. It then implements a non-functional refactoring, commits the candidate, and invokes the merge gate. Build and test commands are owned by the gate and are not run separately as an alternative acceptance path.

The programmer receives bounded feedback from prior attempts on the same issue, including previous strategy descriptions, outcomes, penalty changes, and references to preserved patches. It is explicitly instructed not to repeat a recorded strategy unless the new design is materially different. A run-wide dispatch limit and a per-dispatch gate limit are both set to three by default.

After a successful merge, the programmer emits a structured `RESULT` line containing the issue identifier and gate-reported penalty values. A skipped result contains the issue and reason. Neither message is accepted at face value: the coordinator requires a matching successful gate record for the same programmer, issue, and penalty pair. If the gate merged successfully but the model terminated before emitting its final result, the deterministic gate record is used to recover the completion.

### 3.6.4. Product backlog

The persistent backlog represents the interface between discovery and implementation. Each item contains an identifier, source location, issue type, analyst explanation, parsed metric values, estimated improvement, impact class, status, assignee, dispatch count, and attempt-history reference. The status transition is

\[
TODO \rightarrow IN\_PROGRESS \rightarrow \{DONE,SKIPPED\}.
\]

An interrupted assignment may return from `IN_PROGRESS` to `TODO`. Deduplication uses the tuple `(file path, line, issue type)` across every status, including completed and skipped items. Retaining skipped issues is necessary to prevent an infeasible finding from being rediscovered indefinitely.

## 3.7. Token-efficient context management

The optimized system reduces LLM context through conservative selection rather than by changing the coordination protocol or acceptance criterion. Three mechanisms are used.

First, the orchestrator receives all active items and aggregate status counts, but only the 24 highest-impact `TODO` items by default. Lower-ranked TODO items and the bodies of completed and skipped items remain in the local backlog. The serialized view reports both shown and omitted counts, preserving awareness that the prompt is incomplete.

Second, analyst leads are paginated. Each dispatch receives at most 15 mutually exclusive leads, ordered by estimated penalty reduction. A lead is marked as reviewed only if the analyst process exits successfully and produces a valid terminal CLI result. Failed or truncated sessions release their reservations for later review. This prevents loss of coverage while avoiding repeated whole-tree analysis in every model context.

Third, the coordinator suppresses model calls that cannot produce useful work. It does not invoke the orchestrator when no idle agent can act on the current state, dispatches analysts in bounded waves rather than refilling individual slots opportunistically, and stops after repeated unusable orchestrator responses or repeated empty discovery scans.

These optimizations are intentionally external to the quality model. They do not alter metric values, backlog semantics, issue acceptance, programmer permissions, or merge-gate decisions. Consequently, their effect can be studied primarily through Token consumption, cost, dispatch count, and successful merges rather than through a changed optimization objective.

## 3.8. Candidate evaluation and integration

The merge gate is the sole mechanism by which a programmer's commit can enter the run-specific integration branch. Table 3 presents the ordered evaluation procedure.

**Table 3. Merge-gate procedure.**

| Step | Operation | Failure behavior |
|---:|---|---|
| 1 | Reject uncommitted worktree changes | Candidate is not evaluated |
| 2 | Rebase feature branch onto the current integration branch | Conflict is left for programmer resolution |
| 3 | Compare changed paths with the authorized target scope | Out-of-scope candidate is rejected |
| 4 | Fingerprint and preserve the committed patch | Previously evaluated identical patch is rejected |
| 5 | Measure integration and candidate penalties | Analysis error fails closed |
| 6 | Require strict penalty reduction | Non-improving candidate is reverted |
| 7 | Execute the native build command | Candidate is retained for repair on failure |
| 8 | Execute the native correctness tests | Candidate is retained for repair on failure |
| 9 | Fast-forward the integration branch | Lost race causes complete reevaluation |

The asymmetry between penalty rejection and build/test failure is deliberate. A non-improving candidate does not satisfy the optimization objective and is therefore reverted. In contrast, a candidate that improves the objective but does not yet compile or pass tests may be repairable; retaining it allows the programmer to address the observed failure and invoke the gate again.

Every gate exit is assigned an explicit outcome. Outcomes distinguish penalty rejection, build failure, test failure, command timeout, process interruption, analysis failure, rebase conflict, fast-forward contention, uncommitted work, scope violation, duplicate patch, attempt-limit exhaustion, and successful merge. Each complete attempt is appended to a concurrent-safe JSONL log, providing the basis for failure analysis without interpreting natural-language agent logs.

Concurrent integration is handled through the local integration branch shared by all worktrees. Before each attempt, the candidate is rebased onto that branch, which immediately reflects every earlier accepted change. If another programmer advances it after the rebase but before the fast-forward, the gate repeats rebase, static analysis, build, and test. Therefore, every accepted commit has been evaluated against the exact repository state it extends.

## 3.9. Scheduling and stopping criteria

The coordinator executes a periodic control loop with a default interval of one second. In each iteration it drains agent messages, reconciles durable gate records, reaps completed futures, evaluates Token or cost ceilings, checks terminal conditions, dispatches useful work, and examines agent timeouts.

### 3.9.1. Stagnation

Let \(\Delta_i = F(S_i)-F(S_{i+1})\) denote the penalty reduction of a successful merge. The default minimum meaningful gain is \(\delta=10\). The stagnation counter is updated as follows:

\[
c_{i+1}=
\begin{cases}
0, & \Delta_i > \delta,\\
c_i+1, & \Delta_i \leq \delta.
\end{cases}
\]

A programmer hard timeout with unresolved work also increments the counter. The run terminates when the counter reaches three. Penalty-rejected gate attempts are retained as failure evidence but do not increment this multi-agent stagnation counter because they do not produce an accepted state transition.

### 3.9.2. Agent time limits

Programmers are subject to a soft model-work threshold of ten minutes per issue and a hard threshold of thirty minutes. At the soft threshold, the orchestrator evaluates whether continued work is justified. Evaluations of the same agent are separated by at least five minutes. At the hard threshold, the process group is terminated, unresolved issues return to `TODO`, and the stagnation counter is incremented.

Time spent inside an active merge gate is excluded from the programmer's model-work budget. Build, test, and total gate execution instead have independent deadlines, because native validation may legitimately exceed the model reasoning interval. Analysts have a separate default hard timeout of 15 minutes; a timed-out analyst releases its reserved lead page.

### 3.9.3. Additional terminal conditions

The system terminates without further model dispatch when the initial penalty is zero; when repeated analyst scans produce no actionable issue and no unreviewed lead remains; when repeated orchestrator responses produce no executable assignment; or when a configured Token or monetary dispatch ceiling has been reached and all in-flight work has finished. A SIGTERM is treated as an external wall-clock termination and triggers controlled shutdown.

## 3.10. Token and cost measurement

Token usage is collected from two sources. Synchronous orchestrator calls append provider usage records to an independent JSONL file. Analyst and programmer usage is reconstructed from their complete stream-JSON logs. The normalization distinguishes uncached input, cache creation, cache reads, and output, and aggregates consumption by role, agent, and dispatch.

For Anthropic records, all reported input classes are summed once. DeepSeek may expose prompt-cache hit and miss fields together with a total prompt count; in that representation, hit and miss replace rather than supplement the prompt total. For Claude CLI sessions, the final result event is preferred because it represents session-level usage. If a process is killed before a terminal result, the implementation falls back to accumulated assistant-message usage.

The system reports total Token consumption, usage coverage, number of turns or calls, effective monetary cost, and Tokens per successful merge. Anthropic cost may use the CLI-reported amount, whereas DeepSeek costs are recomputed from a pinned per-model price table because historical compatible-API fields may use misleading currency names. Native CNY cost is not obtained by converting from USD.

Optional input-Token, output-Token, USD, and DeepSeek CNY ceilings operate at the dispatch boundary. Once a ceiling is observed, no new work is started, but active agents are allowed to complete. The final summary therefore reports both the usage at closure and any overshoot. This semantics avoids discarding completed model work while making the budget behavior reproducible.

## 3.11. Version-control isolation and recovery

Each run is identified by a unique run identifier and receives an integration branch named `refactor/<run-id>`. The branch is created at a fixed baseline commit, while the repository's primary branch remains unchanged. Before initialization, the system refuses tracked modifications and unknown untracked artifacts. It records whether the user's checkout was attached to a branch or detached at a commit, and restores that exact identity after the run.

Programmers receive feature-branch worktrees; analysts receive detached worktrees. Sparse checkout is used when the target subtree is independently buildable. A full worktree is used when native validation requires files outside the measured subtree, without relaxing the edit-scope check.

Run state, backlog state, penalty history, and dispatch numbering are recoverable. A resumed run checks out the existing integration branch without moving it to the baseline, returns crash-abandoned `IN_PROGRESS` items to `TODO`, and remeasures the current branch. The original baseline penalty and metric distribution remain unchanged so that the final improvement is calculated across the complete pre- and post-crash execution.

To preserve experimental comparability, resume is guarded by a schema-versioned SHA-256 fingerprint. The current schema version is 7 and covers the target repository, scope, analysis tools, thresholds, weights, validation commands, provider, models, context-selection parameters, budgets, and price snapshot. A changed fingerprint requires a new run identifier rather than silently continuing under different conditions.

## 3.12. Execution profiles

The system includes two fail-closed production profiles, summarized in Table 4. Profile values cannot be overridden by general-purpose command-line options.

**Table 4. Locked production profiles.**

| Property | FerretDB | MongoDB Query |
|---|---|---|
| Language | Go | C/C++ |
| Measurement/edit scope | Complete repository | `src/mongo/db/query` |
| Worktree materialization | Complete repository | Complete repository for Bazel dependencies |
| Cognitive backend | gocognit | Tree-sitter cognitive-complexity tool |
| Duplication backend | mibk/dupl | Duplo |
| Build validation | Race-enabled compile-only Go test pass | Bazel build of `//src/mongo/db/query/...` |
| Correctness validation | Repository-wide short Go tests | Bazel tests restricted to `//src/mongo/db/query/...` |
| Baseline cache prewarm | No | Yes, using a shared Bazel disk cache |

The MongoDB Query profile directly selects only targets in the Query submodule. Bazel may compile transitive dependencies outside that subtree, but these dependencies are not selected as experimental targets. Integration tests and benchmark targets are excluded from the correctness test command. The baseline Query build is executed before any paid model dispatch, both to detect an invalid environment early and to warm the shared disk cache used by subsequent worktrees.

## 3.13. Experimental procedure

An independent run proceeds as follows:

1. Validate the configuration, provider credentials, model pricing, analysis executables, and native build/test commands.
2. Record the initial checkout and resolve the immutable baseline commit.
3. Create the run-specific integration branch and isolated agent worktrees.
4. Measure the baseline penalty, per-metric breakdown, and metric distributions.
5. When configured, execute the baseline build-cache prewarm before model dispatch.
6. Enter the coordination loop and execute analyst, orchestration, programming, and merge-gate cycles until a terminal condition is reached.
7. Remeasure the accepted target state after every successful merge.
8. On termination, reconcile remaining gate evidence, write all result artifacts, remove worktrees, optionally push the integration branch, and restore the original checkout.

For repeated trials, every run must use a unique identifier but the same baseline reference and treatment configuration. The relevant controlled variables include the target revision, provider, orchestrator and agent models, metric thresholds and weights, number of agents, context-window parameters, build/test commands, exclusion rules, and stopping parameters. Each run retains its own integration branch and result directory; the final state of one trial is not used as the baseline of the next.

The complete treatment uses unit weights for all five metrics. A single-metric treatment sets the selected metric to one and all others to zero. A metric-ablation treatment sets the excluded metric to zero and retains unit weights for the remaining four. Because weights are included in both gate configuration and the resume fingerprint, they remain consistent throughout a run.

## 3.14. Data collection and analysis

Every run produces a structured summary containing its baseline commit, configuration, stop reason, elapsed time, initial and final penalties, total reduction, merge count, metric distributions, backlog state, failure counts, Token usage, cost, and budget behavior. The penalty-history artifact records baseline, merge, timeout, and termination events with elapsed time and the per-metric breakdown at accepted transitions.

Gate attempts are analyzed by explicit outcome rather than by model-generated prose. The collected failure dimensions include non-improving candidates, build and test failures, command timeouts and interruptions, rebase conflicts, fast-forward races, analysis failures, scope violations, duplicate patches, attempt-limit rejections, agent crashes, hard timeouts, discretionary stuck-agent terminations, skipped issues, and phantom analyst findings.

The primary effectiveness outcome is relative penalty reduction,

\[
R_F = \frac{F(S_0)-F(S_n)}{F(S_0)}\times 100\%.
\]

This value is interpreted together with the five metric distributions and the number of cleared dimensions. Efficiency is characterized by elapsed time, total model Tokens, effective cost, successful merges, and Tokens or cost per successful merge. Reliability is characterized by gate-outcome frequencies, agent crashes, timeouts, skipped issues, and the terminal condition.

Runs are considered directly comparable only when their baseline commit, target scope, metric configuration, provider and models, validation commands, and relevant tool versions agree. Runs stopped by external wall-clock limits or incomplete usage coverage should be reported separately or explicitly treated as censored observations rather than silently combined with naturally terminated runs.

## 3.15. Reproducibility and methodological boundaries

The implementation writes a complete configuration snapshot and preserves the accepted code on a dedicated run branch. Agent output is stored once per dispatch, avoiding both overwriting earlier sessions and replaying stale result lines. Raw orchestrator responses, normalized usage events, issue histories, rejected patches, gate outcomes, and metric time series provide complementary evidence for reconstructing a run.

The default local verification suite contains 11 independent suites covering metric calculation, Git isolation, merge-gate behavior, concurrent integration, artifacts, stopping rules, issue attempt policy, provider isolation, Token accounting, Go cognitive complexity, and context-reduction mechanisms. These tests use temporary repositories or stubbed model calls and therefore validate the deterministic experiment infrastructure without incurring API cost.

Several methodological limitations remain. First, the objective captures selected structural properties and should not be interpreted as a complete measure of maintainability. Second, strict penalty-based acceptance excludes changes whose benefits lie entirely outside the selected metrics. Third, passing native tests reduces but does not eliminate the possibility of semantic change. Fourth, the current implementation contains no single-agent comparison and no performance-based merge criterion. Finally, deterministic infrastructure tests establish protocol and integration correctness, but large-scale repeated trials are still required to estimate the behavior and variance of real LLM agents.

