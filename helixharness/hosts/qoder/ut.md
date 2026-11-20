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
permission_prepare: qoder_project_settings
permission_cleanup: qoder_project_settings_exact_revert
```

## Agent/Task 映射

- `dispatch_worker` 使用原生 `Agent` 工具，传公共命令中的完整 worker prompt，并设置
  `run_in_background=true`。
- 返回的 Agent ID 写入 `unittest-queue.md` 与 `active_slots`；最多 10 个并发 worker。
- `wait_worker` 保持每 2 秒检查 `worker-{ID}-result.md` 的机制；超过 3600 秒按公共命令的
  超时协议收口。文件信号、队列对账与 Agent 原生状态冲突时，父流程必须复核真实产物。
- 上下文截断后从队列与 result 文件重建 `active_slots`。
- worker 按 `child_locked_append` 使用目录锁追加共享经验；父流程仍负责最终汇总与校验。

## 权限准备与清理

机制已实测（2026-11-03，Qoder CN `qoderclicn` 1.1.81）：

- 项目级设置文件为 `.qoder/settings.json`，`permissions.allow` / `ask` / `deny` 会被读取。
  在 `.qoder/settings.json` 写入 `permissions.deny = ["Bash(echo denied-probe*)"]` 后，
  同一命令被拒绝，错误原文为 `Command denied by projectSettings rule: ...`。
- 移除该条目并恢复文件后，同一命令恢复执行，且文件与执行前**字节一致**。
  因此「准备 → 使用 → 精确回滚」链路成立。

执行要求：

- 只允许**新增**本轮需要的条目，写入前后各取一次文件字节快照；清理必须把文件恢复为写入前
  的原始内容，不能只是删除自己新增的键（并发方可能同轮写入）。
- 快照不一致或文件在轮内被他人修改时，停止并报告真实原因，不得覆盖他方改动。
- 不得复制 `gradlew`、`mvnw` 等 wrapper 文件，不得修改业务源码或项目构建配置。
- `--setting-sources` 必须包含 `project`，否则项目级条目不会生效。
- 无法证明条目归属时保留原文件并报告，不得猜测删除。

已验证的是**机制**，不是完整 `/helix:ut` 端到端：真实 UT 运行仍需要可用的构建工具链与
被扫描的包，本 Host Kit 只在具备这些条件的仓库中声明 UT 可用。
