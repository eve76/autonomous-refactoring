# 多智能体代码质量重构系统：方法与实现

## 1. 文档目的

本文档说明 `experiment_token_save` 项目当前实现的方法、系统结构、组件职责、运行流程与实验产物。目标读者无需预先阅读其他设计文档；在读完本文后，应能够回答以下问题：

1. 系统试图优化什么；
2. 多个智能体如何分工与通信；
3. 静态分析结果如何转换为优化目标；
4. 一个候选重构如何被验证并合并；
5. 系统如何处理并发、失败、超时、重复尝试与崩溃恢复；
6. Token 优化如何减少模型上下文，同时保持实验语义；
7. 如何配置并运行一次可复现的实验；
8. 应从哪些结果文件中提取实验数据。

本文以当前源码为准。低层函数签名、异常分支和全部命令行参数可进一步参考 `config.py`、`main.py` 及相应模块源码。

---

## 2. 研究对象与系统边界

该系统用于自动重构已有代码，以降低由静态代码质量指标构成的综合惩罚值。系统只允许非功能性重构：候选修改可以改变代码结构，但不得有意添加功能、修改业务行为或通过修改测试来规避验证。

系统采用多智能体结构，默认包含一个 Orchestrator、三个 Analyst 和三个 Programmer。智能体负责需要判断的工作；确定性的 Python 协调层负责共享状态、并发控制、质量计算、验证、Git 集成和停止条件。

当前实现不包含单智能体对照系统，也不把性能 benchmark 纳入合并条件。一次候选修改能否被接受，仅取决于以下三个条件：

1. 静态质量总惩罚严格下降；
2. 项目的原生构建命令成功；
3. 项目的原生正确性测试成功。

系统支持自定义 Git 仓库，同时提供两个锁定的 production profile：FerretDB 和 MongoDB Query。Production profile 固定目标范围、语言、分析工具以及原生 build/test 命令，避免运行者无意中改变实验边界。

---

## 3. 总体方法

系统将自动重构建模为一个受约束的迭代优化过程。每轮工作由问题发现、任务分派、代码修改、质量验证和集成组成：

```text
静态测量
   ↓
Analyst 检查候选函数并报告问题
   ↓
Backlog 保存、去重并排序问题
   ↓
Orchestrator 为 Programmer 分派问题
   ↓
Programmer 在独立 worktree 中重构并提交
   ↓
Merge Gate：rebase → 范围检查 → penalty → build → test → merge
   ↓
协调层重新测量、更新状态和停止条件
   └───────────────────────────────────────↺
```

这一结构将模型判断与确定性控制分离。模型可以决定“哪里值得改”“如何改”和“卡住时是否继续”，但模型不能自行决定一项修改已经通过验证。最终合并权属于 Merge Gate，系统状态的最终写入权属于 Coordinator。

---

## 4. 系统结构

### 4.1 分层结构

系统由五个主要层次组成。

| 层次 | 主要模块 | 职责 |
|---|---|---|
| 入口与配置 | `main.py`, `config.py`, `production_profiles.py` | 解析参数、加载 `.env`、校验配置、选择生产配置 |
| 智能体层 | `agents/` | Orchestrator 决策、Analyst 发现问题、Programmer 修改代码 |
| 协调层 | `coordination/` | 主循环、Backlog、消息队列、Git、停滞、恢复、Token 和失败统计 |
| 分析层 | `analysis/` | 文件选择、静态分析、惩罚计算、指标分布和本地候选线索 |
| 验证层 | `merge_gate/` | 对每个已提交候选执行完整验证并 fast-forward 集成 |

### 4.2 主要进程与线程

Coordinator 运行在主线程中，并维护两个线程池：Analyst 线程池和 Programmer 线程池。每个工作线程启动一个独立的 Claude CLI 子进程。Orchestrator 不作为常驻进程运行，而是在需要分派任务或评估卡死 Programmer 时，由协调层同步调用一次兼容 Anthropic 协议的 API。

默认并发结构如下：

| 角色 | 默认数量 | 执行方式 | 是否直接修改代码 |
|---|---:|---|---|
| Coordinator | 1 | Python 主线程 | 否 |
| Orchestrator | 1 | 同步 API 调用 | 否 |
| Analyst | 3 | 线程池中的独立 CLI 子进程 | 否 |
| Programmer | 3 | 线程池中的独立 CLI 子进程 | 是，在各自 worktree 内 |
| Merge Gate | 按需 | Programmer 调用的独立 Python 进程 | 只负责验证、回退和集成 |

### 4.3 单写者原则

Backlog、运行状态和 penalty history 均由 Coordinator 统一管理。Analyst 和 Programmer 不直接修改共享 JSON，而是将类型化消息写入线程安全队列。Coordinator 每个主循环周期依次处理队列、回收已结束的任务、尝试新分派并检查超时。

该设计避免多个智能体同时写入同一状态文件。Backlog、运行状态和 penalty history 使用临时文件加原子替换的方式持久化，从而降低进程崩溃造成半写文件的风险。需要被多个 Merge Gate 进程并发追加的尝试日志使用 JSONL 和单次追加写入。

---

## 5. 代码质量模型

### 5.1 指标

系统使用五类静态质量指标。

| 指标键 | 含义 | 默认阈值 | 计算范围 | 工具 |
|---|---|---:|---|---|
| `ccn` | 圈复杂度 | 15 | 每个函数 | Lizard |
| `cognitive` | 认知复杂度 | 15 | 每个函数 | C/C++ cognitive-complexity；Go 使用 gocognit |
| `nloc` | 函数逻辑代码行数 | 30 | 每个函数 | Lizard |
| `param` | 函数参数个数 | 5 | 每个函数 | Lizard |
| `duplicates` | 重复代码行比例 | 无正阈值 | 整个目标范围 | C/C++ 使用 Duplo；Go 使用 mibk/dupl |

所有工具使用同一套生产文件选择规则。默认排除常见测试目录，并额外按语言排除诸如 Go 的 `*_test.go` 等测试源文件。自定义实验应显式确认排除规则与目标仓库布局一致。

### 5.2 单函数惩罚

对于具有阈值的指标，值不超过阈值时不产生惩罚；超过阈值时使用双曲函数：

\[
p_m(x)=100\left(1-\frac{T_m}{\max(T_m,x)}\right)
\]

其中，\(x\) 是某个函数的实际指标值，\(T_m\) 是该指标阈值。该函数在阈值处为 0，并随超标程度单调增加，但最大值渐近于 100。

### 5.3 重复代码惩罚

重复代码没有“可接受的非零阈值”，因此使用代码库级饱和函数：

\[
p_{dup}(r)=100\frac{r}{r+0.1}
\]

其中，\(r\) 是 0 到 1 之间的重复行比例。例如 2.41% 必须作为 `0.0241` 传入，而不是 `2.41`。

### 5.4 总目标函数

总惩罚由所有函数的各项惩罚和重复代码惩罚加权求和：

\[
F=\sum_{m}\;w_m\sum_f p_m(x_{f,m}) + w_{dup}p_{dup}(r)
\]

默认所有权重为 1。权重设为 0 时，该指标完全不参与优化目标。由此可以构造：

- 完整指标实验：五个权重均为 1；
- 单指标实验：目标指标为 1，其余为 0；
- 消融实验：被移除指标为 0，其余为 1。

Lizard 记录与 cognitive-complexity 记录作为两个独立函数集合参与计算，不要求两个工具识别到完全相同的函数。这样可以避免在 join 失败时静默丢失认知复杂度惩罚。

### 5.5 指标分布

Penalty 是优化信号，但结果报告同时保存完整分布统计。每个函数级指标包含函数数量、均值、中位数、p90、p95、p99、最大值、超阈值函数数和是否清零。重复代码保存比例、重复行数、总行数、块数量和是否清零。

这两种表示承担不同目的：总 penalty 用于决定候选能否进入下一阶段；分布统计用于解释系统改善了哪些质量维度，以及改善是否集中在极端函数。

---

## 6. 静态分析流水线

### 6.1 文件范围

分析从 `target_subdir` 中枚举与配置语言匹配的生产源文件。目标范围同时约束：

1. 静态指标测量；
2. Analyst 可报告的问题；
3. Programmer 被授权修改的文件；
4. Merge Gate 对越界修改的检查。

完整 worktree 不代表扩大修改范围。某些项目的 build/test 需要仓库其他目录，因此 worktree 可以包含整个仓库，但 measurement 和 edit authorization 仍然限制在 `target_subdir`。

### 6.2 Lizard

Lizard 通过 CLI 的 CSV 模式运行，每个函数只产生一条记录。系统不解析默认表格输出，因为默认输出会在 warning 区域再次列出部分高复杂度函数，造成惩罚和统计重复计算。

### 6.3 认知复杂度

C/C++ 使用基于 Tree-sitter 的 cognitive-complexity 工具。Go 使用 `gocognit -json`，并将经过相同排除规则过滤后的生产文件列表分批传入。当前 Java 和 Python 配置不会产生认知复杂度记录，因此在使用这些语言前需要明确该指标是否应禁用或补充对应工具。

### 6.4 重复代码

C/C++ 使用 Duplo。系统传入绝对路径并解析文本 summary 中的总行数、重复行数和块数量；JSON 模式不包含计算重复行比例所需的完整分母。默认最小重复块长度为 Duplo 自身的 4 行。

Go 使用 `mibk/dupl` 的 plumbing 输出。适配器会规范化正反方向的 clone pair，并合并同一文件中的重叠区间，使重复行比例保持在 0 到 1 之间。

### 6.5 失败策略

静态分析采用 fail-closed 策略。工具缺失、超时、非预期退出或输出无法解析时，系统产生明确错误，而不是把缺失结果解释为较低 penalty。这样可避免“分析失败”被误判为“代码质量显著改善”。

---

## 7. Product Backlog

### 7.1 Issue 数据结构

每个 Backlog 项包含下列核心字段：

| 字段 | 含义 |
|---|---|
| `id` | 形如 `ISSUE-0001` 的运行内唯一标识 |
| `file_path`, `line` | 问题所在文件与行号 |
| `severity`, `issue_type` | 严重性和规范化问题类型 |
| `message` | Analyst 对问题及指标值的描述 |
| `metric_values` | 从描述中解析出的指标值 |
| `estimated_penalty_reduction` | 假设修至阈值时可消除的估计惩罚 |
| `impact` | 依据最小有效收益划分的 high/low |
| `status` | `TODO`、`IN_PROGRESS`、`DONE` 或 `SKIPPED` |
| `assigned_to` | 当前负责的 Programmer |
| `dispatch_count` | 该 issue 已被分派的次数 |
| `attempt_history_path` | 该 issue 的历史记录位置 |

### 7.2 去重与路径验证

Analyst 输出进入 Backlog 前，Coordinator 验证目标文件确实位于仓库或允许的 agent worktree 中，并将路径规范化为仓库相对路径。不存在或越界的文件会被丢弃并计入 phantom issue。

Backlog 使用 `(file_path, line, issue_type)` 去重，且会与已完成和已跳过的问题一起比较。保留 `SKIPPED` 状态而不是删除条目，是为了阻止不可行问题在后续扫描中被反复加入。

### 7.3 尝试历史

每个 issue 都有独立的 `issues/<ISSUE-ID>/history.jsonl` 和候选 patch 文件。历史记录保存策略、结果、penalty 前后值、失败原因、patch fingerprint 和相关路径。

同一 issue 默认最多被分派三次，每次 Programmer dispatch 内默认最多调用 Merge Gate 三次。再次分派时，Programmer 会收到最近失败反馈，并被要求阅读历史、避免重复已经验证失败的策略。完全相同的 patch 会在重新运行静态分析、构建和测试之前被识别并拒绝。

---

## 8. 智能体层

### 8.1 Orchestrator

Orchestrator 只在两个决策点被同步调用。

#### 任务分派

输入包括当前和初始 penalty、停滞计数、逐指标剩余惩罚、空闲智能体以及压缩后的 Backlog。默认情况下，模型看到：

- 所有 `IN_PROGRESS` issue；
- 全部状态数量；
- 估计影响最大的 24 个 `TODO` issue；
- 被省略的 TODO 数量。

完整 Backlog 仍保存在本地，并不因 prompt 压缩而删除。Orchestrator 需要遵守以下分派约束：同一文件不能同时交给多个 Programmer；相关 issue 尽量交给同一个 Programmer；每个 Programmer 一次最多接收两个 issue；Analyst 主要在没有现成 TODO 或系统接近停滞时运行。

Coordinator 不直接信任模型输出。所有 assignment 都会再次与最新 Backlog 状态、空闲 agent、已占用文件和分派上限核对。

#### 卡死评估

Programmer 的模型工作时间超过软阈值后，Orchestrator 会看到运行时间、已分派 issue、是否发生编辑、Merge Gate 调用次数以及最近日志。它可以决定继续、终止，或将部分 issue 标记为不可行。

DeepSeek 模式下，Orchestrator 使用强制工具调用和 JSON Schema 返回结构化结果，并显式关闭 thinking；Anthropic 模式保留兼容的文本协议解析。

### 8.2 Analyst

Analyst 在 detached worktree 中读取当前 integration branch 的已接受状态。每次扫描前 worktree 会同步到最新集成版本，因此 Analyst 不会把另一个 Programmer 尚未合并的临时修改报告为问题。

为了降低 Token 消耗，Coordinator 从当前 Lizard 和 cognitive 记录中生成非权威的本地线索。线索按预计可消除 penalty 排序，每次 Analyst dispatch 最多提供 15 条，多个并发 Analyst 得到互不重叠的页面。线索覆盖 `ccn`、`nloc`、`param` 和 `cognitive`；重复代码仍由 Analyst 结合对应工具判断。

本地线索不能直接进入 Backlog。Analyst 必须阅读函数并确认其适合非功能性重构，然后按以下协议输出：

```text
ISSUE: <file>:<line> - <severity> - <type> - <metric values and explanation>
```

只有 CLI 正常退出且日志包含成功的终端 result 时，该页线索才会被记为已检查。崩溃或截断会释放线索，以供下一次 dispatch 重试。对于只有一个线索且模型明确确认、但遗漏 `ISSUE:` 行的情况，存在严格受限的恢复路径；多线索自由文本不会被自动转成 issue。

### 8.3 Programmer

每个 Programmer 在独立 feature branch 和 worktree 中工作。一次可以收到一到两个 issue，但必须按顺序一次处理一个。标准流程为：

1. 将 feature branch 同步并清理到最新 integration branch；
2. 阅读目标函数、调用者和相关依赖；
3. 实施非功能性重构；
4. 自行提交修改；
5. 调用 Merge Gate；
6. 根据 Gate 的结构化结果修复、重试或结束；
7. 输出 `RESULT:` 协议行。

成功格式为：

```text
RESULT: <ISSUE-ID> - done - merged at penalty <before> -> <after>
```

失败或不可行格式为：

```text
RESULT: <ISSUE-ID> - skipped - <reason>
```

Programmer 报告的成功不会直接改变 Backlog。Coordinator 必须找到同一 agent、同一 issue、相同 penalty 数值的成功 Gate 记录，才能确认 merge。如果 Gate 已经成功，但模型在输出 `RESULT:` 前结束，系统可以从权威 Gate 日志恢复结果，避免重复付费和重复构建。

### 8.4 Agent 运行环境

Analyst 和 Programmer 以 headless Claude CLI 子进程运行。默认允许 `Bash`、`Edit`、`Write`、`Read`、`Glob` 和 `Grep`，并使用 bare、无会话持久化的模式，以隔离用户记忆、项目 hooks、插件和先前会话。

子进程具有独立进程组。终止 agent 时，系统同时终止其派生的构建、Git 或分析进程，避免孤儿进程继续消耗资源。

---

## 9. Git 与并发模型

### 9.1 每次运行的分支

一次运行创建独立 integration branch：

```text
refactor/<run-id>
```

该分支从固定 baseline commit 创建。目标仓库的主分支不会在实验过程中被推进。运行开始前系统记录用户原来的 branch 或 detached HEAD；结束时恢复该精确 checkout 身份并验证 commit。

同一 `run-id` 已有证据文件时，新运行会被拒绝，除非显式使用 `--resume`。运行分支默认只保留在本地；`--push-run-branch` 才会写入远程仓库。

### 9.2 Worktree

每个 Programmer 拥有一个 feature branch worktree；每个 Analyst 拥有一个 detached worktree。对于独立子目录且构建不依赖仓库其他部分的项目，可以使用 sparse checkout。若原生 build/test 需要完整仓库，则使用 full worktree，但目标修改范围仍保持不变。

### 9.3 并发集成

Merge Gate 每次都将 Programmer 分支 rebase 到本地 integration branch。该本地引用会立即反映其他 Programmer 已经完成的 fast-forward merge。若 rebase 冲突，Gate 保留冲突状态，让 Programmer 解决后重新运行。

测试通过后 Gate 尝试 fast-forward integration branch。如果两个 Programmer 在相近时间完成，后者可能输掉竞争。此时 Gate 重新执行完整的 rebase、penalty、build 和 test 流程，确保最终被合并的组合确实在最新代码状态上验证过。

---

## 10. Merge Gate

Merge Gate 是候选修改的唯一接受入口。其配置文件位于结果目录而不是目标 worktree 中，避免 `git clean` 删除配置或配置文件本身使 worktree 被判定为脏。

### 10.1 验证顺序

| 阶段 | 检查 | 失败后的行为 |
|---:|---|---|
| 0 | worktree 是否还有未提交修改 | 拒绝；要求 Programmer 先提交 |
| 1 | rebase 到最新 integration branch | 保留冲突，交由 Programmer 解决 |
| 2 | 修改是否越出 `target_subdir` | 拒绝，不执行项目代码 |
| 3 | 是否为该 issue 已测试过的相同 patch | 拒绝，避免重复分析和构建 |
| 4 | 重新测量 integration 与 candidate penalty | 分析异常时 fail-closed |
| 5 | candidate penalty 是否严格更低 | 若没有下降，回退 candidate |
| 6 | 执行原生 build | 失败时保留修改，允许 Programmer 修复 |
| 7 | 执行原生 correctness test | 失败时保留修改，允许 Programmer 修复 |
| 8 | fast-forward integration branch | 竞争失败则重新执行完整验证 |

Penalty 不下降和 build/test 失败采用不同策略。前者说明候选不满足优化目标，因此直接回退；后者可能只是当前重构中的可修复错误，因此保留候选让 Programmer 继续修正。

### 10.2 Outcome

Gate 为不同失败点设置明确 outcome，包括：`merged`、`penalty_rejected`、`build_failed`、`test_failed`、build/test timeout、进程中断、`rebase_conflict`、fast-forward race、`analysis_failed`、`out_of_scope`、`uncommitted`、`attempt_limit` 和 `duplicate_patch`。

每次完整验证尝试都会写入 `gate_attempts.jsonl`。这使失败计数不依赖自然语言日志解析，并允许多个 Gate 进程安全追加。

---

## 11. 主循环与状态变化

Coordinator 默认每秒执行一次主循环：

```text
drain message queue
→ reconcile agent messages with authoritative gate evidence
→ reap completed futures
→ check token/cost budget
→ stop-condition checks
→ dispatch useful work if available
→ evaluate stuck agents and timeouts
```

主循环只在确实存在可用工作时调用 Orchestrator。例如，没有 TODO、没有理由重新扫描且没有接近停滞时，会跳过同步 API 调用。Analyst 以完整 wave 方式分派，不会因为某个 Analyst 提前结束就立即填补单个槽位，从而避免由调度时序引起的额外扫描和 Token 消耗。

一个成功 merge 的处理过程为：

1. 核对 Gate 日志；
2. 用 Gate 的 penalty 差更新停滞计数；
3. 在 integration branch 上重新运行全部静态分析；
4. 更新当前 penalty、指标分布和本地线索；
5. 将 issue 标记为 `DONE`；
6. 记录 penalty history；
7. 原子保存 run state。

Gate 的 penalty 用于判定该 merge 的收益，而协调层重新测得的 penalty 用于之后的全局状态。这可以避免并发 merge 后沿用过时的整体测量结果。

---

## 12. 停止条件与超时

### 12.1 停滞条件

默认最小有效 merge 收益为 10 penalty points，停滞上限为 3：

- merge 收益大于 10：停滞计数清零；
- merge 收益小于或等于 10：计数加一；
- Programmer 达到硬超时且仍有未解决 issue：计数加一；
- 计数达到 3：以 `stagnation` 停止。

被 penalty 拒绝的候选不会直接增加多智能体系统的停滞计数，但会进入失败统计和 issue 尝试历史。

### 12.2 两级 Programmer 超时

| 阈值 | 默认值 | 行为 |
|---|---:|---|
| issue 软阈值 | 10 分钟 | 请求 Orchestrator 判断 keep/terminate |
| 重评估间隔 | 5 分钟 | 防止每个 tick 重复调用 Orchestrator |
| Programmer 硬阈值 | 30 分钟模型工作时间 | 无条件终止；未解决 issue 返回 TODO；停滞计数增加 |
| active Gate 上限 | 3 小时 | 约束整个 Gate；build/test 还有各自超时 |

Programmer 的 30 分钟预算不包含正在运行的原生 Gate 时间。构建与测试可能很长，因此分别由 build、test 和 Gate 总上限控制。

### 12.3 其他停止条件

| stop reason | 含义 |
|---|---|
| `penalty_zero` | 初始化测量得到的总惩罚已经为零，因此无需进入主循环 |
| `no_actionable_work` | 连续 Analyst 扫描没有发现新问题，且无未检查线索或活动 issue |
| `orchestrator_no_progress` | 连续多次 Orchestrator 响应没有产生可执行分派 |
| `token_budget_input_tokens` / `token_budget_output_tokens` | Token dispatch ceiling 被触发且在途工作结束 |
| `cost_budget_usd` / `cost_budget_cny` | 成本 dispatch ceiling 被触发且在途工作结束 |
| `wall_timeout` | 收到 SIGTERM 后进入受控关闭；常用于外部 wall-clock 限制 |
| `baseline_build_failed` / `baseline_build_timeout` | 生产 profile 的基线预热未通过，因此在模型分派前终止 |

Analyst 默认有 15 分钟硬超时。超时会终止该进程、释放其线索页面，并记录独立事件。

---

## 13. Token 优化与成本控制

Token 优化只减少模型看到的上下文和无效调用，不改变 Backlog、Issue 协议、Merge Gate 或 penalty 数学。

### 13.1 紧凑 Backlog

Orchestrator 默认只看到影响最大的 24 个 TODO，但仍看到所有活动项和完整状态计数。排序是确定性的：先按预计 penalty reduction 降序，再按 issue ID。Prompt 明确标记展示和省略数量，避免模型误认为列表完整。

### 13.2 Analyst 线索分页

本地静态分析先找出超阈值函数，将同一函数的 `ccn`、`nloc`、`param` 和 `cognitive` 合并为一个线索，再按预计收益排序。每次最多发送 15 条；并发 Analyst 页面互斥。Analyst 仍是最终 issue 决策者，因此该优化不会把纯静态告警自动升级为重构任务。

### 13.3 无效调用边界

系统会跳过没有可分派工作的 Orchestrator 调用；连续三个无效 assignment 响应会停止运行；空扫描和 Analyst wave 也有明确边界。这些机制防止协议异常或无任务状态造成无限模型调用。

### 13.4 Token 会计

系统规范化 Anthropic、DeepSeek 和 Claude CLI 的不同 usage 格式，并区分：

- 未缓存输入 Token；
- cache creation Token；
- cache read Token；
- 输出 Token；
- 每个角色、agent、dispatch 的总量；
- 每次成功 merge 的 Token；
- reported、estimated 和 effective cost。

DeepSeek 的 cache hit/miss 字段替代而不是叠加 prompt 总数，以避免重复计账。Claude CLI 优先使用最终 result 事件中的会话汇总；若进程在 result 前被终止，则退回累计 assistant 消息 usage。

### 13.5 Dispatch ceiling

可设置输入 Token、输出 Token、USD 或 DeepSeek 原生人民币成本上限。上限是“停止新分派”的边界，不是硬中断：已经运行的 agent 会完成，最终超出量会被记录。DeepSeek CLI 默认成本上限为 300 CNY；其他 Token 和成本上限默认关闭。

---

## 14. Provider 与模型

系统支持 Anthropic 和 DeepSeek。两者的 Orchestrator 都通过兼容 Anthropic 协议的 SDK 调用；Analyst 和 Programmer 使用 Claude CLI。

| 配置 | Anthropic 默认 | DeepSeek 默认 |
|---|---|---|
| Orchestrator model | `claude-opus-4-7` | `deepseek-v4-pro` |
| Analyst/Programmer model | `claude-opus-4-7` | `deepseek-v4-pro[1m]` |
| API endpoint | SDK 默认 | `https://api.deepseek.com/anthropic` |
| Key 环境变量 | `ANTHROPIC_API_KEY` | `DEEPSEEK_API_KEY` |

密钥值只在运行时从环境变量读取，不进入命令行参数、Config、Gate 配置或结果摘要。项目根目录的 `.env` 会自动加载，但不会覆盖 shell 中已经存在的变量。

模型、provider、endpoint 和价格快照都属于实验配置。比较不同运行时，应保证这些配置一致，或将差异明确作为实验变量报告。

---

## 15. Production Profiles

### 15.1 FerretDB

| 项目 | 配置 |
|---|---|
| 语言 | Go |
| 测量与修改范围 | 完整仓库 |
| Worktree | 完整 worktree |
| 圈复杂度/NLOC/参数 | Lizard |
| 认知复杂度 | gocognit |
| 重复代码 | mibk/dupl，默认阈值 100 syntax tokens |
| Build | 仓库原生、race-enabled compile-only 验证 |
| Test | 仓库范围 short unit-test suite |

### 15.2 MongoDB Query

| 项目 | 配置 |
|---|---|
| 语言 | C/C++ |
| 测量与修改范围 | `src/mongo/db/query` |
| Worktree | 完整仓库，以满足 Bazel 依赖 |
| 圈复杂度/NLOC/参数 | Lizard |
| 认知复杂度 | cognitive-complexity |
| 重复代码 | Duplo，默认 `-ml 4 -ip` |
| Build/Test target | 仅 `//src/mongo/db/query/...` |
| 预热 | 在任何付费模型分派前构建 immutable baseline，并使用共享 Bazel disk cache |

MongoDB profile 即使使用完整 worktree，也不允许 agent 修改或直接选择 Query 子模块之外的目标。Bazel 自行编译的传递依赖不视为扩大实验范围。

Production profile 的范围、语言、worktree 模式和原生验证命令不能被普通 CLI 参数覆盖。启动时还会拒绝 tracked 修改和未列入允许清单的 untracked 文件。

---

## 16. 运行生命周期

### 16.1 初始化

一次全新运行依次执行：

1. 校验配置、provider、模型定价、工具和 build/test 命令；
2. 拒绝已经有结果但未使用 `--resume` 的 `run-id`；
3. 记录用户当前 checkout；
4. 解析固定 baseline commit；
5. 创建 `refactor/<run-id>` integration branch；
6. 为 Programmer 和 Analyst 创建隔离 worktree；
7. 初始化 Backlog、日志和 Gate 配置目录；
8. 执行基线静态分析，保存 penalty、breakdown、分布和本地线索；
9. MongoDB profile 预热 baseline Bazel cache；
10. 启动两个线程池并进入主循环。

### 16.2 正常运行

系统通常先在空 Backlog 状态派发 Analyst。Analyst 报告问题后，Coordinator 将其验证、去重并保存。随后 Orchestrator 把高影响 issue 分配给空闲 Programmer。每个成功 merge 都触发新的全局静态测量，使下一轮 Analyst 和 Orchestrator 使用最新质量状态。

### 16.3 关闭

停止后系统会：

1. 停止新 dispatch；
2. 终止并回收仍在运行的 agent 进程组；
3. 处理最后的 Gate 证据和队列消息；
4. 保存状态、penalty history、plot、Token 和最终摘要；
5. 删除 agent worktree；
6. 可选 push 运行分支；
7. 恢复用户原始 checkout。

运行分支是代码结果的永久记录；结果目录是过程和测量证据的永久记录。

---

## 17. 崩溃恢复

`--resume` 不会把 integration branch 重置到 baseline，而是继续已有运行分支。系统恢复：

- 原始 baseline commit 和 baseline penalty；
- 当前 integration branch；
- 停滞计数和 merge 数；
- Backlog 和被遗弃的 `IN_PROGRESS` issue；
- penalty history 和原始开始时间；
- agent 日志 dispatch 编号；
- 已成功检查的 Analyst 线索；
- Token/cost dispatch closure 状态；
- baseline 指标分布和构建预热状态。

恢复时重新测量当前 integration branch，但继续使用原始 baseline 计算整次运行的改善幅度。

系统使用 optimization fingerprint 绑定影响实验可比性的配置，包括仓库范围、工具、阈值、权重、模型、provider、分页、预算、验证命令和价格快照。当前 fingerprint schema 为 7。若 fingerprint 不一致，恢复会被拒绝，运行者必须使用新的 `run-id`。

---

## 18. 结果与实验产物

每次运行写入：

```text
<work-root>/results/<run-id>/
```

主要产物如下。

| 文件 | 用途 |
|---|---|
| `run_summary.json` | 最终摘要；优先用于跨运行统计 |
| `penalty_history.json` | baseline、每次 merge、timeout 和停止事件的时间序列 |
| `penalty.png` | penalty 随时间变化的阶梯图 |
| `backlog.json` | 最终 issue 状态和分派次数 |
| `run_state.json` | 崩溃恢复状态 |
| `gate_attempts.jsonl` | 每次完整 Gate 尝试及 outcome |
| `token_usage.json` | Token、cache、成本、coverage 和每次 merge 成本 |
| `orchestrator_usage.jsonl` | 每次 Orchestrator 调用的紧凑 usage 记录 |
| `orchestrator_raw_responses.jsonl` | Orchestrator 原始响应和实际 parser 输入 |
| `logs/<AGENT>_<nnn>.log` | 每个 agent 每次 dispatch 的完整 stream-json 日志 |
| `issues/<ISSUE-ID>/history.jsonl` | 单个 issue 的跨 dispatch 尝试历史 |
| `issues/<ISSUE-ID>/patches/*.patch` | 候选 patch 证据 |
| `gate_configs/*.json` | 每个 Programmer 的 Gate 配置 |

`run_summary.json` 至少包含以下分析维度：

- baseline/final penalty、下降量和下降百分比；
- merge 数与 stop reason；
- baseline/final 指标分布和均值变化；
- 清零的指标；
- Gate outcome 和系统失败计数；
- Backlog 状态数量；
- Token、cache、成本及每次成功 merge 的消耗；
- Token/cost ceiling 的触发与 overshoot；
- 实际使用的完整配置和 baseline commit。

比较多次实验时，应先按 `baseline_commit`、provider、两个模型、阈值、权重、目标范围、build/test 命令和工具版本筛选可比运行，再比较 penalty、分布、失败率、时间和 Token 成本。

---

## 19. 安装与验证

### 19.1 Python 环境

在项目目录中创建虚拟环境并安装依赖：

```bash
cd writing/experiment_token_save
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Go profile 还需要安装固定版本的 Go 工具：

```bash
GOBIN="$PWD/.venv/bin" go install github.com/uudashr/gocognit/cmd/gocognit@v1.2.1
GOBIN="$PWD/.venv/bin" go install github.com/mibk/dupl@v1.1.0
```

C/C++ profile 使用项目 `bin/duplo` 中的可执行文件。运行前应确认其适用于当前操作系统。

### 19.2 本地验证

完整的无 API-key 验证命令为：

```bash
./.venv/bin/python tests/run_all.py
```

当前 `run_all.py` 包含 11 个套件，覆盖 Merge Gate、运行隔离、指标统计、结果产物、卡死策略、失败计数、issue 尝试历史、provider 配置、Token 会计、Go 认知复杂度和 Token 优化。

这些测试使用临时 Git 仓库或 stubbed LLM，不会运行外部 MongoDB 服务。真实模型 smoke tests 和真实仓库 E2E 不属于默认套件，应按需单独执行。

---

## 20. 运行实验

### 20.1 凭证

可以导出环境变量：

```bash
export ANTHROPIC_API_KEY=...
# 或
export DEEPSEEK_API_KEY=...
```

也可以在项目根目录 `.env` 中配置。不要把密钥作为 CLI 参数传入。

### 20.2 Production profile

FerretDB：

```bash
./.venv/bin/python main.py \
  --profile ferretdb \
  --provider deepseek \
  --run-id ferretdb_complete_01
```

MongoDB Query：

```bash
./.venv/bin/python main.py \
  --profile mongodb-query \
  --provider deepseek \
  --run-id mongodb_query_complete_01
```

MongoDB 命令只测量和授权修改 `src/mongo/db/query`，且 build/test 只选择 Query 子模块目标。它可能需要较长 Bazel 冷启动时间和允许本地 loopback listener 的执行环境。

### 20.3 自定义仓库

自定义运行必须显式提供真实 build 和 test 命令：

```bash
./.venv/bin/python main.py \
  --repo /absolute/path/to/repository \
  --subdir src/component \
  --work-root /absolute/path/to/experiment-work \
  --language cpp \
  --duplo-binary "$PWD/bin/duplo" \
  --build-cmd 'your build command' \
  --test-cmd 'your test command' \
  --provider anthropic \
  --run-id custom_complete_01
```

如果 build/test 依赖目标子目录之外的文件，应加 `--full-worktree`。这不会扩大允许修改的 `--subdir`。

### 20.4 指标权重实验

完整模型使用默认权重，无需传 `--weights`。

仅优化 CCN：

```bash
--weights '{"ccn":1,"cognitive":0,"nloc":0,"param":0,"duplicates":0}'
```

从完整模型中移除重复代码：

```bash
--weights '{"ccn":1,"cognitive":1,"nloc":1,"param":1,"duplicates":0}'
```

配置键必须使用 `nloc`，不能使用 `lloc`。未知指标键会在启动时被拒绝。

### 20.5 Token 与成本边界

示例：

```bash
--max-run-input-tokens 2000000 \
--max-run-output-tokens 200000 \
--max-run-cost-cny 300
```

同一次运行不能同时启用 USD 和 CNY 成本上限。成本上限依赖项目中固定的 provider/model 价格表；没有经过审计定价的模型不能用于可复现成本 ceiling。

### 20.6 恢复与归档

恢复中断运行：

```bash
./.venv/bin/python main.py \
  --profile mongodb-query \
  --provider deepseek \
  --run-id mongodb_query_complete_01 \
  --resume
```

需要将运行分支推送到目标仓库远端时，首次运行加入：

```bash
--push-run-branch
```

由于该选项会产生外部写入，默认关闭。

### 20.7 重复运行设计

若需要对同一配置执行多次独立运行，每次使用不同 `run-id`，并保持相同 baseline ref、provider、模型、阈值、权重、目标范围和验证命令。例如：

```text
mongodb_query_complete_01
mongodb_query_complete_02
...
mongodb_query_complete_10
```

每次运行都会从相同 baseline 创建独立 integration branch，因此不应将前一次运行的最终分支作为下一次运行起点。

---

## 21. 如何解释一次运行

推荐按以下顺序审计结果：

1. 查看 `run_summary.json.config`，确认实验范围、模型、权重和验证命令；
2. 查看 `baseline_commit` 与 `baseline_build`，确认起点和运行环境有效；
3. 查看 `stop_reason`，判断是自然完成、停滞、预算还是协议问题；
4. 比较 baseline/final penalty，但同时检查逐指标分布；
5. 查看 `failures`，确认降低 penalty 的过程中是否伴随大量拒绝、冲突或测试失败；
6. 查看 `tokens.tokens_per_successful_merge` 和成本，而不是只比较总 Token；
7. 对异常运行检查 `gate_attempts.jsonl`、issue history 和 agent logs；
8. 只有实验配置和 baseline 一致时，才聚合不同运行。

Penalty 下降代表系统按定义的静态指标改善，并不自动证明可读性、可维护性或运行性能在所有意义上改善。因此，对最终代码仍应结合人工审查、原生测试结果以及必要的独立性能测量进行解释。

---

## 22. 当前限制

1. 当前项目只实现多智能体系统；没有单智能体对照。
2. 静态指标是可计算的代理目标，不覆盖所有软件质量维度。
3. Java/Python 当前缺少认知复杂度 backend。
4. 性能 benchmark 尚未接入当前 Merge Gate。
5. Token ceiling 基于 provider 已上报用量，因此允许在途任务造成可见 overshoot。
6. Production profile 固定了本机仓库路径；在其他环境部署时需要修改 profile 或使用自定义配置。
7. 通过 stubbed/fake-CLI 测试证明的是协调和验证逻辑，不等于证明真实模型在大规模任务上的判断质量。
8. 跨 provider 或跨模型比较必须控制模型能力、价格、缓存语义和上下文长度差异。

---

## 23. 代码索引

需要追踪具体实现时，可从下表进入源码。

| 主题 | 入口文件 |
|---|---|
| CLI 和 `.env` | `main.py` |
| 全部配置、默认值和校验 | `config.py` |
| FerretDB/MongoDB 锁定范围 | `production_profiles.py` |
| 主循环和生命周期 | `coordination/coordinator.py` |
| Backlog schema 与原子持久化 | `coordination/backlog.py` |
| Git branch/worktree | `coordination/git_manager.py` |
| 停滞计数 | `coordination/stagnation.py` |
| Gate 尝试和失败统计 | `coordination/gate_attempts.py` |
| Issue 尝试历史 | `coordination/issue_history.py` |
| Token 与成本 | `coordination/token_usage.py`, `coordination/model_pricing.py` |
| Orchestrator | `agents/orchestrator.py` |
| Analyst | `agents/analyst.py` |
| Programmer | `agents/programmer.py` |
| CLI 子进程和日志流 | `agents/agent_runner.py`, `agents/log_parser.py` |
| Provider 环境隔离 | `agents/provider.py` |
| 静态分析 | `analysis/tools.py` |
| Penalty | `analysis/penalty.py` |
| 分布统计 | `analysis/metrics.py` |
| 本地 Analyst 线索 | `analysis/candidates.py` |
| Merge Gate | `merge_gate/gate.py`, `merge_gate/cli.py` |
| 原生 production 验证 | `scripts/production_validation.py` |
| 验证套件清单 | `tests/run_all.py` |
