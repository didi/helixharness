# Qoder UT 运行适配

本资源只实现 [公共 UT 命令](../../commands/ut.md) 的宿主机制；不定义扫描、6 Loop、重试、
验证或完成业务语义。若与公共命令冲突，以公共命令为准。

```text
dispatch_mode: background_agent
experience_write_mode: child_locked_append
runtime_handle_in_queue: true
max_parallel: 10
task_timeout_seconds: 3600
poll_interval_seconds: 2
permission_prepare: qoder_project_settings_pending_verification
permission_cleanup: qoder_project_settings_exact_revert_pending_verification
```

## Agent/Task 映射

- `dispatch_worker` 使用原生 `Agent` 工具，传公共命令中的完整 worker prompt，并设置
  `run_in_background=true`。
- 返回的 Agent ID 写入 `unittest-queue.md` 与 `active_slots`；最多 10 个并发 worker。
- `wait_worker` 保持每 2 秒检查 `worker-{ID}-result.md` 的机制；超过 3600 秒按公共命令的
  超时协议收口。文件信号、队列对账与 Agent 原生状态冲突时，父流程必须复核真实产物。
- 上下文截断后从队列与 result 文件重建 `active_slots`。
- worker 按 `child_locked_append` 使用目录锁追加共享经验；父流程仍负责最终汇总与校验。

## 权限准备与清理（待验证）

**本项是本 Host Kit 唯一未经实测的能力。** Qoder 的设置结构支持
`permissions.allow` / `ask` / `deny`，但「按条目精确回滚」尚未验证。

在验证完成前必须遵循：

- 无法确认可精确回滚时，**不得写入项目设置来临时放宽权限**，改为通过宿主原生审批逐项授权，
  或在该阶段标记 `blocked` 并说明原因。
- 不得复制 `gradlew`、`mvnw` 等 wrapper 文件，不得修改业务源码或项目构建配置。
- 清理只允许撤销本轮自身写入的条目；无法证明归属时保留原文件并报告真实原因。

验证方式：执行一次 `/helix:ut`，确认 workers 结束后 `.qoder/settings.json` 与执行前字节一致。
