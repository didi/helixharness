# HelixHarness Tool Semantics 1.0

`commands/*.md` 使用 Claude Code 兼容的工具拼写作为公共语义表面。这些名称在
Claude Code 中使用原生语义；其他宿主不得伪造同名工具，必须通过当前
Host Kit 映射到真实能力。

| 公共拼写 | 逻辑动作 | 不可降低的语义 |
|---|---|---|
| `Read` | `read_file` | 读取确定路径，不扫描 plugin/cache 猜资源 |
| `Write` / `Edit` | `edit_file` | 遵守 sandbox、write scope、单写者和原子性 |
| `Bash` | `run_command` | 执行有边界的命令并保留真实退出状态 |
| `AskUserQuestion` | `ask_user` | 只在需要会改变业务结果的决策时询问 |
| `Skill` | `invoke_skill` | 必须真实加载并执行，不得降级为可选建议 |
| `TodoWrite` | `update_plan` | 记录当前工作流进度；宿主不支持时回退到 Feature `tasks.md` |
| `Agent` / `Task` | `spawn_job` + `wait_job` | 保留 job 数量、依赖、并行性、role 隔离、产物、失败传播和完成验证 |
| 阶段结束 | `stage_finish` | 由公共命令显式记录阶段真实终态，不以会话 Stop 代替 |

## Agent / Task 语义

command 中的 `Agent` 或 `Task` 是逻辑 job。每个 job 必须由 command-local Dispatch Contract
给出 `job_id`、`prompt_source`、`depends_on`、`read_scope`、`write_scope`、`expected_artifacts`、
`validation` 和允许的 `unavailable_fallback`。当前 `resources.subagent_protocol` 只决定物理派发、
等待、写入者和资源回收，不得改变这些业务字段。

Claude Code 投影使用公共命令声明的 Agent/Task 数量、依赖、并行度和写入范围；
其他宿主把同一逻辑 job 映射为自身子代理能力。容量暂满、等待中或原生失败不等于
能力不可用，不得因此自动改成内联成功。
共享 command 中的 `subagent_type="general-purpose"` 表示“通用子代理”语义；只有宿主 schema
真实支持该参数时才传入原字段，其他宿主按 `resources.subagent_protocol` 映射为自身通用类型。

## 边界

工具语义映射不覆盖项目入口文件、settings/hooks、telemetry 会话采集、安装更新、
delivery 或 autopilot 驱动方式；这些差异继续由当前 Host Kit 的能力与资源决定。
