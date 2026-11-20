# Qoder 宿主运行时

Qoder 的工具名与 Claude Code 兼容拼写基本一致，但**命令面由 Skill 承载**：实测
`commands/*.md` 可被插件校验识别，却不会暴露为可调用入口；`skills/{name}/SKILL.md`
才会成为 `/name`（限定名为 `/helix:{name}`）。因此本宿主 `command_surface=emulated`，
薄 Skill 入口位于 `hosts/qoder/skills/`。

| 逻辑动作 | Qoder 行为 |
|---|---|
| `read_file` | 使用原生 `Read` 工具读取确定路径 |
| `edit_file` | 使用原生 `Edit` / `Write`，保留写域与单写者约束 |
| `run_command` | 使用原生 `Bash` 并保留真实退出状态 |
| `invoke_skill` | 使用原生 `Skill` 工具加载当前插件限定的 Skill |
| `spawn_job` | 使用原生 `Agent` 工具，`subagent_type` 可用内置 `general-purpose` 或插件作用域 `helix:{name}` |
| `wait_job` | 等待原生 Agent 终态，不把派发成功当完成 |
| `ask_user` | 使用原生 `AskUserQuestion`；无可用结构化能力时用普通文本并等待真实回答 |
| `update_plan` | 使用 `TaskCreate` / `TaskUpdate` / `TaskList`；不可用时更新当前 Feature `tasks.md` |
| `stage_finish` | 在命令内按真实终态显式调用 `scripts/metrics.py finish` |
| `project_entry` | 生成或合并 `AGENTS.md` |
| `project_hooks` | 由父流程合并 `.qoder/settings.json`；子 Agent 不拥有该文件 |

## 与 Claude Code 的已知差异

- 插件根变量为 `${QODER_PLUGIN_ROOT}`，不是 `${CLAUDE_PLUGIN_ROOT}`。
- 用户配置根为 `~/.qoder-cn`（中文版）或 `~/.qoder`（国际版），由 `QODERCN_CONFIG_DIR` /
  `QODER_CONFIG_DIR` 覆盖；不得据此猜测宿主身份。
- 插件清单目录为 `.qoder-plugin/`，且 `skills` / `commands` 必须显式声明路径。
- 内置任务工具为 `TaskCreate` 系列；`QODER_FEATURE_TASKS=0|false` 时回退 `TodoWrite`。
- 不注册全局 `Stop` / `PostToolUseFailure` 业务钩子；阶段终态仍由公共命令显式记账。

## 边界

Qoder 官方提供了 `.claude/settings.json` 到 Qoder 设置的部分兼容转换（`permissions.defaultMode`
→ `general.defaultPermissionMode`），但本 Host Kit **不依赖**该转换：所有需要写入的宿主配置
都通过 `resources.project_config` 与 `resources.project_hooks` 显式声明。
不得把 `~/.qoder-cn` 下的缓存当作插件根，也不得回退到 Claude Code 资源。
