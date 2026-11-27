# OpenCode 子代理适配

本适配器把公共 Subagent 协议映射到 OpenCode V2 的原生 `subagent` 工具，不新增业务任务、依赖、
写域、产物或完成条件。

```text
apply_shared_write_mode: child_scoped_write
ut_experience_write_mode: parent_finalize
result_encoding: native_agent_result
dispatch_mode: native_job
```

## 派发与等待

- 每个逻辑 job 使用一次 `subagent` 调用。普通 leaf job 使用内建 `general` agent；明确声明
  `delegation_policy=may_delegate` 的通用 job（当前为 Workspace Apply RepoJob）使用
  `helix-worker`；Init 角色和 UT worker 使用本插件注册的 `helix-<name>` agent。业务角色正文由
  `prompt_source` 决定，agent ID 只负责选择运行配置。
- 每次派发的 prompt 必须完整包含当前 Host Context、当前命令的 Dispatch Contract、已解析的
  `read_scope`/`write_scope`/`forbidden_scope`、`delegation_policy` 和
  `interaction_mode=host_native`。保持 job 已解析的策略：缺省为 `leaf`；只有命令明确声明的
  job 才能使用 `may_delegate`，不得把 Workspace Apply RepoJob 强制改为 leaf。
- 按当前版本实际暴露的原生 `subagent` schema 选择执行与等待方式。不同 OpenCode V2 版本
  不保证提供相同的前台/后台参数；不得假设默认前台执行，也不得传入 schema 不支持的
  `background` 参数。提供前台执行时等待真实返回结果；后台派发时等待原生完成通知，再读取
  child 的真实结果。不得用固定轮询或文件出现代替终态。
- `helix-worker` 是 Host Kit 注册的通用运行角色，只执行 dispatch prompt，不承载业务步骤。
  内建 `general` 默认禁止 `subagent`，因此不能用于 `may_delegate` 的 RepoJob。插件为所有
  canonical `helix-*` 角色追加 `subagent` deny 权限，保持 Init 与 UT 角色的 `leaf`；只有
  `helix-worker` 保留派发能力。RepoJob 派发的独立 reviewer 仍为只读 leaf，不得继续派生。
- 返回值中的 `sessionID` 只表示 child 会话引用，不能证明完成；只在本次逻辑 job 中用于继续
  同一 child 会话，不作为业务工件或跨会话 handle。父流程必须等到真实完成并验证结果与产物。
- 只把仍为 running 的 child 计入并发预算。OpenCode 不提供固定 slot/queue 计数；容量暂满、
  等待中或单个 job 失败不得触发内联重试。
- child 原生失败、取消或等待超时按 `BLOCKED: agent_failed` 收口，不自动转内联。只有当前
  运行时明确没有暴露且无法发现 `subagent` 能力，并且命令 Dispatch Contract 允许时，才按同一
  prompt 串行内联一次。

## RepoJob 会话目录绑定

原生 child 默认继承父会话目录。Dispatch prompt 中的 `cwd` / `ownership_root` 只声明目标，
不会自动修改 native session location；仅在 `shell` 中 `cd` 也不会改变后续命令和 Skill 的绑定。
当 RepoJob 的 `ownership_root` 与继承目录不同时，在任何 repo-local 读取、写入、命令或 Skill
之前，按当前暴露的 schema 使用原生会话目录切换能力（2.3.2 的 Code Mode 可调用 `tools.opencode.session_move`；直接工具表面可能显示为
`opencode_session_move`，以当前 schema 为准），
目标为规范化的 `ownership_root` 绝对路径。确认返回位置与目标一致后，才继续公共命令 Router
与 Single workflow。后续 reviewer 继承已切换的 repo 目录，仍保留原始父子关联与委派深度。

不得伪造 `subagent` 的 `cwd` / `directory` 参数，不得通过替换 prompt 中的 `repo_root`
掩盖真实会话目录不符。目录切换能力未暴露、被拒绝或结果不一致时返回
`BLOCKED: native_session_location_unavailable`；父流程保留该 job，不代写子仓工件或降级内联。

## 工作区深化

OpenCode 默认子代理嵌套深度为一。Workspace Apply 的 RepoJob 明确声明
`delegation_policy=may_delegate`，需要在当前项目或全局 `opencode.json(c)` 中设置
`experimental.subagent_depth` 至少为 `2`：

```jsonc
{
  "experimental": {
    "subagent_depth": 2
  }
}
```

该设置只允许 RepoJob child 继续派发其 Single Apply 所需的只读评审角色。未设置或深度不足时，
child 必须返回具体缺口并由父流程按 blocked 语义处理，不得把 depth 限制当作“subagent 不可用”
而降级内联，不得由 Implementer 自审或绕过独立评审，也不得虚构已完成的 Single Apply。Single 仓流程、Propose、Archive 和 UT 的默认
`leaf` 语义不受影响。

## 写入所有权

- Apply 及通用子任务使用 `child_scoped_write`：child 只能写 Dispatch Contract 显式授权的
  路径；共享索引、计划、checkpoint 和最终报告仍由父流程串行写入。
- UT worker 使用 `parent_finalize`：worker 不写 `unittest-experience.md` 等共享文件，只在
  结果中返回有界经验摘要；父流程验证后串行、原子合并。
- 并行 job 的写域不得重叠。父流程必须校验真实 diff、写域和产物，文本自报不等于完成。

## 结果与验证

- 保持 OpenCode 原生 agent result，不要求 child 输出其他宿主的文本 marker。
- 父流程从原生 result 映射 `completed|blocked|failed`、写入路径、验证结果和错误，并独立读取
  关键产物。只读 job 通过命令约定的结构化结果返回，不写共享报告。
- `result_reencode_budget=1` 只适用于 `helixharness_structured_text`；本宿主使用
  `native_agent_result`，不进入该分支。

## Init 原生角色映射

仅当当前会话实际列出下列 agent ID，且其正文来自本次 Host Context 绑定的同一插件安装时，Init
使用对应角色；否则使用内建 `general` 并在 prompt 中完整指定同一 `prompt_source`。不得搜索或
猜测其他 agent 类型。

| Init job_id | OpenCode agent ID |
|---|---|
| `init_scan_tech` | `helix-scan-tech` |
| `init_scan_db` | `helix-scan-db` |
| `init_scan_api` | `helix-scan-api` |
| `init_scan_service` | `helix-scan-service` |
| `init_scan_boundary` | `helix-scan-boundary` |
| `init_scan_service_meta` | `helix-scan-service-meta` |
| `init_scan_frontend_meta` | `helix-scan-frontend-meta` |
| `init_scan_frontend_api` | `helix-scan-frontend-api` |
| `init_scan_frontend_routes` | `helix-scan-frontend-routes` |
| `init_scan_frontend_state` | `helix-scan-frontend-state` |
| `init_scan_frontend_components` | `helix-scan-frontend-components` |
| `init_scan_frontend_tracking` | `helix-scan-frontend-tracking` |
| `init_service_bootstrap` | `helix-init-service-bootstrap` |
| `init_frontend_bootstrap` | `helix-init-frontend-bootstrap` |
| `init_workspace_bootstrap` | `helix-init-workspace-bootstrap` |

原生角色仍是直接执行 `prompt_source` 的 leaf。完整派发 message、唯一 job handle、依赖 DAG、
授权写域和父流程产物验证保持不变；工具调用成功或角色自报不能代替这些门禁。

## 读取边界与恢复

- `read_scope` 是完整读取授权清单；父流程传入规范化绝对路径和 `ownership_root`。child 不得从
  workspace 根或父目录发起宽泛搜索，也不得跟随链接读取范围外内容。
- 跨会话只持久化逻辑 job 状态、输入摘要和产物路径，不持久化 child sessionID。恢复时重新派发
  没有可验证终态的 `in_progress` job。
- child 的关闭和资源回收由 OpenCode 管理；公共命令不假设存在 close 工具。
