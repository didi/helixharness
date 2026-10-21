---
description: "Single Archive：校验 spec/tasks/evidence，按确认沉淀知识并完成归档。"
---

# Single Archive · Finalize

仅当公共 Archive Router 或 Propose `--loop` 已定位当前单仓 feature 的 Archive 阶段，或 Bug Fix 已完成 Apply 后执行本工作流。内部阶段调用保留 `trigger=loop|bug-fix`，执行相同归档门禁。

Router 返回 `reason=lite_archive_telemetry_recovery` 时，业务归档已经完成；仍先读取本轮统计授权状态，然后跳过新的 Stage start、业务 preflight、知识交互和 move，直接执行第 4 节的“统计收口恢复”。

## 统计授权状态

在启动 HelixHarness 统计或进入归档门禁前，读取 `reference/eval-protocol.md` 第 2 节的共享 consent 合同，并执行：

```bash
python3 "{plugin_root}/scripts/telemetry_consent.py" --repo-root "{repo_root}" --platform "{current_platform}" resolve
```

Archive 只读取当前 `effective_mode`。即使无缓存或 `prompt_due=true`，也不得展示固定数据说明或 `allowed_actions[]`，不得调用 `choose/set/install-python`；当前没有有效开启记录时按 disabled 继续，把授权询问留给下一次用户直接执行 HelixHarness Propose。未注册宿主同样不询问并按 disabled 继续。resolver 或缓存异常只输出 `WARN:telemetry_consent_cache_unavailable` 并按 disabled 继续，不得阻塞业务或手写缓存。

HelixHarness 只复用统一 consent 的当前结果，只使用当前 HelixHarness Ledger；随后直接执行 `metrics.py start`，由 HelixHarness Ledger 自己维护 `docs/eval/**` 的跟踪准备。Archive 不执行版本更新检查，也不复述 Propose 的版本结论。

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

在定位 feature 后、进入归档门禁前，执行：

```bash
python3 "{plugin_root}/scripts/metrics.py" --repo-root "{repo_root}" --platform "{current_platform}" start --feature "{feature}" --stage archive --trigger "{metrics_trigger}" --route-reason "{router_reason|loop_orchestrated|bug_fix_active}"
```

`reason=lite_archive_telemetry_recovery` 时不执行本节的 `start`，按第 4 节恢复既有 Archive Stage。

`router_reason` 取公共 Router 本轮返回的受控 `reason`；Propose --loop 固定为 `loop_orchestrated`，Bug Fix 固定为 `bug_fix_active`。同步 recorder 按上一步解析后的有效 consent 写 HelixHarness Ledger 和私有 Workflow Trace；阶段结束后由独立 Runtime 异步补充宿主数据，不得阻塞 Archive。知识更新等用户确认只暂停业务交互，保持同一个 Stage Entry 与 `attempt`，不得 `finish` 或重新 `start`；用户回答和 Agent 回复由异步 Supplement 按顺序聚合。只有用户明确重新执行入口命令才创建新 Entry 并递增 `attempt`。门禁失败使用 `blocked + prerequisite_missing`，其他失败使用对应受控 failure code。

## 1. 归档前门禁

先执行统一确定性 preflight：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" archive-preflight --feature "{feature}" --review-choice
```

它必须确认：

- `spec.md` 为 `status: implemented`；
- `tasks.md` 为 `status: completed`，所有 task 都已完成；
- `verify-report.md` 正文非空、Spec/Tasks 收据摘要匹配、`must_fix_rounds` 为任意非负整数、原 Review 的 Standards 与 Spec 均 APPROVE；存在 repair revision 时，最新 Evidence 已绑定修订后的 Spec/Tasks receipts；
- 当前文件内容、路径、类型和权限与封存 Review Scope 一致，或已有用户对当前内容的直接归档确认。原样暂存、commit、push，以及测试环境验证后本地内容不变，正常放行；不要求当前 HEAD 等于 Review HEAD，不得仅因提交号或历史变化要求重新 Review。内容由脚本比对，不由 Agent 阅读 diff 后判断是否相同；
- 正常入口为 Spec `implemented`、Archive trace `pending`，返回 `archive_state=ready`；中断恢复入口为 active Spec `completed`、Archive trace `completed`，返回 `archive_state=finalizing`；

- completed 目录不存在同名 feature。

### 内容变化时的用户选择

`--review-choice` 返回 `ready=false`、`decision_needed=review_scope_changed` 表示等待选择，退出码 0 不代表可以归档。先根据 `changed_since_review` 和已核实的上下文，用 1～3 条简短摘要说明主要变化，再询问选择：

- 按目录或已确认的模块职责归纳，例如“订单处理相关代码发生变化”“补充了相关测试”“更新了工作流说明”。只有实际上下文或可核实的 diff 支持时才描述具体逻辑变化；否则只说明涉及哪些目录。
- 文件多时合并同类变化，最多展示 3 项，可附总文件数或“另有其他目录变化”；默认不展开完整文件清单，不生成明细报告。用户追问时再展开对应范围。
- 不展示权限数值、文件类型、摘要 Hash 等技术细节，也不为归档选择做一次额外的完整代码评审。只有确认同步过 master 时才说明“包含同步 master 带来的变化”，不猜测变更来源。
- 摘要只影响展示，底层仍使用完整文件快照校验和绑定本次用户确认。评审时可能存在未提交内容，不把 `git diff review_head HEAD` 直接当作评审后的完整增量。

> 评审后检测到变更，主要涉及：
>
> {1～3 条变更摘要}
>
> 这些变化尚未重新 Review。请选择：
> 1. **重新 Review 后归档（推荐）**
> 2. **直接归档，不重新 Review**

必须等待用户明确选择；`--commit`、`--no-commit`、原有 push 授权和 Loop 自动执行均不代表接受未复审的变化。不得自动采用选项 2。

- 选择 1：复用首次 Apply baseline 和原 `review_fixed_point`，回到 Apply 收尾的最新验证、`skills/code-review` 双轴 Review 及 Evidence 生成步骤；按 Apply 处理真实失败和修复，不重建 baseline、不把旧 APPROVE 复制成新结论、不调用已移除的公共 `/helix:review`。通过后回到本节重新执行 preflight，继续归档。
- 选择 2：把刚展示结果中的 `current_scope_sha256` 原样传给下方命令。脚本重新核对内容与其他门禁，将用户选择、摘要及变化路径写入 `verify-report.md`，并保存私有 `archive-review-acceptance.json`；保留原 Review HEAD、摘要及双轴结论，不重写 `review-state.json`，不把此次变化标记成已复审。

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" accept-review-drift --feature "{feature}" --expected-scope-sha256 "{current_scope_sha256}" --user-confirmed
```

确认前后内容再次变化时，旧确认失效，重新执行本节 preflight 并重新展示选择；仅暂存、原样提交或 push 不会使确认失效。此选择只放宽内容漂移，不豁免工件缺失、任务未完成、原 Review 不通过、Spec/Tasks 收据不匹配、执行前用户改动范围冲突或未解决的 Git 合并冲突。进入 `finalizing` 后仍按第 3 节只允许已授权的 knowledge 更新，不能通过新确认掩盖归档收尾期间的并发代码修改。

只有 preflight 或确认命令明确返回 `ready=true` 后，才执行 `metrics.py ... trace-event --feature "{feature}" --stage archive --event-id archive_preflight --kind gate --status passed` 并继续。`review_accepted_changes=true` 表示用户确认直接归档，完成回执必须写明“内容变更后由用户确认直接归档，未重新 Review”。统计收口恢复不补写该事件；trace 缺失或记录失败只输出 warning。

校验器不可用时停止并输出 `BLOCKED:lite_artifact_invalid`。若失败仅来自 frontmatter、状态或 stage trace 的可确定错误，Agent 可依据当前 Spec、Tasks、实际验证和既有 Review 事实做最小修正后立即重跑；不得改写业务结果、补造收据摘要、验证/APPROVE 或覆盖 completed 冲突。涉及事实缺失、Review 未通过，或修正后仍失败时必须阻断。不移动目录、不创建非当前合同工件、不自动重新 Apply，也不得跳过确定性结果。

## 2. 确认并提交本需求改动

初次 preflight 通过后先执行只读范围检查：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" archive-code-scope --feature "{feature}"
```

正常路径的需求范围等于已封存的 Apply Review Scope；`review_accepted_changes=true` 时使用用户刚确认的内容快照，`reviewed_paths` 仍仅表示原 Review 路径。不根据全局 `git status`、分支名称、提交时间或文件语义重新判断。后续只使用命令返回的 `committed_paths`、`working_tree_paths` 和 `excluded_preexisting_paths`；既有用户改动自动排除。接受内容变化不等于授权 commit 或 push，仍执行下方提交选择。命令失败或返回 `sealed != true` 时输出 `BLOCKED:lite_artifact_invalid`。

随后执行只读 Git 状态检查：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" archive-git-state
```

若返回的 `operations` 或 `unmerged_paths` 非空，立即输出 `BLOCKED:git_operation_in_progress`；不得自动解决冲突或继续归档。无法确定当前分支、目标分支或 merge-base 时停止。Git 信息只用于记录代码获取方式，不得扩大已封存的需求范围。

初次 preflight 返回 `archive_state=ready` 后处理提交选择：

- `--commit` 与 `--no-commit` 同时出现时参数含义冲突，立即停止并要求用户只保留一个。
- `working_tree_paths` 为空：不创建空提交。
- 显式传入 `--no-commit`：把提交决定延后到第 4 节，不授权 commit。
- 显式传入 `--commit`：只授权提交 `working_tree_paths`，不授权 push。
- 未传任一 flag 且 `working_tree_paths` 非空：先展示封存的 `working_tree_paths` 与自动排除的 `excluded_preexisting_paths`，再用普通文本或宿主可用的等价交互展示以下固定三个选项，不依赖平台专属弹窗：
  1. `提交已封存的 Apply Scope 并继续归档`
  2. `暂不提交 Apply Scope，归档后与 eval-log 一起决定`
  3. `取消归档`

选择提交时，重新执行 `archive-code-scope`；范围发生变化则停止。当前必须位于非目标、非 detached 的功能分支。禁止 `git add -A`，只对 `working_tree_paths` 执行 `git add -- {working_tree_paths...}` 和 `git commit --only -m "feat: complete {feature}" -- {working_tree_paths...}`，并验证新 commit 只包含这些路径。提交失败不得扩大范围或假装成功；只有单个 commit 准确覆盖完整需求时才记录 `implementation_commit_id`。

选择暂不提交或传入 `--no-commit` 时保存 `apply_scope_commit_deferred=true` 并继续归档；第 4 节始终处理最终交付选择，把未提交的需求改动和本轮可提交的最终 eval-log 放在同一个选择中。没有可提交的 eval-log 时仍处理需求代码与推送选择。

## 3. 按需准备知识候选

候选准备阶段只读。

仅当初次 preflight 返回 `archive_state=ready` 时，完整遵循 `reference/loading-protocol.md` 的 archive 规则：不加载 harness；读取 `docs/knowledge/index.md`，按归档需要选择叶子并遵循时间戳缓存，随后声明 `已加载：<knowledge index 与叶子>`。返回 `archive_state=finalizing` 表示上次已进入不可逆收口，中断恢复时跳过新的提交与知识交互，直接执行第 4 节。仅从 spec、tasks、evidence 和当前代码事实提取：

- 跨需求稳定的业务术语；
- 跨需求有效的架构或系统知识；
- 已由代码、测试、DDL 或 migration 证实的知识漂移修正。

展示对现有 knowledge 文件的精确修改建议，等待用户确认。此时只能展示候选 diff 或文本说明，`authorize-knowledge` 成功前不得对 knowledge 文件执行 Edit 或 Write，也不得通过格式化器、生成器或 shell 间接写入。用户拒绝知识更新不阻止归档。把用户确认的 repo-relative 文件路径 JSON 编码为一个参数；没有更新时传空数组。在对 knowledge 文件的首次写入前执行：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" authorize-knowledge --feature "{feature}" --paths-json '{confirmed_paths_json}'
```

该命令先要求每个路径都是 `docs/knowledge/` 下已存在的 regular non-symlink 文件且全部祖先目录安全，再重新执行 `ready` preflight，并把只允许本轮变化的精确 knowledge 文件路径写入私有 `archive-state.json`；路径不存在、不是具体文件、包含目录穿越/重复、经过 symlink，或当前 scope 已漂移时均阻断。命令成功后才可对授权列表中的文件执行首次写入。若因提前写入、并发修改或任何 scope 漂移失败，输出 `BLOCKED:lite_review_scope_drift` 并保留当前工作区和 Git 历史供用户判断；不得自动执行 `git reset`、`git restore`、`git checkout`，不得回退已完成的 Apply Scope 提交，也不得修改文件后自行重试授权。用户拒绝知识更新时以 `[]` 授权零路径后继续。不要自动创建新的 CONTEXT/ADR 文档体系，不修改 harness。

任何阶段都禁止自动连接或查询开发、测试、生产数据库。

## 4. 完成归档

### 统计收口恢复

仅当 Router 返回 `reason=lite_archive_telemetry_recovery` 时执行。此时业务目录已经位于 completed，不得重新 start、运行业务门禁、更新知识或移动目录。执行 `archive-code-scope --feature "{feature}" --completed` 恢复已封存范围，再以 `--recover-stage` 调用 Archive finish；无论统计恢复成功、跳过或失败，都进入下文“最终交付选择”。统计恢复失败不阻断独立的代码提交／推送，也不得重复归档。

### 正常归档

在归档前的提交与知识交互结束后（归档后的最终提交／推送选择仍按下文执行）：

1. 若初次状态为 `ready`，以刚通过的 `authorize-knowledge` 作为知识交互后的第二次完整门禁；此后只允许写其记录的用户确认精确 knowledge 路径，不再修改代码。知识写入后将 spec `status` 更新为 `completed`，保持 completed Tasks 不变，将 evidence 的 `stage_trace.archive` 更新为 `completed`；再执行 `finalize-preflight --feature "{feature}"`。它只接受 `finalizing` 业务工件，并把当前 scope 与私有 `review-state.json`（直接归档时为用户确认的 `archive-review-acceptance.json`）逐项比较：只豁免 `archive-state.json` 中精确授权的 knowledge 文件，任何未确认 knowledge 漂移或同时发生的新增、修改、删除代码均以 scope drift 阻断。成功后执行 `metrics.py ... trace-event --feature "{feature}" --stage archive --event-id finalize_preflight --kind gate --status passed`；trace 失败只输出 warning。
2. 若初次状态已经是 `finalizing`，不重复知识交互或改写完成事实，直接复用本轮已通过的 preflight。
3. 原子移动 `docs/specs/active/{feature}/` 到 `docs/specs/completed/{feature}/`。
4. 校验 completed 下 spec/tasks/evidence 可读，active 已不存在；随后执行 `metrics.py ... trace-event --feature "{feature}" --stage archive --event-id archive_move_verified --kind gate --status passed`，trace 失败只输出 warning。

不可逆 move 必须是最后一次业务工件写入；move 后不得再修改归档产物，只允许完成下述统计收口、已封存 Apply Scope 与 Ledger 的精确提交，以及由用户明确授权的功能分支推送。

若第 2 节返回 `review_accepted_changes=true`，业务 move 校验成功后仍执行 `metrics.py ... finish --feature "{feature}" --stage archive --status completed`，但不传下段的 Review Scope 参数、`--reviewed-path` 或 `--implementation-commit`：现有统计协议不表达未复审的当前范围，由 recorder 使用已有代码定位回退，不把用户接受的变化上报为已评审范围。原 Review 及直接归档决定保留在 `verify-report.md`；回执说明本次当前范围未重新 Review。统计恢复时同样读取 `archive-code-scope --completed` 的标记处理。

仅当 `review_accepted_changes=false` 时，业务 move 校验成功后，执行 `metrics.py ... finish --feature "{feature}" --stage archive --status completed`，并把第 2 节保存的封存范围作为重复参数传入：`--review-fixed-point "{review_fixed_point}" --review-head "{review_head}" --review-scope-sha256 "{review_scope_sha256}" --archive-scope-sha256 "{archive_scope_sha256}"`，每个 `changed_paths` 再各传一个 shell 安全引用的 `--reviewed-path "{path}"`；范围为空时不传 `--reviewed-path`。本轮已得到能准确覆盖完整需求的单一实现 commit 时再追加 `--implementation-commit "{implementation_commit_id}"`。recorder 会验证 commit 与封存 scope 收据，写入固定证据，并冻结该需求已登记的全部 Stage Entry 及各 Session 日志的当前字节读取上限后唤醒异步 Runtime；没有准确 commit 时自动写分支回退，但 `reviewed_paths` 仍是唯一需求范围。同步路径不读取 transcript/rollout、不复制原始日志、不调用语义分类或其他流程 Archive finalizer。统计失败只输出 warning，不得回滚、重复 move 或改变归档结论。

统计已开启且上述 finish 返回 recorded 时，在最终交付选择前原样展示其 `notice`，标记固定为 `NOTICE:lite_telemetry_delivery_required`。该提示说明：HelixHarness 数据统计已开启；请务必将最终 `eval-log.json` 提交并随功能分支 push，否则平台无法完成关联统计。同一需求的本次交付只展示一次；前文已经展示时，恢复、继续执行或复用已有交付授权时不重复展示。它是交付提醒而非业务门禁；用户选择后按实际执行结果报告状态，不再重复通用 NOTICE。

### 最终交付选择

业务归档成功后必须进入本选择门，不以 telemetry consent、finish 的 `recorded|skipped|失败` 或 eval-log 是否已提交决定是否询问推送。统计状态只决定本轮是否允许提交 Ledger。该规则统一适用于普通 Archive、Propose `--loop`、Bug Fix 及 Workspace 子仓的 Single Archive。

先通过 `archive-code-scope --feature "{feature}" --completed` 和 `archive-git-state` 重新校验本次封存范围及 Git 状态。范围无法验证、发生漂移、分支变化或存在 Git operation 时停止交付并报告真实原因，不回滚业务归档。以最新的 `working_tree_paths` 判断是否有待提交代码，不依赖此前是否保存了 deferred 标记。

- **待提交代码**：仅为最新封存范围中的 `working_tree_paths`，不得夹带既有改动或 completed 业务工件。
- **待提交 Ledger**：仅当本轮有效统计授权为 enabled、Archive finish 返回 recorded、最终 `docs/eval/{feature}/eval-log.json` 为有效完整的 archived Ledger，且该路径相对 HEAD 有未提交变化时存在。只有本文件名存在不能证明它是最终 Ledger。
- **Ledger 已提交**：有效最终 Ledger 已在当前 HEAD 历史中且该路径无未提交变化；不创建空提交，也不省略推送选择。
- **Ledger 不可提交**：统计关闭、宿主不支持、finish 跳过或失败、文件缺失／无效时，不创建、补造或提交 Ledger，也不为了交付重开统计；说明真实统计状态并继续代码提交／推送选择。残留或不完整 eval-log 不进入提交范围。

已有明确且覆盖当前精确范围及动作的用户授权或明确暂不交付决定时直接复用，不重复询问；telemetry consent 和归档前的暂不提交不能替代最终决定。否则通过当前 Host Kit 的 `ask_user` 展示下列适用选项并等待真实回答。支持且允许用于当前决策的结构化单选能力必须使用；不可用时按宿主规则用普通文本完整展示选择并等待。Workspace 子任务按派发协议向父 Coordinator 返回 `needs_input` 与精确范围／选项，由父流程取得回答后续跑，不直接向用户提问。只发出问题、工具受理回执、默认预选或等待超时均不构成回答，不得自动选择暂不提交或暂不推送。

展示选项前列出精确待提交路径、目标 `source_branch` 和不参与本次提交的 Ledger 状态。说明普通 push 会发送当前功能分支尚未到达远端的提交，可能触发远端 CI；它不授权部署或 force push。

等待期间业务状态仍为归档完成，提交／推送决定仍待回答；不得重复业务归档、改写 completed 工件或重新 start/finish。此时不得输出表示循环已结束的完成摘要，也不得只说“提交选项已展示”。只有收到真实选择并完成相应操作、如实报告操作失败，或用户明确取消后，才输出最终回执。用户稍后回答时直接继续本节，重新校验封存范围和分支后执行获授权的操作，不重跑路由、preflight 或 move 来重做归档。

按真实待提交项使用以下唯一一组选项，不展示与本轮状态不符的空提交选项：

| 待提交代码 | 待提交 Ledger | 选项 1 | 选项 2 | 选项 3 |
|---|---|---|---|---|
| 有 | 有 | 提交已封存的 Apply Scope 与最终 eval-log，并立即推送当前功能分支 | 提交已封存的 Apply Scope 与最终 eval-log，暂不推送 | Apply Scope 与 eval-log 都暂不提交 |
| 无 | 有 | 提交最终 eval-log 并立即推送当前功能分支（含需求代码与 eval-log） | 只提交最终 eval-log，暂不推送 | 暂不提交最终 eval-log |
| 有 | 无 | 提交已封存的 Apply Scope，并立即推送当前功能分支 | 只提交已封存的 Apply Scope，暂不推送 | 暂不提交 Apply Scope |
| 无 | 无 | 立即推送当前功能分支 | 暂不推送 | 不展示第三项 |

有待提交项时，选项 1 授权这些精确提交及一次普通 push；选项 2 只授权这些精确提交；选项 3 不授权 commit 或 push。没有待提交项时，选项 1 只授权一次普通 push，选项 2 不授权任何写入。授权只覆盖已展示的路径和当前功能分支，不得扩为 `git add -A` 或部署。

需要提交代码时，先按第 2 节的方法提交并验证最新 `working_tree_paths`，信息为 `feat: complete {feature}`；需要提交 Ledger 时再执行下述独立 Ledger 提交。不存在待提交 Ledger 时直接跳过 Ledger 操作，不调用 `commit-ledger` 来获取推送许可。代码提交失败时输出 `WARN:lite_apply_scope_commit_failed`，不继续 Ledger commit 或 push；用户已选择的 Ledger 提交失败或因授权变化被跳过时输出 `WARN:lite_ledger_commit_failed`，不回滚成功的代码提交，也不得自动降级成只推代码。只有事先展示并获授权的无 Ledger 交付路径才允许独立推送代码。

获得上述“提交最终 eval-log”授权后执行：

```bash
python3 "{plugin_root}/scripts/metrics.py" --repo-root "{repo_root}" --platform "{current_platform}" commit-ledger --feature "{feature}" --commit-authorized
```

该确定性操作只提交 `docs/eval/{feature}/eval-log.json`，提交信息固定为 `chore(eval): record {feature}`，并验证新提交只包含最终 `eval-log.json`；它不得夹带既有 staged/unstaged 文件，本身不自动 push。重复执行在 Ledger 已提交且未变化时幂等跳过。未选择含“提交最终 eval-log”的固定选项、telemetry disabled、finish 未 recorded 或前置 Apply Scope 精确提交失败时不得调用。Ledger 提交失败只输出 `WARN:lite_ledger_commit_failed` 并保留可手工提交文件，不回滚业务归档、不修改实现 commit 引用，也不自动扩大提交范围。

`commit-ledger` 返回 committed 或 `ledger_already_committed` 时，只记录已验证的 Ledger 提交状态；该操作不返回通用 NOTICE，也不能证明已经推送。兼容旧版 CLI 返回的同名 NOTICE 时不重复展示。Ledger 未参与本次交付时，不要求该调用或回执，也不得虚构 NOTICE。

### 执行已授权的推送

获得最终选择后、任何提交前记录当前完整 `HEAD` 为 `delivery_head`；每次本次授权的精确提交成功后将它更新为已验证的新 commit。Ledger 本来已提交或返回 `ledger_already_committed` 时，只验证其 commit 属于当前 HEAD 历史且 Ledger 路径仍无未提交变化，不把它的历史 commit 当作 `delivery_head`。没有新提交时保留原 HEAD。

若用户在同一个最终选择门选择了选项 1，不再追加第二次交互；在全部获授权提交成功或本来无需提交后，重新校验封存 Apply Scope 已无待提交代码，当前仍位于先前记录的非 `target_branch`、非 detached `source_branch`，`HEAD` 等于 `delivery_head`，没有进行中的 Git operation，且 `origin` 可解析。本轮参与交付的有效 Ledger 路径必须已提交且无新变化。任一条件不满足时输出 `WARN:lite_feature_branch_push_failed`，不得猜测其他分支或远端。全部满足后只执行普通非强制 push：

```bash
git push origin "HEAD:refs/heads/{source_branch}"
```

push 返回成功后执行只读确认：

```bash
git ls-remote --heads origin "refs/heads/{source_branch}"
```

只有远端返回的 commit 与 `delivery_head` 和当前 `HEAD` 完全一致时才报告“功能分支已推送”；否则输出 `WARN:lite_feature_branch_push_failed`。push 失败不得使用 force、删除远端 ref、切换分支、重写历史、重试到其他 remote 或触发部署。选项 2 或 3 不执行任何网络写入；回执分别列明代码、Ledger、推送的真实状态，没有待提交文件时不得声称新建了提交。

最终回执只报告真实交付状态，不再重复 `NOTICE:lite_telemetry_delivery_required`。只有有效最终 Ledger 已包含在验证成功的远端 HEAD 历史中时，才能报告“最终 eval-log 已提交并推送，平台可继续关联统计”，不能声称关联已经完成。只提交未推送时报告“eval-log 已提交，尚未推送，平台暂时无法关联”；未提交时报告“最终 eval-log 尚未提交，平台暂时无法关联”；操作失败时说明失败原因、已成功的步骤和待完成动作，不把用户选择当作执行成功。统计关闭时报告“统计未开启”；统计不支持或失败时报告真实原因，不能把代码 push 成功说成统计成功，也不伪造 NOTICE。用户选择不提交、不推送或明确取消均保持业务归档完成，不重新归档。
