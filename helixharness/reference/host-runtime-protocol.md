# HelixHarness 宿主运行时协议

本协议是所有 command、skill 和 agent 进入业务工作流前的统一宿主边界。共享内容只能描述
逻辑动作和业务语义；工具名、启动注入、生命周期事件、子 Agent API、安装命令和项目配置
都由 `hosts/{host-id}/` Host Kit 提供。

## 1. Host Context 绑定

Host Context 在 command/Skill 进入业务工作流前由安装分发或宿主启动适配器绑定，不在每条命令中
启动 Python 进程重新识别：

1. Claude Code 的 `commands/{entry}.md` plugin command 表面绑定 `hosts/claude-code/host.json`；
2. Codex 的 `hosts/codex/skills/{entry}/SKILL.md` plugin Skill 表面绑定 `hosts/codex/host.json`；
3. OpenCode 的 `opencode/index.ts` 注册的原生命令或 Skill 表面绑定 `hosts/opencode/host.json`。

“分发表面绑定”是一种确定性入口绑定，不要求宿主额外设置环境变量或在目标项目
生成 settings：当前产品加载 `resources.command_projection` 已声明的原生命令或宿主 Skill
入口时，产品身份与该入口所属插件分发表面已经唯一确定 Host Context。Claude Code 原生命令以
`commands/{entry}.md` 为入口和公共 contract；Codex 命令以宿主 Skill 为入口、以同一
`commands/{entry}.md` 为公共 contract；OpenCode 通过插件命令执行器加载同一 contract 并注入
Host Context。根 `skills/{name}/SKILL.md` 只保存非命令的方法能力；
在已绑定 workflow 内调用时继承当前 Host Context，直接调用时由当前插件分发表面绑定。
入口使用自身实际路径和 projection 声明的固定资源定位插件根；宿主私有路径换算只写在对应
Host Kit，不得假定所有入口都位于根 `skills/{entry}/`。这一确定性路径换算不属于宿主发现；
`session_start=unsupported` 只表示没有启动 hook，不会取消分发表面已完成的绑定。
不得为补齐 Host Context 去搜索目标项目的 `.codex/settings.json`、`.Codex/settings.json`、
环境变量或 plugin cache。

绑定后读取精确路径 `hosts/{current_host_id}/host.json`，确认 `host_id` 与目录名一致、
`host_api_version` 为当前支持的 1.1，且引用的资源位于插件根内，再读取
`resources.runtime`，并绑定不可变规范身份 `canonical_host_id = host.json.host_id`。`current_host_id`
只用于选择 manifest 路径；后续版本检查、遥测 `--platform` 和宿主回执统一使用
`canonical_host_id`，不得从操作系统、CLI 名称、环境变量或自然语言别名重新猜测。
恢复同一命令时使用
当前分发表面重新绑定，不复用其他会话或宿主的 Host Context。

`scripts/host_runtime_cli.py resolve` 保留为安装器、开发诊断和契约测试工具；正常业务命令不得为了
识别宿主调用它。这样 Python 不可用且 telemetry 关闭时，原本不依赖 Python 的 Claude Code/Codex
业务动作不会因 Host Registry 新增阻断或工具调用；OpenCode 插件同样只用 TypeScript 完成注册与
Host Context 注入。

当前产品与插件分发表面无法共同确定唯一宿主、规范 ID 含路径分隔符或 `..`、清单不匹配、版本
不支持、资源越过插件根目录时，立即返回 `BLOCKED: helixharness_host_unavailable`。不得从 PATH 中
存在的 CLI 猜测宿主，也不得默认或回退到 Claude Code。

未知宿主、契约无效或绑定冲突时不得回退到 Claude Code，也不得混用其他宿主工具；应返回
`BLOCKED: helixharness_host_unavailable`。这项阻断只表示当前安装缺少有效 Host Kit，不得修改
项目业务文件。

## 2. 逻辑动作

公共 command 的工具拼写和不可降低语义先读取
`reference/tool-semantics.md`。Claude Code 对这些拼写使用原生工具语义；Codex 按 Host Kit 映射。

共享工作流使用下列逻辑动作，实际工具名以 `resources.runtime` 为准：

| 逻辑动作 | 业务语义 |
|---|---|
| `read_file` | 读取确定路径的文件 |
| `edit_file` | 在宿主权限边界内修改文件 |
| `run_command` | 执行有边界的命令并取得真实退出状态 |
| `ask_user` | 在确实需要用户决策时请求输入 |
| `invoke_skill` | 加载并执行指定 Skill |
| `spawn_job` | 按 command-local Dispatch Contract 派发逻辑任务 |
| `wait_job` | 等待已派发任务的原生终态 |
| `update_plan` | 记录当前工作流进度 |
| `stage_finish` | 按当前生命周期能力完成阶段终态 |

command 中的 `Read`、`Write`、`Edit`、`Bash`、`AskUserQuestion`、`TodoWrite`、
`Skill`、`Agent` 和 `Task` 属于 HelixHarness Tool Semantics 的 Claude Code 兼容拼写。
Codex 与 OpenCode 不得虚构同名工具，必须按 Host Kit 映射为上述逻辑动作。

## 3. 能力选择规则

能力只允许 `native`、`emulated`、`unsupported` 三个等级；未声明等同 `unsupported`。

- `skill_discovery=native|emulated`：使用 Host Kit 的 `invoke_skill` 映射；为
  `unsupported` 时，只有宿主具备 `read_file` 才能直接读取同名 `SKILL.md`，否则阻断该步骤。
- `subagents=native|emulated`：使用 Host Kit 的 `spawn_job/wait_job` 映射，并完整保留
  `reference/subagent-runtime-protocol.md`、`resources.subagent_protocol` 和 command-local
  ownership、依赖、终态、结果及产物校验；为 `unsupported` 时只允许命令已明确记录的 inline
  fallback，不得伪造子 Agent 调用。
- `workflow_transition=native|emulated`：读取 `resources.workflow_transition`，按其 `command|inline`
  模式进入下一业务命令；不得根据平台名称推断递归命令能力。
- `unit_test_workflow=native|emulated`：只读取当前 Host Kit 的 `resources.ut`；不得读取其他宿主
  权限或调度手册。
- `command_surface=native|emulated`：读取 `resources.command_projection`，只暴露其中完整声明的
  `commands/{command}.md` 唯一契约入口；宿主资源不进入公共命令面。
- `project_hooks=native|emulated`：读取 `resources.project_hooks`，按 owner、目标、合并策略和去重键
  执行；为 `unsupported` 时不创建替代 settings 文件。
- `task_tracking=native|emulated`：使用宿主计划工具；为 `unsupported` 时回退到当前 Feature
  的 `tasks.md`，不得另造宿主专属状态文件。
- telemetry 命令必须读取 `reference/telemetry-command-protocol.md` 与
  `resources.telemetry_protocol`，只按 `hook|command|disabled` 生命周期模式执行；不得按宿主 ID、
  缓存路径、Hook 名称或日志格式分支。`telemetry=unsupported` 时返回 `unsupported_host` 并继续
  业务工作流，不得读写 consent、eval-log 或复用其他宿主授权。
- `plugin_update=unsupported`：版本检查返回 `continue_silently`；不得调用其他宿主安装命令。

## 4. 生命周期

业务阶段显式调用 HelixHarness recorder 的 start/finish；宿主 Hook 仅补充会话上下文与授权清理。完整协议见 `reference/telemetry-command-protocol.md` 和 `resources.telemetry_protocol`。统计关闭不改变业务阶段完成门禁。

## 5. 未来宿主扩展契约

当前仓库发布 Claude Code、Codex 与 OpenCode Host Kit，其中 OpenCode 的 telemetry 和
plugin_update 明确为 `unsupported`。未来重新接入新宿主时，正常改动范围应限制为：

- `hosts/{host-id}/host.json`；
- 该 Host Kit 引用的分发、安装、入口投影、启动、工具映射、生命周期、项目配置、遥测或更新适配器；
- 宿主专项契约测试、安装文档和发布元数据。

如果接入必须修改 `commands/`、共享 `skills/`、`scripts/metrics.py`、
`scripts/version_update.py` 或 Host Registry 中的平台列表，说明 Host API 缺少通用能力。此时只能
先以版本化、宿主无关的方式扩展 Host API，并为现有宿主补齐兼容测试；禁止加入
按平台名称选择业务流程的核心分支。
