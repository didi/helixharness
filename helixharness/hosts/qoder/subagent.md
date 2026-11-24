# Qoder 子代理运行协议

本协议只决定逻辑 job 的物理派发、终态取得、写入者与资源回收，不改变 command-local Dispatch
Contract 的 `job_id`、`prompt_source`、`depends_on`、`read_scope`、`write_scope`、
`expected_artifacts`、`validation` 与 `unavailable_fallback`。

## 派发

```text
dispatch_mode: native_job
result_surface: single_terminal_message
wait_api: runtime_notification
write_scope_isolation: parent_enforced
resource_cleanup: not_required
```

- 派发使用 `Agent` 工具。`subagent_type` 取注册角色名：插件角色写作 `helix:scan-api`、
  `helix:code-review` 的拥有者形式 `helix:<agent>`，通用角色写作 `general-purpose`。
  `subagent_type="general-purpose"` 是本宿主真实支持的原生取值，直接传入，不需要别名映射。
- `prompt` 必须自包含：子代理看不到当前会话，需显式给出绝对 plugin_root、host_manifest、
  `prompt_source` 路径、写域与期望产物。
- `description` 给短标签，`name` 在需要稳定引用该 job 时给出可解析的名字。
- 并行派发即在同一轮发出多个 `Agent` 调用；不得为此重复发起同一角色。
- `run_in_background: true` 用于与其它工作重叠的长任务。命名子代理始终后台运行。

## 等待与终态

- 本宿主没有 `wait_job` 原生接口。后台 job 完成时由运行时投递通知；在通知到达之前，父流程对
  子代理结果一无所知，不得预测或提前汇总。
- 不得用 `sleep`、轮询目录或反复读取会话记录来代替等待。等待期间没有其它可做的事时，如实报告
  子代理仍在运行，而不是给出猜测。
- 需要终止时使用 `TaskStop`；其返回是终止动作的结果，不代替业务终态判定。
- 子代理返回的单条消息不直接呈现给用户，父流程必须向用户转述结论。
- 文本自报不等于完成：取得终态后按 `expected_artifacts` 用 `Read`/`Glob`/`Bash` 核查文件系统中
  的真实产物与 diff，再判定 `validation`。

## 写域

- 权限模式与 `mode` 参数只控制确认方式，不构成写域隔离；`plan` 不阻止子代理落盘。
- 写域重叠的角色由父流程串行化。确需并行且仓库受 git 管理时，可使用 `isolation: "worktree"`
  让子代理在独立副本中执行；该副本的改动需要显式核对后才进入主工作树，未改动时由运行时清理。
- 子代理不得写 `.qoder/settings.json`、`docs/specs/` 状态文件、共享经验库或其它 job 的产物。
- 单个 job 失败、被取消或越界写入时，停止新任务准入，等待已派发 job 安全收口并输出部分结果。

只有确认当前运行时没有暴露子代理能力时才允许 inline fallback，并记录
`WARN:qoder_subagent_unavailable`；容量暂满、仍在等待、单个 job 失败或取消都不构成能力不可用。
