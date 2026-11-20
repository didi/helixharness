# opencode 生命周期适配

OpenCode 不需要 plugin hook manifest。`opencode/index.ts` 的 `setup` 在插件装载时注册命令、
方法 Skill 和子任务角色，并在 `tool.execute.after` 为方法 Skill 绑定当前会话 `repo_root`。

- 不注册 SessionStart、SessionEnd、Stop 或 Failure 业务钩子；`session_start` 由命令执行和
  Skill 加载时的 Host Context 注入等价实现。
- 阶段开始、完成与失败仍由公共命令显式调用 HelixHarness 生命周期脚本；会话空闲或结束不代表
  阶段完成。
- `session_events`、`session_end`、`stop_hook` 和 `failure_hook` 声明为 `unsupported`，不得
  虚构事件或模拟清理。
- 项目 hooks 由 `project-hooks.json` 独立声明为 `unsupported`；Init 不创建 `.opencode/`
  设置或替代 hook 文件。
- `telemetry=unsupported`：统计关闭不改变工作流完成门禁，脚本返回 `unsupported_host` 后继续
  业务。
- 插件更新由 OpenCode 的插件管理完成；`plugin_update=unsupported` 时 `/helix:update` 只返回
  `continue_silently`，不调用其他宿主的安装命令。
- 修改插件源码或局部依赖后按 OpenCode 要求重启服务或重新加载会话；更新后新开会话确认九个
  `helix:*` 命令、`helix-*` 方法 Skill 和子任务角色可见。
