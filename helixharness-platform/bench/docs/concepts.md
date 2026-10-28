# HelixHarness evaluation concepts

Bench owns Case publication and calibration, immutable Runs, execution, judging and reports. The current platform evaluates the public HelixHarness workflow. Configuration is described in the [platform README](../../README.md).

## Case、来源与校准

**Case / Scenario（评测场景）**描述起始条件、需求、业务约束和验收依据。Case 不包含某个 HelixHarness 版本的执行命令；命令属于执行策略。来源目录包含 `case.json`、初始需求、澄清规则、Gold 和声明的工作区资产。

**Source Interval（来源区间）**用真实业务仓库的固定 `base_commit` 与 `solution_commit` 表示需求前后状态。仓库通过稳定 `repository_id` 登记；本机缓存路径属于部署配置，不是 Case 身份。Case Creator 的本地仓库参数统一写成 `repository_id=/path/to/checkout`。

**Gold（业务标准）**包含需求、来源证据、确定性检查和结果检查。F2P 验证新增行为由失败变为通过，P2P 验证既有行为持续通过。校准必须实际执行这些检查，不能用空命令或“文件存在”冒充业务验收。

**Preflight（发布前检查）**验证来源包结构、引用、路径安全和证据完整性。通过 Preflight 表示可以发布，不等于已经校准。真实仓库 Case 进入正式任务前仍须满足当前校准门禁。开发 fixture 只用于本地验证，Demo 明确标注为合成数据。

Case 更新产生新版本并使相应校准失效。非终态任务持有当前 Case 的编辑保护；历史任务始终读取创建时冻结的快照。

## Run、策略与执行

页面关系为：`评测任务 → Case → 执行侧 → 执行尝试 → 对话轮次`。

| 概念 | 当前含义 |
| --- | --- |
| Run | 一次不可变评测活动，冻结 Case、策略、源码、模型、预算和评分合同 |
| Run Kind | `DEVELOPMENT` 用于开发验证，`FORMAL` 用于正式评测 |
| Execution Scope | `PAIRED` 比较 Base/Candidate；`BASE_ONLY` 只验证基准侧 |
| Base | 将配置的基准引用解析为精确 Git commit；执行期不跟随移动分支 |
| Candidate | 待评估的精确 Git commit，使用与 Base 相同的业务标准 |
| Strategy | 某一侧的实际命令、阶段、终态与产物来源，带源码文档 Hash |
| Attempt | 同一执行侧的一次实际尝试，保留独立状态、资源和证据 |
| Turn | Agent 的一轮输入与响应；与 Attempt 和整个 Run 的状态分开记录 |

基准引用由 `HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_REFERENCE_REF` 配置。源码可以是插件独立仓库，也可以是包含 `helixharness/` 的整体仓库；布局从冻结提交中解析。源码冻结不依赖当前工作区或插件 bundle 生成器。

当前命令链为 Propose → Apply → Archive，评审与验证在 Apply 内完成；`propose --loop` 可以声明完整闭环。Init 和独立 UT 可按实际需求加入声明。`--` 后保留为字面需求；不把其中的选项词误判为执行开关。平台拒绝已移除的命令和选项。

创建前的 Preview 是无状态校验，确认时重新验证并冻结合同。正式任务使用 Codex 执行器；Mock 仅用于开发验证，其分数不代表模型能力。

## Worker、恢复与数据边界

`FORMAL_EVALUATION` 和 `EVOLUTION_EVALUATION` 使用独立 Worker 池、队列与并发配置；共享 Bench 的 Case、评分实现和数据库。每个 Worker 只消费其配置的通道。租约检查防止同一执行侧被重复完成。

恢复只使用冻结的阶段动作、完整检查点和剩余预算。模型输出一条完成标记不等于 Archive 工件已经满足合同；缺失、冲突或无法验证的检查点不能自动补成成功。基础设施故障、模型轮次状态、业务验收结果和最终 Run 状态分别保存。

资源观测区分 `attributable`（参与比较的完整业务轨迹）、`actual`（全部已观测尝试成本）和 `excluded`（差额）。报告读取当前结构，不将历史平铺字段作为别名。缺失证据保持不可用。

运行工作区、检查点、诊断与模型输出不是发布源码。小型诊断包默认在任务终态后保留 7 天，孤立诊断保留 30 天，每 5 分钟扫描；可通过 `HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DIAGNOSTIC_*` 配置。诊断保留与不可变任务记录分开处理。

## 评分与报告

**Outcome Quality（结果质量）**评价业务目标和本次实现。F2P/P2P 未通过时先触发业务硬门禁，不调用 Outcome Judge 生成可由其他分数补偿的结果。

**Framework Workflow Quality（工作流质量）**评价冻结的阶段产物：目标与需求、决策与计划、实现追溯、验证与完成真实性四个轴各占 25%。文件数量、篇幅或模板相似度不直接代表质量。

当前综合分由结果质量 60%、工作流质量 20% 和效率 20% 组成；结果质量内部为需求达成 90%、实现质量 10%。权重、门禁和资源比较合同在任务创建时冻结。平台先算每侧的绝对分，再算双方差值。

**Hard Gate（硬门禁）**记录业务失败、证据污染或其他不能由平均分抵消的条件。证据缺失导致不可比较时，不伪造零分；报告同时显示可比较覆盖率、忽略原因和硬失败。

**Report（报告）**读取任务的持久化结果和当前有效计算。重算追加计算记录，不覆盖冻结 Case、原始执行或既有计算。查看报告时应同时检查业务验证、工作流证据、资源完整性和适用边界，不能只看任务状态或总分。
