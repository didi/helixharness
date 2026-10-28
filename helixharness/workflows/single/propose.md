---
description: "Single Propose：用完整需求决策树收敛需求，生成需求契约 spec.md 与可执行任务清单 tasks.md。"
---

# Single Propose · 需求决策树

仅当公共 Propose Router 已返回本文件且 `execution_scope=single`，`/helix:propose --loop` 已确定当前阶段，或 `/helix:bug-fix` 已通过 active HelixHarness repair preflight 时执行本工作流。

Spec/Tasks 不绑定具体 Agent 入口，Apply 开始前可由任意兼容入口继续。Apply 开始后，为了区分执行前已有改动与本需求改动，后续 Apply/Archive 必须在同一工作区继续；新 clone 或新 checkout 缺少该本地基线，不得猜测恢复。

## 单仓前置检查

本工作流只接受 Router 返回的 `execution_scope=single`。若返回 `workspace`，加载 `workflows/workspace/propose.md`；若 scope 与当前目录的 `helixharness.workspace.yaml` 标记冲突，输出 `BLOCKED:lite_execution_scope_conflict`。这里不承载 Workspace 的编排、工件或调度逻辑。

单仓模式下，默认交付范围就是当前仓库。后续读取 requirement 文件或 URL source 时，上游需求文档提及多个系统、仓库、前端、网关或全链路，不等于本次交付必须跨仓；source 只能作为范围背景，不能作为多仓阻断依据。若本次命令已经明确只实现当前仓库或当前系统部分，继续当前仓库的流程；把其他系统的改造作为上下游背景或明确的 non-goals，并在 Spec 的范围边界、依赖和验收行为中如实记录，不得要求当前仓承诺或伪造其他仓的交付结果。

若 source 描述了多仓或全链路、但本次命令没有明确要求多仓共同修改，必须保持默认当前仓范围。只需告知用户本次将按当前仓库出方案并继续，把其他系统作为上下游背景、依赖或 non-goals；不得暂停等待范围确认，不得要求用户回复选项或自行重新描述应当实现哪个仓，也不得仅凭 source `finish --status blocked`。告知后直接进入同一轮需求决策 frontier，正常收敛当前仓 Spec/Tasks。告知可使用以下等价的简短文本，不得附加确认问题：

```text
范围说明：上游需求涉及多个系统；本次 HelixHarness 默认只为当前仓库出方案并实现，其他系统作为上下游背景、依赖或 non-goals。下面继续确认当前仓的需求决策。
```

只有用户在本次命令的需求描述中明确要求多仓共同修改，或在后续主动补充中明确把范围改为多仓，才能判定当前 Single 范围不匹配并以 `blocked + prerequisite_missing` 收口，提示用户回到 Workspace 根重新执行 Propose；不得自动切换已经确认的 scope。用户没有作出上述明确表达时不得从 source、代码影响面或 Agent 推断升级为多仓交付。

## 统计授权与提醒

单仓前置检查通过后，读取 `reference/eval-protocol.md` 第 2 节的共享 consent 合同，并先执行：

```bash
python3 "{plugin_root}/scripts/telemetry_consent.py" --repo-root "{repo_root}" --platform "{current_platform}" resolve
```

支持宿主上用户直接执行 `/helix:propose`、直接执行 `/helix:propose --loop` 并进入 Stage A，或 `/helix:bug-fix` 因契约修订进入 Propose 时，在无缓存或提醒到期、即 `prompt_due=true` 时逐字展示该协议的固定数据说明，并按返回顺序原样展示 `allowed_actions[]`。本段结构化单选要求只适用于统计授权：支持结构化单选交互的宿主必须使用该能力，Claude Code 必须调用 `AskUserQuestion`；只有宿主确实没有该能力时才允许使用普通编号文本。不得把这一授权交互规则用于后续需求决策、推荐问题或共享理解确认。用户选择后调用 `choose --action "{selected_action_id}" --user-confirmed`。有效缓存直接复用，不重复询问。`invocation_trigger=agent_workflow` 的其他自动调用不弹出询问、不调用 `choose/set/install-python`，只使用当前 `effective_mode`；未注册宿主不询问并按 disabled 继续。resolver 或缓存异常只输出 `WARN:telemetry_consent_cache_unavailable` 并按 disabled 继续，不得阻塞业务或手写缓存。

HelixHarness 只复用统一的授权决策、提醒周期与缓存，只使用当前 HelixHarness Ledger；授权处理完成后继续下方 HelixHarness 版本检查和 `metrics.py start`，由 HelixHarness Ledger 自己维护 `docs/eval/**` 的跟踪准备。

## 版本快速检查

统计授权处理完成后，完整读取 `reference/version-update-protocol.md`，按其中“主流程快速版本检查模式”执行。Propose --loop 或 Bug Fix 实际进入本正文时，本节就是本次编排唯一一次版本检查；从 Apply/Archive 续跑时不会重新进入本节：

```bash
python3 "{plugin_root}/scripts/version_update.py" --platform "{current_platform}" --repo-root "{repo_root}" check
```

只按本次返回的 `next_action` 继续、提示、阻断或原样输出 warning；不得继承历史版本结论、手工读取其他版本来源或自动安装更新。需要升级时严格使用该协议定义的受控确认链路。

## HelixHarness 统计（非阻塞）

此处 `{current_platform}` 表示当前 Agent 宿主，不是操作系统：Claude Code 固定为 `claude-code`，Codex 固定为 `codex`。禁止传入 `darwin`、`linux`、`windows` 等 OS 标识。该映射适用于本 workflow 内所有 `metrics.py --platform` 调用。

调用 `start` 前，先按实际入口确定唯一的 `metrics_trigger`：

- 用户直接执行当前公共命令时固定为 `user_command`；
- 由 Propose --loop 进入当前阶段时固定为 `loop`；
- 由 Bug Fix 进入当前阶段时固定为 `bug-fix`。

Workspace RepoJob 沿用父 Coordinator 显式传入的 `metrics_trigger`（根入口为 --loop 时为
`loop`，直接执行 Workspace 命令时为 `user_command`）。`invocation_trigger=agent_workflow`
只表示静默使用现有统计授权，不是 recorder 的 trigger 值，禁止把 `agent_workflow` 传给 `--trigger`。

`metrics_trigger` 表示调用来源，不是当前阶段或命令名；不得把 `propose`、`apply` 或 `archive` 作为 `--trigger` 的值。

在确认 feature 名且进入业务步骤前，执行：

```bash
python3 "{plugin_root}/scripts/metrics.py" --repo-root "{repo_root}" --platform "{current_platform}" start --feature "{feature}" --stage propose --trigger "{metrics_trigger}" --route-reason "{router_reason|loop_orchestrated|bug_fix_active}"
```

`router_reason` 取公共 Router 本轮返回的受控 `reason`；Propose --loop 固定为 `loop_orchestrated`，Bug Fix repair mode 固定为 `bug_fix_active`。recorder 按上一步解析后的有效 consent 写 HelixHarness Ledger，并在私有 Workflow Trace 中记录本轮组件版本和路由；阶段结束后由独立 Runtime 异步补充宿主数据。公共入口已经完成 Python Runtime preflight；正文内的 disabled、unsupported 或 recorder/trace 失败不得阻塞 Propose 或改走其他流程。需要用户回答真实决策时只暂停业务交互，保持同一个 Stage Entry 与 `attempt`，不得 `finish` 或重新 `start`；用户回答和 Agent 回复由异步 Supplement 按顺序聚合。只有用户明确重新执行入口命令才创建新 Entry 并递增 `attempt`。硬前置失败使用 `blocked + prerequisite_missing`，其他真实失败使用对应受控 failure code。

保存并检查 start 的 JSON 结果，然后继续业务步骤。

## Workspace 合同修订入口

只有 Router 实际返回 `workspace_revision.required=true` 时使用本分支。该上下文已经核对
显式 source、Workspace registry、当前 repo 身份及根 Spec 的规范化收据；不得根据聊天声明
或自行改状态启用。普通重复 Propose 仍遵循原阶段路由。

在修改任何 Spec/Tasks 前，用 Router 返回的精确值执行：

```bash
python3 "{plugin_root}/scripts/workspace.py" --workspace-root "{workspace_revision.workspace_root}" workspace-begin-revision --feature "{feature}" --repo "{workspace_revision.repo}" --expected-workspace-receipt "{workspace_revision.source_sha256}"
```

该命令创建或恢复同一 `contract_amendment` Repair Revision。使用它返回的 `repair_key`、
`source_sha256` 和 `repair_revision`，不要重新对整个 Markdown 求哈希：
Workspace source 的身份是排除状态后的根合同收据，状态推进不能产生另一轮修订。
完整读取根 source 及对应 Repo Handoff，并执行下面同一套需求决策树。
返回 `status=in_progress` 时恢复同一修订，不重复追加已经写入的 revision/task；
返回 `status=bound` 时跳过编辑与再次 rebind，直接执行 `validate-tasks --stage apply`
确认后返回父 Coordinator。不得对已经 bound 的修订调用未绑定阶段的 `--repair-key` 校验。
若先前 rebind 遗漏父合同收据，入口仅在工件与 bound receipts 一致、当前修订任务仍全部
pending 且未开始新执行/评审时返回 `binding_reopened=true, status=in_progress`，重新打开
同一个 revision。继续修正已有工件，不追加重复任务，不删除状态；已变化或已执行时停止。

修订 Spec 时必须将 frontmatter 的 `execution_scope: single`、`workspace_repo`
和 `workspace_contract_sha256` 绑定到本次 Router 的 repo 与 `source_sha256`。
只更新 Repair Revisions 表中的 Source SHA-256 不算完成绑定。Propose 校验与
`repair-rebind` 都会核对这三个字段，不允许旧父合同收据进入 bound。

本分支与 Bug Fix 共用 **Repair mode** 的工件规则：保留原任务 ID、说明、依赖、完成状态
和最初的 Apply baseline；追加当前 `R-NN` 的 pending 任务；只读仓追加真实契约复核任务。
禁止清空私有状态、把旧 completed 任务重置为 pending、重建基线或复用已经失效的 APPROVE。
缺失基线或修订冲突时停止，不根据代码或聊天猜测私有状态。

修订编辑完成后执行带 `--repair-key` 的 Propose 校验，然后执行 `repair-preflight` 取得
当前新 Spec/Tasks receipts，并调用：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" repair-rebind --feature "{feature}" --repair-key "{repair_key}" --expected-spec-receipt "{new_spec_receipt_sha256}" --expected-tasks-receipt "{new_tasks_receipt_sha256}"
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" validate-tasks --feature "{feature}" --stage apply
```

`repair-rebind` 保留原始基线、使旧 Review seal 失效；未开始 Apply 的仓回到 ready，已有
Apply 基线的仓回到 applying。完成上述校验后本 Propose job 即结束并返回父 Coordinator，
由 Workspace 后续 Apply/Archive 批次继续。不得借用完整 `bug-fix` 命令提前实现或归档子仓。

## 输入与路径

- 输入：feature 名、需求描述或用户提供的 source；`--loop` 由公共入口与 Router 消费并剥离，不能混入需求正文。
- active 目录：`docs/specs/active/{feature}/`。
- `spec.md`：需求契约，只说明为什么做、要实现什么和已经确认的边界。
- `tasks.md`：执行进度，只记录端到端任务、可选 AC 引用、依赖和状态。

在任何写入前检查：

- `docs/specs/completed/{feature}` 不存在；
- active 已有同名目录时，普通 Propose/Propose --loop 只允许恢复身份与阶段一致、仍处于 planning 的 HelixHarness Propose；Repair mode 必须已由 `begin-repair` 或上述 Workspace 修订入口锁定同一 active HelixHarness feature，允许修订 `planning|ready|applying|implemented` 的同一 Spec/Tasks；存在不符合当前合同的工件或其他工件冲突时停止；
- 只消费 Router 返回的 `workflow_arguments`；其中已剥离循环控制参数，不重新解析原始 `$ARGUMENTS`。

不满足时停止，不覆盖或迁移既有工件。

需求来源是文件路径或 URL 时，必须先完整读取 source（包括可见批注/评论），把其内容作为需求事实来源；路径或 URL 不可读时明确阻断，不能把路径字符串本身当需求正文。直接描述则以用户原文为初始 source。



## 1. 按需加载项目上下文

按 `reference/loading-protocol.md` 加载项目上下文：

1. 按协议确认原生项目入口；已注入内容直接沿用，未注入时只检查当前 Host Kit 声明的根级入口路径。缺失或内容冲突时如实声明并从索引与代码事实调查，不搜索其他宿主或旧版本入口。
2. 在第一轮需求决策前读取 `docs/harness/index.md` 与 `docs/knowledge/index.md`，按当前协议的“加载时机”选择必读项及相关叶子。
3. 结合初始需求、已经查明的代码事实和后续决策，读取当前 Propose 阶段的全部必读项及当前触发条件命中的叶子；每轮需求决策后若需求边界变化，重新判断是否需要加载新的相关文档。
4. 在第一轮问题前用 `已加载：<实际项目入口路径及加载状态，或“项目入口缺失/冲突”及声明路径；索引和叶子列表>` 声明实际加载范围。索引缺失时必须如实声明缺失，并从代码、配置、测试和迁移文件补足事实来源；不得声称已读取不存在或未读取的文档，也不自动调用 Init 或 Status。
5. 每个需要用户决定的问题都应给出足够的仓库事实及其事实来源；事实不足时先继续调查，不能把可由仓库回答的问题交给用户。
6. 数据库相关需求只读取仓库内知识、DDL、migration、ORM 与测试；禁止自动连接或查询任何真实数据库。

## 2. 需求决策树

用户提供已有讨论摘要时，先用当前仓库事实核验并复用已确认的决定，再按下述决策树完成共享理解确认和正式 Spec/Tasks 生成。

持续访谈，直到与用户达成共享理解。把全部决策映射为 design tree：每个决策都继续分出依赖它的后续决策。

每轮计算 frontier 前，先使用已加载的 `docs/harness/`、`docs/knowledge/` 和项目入口，再按需要直接调查仓库内的代码、配置、测试和 migration。查找事实是 Agent 的工作，不是用户的工作；不得向用户询问可以自行查明的信息，也不得猜测仓库现状。

代码、配置和已有约束中能够直接查明的客观事实，由 Agent 自行调查并作为提问背景；如果这些事实仍对应多个可能的产品行为或修改方案，必须交由用户决策。决策是用户的工作：将每个决策交给用户，并等待回答。

按轮处理 design tree。frontier 是所有前置条件已经确定、现在无需猜测其他答案就能询问的决策。每轮必须一次展示整个 frontier：逐题编号、给出推荐答案，然后等待用户回答再进入下一轮；不得只选择一题形成历史式一问一答。

### 视觉还原验收（可选决策）

仅当需求涉及 UI/样式/组件改动且存在设计素材（MasterGo 链接或设计截图）时，design tree 中必须包含"本次是否要求视觉还原验收"这一决策；素材存在性由 Agent 在提问前自行调查，不要求用户先替 Agent 判断。默认不要求，不把视觉验收设为默认必跑项，也不因素材存在自动升格为必做。用户明确选择要求后，把 `视觉还原验收: 是` 与 `Design References`（素材清单）写入 Spec 的 Testing Decisions；相关 AC 必须可观测（几何/样式/交互行为），素材作为 AC 的验收依据。不要求或无素材时，Spec 不记录视觉验收要求，后续 Apply/Review 无任何视觉验证负担。

design tree 必须覆盖所有会影响目标行为、范围、设计或正式 Spec 的未决选择，不得因其属于实现、异常处理、兼容性或测试策略而静默代选。只有用户已明确决定，或现有约束确实只允许一种做法时，才视为已确定。Spec 确认后的纯任务拆分无需再次询问。

每个问题都必须说明已查明的相关事实及事实来源，并可包含多段说明和多个选项。推荐答案必须给出一个具体选择、选择理由和主要取舍；不能只复述问题或提供没有立场的选项列表。逐题编号并严格使用以下格式，问题之间保留空行：

```text
❓ **Q1** – **<决策标题>**: <问题正文；可以包含多段说明、事实来源和多个选项>

💡 <具体推荐、理由和主要取舍>
```

同一轮问题按 `Q1`、`Q2` 连续编号。用户回答后重新计算下一轮 frontier；尚未展示的问题不得视为用户已接受推荐答案。

每轮完整 frontier 必须由 Agent 以普通对话文本一次展示并等待用户回答，不使用结构化问答、选择器、表单或弹框，也不得拆成多次单题交互。该限制只适用于需求决策，不影响统计授权等固定门禁。

每轮回答都会重塑 design tree：已确定的决策向外推进并解锁依赖分支。回答后重新计算 frontier，只展示新解锁的完整 frontier；依赖本轮其他未决答案的问题必须留到后续轮次，不得提前询问或替用户假定答案。

只有 frontier 为空、design tree 的每个分支都已处理且没有静默假设时，需求收敛才结束。此时总结已经确认的事实、决策与假设，并请求一次共享理解确认；确认前不得写入 Spec/Tasks 或开始实现。总结前必须原样展示以下固定提示：

```text
📌 **当前阶段：共享理解确认（非决策问答）**
当前 design tree 的 frontier 已为空，每个分支都已处理且没有静默假设，因此本轮不再展示问题；请核对下方共享理解，回复“确认”后才生成并校验正式 spec.md 与 tasks.md。
```

用户确认后直接进入写入，不得重复已经确认过的问题。纯 task 数量、粒度或自动拆分方案在确认 Spec 后由第 4 节派生，不得伪装成尚未出现的 design-tree 分支；只有用户指出理解有误或主动改变需求，才据此重开受影响的 frontier。不得边提问边生成正式 Spec，也不得再启动独立文档 Review Agent。

## 文件工具调用纪律

宿主提供 Write / Read / Edit 文件工具时必须遵守：

- Write 必须在同一次工具调用中显式提供绝对 `file_path` 和 `content`；JSON 对象参数没有顺序要求。
- 新文件直接使用 Write，不得先用 `touch` 创建空文件。
- 文件已经存在时，必须先 Read 同一路径，再使用 Edit 或 Write。
- 出现 `required parameter file_path is missing`，表示实际工具参数确实缺失；不得声称“已经携带”或归因于参数顺序。
- 此时只允许重试一次，并重新显式填写完整 `file_path` 与 `content`；不得通过 `touch → Read → Edit` 绕行。
- 所有成功结论以工具返回结果和最终文件读取结果为准。

## 3. 生成 Spec

读取 `templates/spec.md.tpl`，在全部必要决策与共享理解确认后生成或补全 `docs/specs/active/{feature}/spec.md`；正式 Spec 是本需求首次写入的业务工件，模板定义 HelixHarness 自包含正文，但最终文件名仍统一为 `spec.md`。frontmatter 至少包含：

```yaml
feature: {feature}
status: planning
testing_seam_confirmed: true
```

可以记录当前 branch 作为说明，但它不参与流程门禁。

普通 Single 不写额外 scope 字段。仅当本次完整 Single Propose 是由上层 Adapter 调度，且受控 execution context 同时提供了 parent contract source、当前 repo key 与确定性 contract receipt 时，保留 Adapter 传入的父合同绑定字段；不得从目录名、需求正文或当前仓自行猜测：

```yaml
execution_scope: single
workspace_repo: {adapter_repo_key}
workspace_contract_sha256: {adapter_contract_receipt}
```

这三个字段只声明本仓仍是 Single participant 及其父合同身份，不使本正文承担父级调度逻辑。字段缺失、重复、receipt 格式非法或 source 与父合同不一致时停止，由上层 Adapter 重新派发；不得静默降级成普通 Single feature。

Repair mode 不写 `trigger` 或 `bug_signature` 需求身份字段；使用已锁定的 Repair state 保留历史，设置单调递增的：

```yaml
repair_revision: {N}
```

并在正文 `Repair Revisions` 表追加当前 revision。旧 Tasks 行及状态保留，新行使用当前 `R-NN` 且为 `pending`。修订期间保留 Spec/Tasks 原有 frontmatter 状态，不套用普通 Propose 的 `planning → ready` 转换；最终状态由 `repair-rebind` 设置。恢复同一修订时沿用已绑定的 Repair state，不重建 revision，不手工重置 Apply baseline 或 Review state。

Repair Revisions 的列为 `Revision | Repair Key | Kind | Source SHA-256 | Summary`；
当前行使用 `R-NN`、已锁定的 `repair_key` / `source_sha256` 和 `contract_amendment`
（实现回归按其已锁定 kind）。哈希必须写为裸的 64 位小写十六进制值，不加反引号。
Tasks 增加 `Revision` 列，原本没有修订号的旧行标为 `R-00`；旧 ID、描述、依赖、状态
保持原样，新任务使用更大的 ID、当前 `R-NN` 和 pending。

正文建议覆盖以下信息；允许使用符合仓库语言的等价标题或合并表达，确定性校验不以标题措辞阻断：

1. Problem Statement
2. Solution
3. User Stories / Acceptance Criteria
4. Implementation Decisions
5. Testing Decisions
6. Out of Scope
7. Further Notes

Testing Decisions 优先选择仓库现有最高层公共 seam。测试 seam 必须作为需求决策树 frontier 的用户确认内容或共享理解中的明确确认项；正式 Spec 写入 `testing_seam_confirmed: true`，未确认不得进入 ready。User Stories / Acceptance Criteria 中的每项可验收行为必须使用稳定的 `AC-01`、`AC-02` 编号；Implementation Decisions 使用 `D-01`、`D-02` 编号。不得刻意扩写成完整 PRD，也不得写具体文件路径、执行步骤或实际验证结果。

## 4. 自动派生 Tasks

从已确认 Spec 自动派生 tracer-bullet 垂直切片及真实阻塞关系，不得因普通任务拆分再次打断用户，也不要求二次批准后才进入 Apply。

只有拆分过程暴露出一个此前未出现、尚未确定且会改变计划、设计或 Spec 的真实决策分支时，才回到需求决策树并按同一 design-tree 合同展示对应完整 frontier；用户明确要求审阅任务拆分时，才展示全部候选及其粒度、阻塞、合并或拆分关系供确认。纯执行粒度选择不伪装成新的设计决策。

读取 `templates/tasks.md.tpl`，在同目录生成 `tasks.md`。Tasks 只保留执行和续跑所需的机器可读字段：每行必须有稳定 ID、非空任务说明和状态；`Covers`、`Blocked By` 与其他列按需使用。脚本不强制固定正文标题、完整列结构、AC 映射或说明措辞。

拆分规则：

1. 按可独立完成并验证的端到端行为形成任务，不按数据库、后端、前端、测试等技术层横向拆分；
2. 小需求只生成一个 task，大需求才生成多个；不建立固定任务数量或规模档位；
3. 每个 task 只写 ID、非空的简短端到端任务说明、可选 AC 引用、阻塞 task 和状态；不得复制 Spec 正文、实现设计、文件路径、测试计划或预期输出；脚本只检查说明存在，不判断措辞或业务语义；
4. 建议用 `Covers` 关联 AC、用 `Blocked By` 表达真实依赖；脚本不因可选关联缺失而阻断，但已有依赖必须可解析且无环；
5. 初始 task 状态为 `pending`。

已存在身份一致的完整 `status: planning` Spec 时，只恢复缺失或未完成的 Tasks 生成；已有可继续执行的 `tasks.md` 不得自动重新派生。用户补充或改变需求时，重新打开受影响的决策分支并再次确认共享理解；若变化已经影响任务范围，再依据更新后的 Spec 修正 Tasks，不能依赖脚本猜测语义差异。

普通 Propose 生成或恢复 Tasks 后执行：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" validate-tasks --feature "{feature}" --stage propose
```

普通 Propose 只有校验器确认 HelixHarness 身份、阶段状态、任务 ID、非空说明、状态和已有依赖可继续执行后，才把 Spec `status` 从 `planning` 更新为 `ready`，随后用同一命令复验。校验失败时先读取明确错误；若是本轮生成造成的 frontmatter、状态、任务 ID、空说明或依赖错误，Agent 可在不改变用户需求和任务范围的前提下最小修正，并立即重跑同一校验。涉及真实需求取舍、无法确定如何修复、或修正后仍失败时，才停止并输出 `BLOCKED:lite_artifact_invalid`。不得跳过校验、猜测通过、安装依赖或生成 `proposal.md`、`design.md`、`verification.md`、独立 `review.md`。

Repair mode 使用已锁定并保存在当前 Repair state 中的同一 `repair_key`，改用以下只读校验：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" validate-tasks --feature "{feature}" --stage propose --repair-key "{repair_key}"
```

此路径校验未绑定的 Repair state、修订身份、旧任务完整保留、新增任务为当前 revision 的 `pending`、任务 ID 及依赖图。不得把旧 `completed` 任务改成 `pending` 来通过普通校验。Bug Fix 入口校验通过后返回其编排执行 `repair-preflight` 与 `repair-rebind`；Workspace 修订入口按上文在本 Propose job 内完成 rebind 和 apply 阶段校验后返回父 Coordinator。都不得手工把 Spec/Tasks 改成 `ready`。Repair key 缺失、不匹配或校验失败时停止，不回退普通 Propose 校验。

## 5. 结束

普通 Propose 确认 `spec.md` 与 `tasks.md` 可读、Spec 为 `status: ready`、Tasks 处于可开始状态且没有未决 frontier；Repair mode 则确认对应修订校验通过且没有未决 frontier，Workspace 修订还须完成 rebind 及 job 的 completion check。满足对应条件后，先执行 `metrics.py ... trace-event --feature "{feature}" --stage propose --event-id lite_tasks_ready --kind gate --status passed`，再执行 `metrics.py ... finish --feature "{feature}" --stage propose --status completed`。统计关闭或 recorder/trace 写入失败只输出 warning，不得改变业务成功结论。

普通 Propose 完成后报告下一步 `/helix:apply {feature}`。`--loop` 入口返回公共 Propose 的循环编排继续下一阶段；Bug Fix repair mode 返回修订编排，由其完成 Repair rebind 后继续 Apply；Workspace 修订 job 返回父 Coordinator，由其继续拓扑批次。当前阶段不自动提交。
