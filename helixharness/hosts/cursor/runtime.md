# Cursor 宿主运行时

当前 Host Context 必须显式绑定 `cursor` 和本次插件安装。首期支持桌面 IDE；本地 CLI
需单独原生验收，Cloud Agents 不在此支持范围。工具与能力以本轮 Cursor 实际提供的 schema 为准。

| 逻辑动作 | Cursor 映射 |
|---|---|
| `read_file` | 当前宿主的原生读取工具，完整读取指定文件 |
| `edit_file` | 当前宿主原生编辑工具，保留 sandbox、写域和单写者约束 |
| `run_command` | 原生 Shell/终端工具，保留 cwd、退出状态和实际输出 |
| `invoke_skill` | 当前安装的 Cursor Skill；内部方法完整读取唯一 canonical 正文 |
| `spawn_job` / `wait_job` | `resources.subagent_protocol` 的原生 Task 与对应完成结果 |
| `ask_user` | 本轮实际可用的交互工具；不可用时普通文本提问并等待真实回答 |
| `update_plan` | 本轮原生待办能力；不可用时维护当前 Feature `tasks.md` |
| `stage_finish` | 公共命令直接验证阶段工件；会话结束不等于阶段完成 |
| `project_entry` | 依 `resources.project_config` 合并 `AGENTS.md` |
| `project_hooks` | 父流程依 `resources.project_hooks` 幂等合并 `.cursor/hooks.json` |

不把公共拼写当作 Cursor 工具名。尤其不调用其他宿主的私有工具、loader、环境配置或运行日志。
额外权限必须使用 Cursor 的真实审批；不得为工作流修改全局审批模式或自动批准所有工具。

## canonical 方法的资源路径

canonical 方法正文中的 `HELIXHARNESS_PLUGIN` 只指本次已验证的 `plugin_root`。不信任继承的同名环境变量，
不搜索全局 Skill、其他版本缓存或其他宿主安装。每次 Shell 调用都须用该根路径重新绑定变量，并在同一次
调用中执行相关命令；不依赖跨调用环境保持，也不依赖 session hook 向终端传播环境。
对变量赋值使用 Shell 安全引用，对 `$HELIXHARNESS_PLUGIN/...` 展开后的完整路径安全引用；也可将其替换为
本次根内的绝对路径再完整引用。赋值必须先于使用该变量的命令，不能把未绑定变量的展开留在临时环境赋值的同一命令中。
方法正文的 `Read`、`Write`、`AskUserQuestion`、`Skill` 等公共拼写按 `reference/tool-semantics.md`
和上表映射；MCP 操作只使用当前宿主真实提供的工具 schema，不照搬其他宿主的工具前缀。

## 命令表面

本次 Cursor 公开入口使用 `/helix-{command}`。在输入框输入 `/helix-` 后选择对应 Skill；
不提供 `/helix:{command}` 原生别名或冒号前缀补全。以下左列仅为公共合同的逻辑名称。

| canonical 逻辑名称 | 当前 Cursor Skill |
|---|---|
| `/helix:init` | `/helix-init` |
| `/helix:propose` | `/helix-propose` |
| `/helix:apply` | `/helix-apply` |
| `/helix:archive` | `/helix-archive` |
| `/helix:bug-fix` | `/helix-bug-fix` |
| `/helix:ut` | `/helix-ut` |
| `/helix:update` | `/helix-update` |
| `/helix:status` | `/helix-status` |
| `/helix:config` | `/helix-config` |

展示建议命令时使用此映射；`--loop` 内部继续执行 Router 返回的 canonical workflow，不依赖菜单
名称、重新输入 slash 命令或再次调用入口 Skill。命令参数由 `skill-entry.md` 明确绑定。
方法 Skill 是可复用方法，不增加研发阶段或新的公开工作流命令。

## 生命周期、统计与交互

统计和更新通过本 Host Kit 的 `emulated` 适配器执行同一套公共授权与业务协议。
统计只在用户已明确开启后记录阶段、事件和可归属的用量；不读取 Cursor 私有数据库或 transcript。
Token 仅使用原生 hook 实际提供的数字，重复 generation 快照只计一次；缺字段、跨阶段或缺少
完整轮次边界时标记 unavailable/partial，不估算、不填 0，不把父代理用量宣称为全部子代理用量。
`stop` 只观察事件，不触发阶段完成、自动续跑或替代 D2C 门禁。

每次执行 HelixHarness Python 命令时，优先使用本次 `beforeSubmitPrompt` 注入的真实绑定；
没有本次绑定时只可使用仍属于当前会话的 `sessionStart` 绑定，不能借用此前会话。
beforeSubmitPrompt 的 additional_context 是经过受测客户端探针验证的兼容路径，
不是所有 Cursor 版本均保证的接口；实际不可见时仍按缺身份降级。
通过工具的环境参数或 Shell 安全引用显式传入 `HELIXHARNESS_CURSOR_CONVERSATION_ID` 与
`HELIXHARNESS_CURSOR_WORKSPACE_ROOTS`（原生 `workspace_roots` 的 JSON 数组）。这些变量只作用于
当前命令；不得从旧进程、最近会话文件或用户输入中猜测身份。Cursor 对 hook 的环境传播不保证
Shell 继承，必须每次绑定。缺少身份或当前仓库不在这些 roots 内时，统计受控跳过并保留原因，
业务继续，不把其他会话的 ID 借来使用。父流程传递明确的绑定与仓库范围，子任务不得扩大归属。
授权 enabled 不代表本次采集可用。开启或首次尝试统计时，如当前模型上下文缺少真实绑定，说明
`cursor_session_context_unavailable` 和本次统计跳过；不改授权设置，不反复打断后续业务。
受测 Agents Window 的 sessionStart 上下文曾未进入模型，不能仅凭 Hook 输出有效就宣布统计可用。

主动关闭授权立即清理当前仓库的 Cursor 私有状态；sessionEnd 与下一次 sessionStart 只作尽力补扫。
不承诺关窗即时删除，不在 stop 删除仍可能使用的 journal；保留其他宿主、公开账本及归属不明数据。

更新使用 `resources.installer` 登记的 Helix-managed local 安装。检查版本不改变安装；实际升级
必须走公共 `prepare → decide → install` 的真实用户确认和一次性授权，只部署批准的来源与版本。
未登记的副本、Cursor Marketplace 缓存不被接管。升级完成后按 installer 重载，再重新绑定宿主。
公共业务门禁、TDD、双轴 Review、归档证据仍完整执行；统计关闭不能用作跳过门禁的理由。

用户决策须取得真实回答；取消、空结果、超时或默认高亮不构成授权。没有合适交互工具时用文本
展示完整选项，并在必要决策处等待；可以先推进不依赖回答的工作。既有明确授权按命令合同复用。
提问不构成决策完成，不能用完成摘要替代待回答问题；`--loop` 不能授权提交、推送或发布。

## D2C 的显式完成门禁

完整执行 `skills/d-d2c/SKILL.md` 正文及所选 mode 的 rules：phase 0 初始化、实际构建和 SPA
runtime 健康、设计 DSL、截图、geometry/vertical/render/overlay 联合检查、fidelity 评测与 phase 6
清理均不可省略。`d2c-fidelity-evaluator` 仍须产出真实报告，不以投影说明代替。

canonical frontmatter 中的 Claude skill hooks 不在 Cursor 注册。当前适配使用可执行的显式门禁：

1. 新 D2C 工作流先按 canonical 初始化新的 phase 0，从其 checkpoint 回执记录本次
   `pageDir`、phase 0 的 `enteredAt`。明确 `--resume` 时使用已选择恢复的同一工作流 checkpoint，
   核对这两个锚点；不得借用无关或先前已完成工作流的锚点。
2. 任何结束当前 turn 或询问用户前，用当前真实项目根和上述锚点运行：

   ```text
   node "{plugin_root}/hosts/cursor/d2c_gate.cjs" --repo-root "{repo_root}" --page-dir "{pageDir}" --started-at "{enteredAt}" --action stop
   ```

   询问用户用 `--action ask`；headless 验收额外传 `--headless`。
   checkpoint 已绑定 owner 时还须传本次真实会话的 `--session-id`，不能从其他会话借用身份。
   当前上下文缺少真实身份时，该 owner 完成结论不可用；不得删掉、复制或改写 owner 绕过门禁。
3. 只接受退出码 0 且 JSON `allow=true`。`allow=false` 或脚本失败时按 `action` 继续修复/推进；
   缺 checkpoint、旧 invocation 或路径非法须恢复正确状态，不能声称完成。合法等待仍不代表完成，
   只有 `action.kind=done` 才能声明 D2C 完成。合法用户取消、外部输入或 browser handoff 保留原语义。
   完成还必须满足当前阶段为 6，阶段 0/1/2/3/4/6 的 enteredAt/completedAt 属于本次 invocation
   且按依赖有序，阶段 4 已选择 cancel。新 phase 0 残留的旧 phase 6 不能成为完成证据；
   pause 与 browser handoff 也必须有本次 invocation 内的新鲜时间证据。旧 browser handoff 在决策前忽略，
   既不能放行本轮，也不能覆盖本轮合法的 pause 或取消。

该 CLI 只检查本次业务 checkpoint，不读取 transcript，不声称具有原生 Stop/提问拦截能力。
原生工具不可用或门禁不能运行时应报告阻塞；不能把检查缺失改成成功。


## Cursor native update boundary

仅 Cursor 原生安装：`check` 返回 `reason=installation_update_not_supported`、`next_action=continue_with_warning` 时，展示当前安装及手动更新步骤后结束更新流程；业务继续。不得进入 prepare/decide/install，不得改用旧 Local 缓存或授权，不把 unknown 当作已是最新。显式后续更新操作退出码为 2。此限制不改变 managed-local、Claude Code 或 Codex 的原协议。
