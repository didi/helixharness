# Qoder 子代理适配

本适配器只映射逻辑 job 字段，完整服从公共
[Subagent 协议](../../reference/subagent-runtime-protocol.md)。Qoder 的 `agents/*.md`
定义格式与 Claude Code 一致（frontmatter：`name` / `description` / `tools` / `model` / `color`），
插件作用域名称为 `{plugin}:{agent}`。

```text
apply_shared_write_mode: child_scoped_write
ut_experience_write_mode: child_locked_append
result_encoding: native_agent_result
delegation_policy: leaf
```

## 已实测能力（2026-11-02）

- `Agent` 工具存在，`subagent_type` 支持内置 `Explore` / `Plan` / `general-purpose`。
- 插件 agent 以作用域名可派发，例如 `hvprobe:scout`；无歧义时平铺名 `scout` 同样可用。
- **并行派发成立**：单条消息内 3 个 `Agent` 调用全部返回真实终态，满足 `init` 的并行扫描 DAG
  与 `code-review` 的双轴隔离 Review。
- 后台 Agent（`run_in_background`）可用于 `/helix:ut` 的 worker 派发。

## 派发规则

- 通用逻辑 job 必须显式使用宿主真实支持的 `subagent_type="general-purpose"`，不得省略该字段
  让宿主选择默认受限类型；业务角色写入 prompt。
- Init 的已注册角色按下方专用映射选择；无法确认类型或版本来源时回退 `general-purpose`，
  并在 prompt 中完整指定同一 `prompt_source`。
- 每次派发都在 prompt 中显式加入解析后的 `delegation_policy` 与 `interaction_mode`；未声明时
  固定为 `delegation_policy=leaf`、`interaction_mode=host_native`。
- 保留当前命令 Dispatch Contract 的依赖、ownership、写域、终态和产物校验。
- 并行扫描任务只允许写入互斥路径；共享索引由父流程串行写入。
- 使用 Qoder 原生 Agent result，不要求 child 输出其他宿主的文本 marker。
- Agent 原生不可用时只有命令允许才内联；容量暂满、等待中或任务失败不得内联重试。

## Init 原生角色映射

仅当当前会话实际列出下列类型，且角色正文来自本次 Host Context 绑定的同一插件安装时使用；
否则一律使用 `general-purpose` 并在 prompt 中指定同一 `prompt_source`。

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
