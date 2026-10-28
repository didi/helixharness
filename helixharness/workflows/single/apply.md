---
description: "Single Apply：以 spec/tasks 续跑，按 Lite TDD 实现，完成最终测试和 Standards + Spec Code Review。"
---

# Single Apply · Implement

仅当公共 Apply Router 或 Propose `--loop` 已定位当前单仓 feature 的 Apply 阶段，或 Bug Fix 已完成 Repair rebind 后执行本工作流。内部阶段调用保留 `trigger=loop|bug-fix`；所有入口都执行相同前置、TDD、完整测试与双轴 Review。

HelixHarness Apply 不读取或锁定宿主；它只依据 Spec、Tasks、仓库基线与当前工作区继续同一业务合同。

## 统计授权状态

在启动 HelixHarness 统计或进入业务硬前置前，读取 `reference/eval-protocol.md` 第 2 节的共享 consent 合同，并执行：

```bash
python3 "{plugin_root}/scripts/telemetry_consent.py" --repo-root "{repo_root}" --platform "{current_platform}" resolve
```

Apply 只读取当前 `effective_mode`。即使无缓存或 `prompt_due=true`，也不得展示固定数据说明或 `allowed_actions[]`，不得调用 `choose/set/install-python`；当前没有有效开启记录时按 disabled 继续，把授权询问留给下一次用户直接执行 HelixHarness Propose。未注册宿主同样不询问并按 disabled 继续。resolver 或缓存异常只输出 `WARN:telemetry_consent_cache_unavailable` 并按 disabled 继续，不得阻塞业务或手写缓存。

HelixHarness 只复用统一 consent 的当前结果，只使用当前 HelixHarness Ledger；随后直接执行 `metrics.py start`，由 HelixHarness Ledger 自己维护 `docs/eval/**` 的跟踪准备。Apply 不执行版本更新检查，也不复述 Propose 的版本结论。

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

在定位 feature 后、进入硬前置前，执行：

```bash
python3 "{plugin_root}/scripts/metrics.py" --repo-root "{repo_root}" --platform "{current_platform}" start --feature "{feature}" --stage apply --trigger "{metrics_trigger}" --route-reason "{router_reason|loop_orchestrated|bug_fix_active}"
```

`router_reason` 取公共 Router 本轮返回的受控 `reason`；Propose --loop 固定为 `loop_orchestrated`，Bug Fix 固定为 `bug_fix_active`。同步 recorder 按上一步解析后的有效 consent 写 HelixHarness Ledger 和私有 Workflow Trace，不读取 session，也不得阻塞 Apply；阶段结束后由独立 Runtime 异步补充宿主数据。需要用户回答时只暂停业务交互，保持同一个 Stage Entry 与 `attempt`，不得 `finish` 或重新 `start`；这明确包括最终测试出现失败、正在核对是否属于已知基线并等待用户确认是否接受豁免的场景。用户回答和 Agent 回复由异步 Supplement 按顺序聚合。只有用户明确重新执行入口命令才创建新 Entry 并递增 `attempt`。前置缺失、验证失败、Review 未通过或 Agent 失败分别使用受控 `failure_code`；一旦以 `failed|blocked` 真正收口，本次 Apply 必须停止，后续显式重跑先 `start` 新 attempt，不得继续旧 attempt。

## 硬前置

- `docs/specs/active/{feature}/spec.md` 存在；
- `docs/specs/active/{feature}/tasks.md` 存在；
- Spec 与 Tasks 的 feature 必须一致；正常入口 Spec `status` 为 `ready|applying`，恢复入口允许 `implemented|completed` 但只用于修复缺失/无效 Evidence；Tasks `status` 为 `ready|applying|completed`；
- Tasks 至少包含可识别的任务 ID、非空任务说明和状态；脚本不判断说明措辞或 AC 语义；已有依赖必须有效、无环且已完成任务不能越过未完成依赖；
- 同目录不存在 `proposal.md`、`design.md` 或 `verification.md` 等非当前合同工件；
- 当前 spec 没有未确认的 Decision Needed。

Spec 为 `ready` 的首次入口先调用统一门禁确认初始 Tasks，再记录 Apply 开始时的 Spec 收据与执行前工作区基线：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" bind-apply --feature "{feature}"
```

`bind-apply` 是首次入口的第一项工作区动作。不得在 `bind-apply` 前要求用户归属、提交、删除、还原、暂存或迁移普通脏文件；普通 tracked、staged、unstaged 与非 ignored untracked 状态不阻断 Apply，也无需判断既有脏路径属于哪个 feature。不得为了把工作区清理干净而执行 `rm`、`git restore`、`git checkout --`、`git stash`，或另建 branch/worktree；这些动作既不是记录基线的前置，也不得由 Agent 以“隔离 Review”为由提出。只有 dirty submodule、nested Git repo、目录型条目等脚本明确拒绝的非普通状态才按错误停止。

执行前工作区基线是当前 `HEAD` 与 Apply 开始前已存在的 staged、unstaged、非 ignored untracked 文件内容摘要；它只记录状态，不会清理、暂存、提交或复制用户文件。`bind-apply` 把 `apply_spec_receipt_sha256`、`apply_baseline_head`、`apply_baseline_sha256` 与既有未提交路径清单原子写入当前 checkout 的私有状态 `.helixharness/workflow/{feature}/apply-state.json`；该目录被 Git 忽略且不属于业务工件，`tasks.md` 继续只记录任务和进度。中断、换 session 或在同一 checkout 换兼容宿主后仍以这些持久化事实区分本需求增量与未变化的用户改动。进入 Review 时，Apply 开始后内容未变化的既有未提交路径自动排除，不询问用户；只有某个路径在执行前已有未提交改动、Apply 又修改了同一路径，脚本才因无法证明两部分改动的边界而停止并返回一次精确的 `DECISION_NEEDED`。随后调用统一门禁，并把 JSON 返回的 `next_task` 作为续跑位置：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" validate-tasks --feature "{feature}" --stage apply
```

缺少业务前置或校验器不可用时停止并输出 `BLOCKED:lite_artifact_invalid`。校验返回明确错误时，Agent 可先修正不改变需求含义的 frontmatter、状态、任务 ID 或依赖错误并立即重跑；不得借修正重新定义需求、重排业务任务或伪造完成状态。Apply 已开始后，私有状态缺失、无效或无法可靠区分既有脏改动时返回 `DECISION_NEEDED`；这包括只带业务工件的 clean clone 或新 checkout，不得把当前工作区猜成空基线或现场重建旧基线。需要产品/技术取舍、应返回 Propose 重建任务，或修正后仍失败时才阻断。不得创建临时 Work Brief、补建非当前合同工件、跳过校验或猜测通过。

恢复入口若 Spec 已是 `implemented|completed` 但 Evidence 缺失或 preflight 无效，先用既有 Apply Spec 收据与执行前工作区基线复验 Tasks，再把 Spec 恢复为 `applying`，保留旧 Evidence 供诊断并在新 Review 完成后原子替换为 Archive trace pending 的新 Evidence；重新完成最新全量验证、Review Scope 和双轴 Review，不得沿用无效 APPROVE。

Bug Fix 必须先完成 `begin-repair` 与 `repair-rebind`。`repair-rebind` 只更新 Apply Spec receipt，保留首次 `apply_baseline_head` 和 baseline manifest，并使旧 Review seal 失效；Apply 不得再次 bind 或把 fixed point 缩到 repair 开始时。最终 Review Scope 仍覆盖首次 Apply baseline 到修复后最终 HEAD。

## 1. 确认任务契约并保护当前工作区

1. 读取完整 spec，以当前内容作为实现依据；每个 task 前、进入 Review 前和写 Evidence 前都重跑门禁，比较私有 Apply state 中忽略 `status` 的 `apply_spec_receipt_sha256`。不匹配时输出 `BLOCKED:lite_spec_drift`，不得继续使用旧 Tasks 或旧 Review。
2. 读取完整 tasks。校验器同时返回 `in_progress_tasks` 和建议 `next_task`：有多个执行中任务时不阻断，Agent 结合任务说明与当前代码判断继续顺序；没有执行中任务时，`next_task` 是依赖已完成的第一个待执行任务。不得仅因多个 `in_progress` 自动改写、重新派生或重排 Tasks。
3. `bind-apply` 成功后才把 Spec 与 Tasks 的 `status` 更新为 `applying` 并复验；中断后由私有 Apply state 保留执行前工作区基线，业务工件只保留阶段状态和逐 task 进度。
4. 修改前只读检查当前分支、`HEAD`、暂存区、工作区和非 ignored untracked 文件以避免覆盖用户已有改动；检查结果不得转化为普通脏文件的清理、归属确认或逐文件提问。Review 开始前按 `code-review` 确定 fixed point：仅审查当前未提交增量时自动使用当前 `HEAD` 作为 fixed point；只有需要包含已提交历史或范围不明时才询问用户，不得根据分支名、时间或语义猜测其他起点。
5. 执行期间发现 Spec/Tasks 契约失配或与当前任务无关的外部修改时停止，不覆盖用户工作。

## 2. 按需加载约束与知识

完整遵循唯一的 `reference/loading-protocol.md`：按协议确认原生项目入口，再读取 `docs/harness/index.md`、`docs/knowledge/index.md` ，根据加载时机读取相关叶子。除了当前 Spec/Task 命中的叶子，还必须读取全部 `apply 阶段始终加载` 项。每个 task 开始前按 Spec 与当前 Task 重新评估触发条件，并遵循协议的时间戳缓存；首次和变更重读后声明 `已加载：<实际入口路径及加载状态，或“项目入口缺失/冲突”及声明路径；索引、叶子>`，未变时声明沿用范围。Harness 是规范性约束；Knowledge 必须由当前代码、配置、测试、DDL 或 migration 核验。

缺少 docs 时如实声明后直接探索代码，不自动调用 Init 或 Status。最终验证和双轴 Review 前直接读取 `docs/hooks/cr-checklist.md`（若存在），它不走 index，并加入 Standards 来源。涉及数据库时禁止自动连接或查询真实数据库。

## 3. HelixHarness TDD 实现

完整读取并执行 `skills/tdd/SKILL.md`，复用第 2 节已加载的项目入口、Harness、Knowledge、Spec 与 Task 范围。按 `tasks.md` 的可执行 frontier 推进；每个 task 开始时设为 `in_progress`，完成相关验证后立即设为 `completed`，失败或外部前置不足时设为 `blocked` 并停止依赖它的 task。

实现期间定期运行当前单测试文件，以及仓库已有的 typecheck、编译、lint 或等价标准检查。TDD seam、RED-GREEN 切片、好测试标准与非 TDD 变化的确定性检查均以该 Skill 为唯一方法来源，Apply 不重复定义。

### UI 任务视觉验证（仅已声明时）

仅当 Spec 的 Testing Decisions 已声明 `视觉还原验收: 是` 时执行；未声明时本节不存在，UI 任务与其他任务一样只走 HelixHarness TDD 与仓库标准检查。task 涉及 UI/样式/组件改动时，该 task 完成前必须通过当前 Host Kit 的 `invoke_skill` 映射加载并执行 `skills/d-d2c/SKILL.md` 的截图+几何对比链路，并遵循该 Host Kit 的技能生命周期约束：产物落 `d-d2c/temp/<page>/r<N>/` 目录（`geometry-diff.json`、`*-overlay.png` 等），并把 d2c 命令与输出摘要作为该 task 的最小 TDD 事实保留。hard violation > 0 时分析根因→修复→重拍，直到 0 new violation；同一 violation 连续 3 轮无改善时暂停，保留证据并等待用户豁免或改方案。d2c skill 或浏览器不可用时输出 `WARN:visual_verification_unavailable`，保留已获证据并暂停等用户豁免或改代码级验收，不得自动跳过。需登录态时记录 session-state 使用，不自动跳过。

实现期间只保留该 Skill 规定的最小 TDD 事实，不写正式 Evidence。TDD 循环中不得创建或更新 `verify-report.md`，也不得为了记录证据而重复执行已经取得有效 RED/GREEN 结果的测试；若 Apply 在整体 APPROVE 前停止，不生成不完整报告。

## 4. 最终测试

全部实现结束后，基于当前工作区运行一次仓库完整测试套件。若没有可识别的完整套件，则运行与本次改动相关的最广覆盖测试和仓库标准检查。只接受当前代码的本轮结果；确认属于本次回归的失败或必要检查无法执行时停止，不得进入 Review 或声称完成。若失败疑似为 Apply 开始前已经存在的基线，只能保留原始失败证据并暂停等待用户确认；暂停期间不得调用失败 finish。用户明确接受基线豁免后，执行 `metrics.py ... trace-event --feature "{feature}" --stage apply --event-id verification_baseline_waiver --kind fallback --taken true --reason-code user_confirmed_preexisting_baseline`，再运行能覆盖本次范围的最广验证并记录豁免边界，只有该验证通过才可在同一 attempt 进入 Review；trace 失败只输出 warning。用户不接受或无法证明是既有基线时才以失败终态停止。

Spec 已声明 `视觉还原验收: 是` 时，本阶段还必须完成一次 d2c 证据链审核（不重复截图）：每个 UI task 有对应 d2c 产物；`d-d2c/temp/<page>/r<N>/geometry-diff.json` hard violation = 0；`*-overlay.png` 存在且非空；该 task 的 d2c 命令与输出摘要已在最小 TDD 事实中保留。violation 未清零且未豁免时不得进入 Review。用户豁免时保留原始证据、记录豁免边界，并执行 `metrics.py ... trace-event --feature "{feature}" --stage apply --event-id visual_verification_waiver --kind fallback --taken true --reason-code user_confirmed`；trace 失败只输出 warning。

## 5. Standards + Spec Code Review

最终测试通过后，完整读取并执行 `skills/code-review/SKILL.md`。

Apply 只按该 Skill 的最终结果转移流程：

- `MUST_FIX`：由原 Implementer 按 Skill 完成定向修复、最新验证和完整复审；若 Skill 返回 `BLOCKED:lite_review_no_progress`，保留可续跑状态并停止。
- `DECISION_NEEDED`：保留 Spec/Tasks 当前阶段、任务进度和最初的 Apply/Review state，返回需求修订决策树。单仓通过既有 Bug Fix 的 `contract_amendment` Repair mode 收敛并绑定修订；Workspace 子任务把精确决策交回 Coordinator，由父流程确认并修订根合同，再通过受控 Workspace Propose 修订入口继续。旧任务及基线必须保留，只追加修订任务，经 `repair-rebind` 更新合同收据并使旧 Review seal 失效。禁止清除 Apply state 后重建基线或把旧 completed 任务重置为 pending。
- `APPROVE`：最后一次代码修改后的验证与完整复审均已通过，继续生成 Evidence。

整体 APPROVE 且存在一条或多条 Advisory 时，在生成 `verify-report.md` 前必须醒目展示固定标题 `⚠️ ADVISORY（非阻断）`，逐条列出稳定 ID、所属轴、证据、不阻断原因和可选改进，并明确说明：`不修复也保持 APPROVE，可正常生成 verify-report.md 并归档。` 随后用普通文本或宿主可用的等价原生单选交互展示：

1. `保留 Advisory，不修复并继续`
2. `修复全部 Advisory 后重新验证和完整复审`
3. `仅修复指定 Advisory`

选择 1 时保持双轴 APPROVE，把用户选择及未修复 Advisory 原样记入 `verify-report.md`，不降级 verdict，不重复追问同一批稳定 ID。选择 2 或 3 是用户对对应可选改进的显式范围扩展授权；Implementer 只修复获授权项，运行受影响测试和仓库完整测试套件，以同一 fixed point 重新执行 `review-scope` 并启动完整并行双轴复审。如修复引入新的产品、接口、安全或不可逆决策，转为 `DECISION_NEEDED`，不把此选择扩张成未表达的需求授权。代码一旦变化，原 APPROVE 立即失效，必须以新 Review Bundle 的 verdict 为准；自愿修复不增加 `must_fix_rounds`，但在报告正文记录 `advisory_remediation_rounds`、获授权 ID、修复证据和复审结论。复审后只对新出现且尚未处理的 Advisory 展示选择；已明确保留且证据与机制未变的稳定 ID 不再询问。没有 Advisory 时不得制造额外交互。

## 文件工具调用纪律

宿主提供 Write / Read / Edit 文件工具时必须遵守：

- Write 必须在同一次工具调用中显式提供绝对 `file_path` 和 `content`；JSON 对象参数没有顺序要求。
- 新文件直接使用 Write，不得先用 `touch` 创建空文件。
- 文件已经存在时，必须先 Read 同一路径，再使用 Edit 或 Write。
- 出现 `required parameter file_path is missing`，表示实际工具参数确实缺失；不得声称“已经携带”或归因于参数顺序。
- 此时只允许重试一次，并重新显式填写完整 `file_path` 与 `content`；不得通过 `touch → Read → Edit` 绕行。
- 所有成功结论以工具返回结果和最终文件读取结果为准。

## 6. Evidence Receipt

整体 APPROVE 后读取 `templates/verify-report.md.tpl`，一次性创建 `docs/specs/active/{feature}/verify-report.md`。不得把本步骤拆到 TDD 循环中增量执行。frontmatter 使用可确定性读取的结构：

```yaml
---
feature: {feature}
spec_receipt_sha256: {校验器返回的当前 Spec 收据摘要}
tasks_receipt_sha256: {校验器返回的当前 Tasks 收据摘要}
review_fixed_point: {git rev-parse 后的完整 fixed point commit ID}
review_head: {Review Bundle 中的完整 HEAD commit ID}
review_scope_sha256: {review-scope 返回的可复算 sha256}
review_mode: parallel_subagents
standards_verdict: APPROVE
spec_verdict: APPROVE
must_fix_rounds: {任意非负整数}
stage_trace:
  propose: completed
  apply: completed
  archive: pending
---
```

由 Bug Fix 触发时不得给 Evidence 增加 `trigger` 或 `bug_signature` 身份字段；正文应记录当前 `repair_revision`、Repair Task 和最新验证/复审结果，旧报告只作诊断并由本次最新报告原子替换。

正文至少包含：

- `## AC → Task → Result`：只用 AC ID、Task ID、PASS/MANUAL 与证据引用证明覆盖，不复制 Spec 或 Tasks 正文；
- `## TDD Evidence`：按实际 RED-GREEN 切片记录 Task/AC、测试名、测试文件及简短 RED/GREEN 摘要；不复制完整输出，不为非 TDD 变化伪造循环；
- `## Verification`：实际命令、退出码、执行时点与结果摘要；
- `## Review`：`review-scope` 的 reviewed/excluded path 列表、两个分轴报告、Advisory 稳定 ID、用户选择、`advisory_remediation_rounds`、Must Fix 修复及完整复审结果。

Spec 已声明 `视觉还原验收: 是` 时，`## TDD Evidence` 与 `## AC → Task → Result` 必须包含每个 UI task 的 d2c 命令与输出摘要。

不得在 evidence 中写 Agent 推理、token 或虚构耗时。Review 必须分别保存 fixed point、HEAD、Review Bundle 摘要、两个分轴报告和每轮 verdict；不得把两轴合并成一个结论。统计固定证据写入 `docs/eval/{feature}/eval-log.json`，异步 Supplement 不写入 evidence；Evidence receipt 只用于记录实际验证和 Review 结果。Spec/Tasks 收据绑定 Review 后业务工件；Review Scope 绑定 Review 时的 HEAD 与相对执行前工作区基线的代码内容，并把完整路径摘要原子封存在私有 `review-state.json` 供 Archive 重算。既有未变化的未提交路径明确排除但写入 Bundle。这些私有状态不是新增规划文档。

所有 task 均为 `completed` 后，把 Tasks `status` 更新为 `completed`，再次执行 `validate-tasks --stage apply`；它必须返回 `next_task: null`。再把 Spec `status` 更新为 `implemented`，执行 `validate-tasks --stage archive` 并把返回的 `spec_receipt_sha256`、`tasks_receipt_sha256` 写入 evidence。Evidence 正文必须记录实际验证与 Review 结果且非空。随后执行：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" archive-preflight --feature "{feature}"
```

只有 preflight 确认 Spec/Tasks 已完成、Evidence 非空、收据摘要匹配、`must_fix_rounds` 为任意非负整数、当前内容与 Review Scope 一致（允许原样暂存、commit、push，不要求 HEAD 相等）、双轴 verdict 通过且阶段轨迹可归档，Apply 才成功。Apply 调用不带 `--review-choice`，不接受 Archive 的直接归档确认替代本轮验证和 Review。Evidence 正文的表达和表格布局由 Agent 根据实际结果负责，脚本不判断标题或语义。失败时保留可续跑状态，不伪造 evidence 有效或提示归档。

preflight 成功后执行 `metrics.py ... trace-event --feature "{feature}" --stage apply --event-id archive_preflight --kind gate --status passed`。该结构化记录失败只输出 warning，不改变已经通过的业务门禁结论。

业务完成后调用：

```bash
python3 "{plugin_root}/scripts/metrics.py" --repo-root "{repo_root}" --platform "{current_platform}" finish --feature "{feature}" --stage apply --status completed --metrics-json '{"verification":"passed","standards_verdict":"APPROVE","spec_verdict":"APPROVE","must_fix_rounds":{任意非负整数}}'
```

统计写入失败只输出 warning，不得撤销 evidence 或把成功 Apply 改为失败。正常路径更新当前 active attempt；若同一 session、同一 consent epoch 的 Apply 曾被过早以 `failed|blocked` 收口但业务随后确实完成，completed finish 保留原终态并新增一个从旧 `ended_at` 续接的 completed successor attempt；若最近终态已经是 completed，则只返回幂等成功。不同 session、授权周期变化、存在更新的 in-progress entry 或私有状态不完整时禁止自动续接并返回真实 warning。不得把 failed entry 原地改成 completed。普通 Apply 完成后提示下一步 `/helix:archive {feature}`；`--loop` 入口返回循环编排，Bug Fix 入口返回修订编排，由调用方继续 Archive。当前阶段不自动 commit、push 或部署。
