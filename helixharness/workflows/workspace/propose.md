---
description: "Workspace Propose：生成唯一跨仓概要设计，并调度各仓完成 Single Single Propose。"
---

# Workspace Propose · Cross-repo Design

仅当公共 Router 返回 `execution_scope=workspace` 时执行本工作流。本工作流只负责跨仓协调；每个子仓通过公共命令路由到 `workflows/single/propose.md` 处理本仓业务，Coordinator 不代写子仓工件。

第一次派发子仓前，必须完整读取并执行 `reference/workspace-repo-dispatch.md`、`reference/subagent-runtime-protocol.md` 与当前 Host Context 的 `resources.subagent_protocol`。`workspace-plan` 返回的结构化 RepoJob 是本流程的 Dispatch Contract；必须通过 Host Kit 的 `spawn_job` / `wait_job` 映射实际派发、校验 Router identity、等待原生终态并执行父级 completion check，不能把 slash-command 字符串当 shell 命令或仅展示给用户。

## 1. 身份与安全前置

在读取需求或写业务工件前，确认 Workspace 根的 `helixharness.workspace.yaml` 是 regular non-symlink file，并执行 registry 解析。所有 repo key、路径和读写模式只来自 registry 与用户确认的跨仓范围；禁止扫描目录猜测仓库、跟随 symlink 或把需求文档中的仓库名直接当成本地路径。

Workspace 根不要求是 Git 仓库，也不启动一份伪造的跨 Git HelixHarness Ledger。每个 repo job 保留完整 Single 的版本检查和 repo-local Stage recorder；Coordinator 用 `invocation_trigger=agent_workflow` 传递自动调度上下文，使子仓按现有 consent 合同静默使用当前授权，不重复弹出 Workspace 级授权。最终按 repo 汇总真实统计状态，不构造 root commit、HEAD 或 telemetry recovery。

## 2. 收敛跨仓决策

按照 HelixHarness Propose 的 design-tree 与共享理解规则调查 Workspace 入口、registry、各目标 repo 的 Harness/Knowledge/代码事实，并一次展示当前完整 frontier。除单仓通常决策外，必须关闭这些跨仓决策：

- 纳入交付的 repo、每个 repo 的 `write|read` 模式、职责和非目标；
- 服务、接口、事件、数据模型和错误语义的 owner/consumer；
- 兼容窗口、依赖顺序、发布顺序、回滚与降级策略；
- 状态与数据归属、一致性、幂等、超时、重试和失败隔离；
- 安全、可观测性以及跨仓端到端验收；
- 每个 repo 的测试 seam 与 handoff 边界。

共享理解确认前不得创建任何 feature 工件。

同一未归档 Workspace 在 Apply 中返回需求决策时，由 Coordinator 汇总并取得明确决定，
再修订现有根 Spec 的受影响合同、AC 与 Handoff，把根状态置为 planning。保留已确认的
无关内容及所有子仓实现、任务和基线；不得直接修改子仓状态或工件。存在已归档子仓时停止，
不重开 completed。根合同更新后按下面的计划派发受影响子仓重新绑定。
新的公共调用使用 `/helix:propose {feature} {revised_source} --revise`（可带 `--loop`）；
Router 返回 `revision_requested=true` 后，先确认 registry 中各仓没有同 feature 的 completed
工件、没有活动执行任务，再进入上述决策与修订步骤。不能仅因提供了一个新 source 就把普通
Propose 当成修订授权。

## 3. 生成唯一 Workspace 工件

读取 `templates/workspace-spec.md.tpl`，只生成：

```text
{workspace_root}/docs/specs/active/{feature}/spec.md
```

根 feature 目录不得出现 `tasks.md`、`verify-report.md`、`repo-plan.yaml`、状态文件、问题文件或其他业务工件。Spec frontmatter 至少包含：

```yaml
feature: {feature}
status: planning
execution_scope: workspace
testing_seam_confirmed: true
```

概要设计是跨仓静态合同，也是 Coordinator 的唯一静态调度输入。它必须包含技术架构 Mermaid 图、端到端 Mermaid 时序图、Repo 责任与依赖矩阵、稳定的 `C-NN` 跨仓契约、数据/状态归属、兼容/发布/回滚、稳定性/安全/可观测性、跨仓验收标准和逐仓 Repo Handoff。不得把运行状态、临时 receipt 或任务完成度写成第二份根工件。

生成后执行：

```bash
python3 "{plugin_root}/scripts/workspace.py" --workspace-root "{workspace_root}" workspace-validate --feature "{feature}" --stage propose
```

## 4. 调度各仓完整 Single Propose

执行：

```bash
python3 "{plugin_root}/scripts/workspace.py" --workspace-root "{workspace_root}" workspace-plan --feature "{feature}" --command propose
```

按返回的拓扑批次调度。每个 job 明确返回 RepoJob identity、ownership、依赖、`arguments`、`command`、预期 Single workflow、completion check、`source` 与 `workspace_contract_sha256`：`source` 是 Workspace 根概要设计的绝对路径，公共调用必须等价于 `/helix:propose {feature} {source}`，不能省略 requirement。严格按 RepoJob Dispatch Contract 在 registry repo path 执行该完整公共命令；不能直接调用内部生成函数，也不能由 Workspace 根代写一套“简化版子仓工件”。子仓 Router 因不存在 Workspace marker，必须进入 `workflows/single/propose.md`。

父 Coordinator 负责统一承接用户交互，并把 job 中的 Repo key、Mode、Workspace contract receipt 及概要设计里的对应 Repo Handoff 作为受控 execution context 传入子仓。每个子仓仍通过完整 Single Propose 生成并拥有自己的 `spec.md`、`tasks.md`，其中 Spec 除 Single 字段外必须绑定：

```yaml
execution_scope: single
workspace_repo: {repo_key}
workspace_contract_sha256: {workspace_spec_receipt}
```

`write` repo 的本地方案覆盖实现与验证；`read` repo 只生成兼容性检查任务，不得引入业务代码修改。每个子仓必须独立通过：

```bash
python3 "{plugin_root}/scripts/workflow.py" --repo-root "{repo_path}" validate-tasks --feature "{feature}" --stage propose
```

任一子仓失败时保留已完成子仓的本地状态；重跑从当前子仓工件恢复，不重写已确认的 Workspace Spec。

对于显式确认的根合同修订，`workspace-plan` 为已有子仓返回 `workspace_revision`。
子仓仍执行同一完整公共 Propose；Router 校验 source 和 registry 后进入受控 Repair mode，
保留旧任务与最初 Apply 基线、追加修订任务并 rebind，不提前执行 Apply/Archive。
父级严格执行该 job 的 `completion_check`：修订 job 使用 `validate-tasks --stage apply`，
因此完成状态可以是 ready 或 applying，不要求把已有进度重置成全量 pending。
已绑定当前合同且校验通过的子仓列在 `repo_results`，无需重复派发；只执行 `jobs` 中的任务。

## 5. Ready 门禁

所有子仓均绑定当前合同并通过各自 completion check 后，把 Workspace Spec 状态更新为 `ready`。首次 Propose 的子仓为 ready，受控修订的子仓可保留 applying。Workspace Spec receipt 计算时排除 `status`，因此该状态推进不会使已绑定的子仓合同失效。最后重新运行 `workspace-validate --stage propose` 和 `workspace-plan --command apply`。

成功后提示下一步 `/helix:apply {feature}`；Propose --loop 则直接进入 Workspace Apply。
