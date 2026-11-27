# OpenCode 宿主运行时

本 Host Kit 面向 OpenCode V2（最低验证版本 2.2.2）。插件包在本安装的 `opencode/index.ts`
中一次性注册全部宿主表面，不把 canonical Markdown 复制到项目的 `.opencode/`：

- 公开命令注册为 `helix:<name>`，执行时加载同一插件内的唯一 `commands/<name>.md`；
- 方法 Skill 注册为 `helix-<name>`，正文仍来自根 `skills/<name>/SKILL.md`；
- 子任务角色注册为 `helix-<name>`，正文仍来自根 `agents/<name>.md`。

命令执行、原生 `skill` 工具结果与 Skill mention 模型请求由插件注入预绑定 Host Context（`canonical_host_id=opencode`、
`plugin_root`、`repo_root`、`host_manifest`、`capabilities` 和全部资源绝对路径）。这等价实现
`session_start=emulated`；不得从环境变量、项目文件或其他插件缓存重新识别宿主。通过原生工具调用方法
Skill 时，`repo_root` 由 `tool.execute.after` 按当前会话目录绑定。Skill mention、命令附件
与原生 Skill 激活则在 `context` 请求 hook 中绑定；`compaction`、`generate`、`title`
请求复用同一绑定逻辑。只替换本安装注册的完整 Skill 正文中的占位符，用户字面输入、其他
Skill、媒体与元数据保持不变；不修改持久化的源消息，也不缓存某个会话的目录。

命令执行和原生 `skill` 工具调用均以请求会话的 `session.get().location.directory` 为目录来源，
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
| `update_plan` | `scripts/task_plan.py show/update` 操作当前 Feature 的 `tasks.md`；更新须带最新内容回执 |
| `stage_finish` | 在命令内按真实终态显式调用 HelixHarness 生命周期脚本 |
| `project_entry` | 生成或合并根 `AGENTS.md` |
| `project_hooks` | 当前不合并项目 settings hooks；插件级 `project_checks` 通过原生编辑事件运行，不创建替代配置文件 |

原生子会话继承父目录；跨仓 RepoJob 必须先用当前原生会话切换能力绑定 `ownership_root`，
再进入 repo-local 命令或 Skill。prompt 的路径字段与 shell `cd` 不能替代会话目录切换；
具体映射与失败处理见 `resources.subagent_protocol`。

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
给定的 `steer|queue`。Skill mention 正文在模型请求前按请求会话目录绑定；查询失败或目录无效时中止该请求，
不向模型交付未绑定的 Skill 正文。所需工具结果与模型请求 hooks 必须先于公开入口注册，
任一必需 hook 不可用时插件装载失败。
`metadata` 只记录逻辑命令名，不进入业务事实。

## 用户决策与等待

- `question` 必须取得真实回答；取消、空结果或超时不等于默认选项。
- 需要文本选择时按公共命令要求展示完整问题和选项，并交还控制等待回答。
- 子代理内部使用 `question` 时保持 `interaction_mode=host_native`；若 child 无法直接交互，
  返回具体缺口，由父流程按命令的 blocked 语义处理。

## 命令入口与资源加载

命令入口已经是公共 contract，不需要额外薄入口。插件在启动时读取并校验一次：

- `hosts/opencode/host.json` 的 `host_id`、`host_api_version=1.1`、能力和全部资源引用；
- 九个 `commands/*.md`、根 `skills/*/SKILL.md` 和 `agents/*.md`。

资源加载在读取文件或枚举目录前同时校验字面路径和 `realpath` 解析后的边界，
覆盖 manifest、adapters、commands、skills 与 agents。插件内部链接允许使用；
外部链接、悬空链接和环状链接均拒绝。资源缺失、越界、宿主 ID 或 API 版本不一致时插件装载失败。
不得扫描其他插件副本或降级到 Claude Code/Codex。Skill 与 Agent 的 canonical 正文只在本插件内解析，不读取项目中的同名
`.opencode/skills` 或 `.opencode/agents`。

## Python 与统计

OpenCode 插件本身使用 TypeScript，不依赖 Python 启动。业务命令仍按 `reference/runtime-preflight.md`
执行 Python preflight。`telemetry=emulated` 复用明确授权、阶段账本、工作流轨迹和异步队列。
通过原生 shell 以绝对路径调用本安装统计脚本，插件给该进程绑定真实会话 ID 和目录；缺失或
不匹配时跳过，不借用其他宿主授权。不读取会话日志，token/未观察到的工具信息保持未知。
`plugin_update=emulated` 复用 check/prepare/decide/install 授权链路，对有明确 upstream 的干净
源码安装执行锁定 commit 的快进更新；其余安装不自动修改。详细操作见[能力适配](capabilities.md)。

## D2C 显式检查与 owner 绑定

OpenCode 不执行 Claude 的 skill-scoped Stop / PreToolUse Hook。采用 canonical Skill 的无 hook
执行语义；下方显式检查不拦截 OpenCode 自身的任意停止动作，也不认证浏览器或模型报告的真实性。

方法 Skill 的工具加载、mention 与原生激活会把本次请求的 `native_session_id` 注入 Host Context。
只能使用该原生身份，不得猜测 ID、借用父/其他会话的 ID，或用手工构造的上下文冒充绑定。
每个新的 D2C 请求先按 canonical checkpoint CLI 执行 `--enter 0`；从本次 marker 取得
`pageDir` 与 `workflowStartedAt`，立即执行一次 owner claim，再开展后续阶段：

```bash
node "{plugin_root}/hosts/opencode/d2c_gate.cjs" --repo-root "{repo_root}" --page-dir "{pageDir}" --started-at "{workflowStartedAt}" --session-id "{native_session_id}" --action claim
```

claim 只用于新初始化的未认领 phase 0；超时或旧状态不能在完成时重新认领，须从新的 phase 0 开始。
需要停止并报告结果前，以相同 page/start/session 参数执行 `--action stop`；需要交互前执行
`--action ask`。只有脚本退出 0 且 `allow=true` 才消费其 `action.kind`：`done` 才是完成，
合法外部输入、浏览器 handoff 或用户取消不等于完成。退出 2、损坏/缺失工件、跨 owner 或旧轮次
均保留阻断原因并按原流程修复/恢复，不通过手工改 marker/状态/报告绕过。后续阶段继续执行
canonical checkpoint 校验，不能把单独的门禁通过当作完整视觉验收。

证据审计有明确容量上限：PNG 文件 32 MiB、解压后像素扫描行 128 MiB、评估报告 8 MiB。
超限返回容量诊断并阻断本次审计，不将大图判定为视觉不合格；须提供容量内的受支持截图。
评估报告读取 canonical Markdown 中的 fenced JSON，并核对本页、本轮、分数与物料；
支持仓库内的自定义报告路径，不接受任意非空文本代替评估结果。
canonical 状态更新与 owner 认领并发时可能返回 busy；等当前写入结束后重试，不手动删除活动锁。
锁使用进程唯一条目；下次写入会回收已终止进程的遗留。canonical 的仅警告测量仍可通过，
门禁会在 `evidenceAudit.warnings` 返回这些警告，不能把它们表述为测量全部匹配。

## 入口能力预检

变更型命令在提交 prompt 前读取原生 `tool.list` 的允许名单，仅注入登记工具名和 session move
API 是否存在，不读取或输出 providers、headers、凭据或全量配置。已知缺少 canonical `read` /
`shell` 时返回 `opencode_preflight_tools_unavailable`，尚不写需求或派发任务。查询不可用、失败或
返回未知结构时标为 `unavailable`，保留现有命令入口；Status/Config 不依赖该查询。

登记与 API 存在不代表当前权限已经批准，也不证明实际执行成功。当前 SDK 未提供有效嵌套深度的
直接查询，因此 `native_delegation_depth=unknown`；不得将其写成检查通过或自动修改配置。
公共 Router 确定 scope 后、任何 Workspace 状态更新和 repo 派发前，按该阶段检查：

- Workspace 需要切换到不同 ownership_root：核对当前暴露的 session move schema。登记目录中缺少
  `opencode_session_move` 且 SDK move API 也不可用时，返回具体目录切换 blocker；未知时保持真实
  schema 发现和返回位置校验，不假设 shell cd 等于切仓。
- Apply 的独立评审/Workspace 嵌套评审：核对实际 subagent 能力、权限与 depth。深度未知须明确提示
  Workspace 配置要求至少 2，并保留原生准入检查；不自审、不降级内联、不把未知当成功。
- 执行阶段需要的 read/shell/question/skill 等以实际 schema 和权限为准，缺失时给出具体缺口，
  不调用其他宿主工具或更改全局审批模式。Python/Git/Node 等进程依赖仍由既有 runtime preflight
  及 D2C 入口检查负责，工具登记不能代替可执行文件探测。

## 项目配置

`resources.project_config` 固定 `entry_file=AGENTS.md`、`settings_file=null`。Init 只合并
`AGENTS.md` 并生成公共工件，不创建 `.opencode/` 设置或虚构 hook 配置。
原生 write/edit/patch 成功后，插件会运行已生成的项目 invariant/spec-sync linters；没有巡检目录
时跳过。失败或超时作为工具反馈，保存的编辑保留；shell 修改仍须执行公共工作流的显式检查。
