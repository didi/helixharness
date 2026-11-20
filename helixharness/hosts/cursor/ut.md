# Cursor UT 运行适配

本资源只映射 [公共 UT 命令](../../commands/ut.md) 的宿主动作。扫描、6 Loop、重试、覆盖率、
缺陷分级和报告均完整执行 canonical；通用派发和终态消费服从 `resources.subagent_protocol`。

```text
dispatch_mode: native_job
experience_write_mode: parent_finalize
runtime_handle_in_queue: false
permission_prepare: host_approval
permission_cleanup: not_required
```

权限准备只验证当前 workspace、worker 临时目录和必要构建工具；需要授权时调用 Cursor 真实审批。
不修改 `.claude/settings.local.json`、其他宿主配置或 Cursor 全局自动批准设置。没有本次权限改动，
清理为 `not_required`，不删除用户设置。

每个 worker 映射为 `ut_worker_<worker_id>` 逻辑 job，并完整读取当前安装的
`agents/unit-test-worker.md`。只授权 worker 自己的临时目录、result/defects、唯一目标测试文件；
业务源码、构建配置、共享 queue/session/summary/experience 与其他 worker 路径禁止写入。
冲突目标测试或 fixture 必须串行，任务数和验证要求不变。

原生 Task 终态后父流程校验 result、defects、目标测试、6 Loop、覆盖率和写域，再更新队列。
队列的 Agent ID 写 `-`，运行 handle 不持久化。经验摘要由 worker 返回，父流程验证后串行原子合并。
失败、取消或越界不当成成功；恢复时不能验证的旧 `in_progress` 重新归为 `pending`。
只有确认子代理能力不可用且公共命令允许时才内联一次，记录 `WARN:cursor_subagent_unavailable`。
