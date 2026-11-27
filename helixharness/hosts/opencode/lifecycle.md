# opencode 生命周期适配

OpenCode 不需要 plugin hook manifest。`opencode/index.ts` 的 `setup` 在插件装载时注册命令、
方法 Skill 和子任务角色，并在 `tool.execute.after` 为方法 Skill 绑定当前会话 `repo_root`。

- 不注册 SessionStart、SessionEnd、Stop 或 Failure 业务钩子；`session_start` 由命令执行和
  Skill 加载时的 Host Context 注入等价实现。
- 阶段开始、完成与失败仍由公共命令显式调用 HelixHarness 生命周期脚本；会话空闲或结束不代表
  阶段完成。
- `session_events`、`session_end`、`stop_hook` 和 `failure_hook` 声明为 `unsupported`，不得
  虚构事件或模拟清理。
- 项目设置 hooks 仍由 `project-hooks.json` 声明为 `unsupported`；Init 不创建 `.opencode/`
  设置。`project_checks=emulated` 通过原生 `execute.after` 在 write/edit/patch 成功后运行已有
  invariant/spec-sync 巡检；错误作为反馈，不撤销编辑、不自动完成或失败一个业务阶段。
- `telemetry=emulated`：仅明确授权后记录阶段账本和工作流轨迹，可经已有异步队列提交；不读取原始
  会话，未采集指标保持未知。统计关闭或异常均不改变工作流完成门禁。
- `plugin_update=emulated`：`/helix:update` 复用既有授权协议，对有明确 upstream 的干净源码
  安装执行锁定 commit 的快进更新；本地修改、游离版本或不受管理的包安装不自动处理。
- 修改插件源码或局部依赖后按 OpenCode 要求重启服务或重新加载会话；更新后新开会话确认九个
  `helix:*` 命令、`helix-*` 方法 Skill 和子任务角色可见。

任务进度由 `scripts/task_plan.py` 对 canonical `tasks.md` 原子更新；完整边界见[能力适配](capabilities.md)。
