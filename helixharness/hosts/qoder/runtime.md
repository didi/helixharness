# Qoder 宿主运行时

该宿主保留 Qoder 原生的命令、Skill、子代理、任务与权限语义，不假装存在 Claude Code 或
Codex 的插件 hook、环境变量与工具名。本资源只映射存在宿主差异的逻辑动作。

SessionStart hook 通过 `hookSpecificOutput.additionalContext` 注入当前安装的
`canonical_host_id`、`plugin_root` 与 `host_manifest`；路径由实际执行的
`hosts/qoder/hook_entry.py` 定位，不依赖统计开关。入口调用
`host_runtime_cli.py resolve --platform qoder --expected-plugin-root {plugin_root}`，
只消费校验后的绝对 `resolved_resources`；命令参数不得覆盖已有会话绑定。
不得把本地源码 checkout 或 marketplace 目录当作安装缓存，也不得搜索其他插件副本。

| 逻辑动作 | Qoder 行为 |
|---|---|
| `read_file` | 使用 `Read` 读取确定绝对路径；大文件用 `offset`/`limit`，不扫描 plugin cache 猜资源 |
| `edit_file` | 已有文件使用 `Edit`，新文件使用 `Write`；遵守当前权限模式与单写者约束，不覆盖用户无关改动 |
| `run_command` | 使用 `Bash` 并保留真实退出状态；长任务用 `run_in_background`，超时上限由 `timeout` 参数显式给出 |
| `invoke_skill` | 使用 `Skill` 工具，插件技能传 `<plugin>:<skill>` 全限定名，例如 `helix:code-review`；不得降级为可选建议 |
| `spawn_job` | 使用 `Agent` 工具，`subagent_type` 取注册角色名（插件角色为 `helix:<agent>`）；遵守 `resources.subagent_protocol` |
| `wait_job` | 宿主无显式等待 API：后台派发的子代理完成时由运行时自动通知。父流程在收到通知前不得假定结果，也不得用 `sleep` 轮询代替 |
| `ask_user` | 使用 `AskUserQuestion`；空结果、取消或未答复不等于选择默认项 |
| `update_plan` | 使用 `TaskCreate`/`TaskUpdate`/`TaskList`；不存在 `TodoWrite`，回退目标仍是当前 Feature 的 `tasks.md` |
| `stage_finish` | `telemetry=unsupported`，不调用宿主记账；阶段终态由公共命令写入仓库证据 |
| `project_entry` | 生成或合并 `AGENTS.md` |
| `project_hooks` | 父流程把 `project-hooks.json` 条目合并到 `.qoder/settings.json`，不覆盖用户条目；子代理不拥有该文件 |

公共拼写与本宿主真实工具的对应关系固定为：`Read` → `Read`；`Write`/`Edit` → `Write`/`Edit`；
`Bash` → `Bash`；`Skill` → `Skill`；`AskUserQuestion` → `AskUserQuestion`；
`TodoWrite` → `Task*`；`Agent`/`Task` → `Agent`。`MultiEdit` 在本宿主不存在，
多条替换使用 `Edit` 的批量参数完成，不得为此增加第二次写操作。

## 用户决策与等待

先确认本轮真实暴露的工具与当前权限模式。流程要求的业务决策使用 `AskUserQuestion` 并取得真实
回答；没有该能力或该能力不被当前步骤允许时，按文本方式展示问题、范围与完整选项，并说明
“等待你的选择”后交还控制权，不追加完成摘要。已有明确授权时遵守命令合同直接复用，不重复提问。

收到新用户消息后先判断它是否回答当前问题；询问进度或讨论其他事项不视为授权。只有真实选择或
明确取消才能结束该决策。

## 写域与权限

`mode` 参数只改变子代理的权限提示方式，不作为写域隔离手段。并行派发可能写入同一路径的角色，
由父流程串行化，或改用 `isolation: "worktree"` 让子代理在独立 git worktree 中执行；收到终态后
必须核查文件系统实际变化，不得把子代理自述当作完成。
