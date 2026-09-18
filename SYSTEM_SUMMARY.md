# 多智能体重构实验系统 — 复现级总结

本文件是 `writing/experiment_token_save/` 的完整系统总结,目标是:**只读本文件即可从零复现该系统的架构、数学、协议、参数与实验流程**。

内容来源:该目录下全部源码(21 个模块,约 5,600 行实现 + 约 3,700 行测试)、`README.md`、`overview.md`、`prompts/*.txt`,以及 `live_smoke_results/` 中已产生的真实运行产物。所有数字均在 2026-07-31 由本人实测核对(见 §17、§18)。

- 论文事实来源:`../Master_Thesis 15-04-2026.pdf`,Method 章 pp. 19–33,Results 章 pp. 34–40。
- 本目录是 **token 节省变体**,`writing/experiment` 是对照(control)。差异见 §14。

---

## 1. 系统做了什么(一句话到一段话)

系统把「代码质量重构」建模为**对一个静态分析惩罚分数(penalty)的持续下降优化过程**,由一个多智能体循环驱动:

```
Analyst(发现问题) → Backlog(产品待办) → Orchestrator(分派决策)
   → Programmer(在隔离 worktree 内重构 + 提交) → Merge Gate(评估 + 集成)
   → 重新测量 penalty → 直到停滞判据触发停止
```

具体交付了以下四件事:

1. **一个可运行的完整多智能体系统**(论文 §4.3.2 的多智能体系统),默认 1 orchestrator + 3 analysts + 3 programmers,以真实 git 仓库为目标,真实调用 Lizard / cognitive-complexity / Duplo,真实执行 build/test,真实做 fast-forward 集成。
2. **论文惩罚数学的精确实现与校准**(Eq 4.3/4.4/4.5),并对论文已发表的 4 个数值锚点做了回归校验。
3. **论文结果章所需数据的完整采集管线**:每次 penalty 变化事件的时间序列、基线/最终指标分布(Table 4.1 / 5.1 形状)、失败计数(§5.1/§5.4)、token 会计、每次 gate 尝试的逐条记录。
4. **可复现性基础设施**:per-run 集成分支协议(§4.4)、崩溃恢复、原子持久化、10 个免 API-key 验证套件、2 类免费 E2E 冒烟测试、1 类付费真实模型有界验证。

**明确不在范围内**:论文 §4.3.1 的**单智能体系统**是作者显式决定不实现的。因此论文中所有单/多系统对比(Table 5.1 的 single-vs-multi、§5.2.1/5.2.2、§5.3.1/5.3.2)超出本仓库职责范围。这是范围决策,不是缺失功能。

---

## 2. 环境与依赖(精确版本)

| 组件 | 实测版本 | 说明 |
|---|---|---|
| Python | 3.13.13 | 3.13+ 强制要求(`modified_cognitive_complexity` 要求) |
| Lizard | 1.22.1 | CCN / NLOC / 参数个数 |
| Duplo | 2.2.0 | 重复块检测;二进制已提交在 `bin/duplo` |
| `modified_cognitive_complexity` | git HEAD | SonarSource 认知复杂度,C/C++ 专用,基于 Tree-sitter |
| `gocognit` | 1.2.1 | SonarSource 风格的 Go 函数/方法认知复杂度 |
| `anthropic` SDK | ≥ 0.40.0 | 仅 orchestrator 使用 |
| `matplotlib` | ≥ 3.8 | penalty 曲线图;缺失则跳过绘图,运行照常完成 |
| `claude` CLI | 需在 PATH | analyst/programmer 子进程运行时 |

`requirements.txt` 内容:

```
anthropic>=0.40.0
lizard>=1.17.10
matplotlib>=3.8
modified_cognitive_complexity @ git+https://github.com/fkie-cad/cognitive-complexity-mod.git
```

安装:

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
GOBIN="$PWD/.venv/bin" go install github.com/uudashr/gocognit/cmd/gocognit@v1.2.1

# Duplo 二进制(按 OS 选择 release)
mkdir -p bin
curl -sL https://github.com/dlidstrom/Duplo/releases/latest/download/duplo-macos.zip -o /tmp/duplo.zip
unzip -o /tmp/duplo.zip -d bin/ && chmod +x bin/duplo
```

**凭证**:`main.py` 启动时自动读取项目根 `.env`(`load_project_dotenv`,`main.py:20-57`)。已存在的 shell/CI 环境变量优先。`.env` / `.env.*` 被 gitignore,`.env.example` 保留在版本控制。密钥值**从不**进入 argv、`Config`、gate config 或任何结果产物 —— 代码只存储**环境变量名**(`Config.api_key_env`)。

---

## 3. 模块地图

```
config.py            (317 行) 全部可调项 + 派生路径 property
main.py              (264 行) argv → Config;除参数解析外无逻辑

coordination/        确定性 Python,拥有全部共享状态,跑在主线程
  coordinator.py     (1574 行) 主循环:drain → 对账 → reap → dispatch → stuck-check
  backlog.py         (150 行)  产品待办、原子持久化、去重
  message_queue.py   (29 行)   类型化 agent → coordinator 消息
  git_manager.py     (220 行)  集成分支、worktree、基线恢复
  stagnation.py      (30 行)   停滞停止判据
  penalty_history.py (160 行)  penalty 时间序列 + 绘图
  gate_attempts.py   (151 行)  逐次 gate 尝试日志 → 失败计数
  run_state.py       (69 行)   崩溃恢复状态
  token_usage.py     (389 行)  provider 中立的 token 会计

agents/
  orchestrator.py    (237 行)  两个同步 LLM 决策点(SDK 直连)
  provider.py        (77 行)   Anthropic/DeepSeek client + 子进程环境
  analyst.py         (263 行)  claude CLI 会话;输出 ISSUE: 行
  programmer.py      (339 行)  claude CLI 会话;重构、调用 gate
  agent_runner.py    (145 行)  子进程启动 + stream-json 日志流
  log_parser.py      (60 行)   stream-json → assistant 文本

analysis/
  penalty.py         (209 行)  Eq 4.4 / Eq 4.5、分解、影响估计
  metrics.py         (139 行)  分布统计(Table 4.1 / 5.1)
  candidates.py      (134 行)  保守的 CCN/NLOC/param 本地线索
  tools.py           (392 行)  Lizard / cognitive / Duplo 封装、文件选择

merge_gate/
  gate.py            (389 行)  rebase → penalty → build → test → ff-merge
  cli.py             (108 行)  programmer 从 worktree 内调用的入口

prompts/             orchestrator_assignment / orchestrator_stuck / analyst / programmer
bin/duplo            Duplo 二进制
tests/               10 个免 API-key 套件 + 4 个可选 E2E/冒烟 + 3 个 fake CLI 辅助
```

**两条边界承载了全部设计**:

1. **`coordination/` 是共享状态的唯一写者**。agent 从不修改 backlog,只投递消息后继续。
2. **`merge_gate/` 作为独立进程**在 programmer 的 worktree 中运行,完全通过一个 JSON 文件配置。因此它**无法**触达消息队列 —— 这正是它把每次尝试追加写入文件的原因(§10)。

---

## 4. 目标函数(核心数学)

实现在 `analysis/penalty.py`。

### 4.1 公式

```
逐函数指标(Eq 4.4):   p_m(x)   = 100 · (1 − T_m / max(T_m, x))      x ≤ T_m 时为 0
代码库级重复率(Eq 4.5): p_dup(r) = 100 · r / (r + k),   k = 0.1        无阈值
```

总 penalty = 所有函数所有指标的**加权和** + 重复率惩罚。

### 4.2 阈值与权重(默认值)

| 键 | 阈值 `thresholds` | 权重 `weights` | 论文对应 |
|---|---|---|---|
| `ccn` | 15 | 1 | T_CCN = 15 |
| `nloc` | 30 | 1 | T_LLOC = 30(论文叫 LLOC) |
| `cognitive` | 15 | 1 | T_Cog = 15 |
| `param` | 5 | 1 | T_Param = 5 |
| `duplicates` | 0(占位) | 1 | 用 Eq 4.5,无真实阈值 |

阈值来自 §4.5.1,取自文献而非从代码库推导。

### 4.3 两个必须复现的实现细节

1. **Lizard 记录与 cognitive 记录保持为两个独立列表,不做 join**(`penalty.py:48-68`)。cognitive 工具能看到但 Lizard 看不到的函数(反之亦然)仍独立贡献自己的 penalty。做 join 会静默丢失 penalty。`tools.run_static_analysis` 里确实有一个 join,但注释明确写着「optional join for debug/inspection — does not change penalty math」。
2. **权重设为 0 = 完全移除该指标**。这就是 Eq 4.3 中 `z_m` 的实现机制,也是两类消融实验的配置方式。

### 4.4 校准锚点(改动惩罚数学后必须仍然成立)

四项全部由论文发表,并由 `tests/test_metric_stats.py` 校验:

| 锚点 | 论文值 | 代码值 |
|---|---|---|
| Eq 4.5 在 r = 2.41 % | 19.4 | 19.42 |
| Eq 4.4,cognitive {19, 16} | 27.3 | 27.30 |
| 基线分解:LLOC 811.2 · CCN 292.7 · Cog 27.3 · Dup 19.4 · Param 16.7 | 合计 1167.28 | 合计 1167.3 |
| Table 4.1 cognitive 行:mean 1.32 / median 0 / max 19 | — | 由合成总体复现 |

> **⚠️ 最容易导致复现失败的单点:重复率是分数,不是百分数。**
> `p_dup(0.0241)` = 19.4(与论文一致);`p_dup(2.41)` = 96.02。这一个单位混淆会把重复惩罚静默放大约 5 倍。

### 4.5 惩罚分解与 backlog 影响估计

- `compute_penalty_breakdown()`(`penalty.py:72`)把同一总数按指标拆开,返回 `{metric: {penalty, violations, worst}}`。其逐指标 penalty 之和**精确等于** `compute_total_penalty()` 在同一输入上的结果(由 `tests/test_merge_gate.py` 断言)。这就是论文要求注入 orchestrator/analyst prompt 的「按改进潜力排序的逐指标分解」。某指标的「改进潜力」正好等于它当前的 penalty 贡献,因为把所有函数压到阈值以下会使其归零。
- `estimate_reduction_from_message()`(`penalty.py:172`)从 analyst 的自由文本中解析指标值,估算修复能移除多少 penalty(假设 programmer 把每个指标恰好压到阈值)。正则同时接受 `CCN=27` 和 `161 NLOC` 两种写法,因为 analyst prompt 的示例本身混用了两种风格。别名表把 `lloc` 映射到 `nloc`,`params/parameter(s)` 映射到 `param`。
- 结果 ≥ `min_merge_gain` 标记为 `high` impact,否则 `low` —— 这就是 orchestrator 排序的依据。
- 同一份实现被 merge gate 和 backlog 影响估计器共享,且遵守同一份权重字典,所以两者不可能漂移。

---

## 5. 测量流水线

实现在 `analysis/tools.py`。`run_static_analysis()` 返回 `(lizard_records, cognitive_records, DuplicationResult)`。**三个工具接收同一份文件列表**,所以重复率不可能在与逐函数指标不同的总体上测量。

### 5.1 Lizard

```bash
lizard -l <lang> --csv -f <filelist>      # timeout 600s
```

CSV 列序:`nloc, ccn, token, param, length, location, file, name, long_name, start_line, end_line`。代码取 `row[0]=nloc, row[1]=ccn, row[3]=param, row[6]=file, row[7]=name, row[9]=start_line`,并要求 `len(row) >= 11`(`tools.py:196-211`)。

> **`--csv` 是承重的。** Lizard 默认表格输出会在结尾的 `!!!! Warnings !!!!` 段落里**重复打印每个触发它自身警告阈值(CCN > 15)的函数**。逐行扫描该输出会把恰好携带 penalty 的那些函数**双计**,从而同时抬高总数、逐指标分解、backlog 影响估计和每一项分布统计。`--csv` 每个函数只输出一行,且给路径加引号,因此含空格的文件名也能正确解析。`tests/test_metric_stats.py` 有回归守卫。

文件列表写入临时文件传给 `-f`(而非 stdin),结束后 unlink。

### 5.2 认知复杂度

C/C++ 逐文件调用 `cognitive_complexity_for_file()`。Go 使用 `gocognit -json`,将与 Lizard/Duplo 相同的过滤后生产文件集合分批传入,因此目录排除和同目录 `*_test.go` 排除不会漂移。两种语言都生成独立的 `{file,line,name,cognitive}` 总体并进入同一个 penalty 公式。工具缺失、非零退出或 JSON 异常时 gate fail closed;其他语言的 cognitive 总体为空。

### 5.3 Duplo

```bash
duplo -ml <n> -ip - -        # 文件列表走 stdin,timeout 300s
```

解析其文本摘要的三个计数器:

```
Lines of code: N
Duplicate lines of code: N
Total N duplicate block(s) found.
```

三个非显然约束:

- **必须用绝对路径**。Duplo 对相对路径会静默报告 0 行,所以 `run_duplo` 对每个路径调用 `.resolve()`。
- **解析文本摘要而非 `-json`**。§4.2.2 提到 Duplo 被选中部分是因为它有 JSON 输出,但该输出**只有块列表,没有行数总计**,因此**无法从中推导重复行比率**。Duplo 自己的 `Lines of code` 是过滤后计数(`-ip` 下丢弃预处理指令、`-mc` 下丢弃过短行),外部无法重建;而且两种模式互斥 —— 传 `-json` 会完全抑制摘要。只有解析摘要才能复现 2.41 % → 19.4 的校准。Table 4.1 与比率并列报告的块数也在摘要里。
- **`-ml` 默认 4,即 Duplo 自身默认值**。论文从未指定最小块大小,所以工具默认是「假设最少」的重建。提高它会抑制短重复块,同时降低比率和块数。`-ip` 被设置是因为 §4.2.2 要求过滤预处理指令;其余所有 Duplo 旋钮(`-pt`、`-mc`、`-d`)一律保持默认。

`DuplicationResult` 携带 `ratio`(分数)、`duplicate_lines`、`total_lines`、`blocks`。

### 5.4 文件选择与排除

**目录排除**(§4.3.2「Test directories are excluded since they do not contribute to the penalty score」。论文用复数且从未枚举,所以这是**实验参数** `--exclude-dirs`,不是论文钉死的东西):

```
test  tests  testing  unittest  unittests  unit_test  unit_tests
gtest  googletest  gmock  mocks
```

匹配规则:**按完整路径分量**、**大小写不敏感**、原地裁剪 `os.walk` 的 `dirs`。因此 `Testing/` 被排除而 `latest/` 不受影响。`None` 表示「用默认列表」;显式空列表表示「什么都不排除」,不得退化为默认 —— 这就是 `_exclude_set()` 不能写成 `exclude_dirs or DEFAULT` 的原因,gate CLI 里同理必须用 `cfg.get("exclude_dirs")` 而非 falsy 检查。

仅匹配名为 `test` 的目录作为默认值太窄:布局为 `tests/` 的仓库其测试文件会被测量,于是 analyst 会对它们提 issue —— 而 programmer prompt 禁止改测试文件,这些 issue 只可能被 skip。

**文件名级排除**(`_is_test_source_file`,`tools.py:79-106`,README/overview 未展开的额外细节):按语言约定识别与生产代码同目录的测试文件。

| 语言 | 规则 |
|---|---|
| go | `*_test.go` |
| python | `test_*` 或 `*_test.py` |
| java | `*Test.java` / `*Tests.java` / `*TestCase.java` |
| cpp(默认) | stem 以 `test_` 开头,或以 `_test` / `_tests` / `_unittest` / `_unittests` 结尾 |

**语言 → 后缀映射**:

| `--language` | 后缀 |
|---|---|
| `cpp`(默认) | `.c .cc .cpp .cxx .h .hpp .hh .hxx` |
| `go` | `.go` |
| `java` | `.java` |
| `python` | `.py` |

### 5.5 失败即关闭(fail closed)

被配置的分析工具若**缺失可执行文件、超时、非成功退出且无有效输出、或输出无法解析**,都抛 `StaticAnalysisError`,gate 记录 `analysis_failed`。它**不能**静默变成一个更小的 penalty 从而放行一个坏的 merge。(Duplo 有文档说明的「发现重复」退出码只在其摘要计数器解析成功时才被接受。)

---

## 6. 分布统计

实现在 `analysis/metrics.py`。penalty 是 agent 的优化信号,但论文**报告**质量是通过每个指标的**分布**(§4.2.3、Table 4.1、Table 5.1)。

逐指标输出:`threshold`、`functions`(总体规模)、`mean`、`median`、`p90`、`p95`、`p99`、`max`、`over_threshold`、`cleared`。

- **统计覆盖全部函数,不只是违规函数**。Table 4.1 中 mean CCN = 3.21 对阈值 15 只有在全体总体上才有意义。
- **百分位使用最近秩之间的线性插值**(numpy 默认)。方法字符串写入 `run_summary.json` 的 `metrics.percentile_method`,以便论文引用而不是留作隐式约定。
- `cleared` = 无任何函数超过阈值(§4.5.1)。对 duplicates,`cleared` = 重复块数为 0。
- duplicates 同时携带 `line_ratio`(分数)与 `line_ratio_pct`,以及 `duplicate_lines`、`total_lines`、`block_count`。
- `mean_change_pct()` 产出 Table 5.1 括号中的百分比变化;duplicates 没有逐函数均值,故用行比率。

---

## 7. 并发与状态模型

按 §4.3.2:1 orchestrator、3 analysts、3 programmers。coordination 层跑在主线程,agent 跑在两个 `ThreadPoolExecutor`(每角色一个,大小等于池数)。

### 7.1 单写者协议

agent 把 `Message(sender, kind, payload)` 投到 `queue.Queue` 后立即继续,不等待。coordinator 在一个循环中排空队列,是 backlog、penalty history、run state 的**唯一写者**。agent 的读取一律通过 `BacklogStore.snapshot()`,返回深拷贝。

7 种消息类型:`add_issues`、`mark_done`、`mark_skipped`、`merge_result`、`programmer_finished`、`analyst_finished`、`programmer_heartbeat`。

### 7.2 主循环(`Coordinator._main_loop`,tick = `backlog_drain_interval_sec` = 1.0 s)

```
排空队列
  → 从 gate_attempts.jsonl 对账已落地的 merge(_recover_active_gate_merges)
  → 回收完成的 future
  → 检查 token 上限
  → 检查「无可做工作」停止条件
  → 需要时分派(_dispatch_if_needed)
  → 检查卡死 agent(两级)
  → sleep(1.0)
```

`_dispatch_if_needed` 在「不可能产出有用分派」时**完全跳过 orchestrator 的 LLM 调用**(`_has_actionable_work`):有空闲 programmer 且有 TODO → 需要;或有空闲 analyst 且(backlog 为空 或 接近停滞)且空扫次数未超限 → 需要;否则直接返回。这是每 tick 的热路径,使 tick 成本由 `queue.empty()` 检查主导。

### 7.3 原子持久化

backlog、penalty history、run state 全部通过 `tempfile.mkstemp` + `os.replace` 写入,所以崩溃绝不会留下半截 JSON。gate 尝试日志是唯一例外,采用追加写(§10)。backlog、state、history、logs、gate configs 全部按 `run_id` 作用域隔离,独立运行之间不可能继承彼此的 issue 或恢复状态。

### 7.4 分派时的冲突控制(`_apply_dispatch`)

- 每个 programmer 每次最多 2 个 issue(`if len(specs) == 2: break`),对应 prompt 中的 1–2 issue 规则。
- `_collect_specs` 丢弃**已不存在或状态已非 TODO** 的 id —— LLM 的分派结果必须先对当前 backlog 状态校验。
- 同文件冲突用 `_conflict_file_key()` 归一化为 repo 相对 POSIX 路径后比较,已 IN_PROGRESS 的文件被视为已占用,同一 tick 内新分派的文件也加入占用集。
- 发现耗尽后(`empty_analyst_scans >= empty_scan_limit`)**不再补充 analyst 槽位**。否则交错完成会造成无休止的滚动波次和无界 token 消耗。

---

## 8. Git 协议

实现在 `coordination/git_manager.py`。§4.4 要求两件事:10 次运行前每次都把代码库重置到原始状态;每次运行把提交记录在一条专属分支上,结束时推送该分支并把工作目录恢复到原始基线。

两者都通过**永不推进仓库主分支**同时满足:

1. `resolve_baseline_ref` — 显式 `--baseline-ref` 优先;否则优先 `origin/<main>`(让重复运行钉在已 fetch 的上游状态);无 remote 时回落到本地分支。
2. 全新运行调用 `prepare_run_branch` — **仓库不干净则拒绝启动**(不 stash、不丢弃);best-effort `git fetch origin`;解析基线 commit;在该 commit 上创建 `refactor/<run_id>`。同一 run id 不带 `--resume` 重跑会被拒绝而不是覆盖其证据(`coordinator.py:151-166` 检查 6 个产物文件是否已存在)。
3. `--resume` 走 `resume_run_branch`:检出已存在的集成分支且**不移动它**。它绝不能调用 `prepare_run_branch`(后者对全新运行的有意行为就是把分支重建到基线)。
4. `create_worktree` — 每 agent 一个 worktree,位于 `<work_root>/worktrees/<run_id>/<agent_id>`。
   - programmer 得到分支 `feature/<agent_id>`;
   - **analyst 得到 detached HEAD**,因为 git 拒绝检出已在别处检出的分支;
   - 目标是子目录时启用 sparse checkout(`sparse-checkout init --cone` + `set <subdir>`),避免为每个 agent 完整物化大仓库。
5. merge gate 只 fast-forward **集成分支**,永不动 `main`。
6. 创建运行分支前先由 `capture_checkout` 记录源仓库的精确 branch/commit/detached 状态;这些字段写入 `run_state.json`。`_finalize_run_branch` 先移除 worktree,再(可选)push,最后用 `restore_checkout` 恢复原始 branch 或 detached commit 并校验 SHA。因此 MongoDB 这种没有检出本地 `main` 的 checkout 也能无人值守运行和恢复。

**后果**:连续运行从完全相同的状态开始且**无需任何破坏性重置**(所以 `main` 上的孤立本地提交绝不会丢);run 分支作为 §4.4 要求的永久记录留存;`git reset --hard origin/main` 这种会摧毁目标仓库本地提交的操作从不需要。

**push 是 opt-in**(`--push-run-branch`)。§4.4 每次运行都 push,但它会写入目标仓库的 remote,所以默认只打印如何开启。

**`reset_worktree` 必须指向本地集成分支**。gate 在每次接受变更时 fast-forward 它且整个运行期间不 push,所以任何 remote ref 全程都是过期的 —— 重置到那里会丢弃至今接受的每一次重构。

`origin` 全程只被触碰两次:启动时 best-effort `fetch`,以及结束时可选的 push。

---

## 9. Merge Gate

`merge_gate/gate.py`,由 programmer 用启动 coordinator 的同一个 Python 解释器调用:

```bash
<venv-python> /absolute/path/merge_gate/cli.py \
  --config <results>/<run_id>/gate_configs/PROG_n.json \
  --issue-id ISSUE-nnnn
```

**config 故意放在目标 worktree 之外**:否则 `git clean -fd` 会删掉它,而留在原地又会让 gate 把 worktree 判为脏。config 内容:`repo_root`、`target_subdir`、`thresholds`、`weights`、`build_cmd`、`test_cmd`、`integration_branch`、`duplo_binary`、`duplo_min_block_lines`、`lizard_binary`、`lizard_language`、`exclude_dirs`、`allowed_untracked_paths`、`max_gate_attempts_per_issue`、`gate_record_start`、`agent_id`、`attempt_log`、`gate_python`、`gate_cli`。

### 9.1 流水线(严格跟随 §4.3.2 的 "Evaluation" 与 "Integration")

| 步骤 | 失败行为 | `outcome` |
|---|---|---|
| −1. 本次 dispatch 内该 issue 的 gate 调用次数已达上限(默认 3,由 CLI 在 build/test 之前强制) | 直接返回,要求上报 skipped | `attempt_limit` |
| 0. 拒绝未提交的工作 | gate 绝不代 programmer 暂存或提交 | `uncommitted` |
| 1. rebase 到集成分支 | **冲突原地留存** —— programmer 手工解决后重跑 | `rebase_conflict` |
| 1.5 拒绝任何提交到 `target_subdir` 之外的路径 | 全 worktree 扩大的是测试可见性,不是重构授权 | `out_of_scope` |
| 2. 测量前后 penalty | 集成分支的 penalty **每次尝试都重新测量**,所以并发 merge 不会留下过期基线 | — |
| 3. penalty 必须下降 | **回退**到重构前状态;programmer 转向下一个 issue | `penalty_rejected` |
| 4. build,然后 test | **不回退** —— programmer 读输出、改自己的代码、重跑 | `build_failed` / `test_failed` |
| 5. fast-forward merge | 输掉竞争 → 整个循环重试,最多 3 次 | `ff_merge_race` / `ff_race_exhausted` |
| 分析工具失败 | fail closed | `analysis_failed` |
| 成功 | — | `merged` |

步骤 3 与 4 的不对称来自论文:penalty 没下降意味着这次尝试毫无意义,直接丢弃;build/test 坏了意味着工作可能仍可挽救,留给 programmer 修。

**每个退出点都设置显式 `outcome` 字段**。build 失败与 test 失败在 `GateResult` 上本来无法区分(两者都留 `tests_passed=False`),而用散文 `reason` 做模式匹配来分类实验数据太脆弱。

### 9.2 若干实现细节

- `_is_dirty()` 用 `git status --porcelain=v1 --untracked-files=all -z`。`gate_allowed_untracked_paths` 里的**精确** repo 相对路径(默认空)可被忽略,但**被跟踪文件的修改永不豁免**,即使其路径恰好匹配某个允许的未跟踪构建产物。
- `_revert()` = `git reset --hard <integration_branch>` + `git clean -fd`。「重构前状态」就是集成分支当前的 tip,因为 programmer 每个 issue 都从同步到它的干净 worktree 开始。
- `_out_of_scope_paths()` 用 `git diff --name-only --no-renames --diff-filter=ACDMRTUXB <integration>...HEAD`;命令失败时 fail closed(返回「无法确定变更路径」)。`_normalized_target_subdir()` 对绝对路径或含 `..` 的目标返回 `None`(视为不安全)。
- `_fast_forward_merge()` 先在 repo_root 幂等地 `checkout <integration>`(防崩溃后残留),再 `merge --ff-only <feature-branch>`。
- `_build`/`_test` 在 worktree 内执行 `build_cmd`/`test_cmd`,只看 returncode。

> **rebase 与 revert 的目标必须是本地集成分支。** 早期版本把两者都指向 `origin/main`,却只 fast-forward 本地分支且从不 push,于是 `origin/main` 永远停在运行起点。两个后果:revert 会把 worktree 回滚到**原始基线**,丢弃全部已接受工作;并且第一次 merge 之后**第二个并发 merge 永远不可能成功**(rebase 到过期 ref → ff-merge 失败 → 3 次重试 → 放弃)。这直接违反 §4.3.2 的「replaying the programmer's commits on top of any changes merged by other programmers in the meantime」。由 `test_merge_gate.py` 场景 5 覆盖。

---

## 10. 失败计数

§5.1 要求「failure counts (reverted attempts, test failures, stagnation exits) for each model」;§5.4 要求系统偏离 —— merge 冲突、崩溃、agent 放弃。gate 是能观察到其中大部分的唯一组件,而它作为独立进程运行,所以它向 `<results>/gate_attempts.jsonl` 每次尝试追加一行:

```json
{"timestamp": 1.7e9, "agent": "PROG_1", "issue_id": "ISSUE-0001",
 "outcome": "penalty_rejected", "success": false,
 "penalty_before": 500.0, "penalty_after": 505.0, "reason": "..."}
```

选择 JSONL 而非 JSON 文档,是因为多个 programmer 从不同进程并发 gate:**POSIX 上单次 `O_APPEND` 写一条短行是原子的**(代码用裸 `os.open(O_WRONLY|O_CREAT|O_APPEND)` + `os.write` 而非 `open("a")`),所以无需加锁,并发写者也不会交错半条记录。

- **「一次 attempt」= 一整轮评估**(rebase → penalty → build → test → merge),不是一次 gate 调用。二者仅在 gate 输掉 fast-forward 竞争并重试时不同,而 §4.3.2 把那定义为重新进入评估阶段 —— 是真正独立的一次尝试。
- **日志严格是观察性的**。写日志的任何错误都被吞掉,所以磁盘问题不可能把一次被接受的重构变成被拒绝。
- **持久性边界(必须写进论文,因为失败计数是实验数据)**:写入过程中崩溃可能留下一条无终止换行的记录,下一条追加会与它熔合,于是**两条都丢失**;更早写入的每条都存活。不尝试恢复熔合对 —— 一次运行几百次尝试里丢一次不改变按运行报告的计数,而防御它意味着加锁或每条记录之间插空行。

### 10.1 计数来源表

coordination 层汇总该文件,并合入自己的计数器写进 `run_summary.json`:

| 键 | 来源 | 论文 |
|---|---|---|
| `gate_attempts`, `merged` | gate 日志 | — |
| `reverted_attempts` | gate 日志(`penalty_rejected`) | §5.1 |
| `test_failures`, `build_failures` | gate 日志 | §5.1 |
| `merge_conflicts` | gate 日志(`rebase_conflict`) | §5.4 |
| `ff_merge_races` | `ff_merge_race` + `ff_race_exhausted` | §4.3.2 集成重试 |
| `uncommitted_invocations`, `analysis_failures`, `scope_violations`, `attempt_limit_rejections` | gate 日志 | — |
| `stagnation_exit` | `stop_reason == "stagnation"` | §5.1 |
| `hard_timeout_kills`, `analyst_timeout_kills`, `stuck_terminations` | penalty history 事件计数 | §5.4 |
| `issues_skipped` | backlog `SKIPPED` 计数 | §5.4「agents give up」 |
| `agent_crashes` | 未被 kill 的会话非零退出 | §5.4「kiro-crashes」 |
| `analyst_phantom_issues` | 文件存在性检查 | §6.2 |
| `by_outcome` | 原始 tally(便于日后新增 outcome 仍可见) | — |

`failures.merged` 与顶层 `merges` 通过**两条独立路径**统计同一批事件 —— 前者来自 gate 自己的日志,后者来自 programmer 上报并被 coordinator 采纳的 `RESULT:` 行。二者应当一致;出现差距意味着某个 agent merge 了却没上报,penalty history 因此少了一个点。

### 10.2 §6.2 的幻影 issue 检查

`analyst_phantom_issues` 对应 §6.2 的失败模式:「analysts occasionally hardcoded the example format from their prompt as an actual issue in the backlog, reporting non-existent problems.」论文的缓解措施是在接受 finding 的位置做文件存在性检查,已实现于 `Coordinator._issue_file_exists`。

- analyst prompt **仍然带着**论文指责的那行示例(`./src/factory.cc:159`),所以没有该检查那个幻影 issue 会被真的分派给 programmer。
- 被拒绝的 finding 被丢弃**并计数**,而不是静默消失。
- 路径解析同时尝试目标子树与仓库根;绝对路径**只在**位于 coordinator checkout 或某个已知 agent worktree 内部时被接受(把 analyst 的绝对 worktree 路径安全映射到相同相对文件,同时拒绝无关的主机文件)。
- 存储时归一化为 **repo 相对路径**(`_canonical_issue_path`),使 target 相对 / repo 相对 / 绝对 / 前导 `./` 四种写法不会变成重复 backlog 项,且在每个 agent worktree 里都有效。

### 10.3 merge 结果的交叉校验与对账

- `_gate_confirms_merge()`:一条 `RESULT` 只在(a)该 issue 当前正分派给该 programmer,且(b)追加日志里存在 outcome=`merged`、issue id 相同、`penalty_before`/`penalty_after` 差值 ≤ 0.05 的记录时才被接受。
- `_recover_active_gate_merges()`:每 tick 在 reap/超时**之前**从持久 gate 日志对账已落地的 merge,防止 gate 恰在 agent 最终 `RESULT:` 消息入队前退出而丢掉一次已完成的 merge。终止是幂等的,被回收的 merge 不计为超时也不退回 TODO。
- `ProgrammerSession._recover_merged_gate_results()`:CLI 省略 `RESULT` 行时,从权威 gate 日志恢复成功记录(只限本次 dispatch 起始偏移之后、属于本 agent、已分派且尚未上报的记录)。

---

## 11. Agent 层

### 11.1 Orchestrator(`agents/orchestrator.py`)

一个只做**两类同步 LLM 调用**的 Python 程序,**不是**常驻进程(§4.3.2)。用 Anthropic SDK/wire 格式而非 CLI,因为它不需要文件编辑或 shell 工具。单条 user message,`max_tokens=4096`,不设采样参数。DeepSeek Orchestrator 请求显式设置 `thinking={"type":"disabled"}`,并通过强制 `submit_assignment` / `submit_stuck_evaluation` 工具调用及 JSON Schema 返回结构化决策;`deepseek_effort` 仅用于 Analyst/Programmer Claude Code 会话。

**决策点 1 — 任务分派。** prompt 携带:当前与基线 penalty、停滞计数、空闲 programmer/analyst 列表、按改进潜力排序的逐指标分解、以及**紧凑 backlog 视图**(JSON):全部 `IN_PROGRESS` + 完整状态计数 + 影响最高的 24 条 TODO。完整 backlog 留在本地。

输出按前缀逐行解析:

```
PROG_1: ISSUE-0001, ISSUE-0003
ANALYST_1: <metric focus 或 directory>
REASONING: <一段>
```

prompt 中的分派规则(逐字来自 §4.3.2):同文件 issue 归一个 programmer;绝不让两个 programmer 碰同一文件;优先高影响;每次分派保持 1–2 个 issue;仅在 backlog 为空或接近停滞时派 analyst(多派时按目录切分)。

**决策点 2 — 卡死 agent 评估。** 每个卡死 programmer 提供:runtime、已分派 issue、是否已做文件编辑、gate 调用次数、assistant 文本尾部(默认 4000 字符)。返回:

```
PROG_n: terminate|keep
INFEASIBLE: <ids> 或 none
REASONING: <一段>
```

评估按 agent 用 `stuck_eval_interval_sec` 节流,且**时间戳在调用之前打上**,所以调用失败不会引发重试风暴。

### 11.2 Analyst 与 Programmer(`agents/agent_runner.py`)

两者都是 `claude` CLI 子进程。Anthropic provider 下保留 Claude Code 的正常认证;DeepSeek provider 下**只有子进程环境**收到 DeepSeek 文档化的变量(见 §11.4)。

```
claude -p --append-system-prompt <role prompt> --permission-mode acceptEdits
       --output-format stream-json --verbose --model <model>
       --bare --no-session-persistence
       --allowedTools Bash,Edit,Write,Read,Glob,Grep
```

任务 prompt 写入 stdin;stdout 逐行流式写入日志文件。

- **bare 模式 + 关闭 session 持久化**让每次 dispatch 独立于项目/用户记忆、CLAUDE.md 发现、hooks、插件和先前会话。
- **显式 allowlist** 给无头 agent 所需的 Bash 访问(git、分析、build、test、gate);`acceptEdits` 本身只批准文件编辑。
- **`start_new_session=True`** 把子进程放进自己的进程组,于是 `os.killpg(os.getpgid(pid), SIGKILL)` 能连带回收 agent **以及**它派生的构建工具或 git 进程 —— 单纯 `process.kill()` 会留下孤儿。

**两个活动信号**从原始 stream-json 流式抓取,供 orchestrator 的卡死评估使用:`edits_made`(出现 Edit/Write/MultiEdit 工具标记)与 `gate_invocations`(出现引用 `merge_gate/cli.py` 的 Bash 调用)。

### 11.3 Analyst 会话细节

- 每次扫描前把自己的 worktree `reset --hard` 到集成分支,所以它只报告存在于**已接受版本**代码里的 issue(§4.3.2)。
- cwd 设为 worktree 内的 target 子目录。
- 任务 prompt 注入:penalty 分解、**ACTIVE/DISABLED METRICS 列表**(由权重 > 0 推导)、**DUPLICATION TOOL 是否配置**、以及最多 15 条本地 Lizard 线索。明确禁止为被禁用指标或未配置二进制去搜索/安装/运行工具,禁止在主机文件系统上找分析二进制。
- 输出格式:每个 finding 一行

```
ISSUE: <file>:<line> - <severity> - <type> - <metric values>
```

- **有界的协议遗漏恢复**(`_recover_explicitly_confirmed_lead`):当且仅当 ①本次只有 1 条线索、②stream-json 有成功的终端 result 事件、③响应中出现该函数名、④出现四条确认短语之一(`i confirm this lead` / `i confirm the lead` / `this lead is confirmed` / `confirmed local lead`)时,才把该线索转成一条 issue。这保留了「analyst 是最终决策者」,同时容忍完成了分析却省略 ISSUE 行的 provider。
- `has_successful_terminal_result()`:仅进程零退出**不足够** —— 截断或畸形的日志不得让 coordinator 把一页本地线索标记为已审阅。

### 11.4 Provider 抽象(`agents/provider.py`)

| | Anthropic(默认) | DeepSeek |
|---|---|---|
| orchestrator 模型 | `claude-opus-4-7` | `deepseek-v4-pro` |
| agent 模型 | `claude-opus-4-7` | `deepseek-v4-pro[1m]`（包括 Claude Code 内部子代理） |
| base URL | SDK 默认 | `https://api.deepseek.com/anthropic` |
| 密钥环境变量 | `ANTHROPIC_API_KEY` | `DEEPSEEK_API_KEY` |
| 子进程环境 | 正常继承(返回 `None`) | 私有副本注入下表变量 |

DeepSeek 子进程环境注入:`ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL`、`ANTHROPIC_DEFAULT_OPUS_MODEL`、`ANTHROPIC_DEFAULT_SONNET_MODEL`、`ANTHROPIC_DEFAULT_HAIKU_MODEL` 与 `CLAUDE_CODE_SUBAGENT_MODEL`（模型变量均为 `agent_model`，默认 `deepseek-v4-pro[1m]`）、`CLAUDE_CODE_EFFORT_LEVEL`(= `deepseek_effort`);并 **pop 掉 `ANTHROPIC_API_KEY`**,防止环境里的 Anthropic key 压过 provider 专用 auth token。父 shell 不被修改。

### 11.5 Programmer 会话细节

- 任务 prompt 明确给出集成分支名与逐步流程:`git reset --hard <branch> && git clean -fd` → 读文件 → 编辑 → **commit** → 调用 gate → 按 gate 的 JSON 判决反应。
- 明确禁止:在 gate 之前另跑 build/test、管道过滤 gate 输出、对同一 issue 调用 gate 超过 3 次。
- 输出格式:

```
RESULT: <ISSUE-ID> - done    - merged at penalty <before> -> <after>
RESULT: <ISSUE-ID> - skipped - <reason>
```

正则容忍 Markdown 装饰(前导 `-`/`*`、`**` 包裹)。
- 结果在进程退出后从日志解析,**按 issue id 去重**且**限定在本次分派范围内**。被 coordination 层 kill 的会话设置 `aborted` 并且**什么都不投递**,所以已退回 TODO 的 issue 不会被重复上报。
- CLI 崩溃或未解决某个已分派 issue 时,coordinator 把该 issue 从 `IN_PROGRESS` 退回 `TODO`。

### 11.6 每次 dispatch 一个日志文件

`<AGENT>_<nnn>.log`(`PROG_1_001.log`、`PROG_1_002.log`、…)。runner 以 `"w"` 打开日志,所以「每 agent 一个文件」会只留下最后一次 dispatch(而 §4.4 要求「the full agent logs」);改为追加模式同样错误,因为结果解析器扫描整个文件,会把早先 dispatch 的 `RESULT:` 行当作新的 merge 重新投递。`dispatch` 计数在首次使用时从磁盘上已有文件名恢复,以支持 `--resume`。

---

## 12. 产品 Backlog

`coordination/backlog.py`。磁盘 JSON,id 形如 `ISSUE-%04d`。

```python
Issue(id, file_path, line, severity, issue_type, message,
      metric_values, estimated_penalty_reduction, impact,
      status, assigned_to, skip_reason)
```

- **去重键:`(file_path, line, issue_type)`**,对**包括已完成项在内的每一项**检查,所以重扫不可能重新加入已完成的工作。`issue_type` 先经 `_canonical_issue_type()` 折叠常见别名(`ccn`/`complexity`/`highComplexity`/`cyclomaticComplexity` → `complexity`;`nloc`/`longFunction`/`highNloc` → `longFunction`)。
- **状态生命周期:`TODO → IN_PROGRESS → DONE`,外加 `SKIPPED`。** §4.3.2 只列前三个并把 skipped 描述为从 backlog 中移除;这里保留第四个状态,因为真正移除会让去重键失效,于是 analyst 会无限重报同一个不可行 issue。
- 被终止的 programmer 的 issue 退回 `TODO`。
- `--resume` 时,崩溃的 coordinator 留下的任何 `IN_PROGRESS` 项同样恢复为 `TODO`(`recover_in_progress()`)。
- `_compute_next_id()` 从已有 id 的最大数字 + 1 继续。

---

## 13. 停止判据与卡死处理

`coordination/stagnation.py`,按 §4.3.2 与 §4.5.2。

| 触发 | 是否递增计数器 |
|---|---|
| 一次 merge 的 penalty 下降量 ≤ `min_merge_gain`(10) | 是 |
| programmer 在 `programmer_timeout_sec`(30 min)被硬杀 | 是 |
| orchestrator 裁量性终止(卡死,但未到 30 min) | 否 |
| 一次 gate 尝试因 penalty 未下降被回退 | 否 —— 见下 |

高于阈值的 merge 把计数器**重置为 0**;计数器达到 `stagnation_limit`(3)时停止运行。下降量**恰好等于** `min_merge_gain` 算作低增益,因为论文重置计数器的条件是严格的 `> 10`(代码:`if penalty_reduction > self.min_merge_gain`)。

> **被回退的尝试有意不递增计数器。** 这看起来与 Tables 4.5/4.6 矛盾 —— 其表注写着「entries marked r denote reverted attempts … counting as zero improvement toward the stagnation counter」,零改进循环理应递增。但那些表验证的是**单智能体**系统,它统计的是顺序循环。§4.3.2 显式定义了多智能体计数器并只列出两个触发:merge 的下降量「falls below 10 units」,或 programmer「terminated for exceeding its 30-minute time limit」。回退尝试不在其中。它们被计数并报告(§10),但不驱动停止条件。

### 13.1 两级卡死处理

- **超过 `issue_timeout_sec`(600 s,即论文的每 issue 十分钟限制)**:coordinator 把每个卡死 programmer 的 runtime、近期日志、是否已编辑文件、gate 调用次数交给 orchestrator。orchestrator 逐 agent 回 `terminate` 或 `keep`,并可额外列出应标记为不可行的 issue。同一 agent 的重评估由 `stuck_eval_interval_sec`(300 s)节流。裁量性终止**不**递增停滞计数。
- **超过 `programmer_timeout_sec`(1800 s)**:coordinator **不咨询 orchestrator** 无条件杀掉,把 issue 退回 TODO,重置 worktree,并递增停滞计数(仅当确有未解决 issue 时)。
- **Analyst 硬超时 `analyst_timeout_sec`(900 s)**:论文没给 analyst 软超时,但外部工具调用不得让无人值守的运行永久挂住。超时会杀掉进程、释放其预留线索、递增空扫计数、记录 `analyst_timeout` 事件。

杀进程一律用 `os.killpg(getpgid(pid), SIGKILL)`(见 §11.2)。

### 13.2 全部 stop_reason

| `stop_reason` | 条件 |
|---|---|
| `stagnation` | 停滞计数器达到上限 |
| `penalty_zero` | 基线测量后 penalty ≤ 1e-9 |
| `no_actionable_work` | 连续 `empty_scan_limit`(3)次空扫,且无 TODO/IN_PROGRESS,且无在飞 analyst,且无未审阅本地线索 |
| `orchestrator_no_progress` | 连续 `orchestrator_no_progress_limit`(3)次不可用的分派响应(仅在无任何 agent 在飞时计数) |
| `token_budget_input_tokens` / `token_budget_output_tokens` | 达到上报用量上限且所有在飞工作已结束、队列已空 |
| `wall_timeout` | 外部 SIGTERM(注册了处理器,worktree 有意保留以便事后勘验) |

---

## 14. Token 节省的三个保守阶段(本目录相对 `experiment` 的唯一差异)

`experiment_token_save` 以 `experiment` 为对照,只做三处保守修改。三者**刻意保留** 3+3 角色、`ISSUE:` 协议、backlog schema、1–2 issue 分派、merge gate 与停滞逻辑。

1. **会计与 dispatch 上限。** `run_summary.json` 含 cache 感知的总计,`token_usage.json` 保留 dispatch 级明细。DeepSeek 默认启用 `--max-run-cost-cny 300`；人民币成本按官方原生价格计算，不经过汇率换算。token 上限与 USD 上限默认关闭。所有上限都**停止新的 dispatch 但让在飞 agent 跑完**,所以最终用量或成本可能超出,超出量被显式报告。Anthropic 优先使用 CLI 实报成本；DeepSeek 历史日志虽使用 `*_usd` 字段名但值为人民币，因此始终根据逐模型 token 和官方价格重新计算。
2. **紧凑 Orchestrator 视图。** LLM 收到每个 `IN_PROGRESS` issue、完整状态计数,以及**影响最高的 24 条 TODO**。DONE/SKIPPED 正文与更低排名的 TODO 留在本地 backlog。选择顺序确定:`estimated_penalty_reduction` 降序,再按 issue id。视图里显式带上 `todo_shown` / `todo_omitted`,所以截断不会被误读为「已覆盖全部」。
3. **保守的 Analyst 线索。** 已有的 Lizard 记录产出**非权威**的 CCN/NLOC/param 线索(`analysis/candidates.py`)。每次 Analyst dispatch 最多发 **15 条互不重叠**的线索(键为 `sha256(file|line|function|metrics)[:20]`,按估计下降量降序)。**只有成功的 Analyst 审阅**才把线索标记为已见(要求进程零退出 **且** stream-json 有成功终端 result);崩溃会释放线索供重试。**认知复杂度与 Duplo 保持原来的 Analyst 驱动路径。**

紧凑视图与线索分页减少的是 LLM 上下文;它们**不删除实验证据**,也**不把静态分析输出直接插入 backlog**。没有引入候选数据库、accept/reject 协议、backlog schema 变更或 merge gate 变更。

**Resume 安全**:`--resume` 校验 provider/两个模型,以及一个 **optimization fingerprint**(schema 版本 6 + repo/验证/指标/分页/预算配置 + 定价快照的 sha256)。指纹不匹配则拒绝 resume,防止配置或价格变化后继续一个不可比较的旧运行。带有 `analyst_lead_seen_keys` 但没有指纹的旧状态同样被拒绝。

**线索焦点回退**:模型给出的目录/指标提示可能匹配不上 Lizard 的路径拼写,所以 `_reserve_local_leads` 在过滤后为空时回落到未过滤的排序页(`(focused or available)[:page_size]`),避免一个匹配不上的提示搁死有效线索并造成无休止的空扫循环。

---

## 15. Token 会计(`coordination/token_usage.py`)

provider 中立,归一化三种用量形状:

- **Anthropic**:`input_tokens`(未缓存)+ `cache_creation_input_tokens` + `cache_read_input_tokens`。
- **DeepSeek**:可能改为报告 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`。此形状下 miss → uncached、hit → cache_read,且**不得**把 prompt 总数再加一次。
- **Claude Code CLI**:优先用最终 `result` 事件(已是会话聚合用量,含 `total_cost_usd`、`modelUsage` 与 `num_turns`);进程在发出它之前被杀时,退回累加 `assistant` 消息的用量。stream delta 事件被忽略以避免重复计账。Anthropic 可采用实报成本;DeepSeek 的 `modelUsage` 按 Pro/Flash 官方价格重算。

约定:`input_tokens = uncached + cache_creation + cache_read`;`total_tokens = input + output`。

两个数据源:
- `orchestrator_usage.jsonl` — 每次同步 SDK 调用一条追加记录(原子 `O_APPEND`),跨 `--resume` 保留;读取时跳过没有终止换行的行。
- `logs/<AGENT>_<nnn>.log` — 从文件名正则 `^(ANALYST|PROG)_\d+_\d+\.log$` 推出 agent/role/dispatch。

输出:`token_usage.json` 含 `totals`、`by_role`、`by_agent`、`tokens_per_successful_merge`、`cost`(实报/估算/有效成本及定价快照)、`coverage`、`dispatches`(逐条明细)、以及 `notes`。DeepSeek 的默认上限使用 `effective_cost_cny`；USD 上限使用 `effective_cost_usd`。

---

## 16. 运行产物

每次运行写入 `<work-root>/results/<run-id>/`:

| 文件 | 内容 |
|---|---|
| `penalty_history.json` | 每个改变 penalty 的事件,含 `elapsed_sec`、每次 merge 时的逐指标分解、gate 的前后值、停滞计数 |
| `penalty.png` | penalty-vs-time 阶梯图,标注 merge(绿点)与终止(红叉)、基线虚线 |
| `run_summary.json` | 见下 |
| `gate_attempts.jsonl` | 每次 gate 尝试一行 |
| `logs/<AGENT>_<nnn>.log` | 完整 Claude CLI stream-json 日志,每 agent **每 dispatch** 一个文件 |
| `orchestrator_usage.jsonl` | 每次 orchestrator SDK 调用一条 token 事件 |
| `token_usage.json` | cache 感知的 role/agent/dispatch 级总计、覆盖率、每次成功 merge 的 token |
| `gate_configs/PROG_n.json` | 外部 gate 配置(在 git worktree 之外) |
| `backlog.json` | run 作用域的 issue 状态 |
| `run_state.json` | run 作用域的崩溃恢复状态 |

per-agent worktree 在 `<work-root>/worktrees/<run-id>/`,所以并发或连续运行不可能共享 issue 状态或 worktree 路径。

penalty history 的事件类型:`baseline`、`merge`、`timeout_kill`、`stuck_terminate`、`analyst_timeout`、`stop`。**时钟从基线测量开始**,而非对象构造时,所以 setup 时间不会给每个点加偏移。

### 16.1 `run_summary.json` 顶层结构

```
run_id, stop_reason, baseline_commit, integration_branch, run_branch_pushed,
elapsed_sec, baseline_penalty, final_penalty, total_reduction, reduction_pct,
merges, stagnation_counter, final_metric_breakdown,
metrics{percentile_method, baseline, final, mean_change_pct, cleared},
failures{...见 §10.1...},
backlog{total, todo, in_progress, done, skipped},
tokens{totals, by_role, by_agent, tokens_per_successful_merge, coverage, notes, ...},
token_budget{dispatch_closed, trigger, usage_at_close, final_value, limit, overshoot, semantics},
token_optimization{orchestrator_todo_window, analyst_lead_page_size,
                   analyst_leads_reviewed, current_local_static_leads,
                   lead_scope, analyst_remains_final_issue_decider},
config{...实际使用的完整参数集...}
```

`metrics` 块是 Tables 4.1 与 5.1 的构建来源:

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

`config` 块回显实际使用的 `weights` 与 `thresholds` —— **审计某个结果时应当先查这里**。

`api_base_url` 在写入摘要前经 `_safe_api_base_url()` 剥离凭证、query 与 fragment。

### 16.2 `--resume` 语义

`run_state.json` 是崩溃恢复文件。`--resume`:
- 保留已存在的集成分支(不移动)与 penalty history;
- 恢复**原始**基线 penalty、停滞计数、merge 数、`started_at`、`agent_crashes`、`phantom_issues`、`analyst_lead_seen_keys`、token 预算关闭状态、基线指标分布;
- 恢复日志编号;
- 把被遗弃的 `IN_PROGRESS` issue 退回 `TODO`;
- 然后**重新测量**当前分支得到 `current_penalty`。

保留原始基线与历史是让改进数值在整个运行范围内可比的关键。penalty history 缺失或损坏时 resume 直接失败(而非静默重建)。

绘图需要 `matplotlib`;缺失时运行照常完成并打印跳过绘图。

---

## 17. 复现实验

### 17.1 基本运行

```bash
export DEEPSEEK_API_KEY=...         # 或写入项目根目录 .env

# FerretDB:整个仓库;真实 Go 编译检查 + short unit tests
./.venv/bin/python main.py --profile ferretdb --provider deepseek

# MongoDB:重构/指标/测试均锁定 query 模块;真实 Bazel build + test
./.venv/bin/python main.py --profile mongodb-query --provider deepseek
```

**关键注意**:
- 两个 production profile 锁定 repo 默认路径、语言、重构范围、full-worktree 模式和真实 build/test,不允许用 CLI 覆盖这些安全边界。FerretDB 的范围是整个仓库;MongoDB 只允许 `src/mongo/db/query`,Bazel 目标为 `//src/mongo/db/query/...`。
- 自定义仓库仍可使用 `--repo/--subdir/--work-root`,但必须显式提供 `--build-cmd` 与 `--test-cmd`。空命令以及 `true`/`:` 会在创建运行状态前 fail closed。命令用 `shlex` 切分,所以不能直接带 shell 操作符(需要管道就包成脚本)。
- 启动时拒绝 tracked 修改和未知 untracked 路径。MongoDB profile 仅允许 checkout 中既有的 `.venv`、`experiment_logs`、`MODULE.bazel.lock`;结束时恢复启动前的精确 detached HEAD。
- `--duplo-binary` 默认 `""`(禁用 Duplo)。设为 `bin/duplo` 才把重复行指标计入 penalty。`main.py` 会 `resolve()` 该路径,因为 gate 从目标仓库 worktree(而非本进程 cwd)运行。
- `--language` 默认 `cpp`。C/C++ 使用修改版 cognitive complexity;Go 使用 `gocognit`;其他语言的 cognitive 总体为空。
- `--subdir` 比仓库原生 build/test 所需文件更窄时,加 `--full-worktree`。静态分析 penalty 与 agent 目标仍限定在 `--subdir`;只有 worktree 物化范围变化,merge gate 仍拒绝子树外的提交。

### 17.2 §4.4 的三个实验(每配置 10 次独立运行)

每次运行都从同一基线 ref 分叉,所以运行之间无需重置,主分支从不被推进。

```bash
# 完整模型(§4.4.1)—— 五个指标全开,z_m = 1
for i in $(seq 1 10); do
  ./.venv/bin/python main.py \
    --repo /path/to/target --subdir src --work-root ./work \
    --duplo-binary ./bin/duplo \
    --build-cmd 'make -j8' --test-cmd 'ctest --output-on-failure' \
    --run-id complete_$i --push-run-branch
done

# 消融(§4.4.3)—— 去掉一个指标,保留另外四个
for m in ccn nloc cognitive param duplicates; do
  for i in $(seq 1 10); do
    ./.venv/bin/python main.py ... --run-id abl_${m}_$i --weights "{\"$m\":0}"
  done
done

# 逆消融(§4.4.2)—— 单独针对一个指标
#   (写出四个 0;被针对的指标保持默认 1)
for i in $(seq 1 10); do
  ./.venv/bin/python main.py ... --run-id inv_ccn_$i \
    --weights '{"nloc":0,"cognitive":0,"param":0,"duplicates":0}'
done
```

**`--weights` 与 `--thresholds` 是合并进默认值,不是替换**(`cfg.weights.update(...)`),这就是消融运行只需写出它移除的那一个指标的原因。五个权重键是 `ccn`、`nloc`、`cognitive`、`param`、`duplicates`。

> **论文的 LLOC 就是配置里的 `nloc`。** 由于两个字典都合并进默认值,未知键**曾经**被接受然后永不被读取:`--weights '{"lloc":0}'` 会产出与完整模型**完全相同**的 penalty,于是 LLOC 消融条件会被当作参考条件运行并制表,而没有任何迹象表明出错。现在未知键在启动时被 `_reject_unknown_metrics()` 拒绝,错误信息显式指出「the thesis's LLOC is 'nloc' here」。

### 17.3 其他复现相关旋钮

```bash
--exclude-dirs 'test,tests,third_party'   # 匹配目标仓库布局;'' = 什么都不排除
--duplo-min-block-lines 4                 # Duplo 自身默认
--min-merge-gain 10 --stagnation-limit 3  # 预研选定值(§4.5.2)
--baseline-ref <sha>                      # 显式钉住基线
--run-id run_baseline                     # 命名运行(同时命名结果目录与集成分支)
--resume                                  # 崩溃后继续
--full-worktree                           # 原生测试需要子树外文件时
--push-run-branch                         # §4.4 的分支推送(默认关闭)
--provider deepseek                       # 切换 provider
--orchestrator-backlog-top-k 24 --analyst-lead-page-size 15
--max-run-input-tokens N --max-run-output-tokens N
```

### 17.4 ⚠️ 信任任何结果之前的校准步骤

对**真实目标代码库**跑一次基线测量,核对论文已发表的值:

- **7 个重复块,比率 2.41 %**(Table 4.1)
- **mean CCN 3.21**
- **总基线 penalty 1167.28**

不一致时,**先怀疑 `--exclude-dirs`**(它决定文件集合),**再查 `--duplo-min-block-lines`**。

**这一步尚未执行** —— 它需要真实目标代码库(见 §20)。

### 17.5 DeepSeek 模式

```bash
export DEEPSEEK_API_KEY=...
./.venv/bin/python main.py --profile ferretdb --provider deepseek

# 角色级覆盖
python main.py ... --provider deepseek \
    --api-key-env MY_DEEPSEEK_KEY \
    --api-base-url https://example.invalid/anthropic \
    --orchestrator-model deepseek-v4-pro \
    --agent-model 'deepseek-v4-pro[1m]' \
    --deepseek-effort high
```

`--model` 是同时设置两个角色模型的简写;两个角色专用 flag 优先。

---

## 18. 验证(实测结果)

```bash
./.venv/bin/python tests/run_all.py
```

无需 API key、无需目标代码库:LLM 调用被打桩,git 仓库在临时目录合成。

**2026-07-31 实测:10 个套件全部通过,共 409 项检查,0 失败。**

| 套件 | 实测检查数 | 覆盖 |
|---|---|---|
| `test_merge_gate.py` | 31 | gate 在真实 git 仓库上端到端:接受、拒绝+回退、拒绝未提交、并发 |
| `test_run_isolation.py` | 33 | §4.4 per-run 分支协议、全新运行拒绝、resume 不重置分支、push、branch/detached 精确恢复、已有本地产物保留 |
| `test_metric_stats.py` | 91 | 分布统计、Table 4.1 复现、Lizard/Duplo 计账、排除语义、外部 gate config、解释器/工具权限、未知指标键校验 |
| `test_artifacts.py` | 51 | penalty history、resume 追加历史、不可变基线 commit 的崩溃恢复、run 作用域路径、`metrics` 块 |
| `test_stuck_policy.py` | 29 | 两级卡死策略、停滞语义、空扫终止 |
| `test_failure_counts.py` | 74 | gate 尝试日志、并发追加、显式分析失败、失败计数、§6.2 检查 |
| `test_provider_config.py` | 42 | Anthropic/DeepSeek 默认值、凭证隔离、endpoint/模型映射、结构化非 thinking Orchestrator 请求、production profile 锁定和 fail-closed CLI |
| `test_token_usage.py` | 15 | provider/cache 归一化、部分会话恢复、追加式 SDK 事件、明细摘要 |
| `test_go_cognitive.py` | 6 | gocognit JSON、Go 文件范围、penalty 接入和 fail-closed 错误 |
| `test_token_optimizations.py` | 39 | Top-24 active-safe backlog 视图、含 cognitive 的保守本地线索、互斥 wave/分页、终端结果重试、resume 指纹、队列安全的 dispatch 上限、有界 no-progress 响应 |

`README.md` 与 `overview.md` 只列套件及覆盖范围;本节保留实测检查总数。

### 18.1 可选的 E2E / 冒烟测试(不在 `run_all.py` 内)

| 测试 | 是否付费 | 内容 |
|---|---|---|
| `test_fake_cli_e2e.py` | 否 | 完整多智能体 E2E:临时克隆本地 FerretDB,注入 3 个受控 Go 质量违规,用真实 Coordinator 跑 3 analysts + 3 programmers,真实 Lizard/Duplo/Go build+test、并发 rebase 与 merge、产物生成、基线恢复。`--provider deepseek` 额外校验子进程环境映射。**绝不修改**提供的 FerretDB checkout |
| `test_real_repo_fake_cli_e2e.py` | 否 | 两个**未修改的真实**本地仓库 + 确定性 fake CLI。MongoDB 严格限于 `src/mongo/db/query/bson`(只允许改 `multikey_dotted_path_support.cpp`,原生构建该 Bazel target 并只跑 `ExtractAllElementsAlongPath`);FerretDB 限于 `internal/util/telemetry`(只跑 telemetry 编译 + `TestState`)。用全 worktree 是因为原生构建图跨越分析子树。每个源 HEAD 被克隆到临时仓库并移除 remote,运行前后校验 status 未变 |
| `test_real_repo_deepseek_smoke.py` | **是** | 有界付费微冒烟:只接受指名函数,最多 1 个 Analyst + 1 个 Programmer,绝不 push,统计原生命令数。支持 `--scenario ferret\|mongo`。若一次成功的 Analyst 结果显式确认了唯一本地线索但省略了 `ISSUE:` 行,可用 `--analyst-evidence <log>` 复用而无需再付费;该恢复路径**刻意狭窄**(日志需有成功终端事件、范围内恰好 1 条本地线索、响应需指名函数并显式确认),provenance 记入 `analyst_recovery.json` |
| `test_real_repo_deepseek_three_issue.py` | **是** | 无人值守多 issue 验证:一条命令跑 1 个固定 MongoDB query/bson issue + 2 个顺序 FerretDB telemetry issue。不导入日志、不需要手工准备 backlog。每 issue 最多 2 次自动 Analyst 尝试与 2 次 Programmer dispatch,拒绝三个指名函数之外的 finding,输出聚合 `three_issue_summary.json`。第三个 issue merge 后保留有界的 discovery-exhausted 状态并要求正常的 `no_actionable_work` 停止原因,不再花额外 orchestrator 调用去探第四个 issue |

三个 fake CLI 辅助:`fake_claude_cli.py`(FerretDB fixture 场景)、`fake_repo_cli.py`(真实仓库场景)、`live_native_runner.py`(付费冒烟的原生命令计数与执行)。

**MongoDB Bazel 的执行环境要求**:其钉住的 Bazel 即使对这个聚焦测试也会启动本地 gRPC/Netty 服务器。必须在允许 loopback listener 的环境运行;拒绝 `bind(127.0.0.1, 0)` 的托管沙箱会在任何 C++ 构建开始前失败。这是执行权限要求,不是要修改目标 checkout。冷启动的 MongoDB Bazel 运行可能很久,并可能需要依赖/缓存的网络访问。

### 18.2 这些测试是为了抓住哪两个已发生过的失败模式

- **Lizard 双计**每个 CCN > 15 的函数 —— 数字看起来合理且一致地被抬高。
- **测试静默测量得比它声称的更少。** 早期一次可移植性改动用了 `shutil.which("lizard")`,它找不到不在 `PATH` 上的 virtualenv 二进制;两个套件在 Lizard 缺失的情况下跑了一段时间,实际只锻炼了认知复杂度。现在辅助函数优先从 `Path(sys.executable).parent` 解析。**一个测得比它声称更少的绿色套件比红色套件更糟。**

---

## 19. 已产生的真实运行证据

`live_smoke_results/` 下有 8 次运行目录(2026-07-30 / 07-31),已被 gitignore。最近一次三 issue 付费验证 `20260731_113519_three_issue`:

```json
{ "command_completed_without_external_intervention": true,
  "passed": true, "expected_issue_count": 3, "completed_merge_count": 3 }
```

| 场景 | 目标 | merges | baseline → final penalty | build / test 次数 | gate outcomes | tokens (in/out) | 上报成本 |
|---|---|---|---|---|---|---|---|
| MongoDB query/bson | `_extractAllElementsAlongPath` @ `multikey_dotted_path_support.cpp:55` | 1 | 21.05 → **0.0** | 1 / 2 | `[merged]` | 104,826 / 10,831 | $0.450 |
| FerretDB telemetry | `initialState` @ `telemetry.go:73`;`makeReport` @ `reporter.go:191` | 2 | 103.52 → 38.78 | 3 / 4 | `[build_failed, merged, merged]` | 152,460 / 9,218 | $0.494 |

两个场景的 7 项检查全部通过:所有预期 issue 完成、每 issue 恰好一次成功 gate、penalty 下降、每 issue 都走了原生 build/test 路径、只有预期源文件被改动、run 分支未被 push、源 checkout 未变。

值得注意的是 FerretDB 场景的 `build_failed → merged` 序列**在真实模型下验证了 §9.1 步骤 4 的不对称语义**:build 失败不回退,programmer 读输出修好自己的代码再重跑 gate,随后成功 merge。

---

## 20. 与论文的偏离清单

每一项要么由运行时强制,要么是有文档记录的技术必然,要么是作者的显式决定。

| 项 | 状态 |
|---|---|
| Agent 运行时 | Claude CLI + Anthropic 兼容 SDK,替代 KIRO CLI —— **这是有意的偏离**。`api_provider` 在不改变协调协议的前提下选择 Anthropic 或 DeepSeek |
| System prompt 递送方式 | `--append-system-prompt`,**由作者决定**。§4.3.2 写的是「the full system prompt to the process's standard input」;CLI 的 `--system-prompt` 更贴合字面但会整体替换 Claude Code 内置 prompt,剥离其工具使用指导并有降低 agent 行为质量的风险。追加方式把角色 prompt 叠加在上面,agent 额外携带 harness 默认值 |
| 模型 | Anthropic 默认 `claude-opus-4-7`;DeepSeek 模式用 V4 Pro。二者都与论文的 Opus 4.6 不同,**必须作为实验配置记录** |
| **单智能体系统** | **有意超出范围,由作者决定。** 本仓库只移植 §4.3.2 的多智能体系统。论文所有实验都在两个系统上跑,所以跨系统对比落在本仓库职责之外。这是范围决策而非缺失功能 —— **不要实现它** |
| Duplo 输出格式 | 解析文本摘要而非 `-json`,因为 JSON 不带行数总计且两种模式互斥(§5.3) |
| Duplo `-ml` | 4 = Duplo **自身默认**;论文从未指定最小块大小。`--duplo-min-block-lines` 可覆盖。其余 Duplo 旋钮全部保持默认,只设 `-ip` 因为 §4.2.2 要求 |
| 排除目录 | §4.3.2 排除「test directories」但不枚举,故为**可配置参数**;默认覆盖按约定必为测试产物的名字。另外还有语言约定的文件名级测试排除(§5.4) |
| 卡死评估触发阈值 | 论文给了十分钟**每 issue**限制但没有显式卡死评估阈值;`issue_timeout_sec` 复用这 10 分钟 |
| `z_m` 选择子 | Eq 4.3 定义 `z_m ∈ {0,1}`;`config.weights` 接受任意实数。这是超集 —— 用 0/1 精确复现论文,而 §4.4 的三个实验只需要 0/1 |
| run 分支自动 push | 已实现但**opt-in**(`--push-run-branch`)。§4.4 每次运行都 push;默认关闭因为它写入目标仓库的 remote |
| Analyst worktree | §4.3.2 为每个 *programmer* 创建 worktree;analyst 额外得到 detached worktree。没有它,analyst 会在 gate 正在 merge 时读取仓库根目录,破坏 §4.3.2 要求的「only reports issues that exist in the accepted version of the code」 |
| Backlog `SKIPPED` 状态 | §4.3.2 只列 `TODO`/`IN_PROGRESS`/`DONE` 并把 skipped 描述为移除。这里保留第四个状态,否则去重键失效,analyst 会永久重报同一个不可行 issue |
| 空发现停止 | `empty_scan_limit=3`;论文规定了停滞判据但没说一个无 issue 的多智能体运行如何终止 |
| 回退尝试不递增停滞计数 | 见 §13 的完整论证(Tables 4.5/4.6 验证的是单智能体系统) |
| KIRO 特有设置 | §4.3 描述的 chain-of-thought 开关、long-term memory、Tangent Mode、Todo Lists、Checkpointing、Context Usage Indicator 在此运行时**没有对应物** |

---

## 21. 未完成项与已知坑

### 21.1 未完成(阻塞真实数据收集)

1. **§17.4 的 Table 4.1 校准尚未执行** —— 需要真实目标代码库。这是信任任何 penalty 数字之前的第一步。
2. **10 次运行的实验本身尚未执行。** 代码现在采集 §5.1 与 §5.4 所需的全部数据,但运行没做。
3. 论文自身的结果 TODO(§5.2.2、§5.3.2、§5.4、§5.5、§5.6)是写作与实验任务。
4. fake-CLI 的 FerretDB 集成测试验证了传输、协调、gate 并发、指标与 build/test 行为,但**不验证真实 CLI/模型的判断质量**。付费三 issue 验证(§19)已部分覆盖这一空白(3/3 merge、真实 build 失败恢复路径),但规模远小于论文实验。

### 21.2 文档一致性

- `README.md`、`overview.md` 与本摘要均记录 10 个免 API-key 套件;实测检查总数见 §18。

### 21.3 遗留死代码(有意保留)

- `analysis/tools.py:_is_excluded()` —— 项目内无调用者。
- `analysis/tools.py:_iter_cpp_files` —— `_iter_source_files` 的向后兼容别名。
- `analysis/tools.py:_function_match_keys()` —— 只用于 debug 用的可选 join,不影响 penalty 数学。
- `merge_gate/cli.py` 的 worktree 本地 `.gate_config.json` 回退路径 —— legacy fallback。

### 21.4 复现时最容易踩的坑(按危险程度排序)

1. **重复率单位**:`p_dup` 吃分数(0.0241),不是百分数(2.41)。错了会把重复惩罚放大约 5 倍。
2. **Lizard 必须 `--csv`**:默认表格输出会双计所有 CCN > 15 的函数。
3. **指标键拼写**:论文的 LLOC = 配置的 `nloc`。现在会在启动时报错,但要理解为什么曾经是静默的。
4. **真实 build/test 不可省略**:production profile 已锁定原生命令;自定义运行若缺少命令或使用 `true`/`:` 会在启动时 fail closed。
5. **Duplo 需要绝对路径**,否则静默报 0 行。
6. **rebase/revert/reset 目标必须是本地集成分支**,不能是任何 remote-tracking ref。
7. **`--exclude-dirs ''` 与省略该 flag 语义不同**:前者「什么都不排除」,后者「用默认列表」。代码中三处必须用 `is None` 判断而非 falsy 检查(`_exclude_set`、`main.py` 参数处理、gate CLI 的 `cfg.get`)。
8. **同一 `--run-id` 不带 `--resume` 重跑会被拒绝**,这是刻意保护已有证据。
9. **`agent_log_dir` 每 dispatch 一个文件**:任何把它改成每 agent 一个文件的「简化」会丢日志或造成 merge 重复上报。
10. **认知复杂度只支持 C/C++ 与 Go**:Go 必须有 `gocognit`;工具缺失或输出异常会 fail closed。Java/Python 当前该指标总体为空,论文里必须说明。
