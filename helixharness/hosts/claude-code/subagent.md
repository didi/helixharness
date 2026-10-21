# Claude Code 子代理适配

本适配器映射逻辑 job 字段，并遵循公共 Subagent 协议。
`spawn_job` 使用当前会话真实提供的 Agent/Task 能力，`wait_job` 使用对应任务的原生完成事件；
公共协议中的 capacity、queue、parent-owned 或结构化 final 规则与基线冲突时不适用于本投影。

```text
apply_shared_write_mode: child_scoped_write
ut_experience_write_mode: child_locked_append
result_encoding: native_agent_result
```

这两个模式定义 Claude Code 的写入所有权：apply 仅按当前命令 Dispatch Contract 授权 child 的写域，UT
worker 继续使用锁追加经验。父流程仍校验终态、实际 diff、write scope、证据和最终工件。

- 通用逻辑 job 必须显式使用宿主真实支持的 `subagent_type="general-purpose"`；不得省略该字段让宿主选择默认受限类型。业务角色写入 prompt。Init 的已注册角色仅按下方专用映射选择；此例外不适用于 UT 或其他通用 job。
- 每次 Agent/Task 派发都在 prompt 中显式加入公共协议解析后的 `delegation_policy` 与
  `interaction_mode`。未声明时固定为 `delegation_policy=leaf`、`interaction_mode=host_native`：child
  不得继续派生子代理，同时遵循当前 Agent prompt 的原生 `AskUserQuestion` 时点和结果语义。
- 保留当前命令 Dispatch Contract 的依赖、ownership、写域、终态和产物校验。
- 并行扫描任务只允许写入互斥路径；共享索引由父流程串行写入。init bootstrap Agent 遵循当前
  write scope，可增量合并 `resources.project_config.settings_file`；不得用其他宿主的 parent-owned
  settings 规则覆盖 Claude Code。
- 使用 Claude Code 的原生 Agent result；公共命令不得要求 Claude Agent 输出其他宿主的文本 marker。
  父流程从真实 result 映射公共业务事实并独立验证产物。
- Agent/Task 原生不可用时，只有命令允许才内联；容量暂满或任务失败不得内联重试。
- 只把仍为 `running` 的 subagent 计入并发预算；spawn/wait 返回终态后即可按既有机制继续派发
  或复用该 agent，不引入公共 queue/slot 计数。
- 生命周期和资源回收由 Claude Code 管理，不由公共命令模拟。

## Init 原生角色映射

仅当当前会话实际列出下列类型，且其角色正文来自本次 Host Context 绑定的同一插件安装时，
Init 可以使用对应的 `subagent_type`。无法确认类型或版本来源时，使用显式
`general-purpose`，并在 prompt 中完整指定同一 `prompt_source`；不搜索或猜测其他角色类型。

| Init job_id | subagent_type |
|---|---|
| `init_scan_tech` | `helix:scan-tech` |
| `init_scan_db` | `helix:scan-db` |
| `init_scan_api` | `helix:scan-api` |
| `init_scan_service` | `helix:scan-service` |
| `init_scan_boundary` | `helix:scan-boundary` |
| `init_scan_service_meta` | `helix:scan-service-meta` |
| `init_scan_frontend_meta` | `helix:scan-frontend-meta` |
| `init_scan_frontend_api` | `helix:scan-frontend-api` |
| `init_scan_frontend_routes` | `helix:scan-frontend-routes` |
| `init_scan_frontend_state` | `helix:scan-frontend-state` |
| `init_scan_frontend_components` | `helix:scan-frontend-components` |
| `init_scan_frontend_tracking` | `helix:scan-frontend-tracking` |
| `init_service_bootstrap` | `helix:init-service-bootstrap` |
| `init_frontend_bootstrap` | `helix:init-frontend-bootstrap` |
| `init_workspace_bootstrap` | `helix:init-workspace-bootstrap` |

原生角色仍是直接执行 `prompt_source` 的 leaf。完整派发 message、唯一 job handle、依赖 DAG、
授权写域和父流程产物验证保持不变；工具类型成功或角色自报不能代替这些门禁。

## 完成事件消费与等待恢复

- 同步 Agent/Task 已返回最终结果时直接验证并推进；不得再为该 handle 固定 sleep 或检查文件出现。
- 后台任务只使用当前宿主实际提供的原生等待/结果读取能力。收到该 handle 的
  `task_notification` 终态后，立即读取原生 result（宿主提供 output_file 时可读取该精确结果路径），
  校验 final、产物和写域，再解除依赖；不得继续将已结束任务标记为 running。
- 文件存在、固定轮询或“我在等待”的文字不能代替终态；未收到终态时不得猜测完成、重复派发或内联。
- 非交互 CLI/SDK 驱动必须保持输入与事件流，持续消费真实完成通知；不得把后台通知产生的
  空 `result` 当父流程完成，也不得在尚有运行中任务时关闭会话。`is_error=true` 优先于
  名为 success 的 subtype，API 超时应如实记录而非算作通过。
- 本仓库的合成验收可使用 `scripts/claude_event_stream.py`；它只转发原生事实，不代替父流程校验、
  重试业务任务或制造成功状态。正常交互会话直接使用宿主原生事件，不依赖验收驱动。
