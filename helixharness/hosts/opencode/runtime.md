# OpenCode 宿主运行时

本 Host Kit 面向 OpenCode V2（最低验证版本 2.2.2）。插件包在本安装的 `opencode/index.ts`
中一次性注册全部宿主表面，不把 canonical Markdown 复制到项目的 `.opencode/`：

- 公开命令注册为 `helix:<name>`，执行时加载同一插件内的唯一 `commands/<name>.md`；
- 方法 Skill 注册为 `helix-<name>`，正文仍来自根 `skills/<name>/SKILL.md`；
- 子任务角色注册为 `helix-<name>`，正文仍来自根 `agents/<name>.md`。

命令执行与 Skill 加载时由插件注入预绑定 Host Context（`canonical_host_id=opencode`、
`plugin_root`、`repo_root`、`host_manifest`、`capabilities` 和全部资源绝对路径）。这等价实现
`session_start=emulated`；不得从环境变量、项目文件或其他插件缓存重新识别宿主。直接调用方法
Skill 时，`repo_root` 由 `tool.execute.after` 按当前会话目录绑定。

命令执行和直接 Skill 调用均以请求会话的 `session.get().location.directory` 为目录来源，
校验为真实存在的绝对目录后绑定，不回退到插件目录、进程 cwd 或其他会话。查询失败报
`repo_root_lookup_failed`；缺失、相对、不可解析或非目录的值报 `repo_root_invalid`。目录绑定
失败时命令不提交 prompt；Skill 结果替换为相应的 `BLOCKED`，不交付未绑定的正文。

| 逻辑动作 | OpenCode 行为 |
|---|---|
| `read_file` | 使用原生 `read` 工具读取确定路径 |
| `edit_file` | 使用原生 `edit`、`write` 或 `patch`，并保留当前写域与单写者约束 |
| `run_command` | 使用原生 `shell` 工具并保留真实退出状态；需要执行目录、超时或后台控制时，只使用当前暴露的 `shell` schema 支持的选项 |
| `invoke_skill` | 使用原生 `skill` 工具；把 `invoke_skill(name)` 映射为 `helix-<name>` |
| `spawn_job` | 保持已解析的 `delegation_policy`；普通 leaf 使用 `general`，通用 may_delegate 使用 `helix-worker`，Init/UT 使用 canonical `helix-<agent>` leaf |
| `wait_job` | 遵循当前暴露的原生 `subagent` schema；等待真实完成结果或后台完成通知，再校验结果与产物；`sessionID` 本身不代表完成 |
| `ask_user` | 使用原生 `question` 工具并等待真实回答 |
| `update_plan` | OpenCode 无原生计划工具，更新当前 Feature 的 `tasks.md` |
| `stage_finish` | 在命令内按真实终态显式调用 HelixHarness 生命周期脚本 |
| `project_entry` | 生成或合并根 `AGENTS.md` |
| `project_hooks` | OpenCode 无项目 settings hook 契约；声明 `unsupported`，不创建替代文件 |

Workspace Apply 的 RepoJob 需要 `experimental.subagent_depth >= 2` 才能派发 Single Apply 的
独立只读 leaf reviewer。深度或权限不足时返回具体 `BLOCKED`，不得内联降级、自审或虚构
review 完成。前台/后台参数随 OpenCode 版本变化，不能假设所有 V2 都有 `background` 选择器。

## 命令与参数

OpenCode 原生命令执行器收到的是用户输入的原始参数字符串。插件把它作为 `$ARGUMENTS` 注入
`commands/<name>.md`，并替换 `{plugin_root}`、`{repo_root}`、`{canonical_host_id}`、
`{current_platform}` 和 `{SHELL_QUOTED_ARGUMENTS}`。替换只发生一次，用户参数中的同名文本保持
字面值。命令通过 `session.prompt` 提交展开后的正文，不递归调用 `session.command`，避免重复
命令生命周期。

用户附件、agent mention 和 skill mention 原样转发到展开后的 prompt；`delivery` 保持调用方
给定的 `steer|queue`。`metadata` 只记录逻辑命令名，不进入业务事实。

## 用户决策与等待

- `question` 必须取得真实回答；取消、空结果或超时不等于默认选项。
- 需要文本选择时按公共命令要求展示完整问题和选项，并交还控制等待回答。
- 子代理内部使用 `question` 时保持 `interaction_mode=host_native`；若 child 无法直接交互，
  返回具体缺口，由父流程按命令的 blocked 语义处理。

## 命令入口与资源加载

命令入口已经是公共 contract，不需要额外薄入口。插件在启动时读取并校验一次：

- `hosts/opencode/host.json` 的 `host_id`、`host_api_version=1.1`、能力和全部资源引用；
- 九个 `commands/*.md`、根 `skills/*/SKILL.md` 和 `agents/*.md`。

资源缺失、越界、宿主 ID 或 API 版本不一致时插件装载失败；不得扫描其他插件副本或降级到
Claude Code/Codex。Skill 与 Agent 的 canonical 正文只在本插件内解析，不读取项目中的同名
`.opencode/skills` 或 `.opencode/agents`。

## Python 与统计

OpenCode 插件本身使用 TypeScript，不依赖 Python 启动。业务命令仍按 `reference/runtime-preflight.md`
执行 Python preflight。`telemetry=unsupported` 时统计脚本返回 `unsupported_host`，业务继续；
不读写其他宿主的 consent、eval-log 或私有缓存。`plugin_update=unsupported` 时版本检查返回
`continue_silently`，不得调用 Claude Code/Codex 的安装命令。

## d-d2c guard 边界

`skills/d-d2c/SKILL.md` 中的 Claude skill-scoped `Stop` / `PreToolUse:AskUserQuestion` guard 不会在
OpenCode 中执行，因为 OpenCode Host Kit 不注册 skill hooks。按该 Skill 的 `--bare` / 无 hook
语义执行：业务步骤、阶段确认和用户授权门禁仍由 Skill 正文与当前命令负责，但不得声称确定性
guard 已运行，也不得用 guard 缺失跳过视觉验收或完成条件。

## 项目配置

`resources.project_config` 固定 `entry_file=AGENTS.md`、`settings_file=null`。Init 只合并
`AGENTS.md` 并生成公共工件，不创建 `.opencode/` 设置或虚构 hook 配置。
