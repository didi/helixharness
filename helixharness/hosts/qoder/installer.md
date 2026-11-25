# Qoder 宿主安装与更新

需要 Python 3.10+、Git 与 Qoder Desktop（或 Qoder CLI）。Qoder 不解析其他平台的插件清单，本宿主
使用 `.qoder-plugin/plugin.json`、约定目录 `agents/`、`skills/`、`hooks/hooks.json`，并在清单里声明
两个 skill 根：`./skills`（方法 Skill）与 `./hosts/qoder/skills`（9 个公共命令薄入口）。ZIP 根即插件
根，不生成另一层薄壳分发。

## 构建安装物

在插件根目录执行，产出只含 Qoder 表面的 ZIP：

```bash
python3 scripts/package_qoder_plugin.py            # 产出 dist/helix-<version>.zip
```

脚本排除 `.claude-plugin/`、`.codex-plugin/`、`hosts/claude-code/`、`hosts/codex/`、根 `SKILL.md`、
`__pycache__` 与本地状态，把 `hosts/qoder/hooks.json` 复制到 ZIP 根 `hooks/hooks.json`，并为每个
`agents/*.md` 注入与文件名一致的 `name` frontmatter——Qoder 按 `name` 注册子代理角色，这是本宿主的
分发细节，不写回共享源文件。`commands/*.md` 随包保留：它不进入宿主命令面，但它是薄入口与 Host
Runtime 投影指向的唯一公共契约，缺少它投影就无法解析。

## 安装

在 Qoder 会话中请求安装本地插件并给出 ZIP 的绝对路径，由 App 安装工具完成校验、版本化缓存落盘与
用户级启用。不要手工改写 Qoder 配置文件替代安装工具，也不要在调用安装工具后再编辑配置文件。

安装后必须开启新会话：插件清单、skills、agents 与插件 hook 在会话启动时装载，当前会话不可见，且实测已启动
的会话不会随更新刷新绑定——安装 1.2.3 之后，旧会话仍注入已被删除的 1.0.0 安装路径，该路径下的资源调用不可用。
项目级 `.qoder/settings.json` 的 hook 不在此列：实测同一轮里写入配置后、13 分钟后的第一次 `Write` 就触发了
handler，没有重启也没有新开会话，所以验收项目 hooks 不必换会话，只要写入位置与该会话的项目根一致。

## 更新与卸载

宿主未提供等价 marketplace CLI，`plugin_update` 因此声明为 `unsupported`。更新方式是重新构建并安装
新版本 ZIP；`.qoder-plugin/plugin.json` 的 `version` 是 Qoder 分发版本，与 `hosts/codex`、
`hosts/claude-code` 清单的版本独立递增，因为 Qoder 按 `<name>/<version>` 目录落盘，同版本覆盖不可靠。
同名冲突按宿主安装工具提示处理，不得删除其他插件绕过冲突。卸载在
`扩展 → 已安装 → 该插件 → … → 删除` 完成。插件卸载不删除业务仓库中的 `docs/harness/`、
`docs/knowledge/`、`docs/specs/` 或业务代码。

## 装载验收

安装后按客户端流程审阅并信任该插件的 hook 定义，再开启新会话逐项确认：

1. SessionStart 注入 `canonical_host_id = qoder` 与当前安装路径，且路径指向本次安装版本目录。
2. 9 个公共入口以 `helix:<command>` 形式出现在 skill 列表：`init`、`propose`、`apply`、`archive`、
   `bug-fix`、`ut`、`update`、`status`、`config`。入口来自 `hosts/qoder/skills/`，不是宿主命令面。
   注入给模型的 skill 清单受上下文预算影响可能不完整，可见数少于 9 不构成装载失败，以逐个调用成功为准
   （实测 1.2.0 会话 9 个入口齐全，1.2.1 会话漏掉 `config`，两次都只隔了一次安装）。
3. `helix:*` 子代理角色可被 `Agent` 工具发现，数量与 `agents/*.md` 一致（实测 16 个）。注意 `list_agents`
   里这些条目的 `id` 形如 URL 编码的 `agent-definition:["plugin","helix@local","<name>"]`，`name` 是裸名，
   `helix:<name>` 形态只出现在 `Agent` 工具的 `subagent_type` 列表里；按 `helix:` 前缀过滤 `list_agents` 的
   `id` 会得到 0 个，那是判读错误而非装载失败。
4. 项目 hooks 能实际执行：单仓 Init 写入 `.qoder/settings.json` 后，在同一会话里再写一次文件即可确认
   `project_linter.py` 被触发；重复 Init 保留用户设置且不增加重复命令。

实测结论（用于校正上面的期望，不要按其他宿主的行为判断）：

- 插件 `commands/` 没有进入任何注册面：1.1.0 会话里模型上下文只有 skill 清单、agent 清单、MCP 目录和 Memory
  概览四类区块，没有把 9 个公共命令列为可调用斜杠命令的区块；1.2.4 会话同样报告没有命令列表区块，只可见
  `helix:<skill>` 与 `helix:<agent>`。程序发送 `/helix:status` 零展开透传，同一路径下 `/help` 却被展开为命令
  消息。因此 `command_surface` 记为 `emulated`，投影使用 `skill` 薄入口。没有排除的是输入框手工提交是否走
  另一条解析路径——消息形态取证切不开这两种情形，只能由用户在客户端确认。
- 项目级 `.qoder/settings.json` 的 `PostToolUse` hook 会被执行，`additionalContext` 注入会话，因此
  `project_hooks` 记为 `native`。配置在事件发生时读取：同一会话中途写入后，首次同类工具调用即触发（实测写入
  与触发之间没有重启、`SessionStart` 或上下文压缩）。hook 只在退出码 0 且 stdout 为合法 JSON 时注入文本——
  打印裸字符串的探测没有任何注入记录，容易被误读成“会话中途写入不生效”，别据此下结论。
- 项目 hook 的注入形态要按宿主界面判读：`additionalContext` 不写进工具返回值文本，而是以独立的
  `PostToolUse hook additional context (tool: <工具名>): <文本>` 区块跟在工具结果之后。因此在目标会话里
  “工具结果没有标记”不等于“hook 没跑”，要看它自己的上下文有没有这个区块。
- 验收项目 hooks 必须在写入 `.qoder/settings.json` 的那个项目根里开启会话：委派新建的会话使用自己的
  临时目录作为项目根，读不到原目录的 settings，会给出“未装载”的假阴性。
- `PostToolUse` matcher 的匹配语义（全名或子串）宿主未文档化，因此 `hosts/qoder/project-hooks.json`
  把三个写入类工具名逐一列出（`Write|Edit|NotebookEdit`），不依赖子串命中。
- hook 以非零码失败时，宿主只留下 `hook_non_blocking_error` 记录：既不注入模型上下文，也不在会话界面显示。
  实测一份把 linter 路径钉在 `.../helix/1.1.0/` 的配置连续 26 次以 exit 1 失败（`missing project linter
  invariant-check; rerun /helix:init`），整个会话里模型一次都没看到这句话。验收 guardrail 必须去读转录里的
  hook 记录，不能以“会话没报错”当作 hook 正常工作的证据。
- 更新插件后重跑 Init 会自动收敛陈旧 handler：本宿主的 `hosts/qoder/project-hooks.json` 声明
  `stale_binding_policy = { recognize: linter_flag, merge: collapse, check: warn }`。merge 按
  `--linter <name>` 加脚本路径尾部的形状认领自生成命令，同一 linter 的重复项只保留第一条并把
  `command` 改写为当前版本；`check` 默认把仍存在的缺失或重复降级为 `warnings` 返回而不阻断 Init，
  需要严格阻断时传 `--strict`。macOS 上同一目录写成 `/tmp/...` 会被规范成 `/private/tmp/...`
  的情况也一并被折叠，不再需要人工清理。1.1.0 之前遗留的手动删条目步骤可以跳过；如果仍看到
  指向已卸载版本目录的 handler，说明它形状没被识别（例如自定义 flag），此时才需要用户确认。
- 根 `skills/` 中声明 `disable-model-invocation: true` 的方法 Skill 不进入模型可见列表，属预期行为；
  验收按“可见数 ≤ 声明数 − 抑制数”核对，不得据此判定装载失败。实测 1.2.0 会话给出上限那一侧：8 个根方法
  Skill 加 9 个薄入口共 17 个声明，3 个声明抑制，可见 `helix:` 条目 14 个。但同一个验收判据在 1.2.1 会话里
  只列出 12 个，缺席的是薄入口 `config` 和方法 Skill `d-d2c`——被截掉哪几项不固定，所以 14 是上限而非期望值，
  逐项调用成功才是判据。
- 首次运行任何插件 Python 脚本会在安装目录里写入字节码：实测一次只读 `host_runtime_cli.py resolve` 就在
  `.../helix/1.2.8/scripts/` 下生成 6 个 `.pyc`（`scripts/__pycache__/` 与 `scripts/host_runtime/__pycache__/`），
  跑过 `helix:status` 的 1.2.4 会话留下 8 个。因此“安装目录 == ZIP”的核对必须双向比并排除 `__pycache__`，
  否则多出的文件会被误读成安装被改动；ZIP 侧本来就不含这些文件，版本目录整体替换也不会跨版本累积。

第 2、3 项在只读验收会话里可自证，但输入框 `/` 菜单的实际条目只能由用户在客户端界面确认；未确认前
不要把 `command_surface` 声称为 `native`。任一项目未通过时保持对应能力为 `emulated` 或
`unsupported`，不得先声称可用。
