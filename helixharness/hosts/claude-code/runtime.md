# Claude Code 宿主运行时

该宿主保留现有 Claude Code 命令、Agent 工具、权限和插件 hook 语义。共享工作流只使用
HelixHarness 逻辑动作；本资源仅映射存在宿主差异的动作。

SessionStart hook 通过 `hookSpecificOutput.additionalContext` 注入当前安装的
`canonical_host_id`、`plugin_root` 与 `host_manifest`。路径由实际执行的
`hosts/claude-code/hook_entry.py` 定位，不依赖统计开关或 `CLAUDE_ENV_FILE`。可用时同时向当前宿主环境写入 `HELIXHARNESS_BOUND_PLUGIN_ROOT`（不写入凭据或用户配置）。
入口调用 `host_runtime_cli.py resolve --platform claude-code --expected-plugin-root {plugin_root}`，
只消费校验后的绝对 `resolved_resources`；命令参数不得覆盖已有会话绑定。环境文件不可写时，
仍使用 SessionStart 注入的根目录作为显式 expected 参数校验，不搜索其他副本。原生命令与方法
Skill 复用此绑定；不得把 marketplace 源码目录当作安装缓存，也不得搜索其他插件副本。

HelixHarness 命令展开（`UserPromptExpansion`）及 `PreToolUse:Skill` 仅刷新当前入口的绝对资源绑定，
不执行统计清理或业务阶段，也不授予工具权限；其他命令与 Skill 静默忽略。

生命周期 hooks 使用 `SessionStart`、`CwdChanged` 与 `SessionEnd`：前两个保存或更新
会话环境，并在统计已授权且存在账本时检查异步统计运行环境；`SessionEnd` 仅在统计关闭时
兜底清理当前项目的私有统计状态与待上报数据。授权关闭和命令内统计接口负责常规清理，
不再在命令展开、Skill 调用、每轮停止或失败时重复触发插件级清理。
阶段 start、trace-event、finish 均由公共命令显式调用；会话结束不代表阶段完成。

| 逻辑动作 | Claude Code 行为 |
|---|---|
| `read_file` | 使用原生 Read 工具读取确定路径 |
| `edit_file` | 使用原生 Edit/Write 工具，并保留写域与单写者约束 |
| `run_command` | 使用原生 Bash 工具并保留真实退出状态 |
| `invoke_skill` | 使用原生 Skill 工具 |
| `spawn_job` | 使用原生 Agent/Task 能力，并保持命令所有权规则 |
| `wait_job` | 等待对应 Agent/Task 返回真实终态和结果，不把派发成功当完成 |
| `ask_user` | 可用时使用原生结构化用户提问能力 |
| `update_plan` | 使用 TodoWrite；不可用时更新当前 Feature `tasks.md` |
| `stage_finish` | 在命令内按真实终态显式调用 `scripts/metrics.py finish` |
| `project_entry` | 生成或合并 `CLAUDE.md` |
| `project_hooks` | 将 HelixHarness 条目合并到 `.claude/settings.json`，不覆盖用户条目 |

现有 `${CLAUDE_PLUGIN_ROOT}` hook 契约继续作为分发细节，不得进入共享工作流。
