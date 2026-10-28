---
description: "Workspace Apply：按依赖调度各仓完整 Single Single Apply，并聚合跨仓完成事实。"
---

# Workspace Apply · Coordinator

仅当公共 Router 返回 `execution_scope=workspace` 时执行本工作流。Coordinator 不实现单仓 Apply，不生成根 Tasks 或根 Verify Report。

第一次派发子仓前，必须完整读取并执行 `reference/workspace-repo-dispatch.md`、`reference/subagent-runtime-protocol.md` 与当前 Host Context 的 `resources.subagent_protocol`。`workspace-plan` 返回的结构化 RepoJob 是本流程的 Dispatch Contract；必须通过 Host Kit 的 `spawn_job` / `wait_job` 映射实际派发、校验 Router identity、等待原生终态并执行父级 completion check，不能把 slash-command 字符串当 shell 命令或仅展示给用户。

## 1. 聚合前置

先执行：

```bash
python3 "{plugin_root}/scripts/workspace.py" --workspace-root "{workspace_root}" workspace-validate --feature "{feature}" --stage apply
python3 "{plugin_root}/scripts/workspace.py" --workspace-root "{workspace_root}" workspace-plan --feature "{feature}" --command apply
```

门禁必须确认根目录只有 Workspace `spec.md`，Spec 为 `ready|applying`，registry/Repo 矩阵/依赖 DAG/契约引用有效，每个子仓是身份一致且绑定当前 Workspace Spec receipt 的 Single feature。随后把根 Spec 状态推进为 `applying`。

## 2. 调度完整 Single Apply

严格按 `workspace-plan` 返回的拓扑批次执行；同一批次中互不依赖的 repo 可以并行。每个 job 的 `ownership_root` 固定为其 registry path，并按 RepoJob Dispatch Contract 执行完整公共命令：

```text
/helix:apply {feature}
```

子仓公共 Router 必须选择 `workflows/single/apply.md`。因此 baseline/state、HelixHarness TDD、最终测试、固定点双轴 Review、Review Scope、Git HEAD 与本地 `verify-report.md` 都由该子仓自己的 Single Apply 负责。Coordinator 只消费完成回执，不伪造跨 Git HEAD、合并 Review Scope，也不直接代写子仓状态机。

Apply RepoJob 使用 `delegation_policy=may_delegate`，以执行 Single Apply 强制要求的原生独立
双轴评审。仅可派发该 Single 合同要求的任务；Reviewer 是只读 leaf，仓库写域仍由 RepoJob
唯一 owner 管理。Propose/Archive 的 leaf 限制不适用于 Apply 的必要评审派发。

`write` job 允许按本地 Spec/Tasks 修改本仓业务代码；`read` job 只能完成兼容性验证，不得修改业务代码。该 job 通常没有代码差异；父 Coordinator
必须附入当前 Workspace plan、概要设计及 receipt，让 Single 的 Code Review 按其明确的
只读 RepoJob 分支执行双轴契约验收。不得为制造 diff 而改代码，也不得跳过真实验证或独立评审。
job 禁止写其他 repo 或 Workspace 根 feature 目录。

任一 job 失败时：

- 保留已完成 repo 的 Single 工件和状态；
- 阻断依赖该 repo 的后续批次；
- 不回滚或重新执行不相关的已完成 job；
- 下次 Apply 重新读取各子仓本地状态，只续跑未完成 job。

## 3. 跨仓聚合验收

所有子仓 Apply 完成后，Coordinator 读取每个子仓 `archive-preflight` 的确定性结果，并按 Workspace Spec 复核：

- 每个 `C-NN` 的 owner 与 consumer 均有对应验证证据；
- 实际实现顺序、接口/事件/数据合同与兼容策略一致；
- 端到端 AC、失败路径、回滚和降级均被覆盖；
- read repo 没有业务代码漂移；
- 没有遗漏 registry repo、失败 repo 或陈旧 `workspace_contract_sha256`。

聚合结论只更新 Workspace Spec 状态，不生成第二份证据文件。全部通过后把根 Spec 状态推进为 `implemented`，再执行：

```bash
python3 "{plugin_root}/scripts/workspace.py" --workspace-root "{workspace_root}" workspace-archive-preflight --feature "{feature}"
```

成功后提示下一步 `/helix:archive {feature}`；Propose --loop 则直接进入 Workspace Archive。Workspace 模式不支持 `/helix:bug-fix`。
