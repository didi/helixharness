# HelixHarness Workspace RepoJob Dispatch Contract

本协议把 `scripts/workspace.py workspace-plan` 返回的 RepoJob 转成可执行的
Single 命令。它只负责跨仓派发，不复制 Propose、Apply 或 Archive 的单仓业务正文。

## 1. RepoJob 是运行合同

每个 job 至少包含：

| 字段 | 含义 |
|---|---|
| `job_id` | 稳定的 `[a-z0-9_]+` 逻辑标识；registry key 只用于生成带摘要的安全 identity |
| `repo` / `mode` | registry repo key 与 `write|read` 权限 |
| `stage` | `propose|apply|archive` |
| `cwd` / `ownership_root` | 子任务唯一允许拥有的 repo 根 |
| `arguments` / `command` | 传给公共 command 的原始参数与用户可读命令 |
| `depends_on` | 必须先成功的 repo key |
| `expected_execution_scope` | 固定为 `single` |
| `expected_workflow` | Router 唯一允许加载的 Single 正文 |
| `completion_check` | 父 Coordinator 独立执行的确定性完成校验 |
| `prompt_source` | 子任务必须完整读取的公共 command 合同 |
| `read_scope` / `write_scope` / `forbidden_scope` | 当前 job 的显式读写所有权边界 |
| `expected_artifacts` / `expected_result` | 子任务必须产生的文件与逻辑终态 |
| `validation` / `unavailable_fallback` | 父级验证与派发能力不可用时的唯一降级 |
| `delegation_policy` / `interaction_mode` | Apply 为 `may_delegate`，Propose/Archive 为 `leaf`；宿主交互模式为 `host_native` |
| `workspace_revision` | 仅受控 Propose 修订 job 存在；携带根合同收据和稳定修订身份，保留子仓旧任务与 Apply 基线 |

`command` 是公共入口的可读表示，不是 shell 命令。禁止在 shell 中直接执行
`/helix:*`，也禁止跳过公共 Router 调用内部生成函数。

## 2. 子任务执行公共命令

每个子任务收到的 message 必须自包含并明确给出：plugin root、Workspace root、RepoJob
完整 JSON、对应 Repo Handoff、`workspace_contract_sha256`（Propose 时）以及
`invocation_trigger=agent_workflow`。同时显式传递根入口已解析的 `metrics_trigger`：根入口为
Propose --loop 时是 `loop`，用户直接执行 Workspace 公共命令时是 `user_command`。
`invocation_trigger` 仅控制自动调度时的授权交互，不能作为 `metrics.py --trigger` 的值。
子任务按以下步骤执行：

1. 把所有 repo-local 工具调用的工作目录固定为 `ownership_root`，完整读取
   `commands/{stage}.md`。
2. 把 RepoJob 的 `arguments` 原样绑定为该公共 command 的 `$ARGUMENTS`，严格执行公共
   command，包括 Runtime preflight、Router 和正文消费规则；不得把 `command` 当 shell
   文本运行。
3. Router 必须返回 `action=load_workflow`、`execution_scope` 等于
   `expected_execution_scope`、`workflow` 等于 `expected_workflow`。任何不一致都返回
   `BLOCKED:workspace_child_route_mismatch`，不得改读另一套正文。
4. 完整执行 Router 选中的 Single 正文。子任务不得内联 Workspace 正文，也不得由
   根 Coordinator 代写 repo-local Spec、Tasks、Verify Report、baseline 或状态机。
   Propose 返回 `workspace_revision.required=true` 时，执行 Single Propose 的受控修订分支，
   不通过手工改 status、清空 state 或另调完整 bug-fix 达成路由。该 job 的完成校验以
   `completion_check` 为准，修订绑定后使用 apply 阶段校验，不套用首次 Propose 的全量 pending 要求。
5. 返回原生终态以及 `{job_id, repo, stage, status, workflow, produced_files,
   blocker}`；不能只返回自然语言“已完成”。

子任务需要新的用户决策时返回 `status=needs_input` 和精确问题，不直接向用户提问。父
Coordinator 聚合同批问题、取得用户回答后再用相同 job 续跑。

## 3. Host Kit 派发

第一次派发前完整读取 `reference/subagent-runtime-protocol.md` 与当前 Host Context 的
`resources.subagent_protocol`。父 Coordinator 必须把每个 RepoJob 作为一个独立的通用
`spawn_job` 逻辑任务派发，并通过 `wait_job` 等待宿主原生终态；通用子代理使用
`subagent_type="general-purpose"` 的公共语义，由当前 Host Kit 映射为宿主真实支持的类型和
schema。不得在共享 workflow 中按平台名称选择工具，也不得只把 slash-command 字符串展示给
用户后声称已经派发。

派发 message 使用第 2 节的自包含合同，并传递 RepoJob 中的 `prompt_source`、读写范围、预期
工件、validation、job 的 `delegation_policy` 与 `interaction_mode=host_native`。
Propose/Archive 固定为 `leaf`。Apply 明确为 `may_delegate`，仅允许派发完整 Single Apply
合同要求的子任务，包括两个上下文隔离、只读的 Standards/Spec Reviewer；这两个 Reviewer
均为 `leaf`，不得再递归派发。子任务不得扩大 repo ownership、写域或重新委派整个 RepoJob。
不得把 Apply 强制降为 leaf 后省略评审、由 Implementer 自评或伪造 APPROVE。宿主确实无法
提供所需独立评审时，按 Single Apply 返回真实 blocker，不能假装完成。
实际 runtime
identity、上下文继承、结果编码、容量准入、等待和生命周期全部由当前
`resources.subagent_protocol` 决定，不写入 RepoJob、业务工件或恢复状态。

同一拓扑批次的不同 repo 可以并行；同一个 repo 同时最多一个 owner。只有 Host Context 声明
`subagents=unsupported`，或当前适配器明确报告 spawn 能力不可用时，才允许
`unavailable_fallback`：父 Coordinator
记录 `WARN:workspace_subagent_unavailable`，按拓扑顺序串行内联执行第 2 节完全相同
的公共 command 合同。容量暂满、子任务失败或状态未知不属于 unavailable，不得内联重跑。

## 4. Ownership 与失败语义

- 子任务只拥有 `ownership_root` 下本阶段 Single 正文允许修改的路径。Workspace 根
  和其他 repo 不可写；父 Coordinator 独占根 `spec.md` 状态推进和最终 move。
- 读取严格限于 RepoJob 的 `read_scope`，不得把“其他 repo 不可写”解释为任意可读。
  Propose 只消费本仓与根概要合同；Apply/Archive 额外允许读取根合同 `Depends On`
  明确声明的直接依赖仓库，路径由 registry 解析并列入 `read_scope`。不自动授权其他仓库、
  反向依赖或传递依赖；这些依赖仍在 `forbidden_scope` 中，禁止修改。
  Reviewer 必须继承同一读取上限。额外跨仓输入缺失时返回父 Coordinator，
  由其执行聚合验收或按已确认合同提供明确输入，子任务不自行扩大范围。
- `read` job 不得修改业务代码；`write` job 也不得越过自己的 repo 根。
- 父 Coordinator 不与仍在运行的子任务并发写同一 repo。子任务原生失败后不自动重试，
  也不以内联执行覆盖失败事实。
- 任一 job 失败或 `needs_input` 时，停止它的依赖后继；不回滚、不重跑不相关的已完成 job。

## 5. 父级完成校验

子任务报告成功后，父 Coordinator 必须在该 repo 独立执行 RepoJob 的
`completion_check`，不能仅信任子任务 final：

- `validate-tasks + stage=propose` → `workflow.py validate-tasks --stage propose`
- `archive-preflight` → `workflow.py archive-preflight`
- `archive-code-scope + completed=true` →
  `workflow.py archive-code-scope --completed`

所有调用同时传入 `--repo-root "{ownership_root}" --feature "{feature}"`。只有子任务原生
终态成功、Router identity 匹配且确定性完成校验通过，RepoJob 才记为 completed。父流程
随后重新运行 Workspace plan/聚合门禁，以本地工件事实决定下一批，而不是信任内存状态。
