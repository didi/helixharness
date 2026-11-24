# Qoder Skill Entry Protocol

Qoder 没有任何注册面把插件 `commands/` 列为可调用的斜杠命令（实测：会话上下文只出现 skill、agent、MCP 与
Memory 区块；程序发送 `/helix:status` 零展开透传，同一路径下 `/help` 却被展开为命令消息。输入框手工提交是否
走斜杠解析未排除）。因此本宿主的公共入口投影为 `hosts/qoder/skills/{name}/SKILL.md`
薄入口，由 `.qoder-plugin/plugin.json` 的 `skills` 声明把 `./skills` 与 `./hosts/qoder/skills` 一起装载，
Qoder 以 `{plugin}:{name}` 命名空间暴露。薄入口只选择加载机制，不承载业务步骤。

`commands/{name}.md` 仍是唯一公共契约，其 Router 选出的 `workflows/single/*.md` 或
`workflows/workspace/*.md` 是对应阶段正文，canonical 方法 Skill 仍位于 `skills/{name}/SKILL.md`。

## 每个薄入口必须完成的顺序

1. 确定 plugin root：优先使用会话里 SessionStart 注入的 HelixHarness Host Context 中的
   `plugin_root`；没有注入时，取当前 `SKILL.md` 向上两级目录。禁止搜索其他版本缓存。
2. 在任何 Python 调用前完整读取 `reference/runtime-preflight.md`，按其 `check` → `exec` 顺序执行
   `scripts/runtime_preflight.sh`。解释器或 Git 不可用时按该协议的语义处理，不得自行猜测路径。
3. 执行一次 `scripts/host_runtime_cli.py resolve --platform qoder --expected-plugin-root "{plugin_root}"`，
   完整消费 `plugin_root`、`host_manifest`、`resolved_resources`。回执缺失、来源不符或
   `status != resolved` 时报告 `BLOCKED:helixharness_host_contract_invalid` 并停止。
4. 完整读取 `resolved_resources.command_projection` 指向的投影、`resolved_resources.runtime`
   （即 `hosts/qoder/runtime.md`），以及 `reference/workflow-projection-protocol.md` 和
   `reference/host-runtime-protocol.md`。
5. 完整读取该命令的 `commands/{name}.md`。文件较长时（`init`、`ut`）用同一读取工具按行区间顺序
   续读直到文件末尾，不得跳段、不得只读开头，也不得用其它宿主的同名文件替代。
6. 把用户原始参数逐字作为契约的 `$ARGUMENTS`，按契约步骤执行；工具名按
   `reference/tool-semantics.md` 与 `hosts/qoder/runtime.md` 的映射落到 Qoder 真实能力。

## 本宿主的差异

- 阶段推进：`hosts/qoder/workflow-transition.md` 声明为 `inline`，同一会话内顺序执行下一阶段，
  不依赖宿主自触发命令。
- 子代理：`hosts/qoder/subagent.md`，派发结果只在终态一次性回传，等待靠运行时通知而非轮询。
- 统计与遥测：本宿主 `telemetry: unsupported`，不注册 Stop/Failure hook，也不安装统计 runtime；
  阶段终态由契约内的显式记录步骤完成。
- 插件更新：本宿主 `plugin_update: unsupported`。`/helix:update` 只能按
  `hosts/qoder/installer.md` 的“重新打包并覆盖安装”路径执行，不得伪造 marketplace 更新。
