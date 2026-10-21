---
description: "Active Single Bug Fix：加载问题来源，追加 Repair Revision，重做 Apply/Review 并继续 Archive。"
---

# Single Bug Fix · Active Repair

仅当公共 `/helix:bug-fix` 的确定性 Router 返回 `action=repair_feature`、`reason=bug_fix_active` 且 workflow 指向本文件时执行。只修改原 `docs/specs/active/{feature}/`；不创建新 feature，不支持 completed、Archive finalizing、其他流程或旧 description-only Bug Fix。

## 1. Repair preflight

在读取 description/path/URL、启动 telemetry 或业务写入前执行：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" repair-preflight --feature "{feature}"
```

Git operation、正在执行的 task、不同修订的未完成 Repair state 或同 feature 修订锁冲突输出 `BLOCKED:bug_fix_stage_in_progress`。completed/finalizing 按 public command 的专用 reason 阻断；不得绕过 Router 重开归档工件。

只传 target 时，先按本轮 `repair-preflight` 返回值处理，不因缺少 description 提前阻断：

- `repair_state=null`：请求问题来源并暂停；保留 target，收到来源后继续第 2 节，不从旧聊天、locator 或文件语义猜测，也不创建空修订。
- `repair_state.status=in_progress`：复用原 `repair_key`、`source_sha256`、`repair_kind` 与 `repair_revision`。跳过第 2 节重新加载 source 和第 3 节重新选择 kind，检查已保存的 Spec/Tasks 修订事实，然后继续第 4 节同一修订的编辑与 rebind；不得重复追加已有 revision/task。若中断前尚未保存必要业务事实，只请求缺少的事实，不以哈希推测内容。
- `repair_state.status=bound`：修订已绑定，跳过第 2～4 节，不再次 begin/rebind、追加 revision 或重写旧任务。Spec 为 `ready|applying` 时直接进入第 5 节 Apply；Spec 为 `implemented` 时先运行 `archive-preflight`，通过则直接进入 Archive，未通过则按既有 Apply 恢复合同补齐测试/Review/Evidence，不把旧 APPROVE 当作最新验证。

恢复中必须校验 Spec/Tasks 身份；`bound` 的修订号必须与当前 Repair state 一致，`in_progress` 允许修订尚未完整写入并由原 rebind 门禁校验。损坏、冲突或前置失败时停止，不手工重建私有状态。显式提供 source 时仍按第 2 节计算身份，相同 key 复用原修订，不同 key 遵循原并发门禁。

## 2. 加载 source 并建立稳定修订身份

description 是文件路径或 URL 时必须完整读取可见内容；不可读时停止，不能把 locator 字符串当正文。直接文本以原文为内容。canonical content 只统一换行符为 LF 并去除整段首尾空白，内部空白和文字保持不变。

```text
source_sha256 = SHA-256(canonical content)
repair_key = SHA-256(feature + "\n" + canonical content)
```

绝对本机路径不得写入 Spec、Tasks、Evidence 或 telemetry；只允许在本轮诊断中展示安全的 repo-relative locator 或 URL origin/path。相同 `repair_key` 且 Repair state 未完成时按第 1 节相同的状态分支恢复原 revision；不同 key 只能在上一 revision 已完成时创建下一 revision。已经完成的相同修订只报告已处理；再次发生必须提供新的复现事实形成新的 canonical content，不提供 `--new-occurrence` 旁路。

## 3. 调查并选择修订分支

先复现、缩小范围并读取原 Spec/Tasks、代码、测试与已加载项目约束；不得仅凭用户猜测改代码。本阶段只调查，业务代码修复在本命令绑定修订并进入 Apply 后执行。选择唯一 repair kind：

- `implementation_regression`：原 Spec 契约仍正确，只追加修复 Tasks。
- `contract_amendment`：原 Spec 的需求、边界或验收事实错误，重新进入 HelixHarness Propose 的完整需求决策树与共享理解确认，随后修订 Spec 并追加 Tasks。

状态路由：

| 当前状态 | 行为 |
|---|---|
| `planning|ready` | 进入 HelixHarness Propose repair mode，修订同一 Spec/Tasks 后回到 `ready` |
| `applying` 且没有 live executor | 追加 revision 和修复 Tasks，继续 HelixHarness Apply |
| `implemented` + implementation regression | 保留原 Spec 契约，追加 revision/Tasks，继续 HelixHarness Apply |
| `implemented` + contract amendment | 先进入 HelixHarness Propose repair mode 修订契约，再继续 HelixHarness Apply |

任何影响 Spec 的取舍仍按 `workflows/single/propose.md` 的 design tree 与共享理解确认暂停；状态路由不能替代业务判断。

## 4. 原子绑定 Repair Revision

以首次 preflight 返回的 Spec/Tasks receipts 调用 `begin-repair`；Tasks 尚不存在时传 `absent`：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" begin-repair --feature "{feature}" --repair-key "{repair_key}" --source-sha256 "{source_sha256}" --repair-kind "{implementation_regression|contract_amendment}" --expected-spec-receipt "{spec_receipt_sha256}" --expected-tasks-receipt "{tasks_receipt_sha256|absent}"
```

只修改三个既有业务工件模型：

1. Spec frontmatter 设置单调递增的 `repair_revision: N`；正文 `Repair Revisions` 表追加唯一且连续的 `R-NN`，列固定为 `Revision | Repair Key | Kind | Source SHA-256 | Summary`。
2. Tasks 表增加 `Revision` 列；旧任务的 ID、说明、依赖、状态保留，新任务使用全局单调递增且不复用的 `T-N`、当前 `R-NN` 和 `pending`。
3. 旧 `verify-report.md` 可保留作诊断，但其 Review seal 立即失效；Apply APPROVE 后用最新报告原子替换，不新增历史 Evidence 文件。

`Repair Key` 与 `Source SHA-256` 两个单元格必须直接写入对应的 64 位小写十六进制值（`[0-9a-f]{64}`）；值本身不得使用 Markdown 反引号、链接、前后空格或其他展示格式。下面的占位符表示替换后的裸值，实际写入时不保留花括号：

```text
| R-01 | {repair_key} | implementation_regression | {source_sha256} | 修复摘要 |
```

需要进入 Propose repair mode 时，先完成 `begin-repair`，再编辑工件；Propose 使用 `validate-tasks --stage propose --repair-key "{repair_key}"` 校验，保留旧任务状态及 Spec/Tasks 原阶段，不执行普通 Propose 的全量 pending 检查或手工 ready 转换。中断后仅从同一 in-progress Repair state 恢复，不根据历史入口行为修改状态。

编辑完成后重新执行 `repair-preflight` 取得当前新 receipts；若并发事实或 Git 状态已变化则停止。再调用：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_root}" repair-rebind --feature "{feature}" --repair-key "{repair_key}" --expected-spec-receipt "{new_spec_receipt_sha256}" --expected-tasks-receipt "{new_tasks_receipt_sha256}"
```

该命令执行 CAS、业务锁、revision 连续性和旧任务保护：planning/ready 回到 `ready`；applying/implemented 回到 `applying`，只更新 Apply Spec receipt，保留最初 `apply_baseline_head` 与 baseline manifest，并使旧 Review state 失效。drift、并发或 row 不匹配一律停止，不用 Agent 手工改私有状态。

## 5. 继续 Apply 与 Archive

直接完整读取 `workflows/single/apply.md`，以 `trigger=bug-fix`、`route_reason=bug_fix_active` 从新增 pending task 继续。最终 Review Scope 必须覆盖首次 Apply baseline 到修复后最终 HEAD，而不是只审 repair delta；重新运行最新完整测试和两个隔离 Review 轴，再原子替换 `verify-report.md`。

Apply 成功后直接完整读取 `workflows/single/archive.md` 完成 Finalize。不得嵌套调用公共 Propose/Apply/Archive，也不创建 wrapper Entry；进入的每个阶段各记录一个 `trigger=bug-fix` Stage Entry。任一阶段 failed/blocked 后立即停止，再次执行同 target（可不重复 source）从 Repair state 和第一个未完成阶段继续。

## 不变量

- 不把 invocation trigger 写成 Spec 的需求身份字段，不写 `bug_signature`。
- 不重置首次 Apply 基线，不复用旧 APPROVE，不缩小最终 Review Scope。
- 不支持归档后 correction/relation tree、Status 纠正链、其他流程或 `--new-occurrence`。
- 不自动 commit、push、部署、连接数据库或扩大 Archive 授权。
- 最终只报告真实 revision、修复任务、验证/Review 证据、归档状态和未完成项。
