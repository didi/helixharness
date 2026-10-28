# Codex 宿主运行时

该宿主保留现有 Codex Skill、协作工具、沙箱和命令内生命周期语义，绝不能假装存在
Claude Code 插件 hook。

| 逻辑动作 | Codex 行为 |
|---|---|
| `read_file` | 使用 `exec_command` 的只读文件命令读取确定路径 |
| `edit_file` | 使用 `apply_patch`，并保留 sandbox、写域与单写者约束 |
| `run_command` | 使用 `exec_command` 并保留真实退出状态；需要额外权限时只申请当前动作最小范围 |
| `invoke_skill` | 使用原生 Skill 发现机制和当前 plugin 限定入口；把 `invoke_skill(name)` 显式映射为 `$helix:{name}` 调用 |
| `spawn_job` | 遵守 `reference/codex-subagent-runtime-protocol.md` 和命令 Dispatch Contract |
| `wait_job` | 使用 `wait_agent` 等待已派发 job 的真实终态，并校验返回身份与产物 |
| `ask_user` | 按下方“用户决策与等待”选择本轮可用工具，并等待真实回答 |
| `update_plan` | 使用 `update_plan`；不可用时更新当前 Feature `tasks.md` |
| `stage_finish` | 使用现有命令内直接 finish 接口 |
| `project_entry` | 生成或合并 `AGENTS.md` |
| `project_hooks` | 父流程合并 `.codex/hooks.json`；子 Agent 不拥有该文件 |

`CODEX_THREAD_ID`、rollout 路径、`spawn_agent` 和运行时 handle 继续作为 Codex
适配器细节，不得进入共享工作流。

## 用户决策与等待

先检查本轮工具是否可用、当前模式及工具用途限制。流程明确要求普通文本的需求决策继续使用普通文本；固定提交／推送选择使用可用且允许用于该决策的结构化输入能力。不得为弹窗切换模式、虚构工具，或将仅允许可选澄清的工具用于提交／推送授权。已有明确授权时遵守命令合同直接复用，不重复提问。

- 同步输入工具必须取得真实用户回答；空结果、取消或超时不等于选择默认项。
- 使用 `request_user_input_async` 等异步输入工具时，`accepted: true` 只表示请求已受理，既不是用户回答，也不能证明用户已经看到弹窗。先完成不依赖回答的独立工作，再使用本轮可用、能被新用户输入唤醒的等待能力继续等待；每次等待有界，单次不超过 60 秒，一次无回答不构成拒绝或授权。不要重复发同一个问题，也不要发送最终完成消息收口或替换仍待回答的问题。
- 没有可用且允许的结构化工具、工具发送失败，或异步问题无法继续等待时，按宿主文本交互规则明确展示当前问题、范围及完整选择，并说明“等待你的选择”；以该待回答问题交还控制，不追加完成摘要。文本格式与选项展示服从当前宿主限制，不能仅写“选项已展示”。

收到新用户消息后先判断它是否回答当前问题；询问进度或讨论其他事项不视为授权。只有真实选择或明确取消才能结束该决策。等待最终提交选择不改变已经完成的业务归档与统计 finish，也不创建新 Stage Entry。

公共命令及其流程声明的方法依赖从当前插件的 `skills/{name}/SKILL.md` 完整读取执行。
这些内部方法不额外投影为公开命令；不得误用用户环境中的全局同名 Skill。
前端还原与还原度评测按调用方声明的条件执行，不得因禁止隐式调用而跳过已明确要求的步骤。

## 命令入口零发现加载

Codex 原生薄 Skill 被调用后，以当前 Skill 的实际路径为唯一锚点，固定向上四级得到 plugin root。
入口先建立 invocation 内存态 `HELIXHARNESS_CODEX_ENTRY_LOAD`，不得把它写入项目、plugin cache 或
telemetry，也不得持久化为第二业务事实源。它只记录本轮已读取资源的 repo-relative path 与
`source_sha256`，hash 未变化时禁止重复读取。

固定使用严格上限 `MAX_ENTRY_RESOURCE_READ_ROUNDS=2`。Codex 专属薄 Skill 只能对 bundle 内
`scripts/codex_entry_load.py` 执行 **一次 prepare**；prepare 生成最多两个逻辑 packet 的受验证快照，
正文通过同一随机 invocation key 的有界 page continuation 顺序消费。每个 prepare/page 回执必须小于
16 KiB；page read 不新增资源发现轮次，不得重复 prepare、逐文件自行探测或增加第三个逻辑 packet：

1. `packets[0]` 完整包含 Skill 明确引用的 projection/runtime/tool
   协议、`hosts/codex/host.json`、当前 `resources.runtime`、`resources.command_projection` 和唯一
   `commands/{command}.md`；每个文件保留 path/hash/page 边界，分页拼接不得截断或改写正文。
2. 可选 `packets[1]` 只包含该 command allowlist 内由薄 Skill 传入的 adapter，例如 subagent、
   workflow transition、UT 或 telemetry。没有相应逻辑动作时不得为通用初始化加载。

生命周期要求独占轮次的 `stage_start`、用户确认和业务命令不计入上述资源读取轮次，仍严格串行。
任一资源或逻辑总包超过脚本资源上限时立即 `helixharness_host_contract_invalid`；物理分页不得改变两个逻辑
packet 上限。不得用
`find plugin cache`、`rg --files plugin cache`、`ls 所有版本目录`、`wc 探测资源长度` 或等价命令
重新发现安装。预绑定路径缺失、越界或 page hash/TTL 不一致时立即
`BLOCKED:helixharness_plugin_catalog_stale|helixharness_host_contract_invalid`，不得猜测其他版本继续。
