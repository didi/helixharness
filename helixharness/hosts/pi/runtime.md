# Pi 宿主运行时

该宿主保留 pi 的原生工具、Skill 发现与扩展事件语义，绝不能假装存在 Claude Code 或 Codex 的
插件 hook 与 marketplace。

| 逻辑动作 | Pi 行为 |
|---|---|
| `read_file` | 使用原生 `read` 工具读取确定路径 |
| `edit_file` | 使用原生 `edit`（精确文本替换）或 `write`，保留单写者约束 |
| `run_command` | 使用原生 `bash` 工具并保留真实退出状态 |
| `invoke_skill` | 使用原生 Skill 发现机制；命令入口为 `/helix-{name}`，强制加载为 `/skill:helix-{name}` |
| `spawn_job` | 本宿主 `subagents=unsupported`：只按 Dispatch Contract 的 `unavailable_fallback` 串行 inline 执行 |
| `wait_job` | 无子代理时不存在等待；inline 结果在父流程内直接校验 |
| `ask_user` | 使用当前会话可用的文本或结构化提问能力，并等待真实回答 |
| `update_plan` | 本宿主 `task_tracking=unsupported`：更新当前 Feature 的 `tasks.md` |
| `stage_finish` | 使用现有命令内直接 finish 接口 |
| `project_entry` | 生成或合并 `AGENTS.md` |
| `project_hooks` | 本宿主不支持项目 hooks；不得改写任何 pi 设置文件 |

`PI_SESSION_ID`、`PI_SESSION_FILE` 和 pi 扩展事件句柄继续作为 pi 适配器细节，不得进入共享工作流。

## 用户决策与等待

pi 的交互提问能力可能来自核心或用户扩展。先检查本轮工具是否可用、当前模式及工具用途限制。
流程明确要求普通文本的需求决策继续使用普通文本；固定提交／推送选择使用可用且允许用于该决策的
结构化输入能力。不得为弹窗切换模式、虚构工具，或将仅允许可选澄清的工具用于提交／推送授权。
已有明确授权时遵守命令合同直接复用，不重复提问。

- 同步输入工具必须取得真实用户回答；空结果、取消或超时不等于选择默认项。
- 异步输入工具返回的“已受理”不是用户回答，也不能证明用户已经看到弹窗；先完成不依赖回答的独立
  工作，再使用本轮可用、能被新用户输入唤醒的等待能力继续等待。每次等待有界，单次不超过 60 秒，
  一次无回答不构成拒绝或授权。不要重复发同一个问题。
- 没有可用且允许的结构化工具时，按宿主文本交互规则明确展示当前问题、范围及完整选择，并说明
  “等待你的选择”；以该待回答问题交还控制，不追加完成摘要。

收到新用户消息后先判断它是否回答当前问题；询问进度或讨论其他事项不视为授权。只有真实选择或
明确取消才能结束该决策。等待最终提交选择不改变已经完成的业务归档与统计 finish，也不创建新
Stage Entry。

公共命令及其流程声明的方法依赖从当前插件的 `skills/{name}/SKILL.md` 完整读取执行。这些内部方法
不额外投影为公开命令；不得误用用户环境中的全局同名 Skill。前端还原与还原度评测按调用方声明的
条件执行，不得因禁止隐式调用而跳过已明确要求的步骤。

## 运行依赖与 preflight

任何 `python3` 调用前先执行一次 `sh {plugin_root}/scripts/runtime_preflight.sh check`，按
`reference/runtime-preflight.md` 处理结论：`git.status=missing` 立即
`BLOCKED:helixharness_git_runtime_unavailable`；`python.status=missing|unsupported_version|unusable`
输出 `WARN:helixharness_python_runtime_unavailable`，继续不依赖 Python 的步骤。

本宿主 `telemetry=unsupported`，preflight `next_action` 中与统计相关的建议（例如
`install_python_or_disable_telemetry`）不适用：不展示统计选项，也不为此安装 Python；需要 Python 的
工作流命令仍按上述 WARN 和用户确认处理。

## 命令入口加载

pi 原生薄 Skill 被调用后，以当前 Skill 的实际路径为唯一锚点，固定向上四级得到 plugin root。
入口只读取 `hosts/pi/host.json`、manifest 绑定的 `resources.runtime`、`resources.command_projection`、
`reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和唯一 `commands/{command}.md`；
由 command 正文再按需读取其显式声明的 adapter。

不得用 `find`、`rg --files`、`ls 所有版本目录` 或等价命令重新发现安装。预绑定路径缺失或越界时
立即 `BLOCKED:helixharness_host_contract_invalid`，不得猜测其他版本继续。
