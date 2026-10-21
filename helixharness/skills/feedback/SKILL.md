---
name: feedback
description: "HelixHarness 使用问题反馈通道。当用户在 /helix:* 命令与工作流中遇到问题并表现出困扰时主动使用：命令反复失败或报错无法推进、生成的文档/字段/结构与用户预期不符、流程在某一步卡住不动、用户对 HelixHarness 表达不满困惑或抱怨、放弃工作流改用手工方式，或明确要求提交反馈。此时向用户建议提交反馈，发送前必须经用户确认。不触发：用户业务代码的问题、与 HelixHarness 无关的报错、一般使用咨询、模型已自行修复的故障。"
---

# HelixHarness feedback

> 反馈边界（本 skill 的硬约束，来源 `reference/feedback-protocol.md`）：反馈是用户显式提交的实时上报，与 usage telemetry 完全解耦——不读取对话之外的任何数据、不创建 eval entry、不消费 telemetry consent（已配置反馈目的地时，telemetry 关闭不影响反馈；默认没有平台地址，未配置时仅保留入口，不能发送。未注册宿主以 `host=unknown` 上报）。用户明确要求附带时，会话上下文（对话文本 + 工具调用轨迹）随反馈一并发送，发送前经过裁剪（不含工具原始输出、thinking、宿主注入内容）、脱敏（密钥/手机号/邮箱/内网地址替换）与本地标识剥离；绝不写入用户仓库，绝不自动外发任何未经确认的内容。

> Runtime 硬门禁：在任何 `python3` 调用前，必须读取并执行 `{plugin_root}/reference/runtime-preflight.md`（本文所有 `reference/` 相对路径均指 plugin 根下的 `reference/`，**不是**本 skill 所在目录旁的 reference/）。runtime preflight 返回 `runtime_unavailable` 时按其 fallback 规则处理，不得静默跳过运行时检查。

> 参数宽容规则：解析 `$ARGUMENTS` 时先按 shell token 分词；每个 token 仅去除尾部常见中英文句读（`, . ; : ! ? ，。；：！？、` 及右侧闭合括号）后，再对 flag 做精确匹配。不得使用任意子串匹配，例如 `--category-bug` 不能视为 `--category`。

## 使用前提

接收地址默认由发布方在 `scripts/reporting_config.py` 的 `DEFAULT_FEEDBACK_ENDPOINT` 统一配置，当前默认值为空，后续需要时再填写。用户可以创建或修改 `~/.helixharness/feedback/config.json`，用非空 `endpoint` 覆盖发布默认值：

```json
{
  "endpoint": "http://127.0.0.1:7860/feedback/report"
}
```

发布脚本与用户配置文件是仅有的两种地址来源，不读取地址环境变量。`HELIXHARNESS_FEEDBACK_HOME` 仍可覆盖配置、草稿和队列目录。缺少文件、字段或值为空时使用发布默认值；文件损坏、字段类型错误或非空地址无效时停止准备，不回退到其他接收方。

接收端必须是实际运行的 helixharness-platform；本机可运行 `helixharness-server --port 7860`，远程平台替换为可访问的完整地址，`127.0.0.1` 指插件所在机器。发布地址已有效时无需用户再配置。不猜测接收方，不复用统计的 `/report` 地址；两种来源均为空时停止发送准备并说明配置方法，不声称“已上报”。

## 触发形态与同意语义

本 skill 支持三种触发形态。**终确认必须覆盖准备好的实际接收地址和完整脱敏内容，不跟着触发形态走**：

- **图形选择定型（AskUserQuestion）**：收集用户原文、分类与 feature 后进入 Step 3 准备草稿；完整内容和接收地址已明确确认时直接发送，不重复确认。
- **文本口头定型（降级路径）**：口头回答可能有映射歧义，进入 Step 3 后连同实际地址和脱敏内容一并回显确认。

| 触发形态 | 同意规则 |
|---|---|
| 用户调用 feedback Skill 且带反馈正文 | 用户显式发起、正文即用户原话，跳过描述收集；分类与 feature 按 Step 2 收集，按上表定型方式决定是否复述 |
| 用户调用 Skill 但未带正文（收集式提交） | 按下方流程收集，按定型方式决定是否复述 |
| 模型自动触发（识别到用户遇到 HelixHarness 问题） | 同意提交后走 Step 2 收集，按定型方式决定是否复述；文本路径下须复述完整内容（正文 / 分类 / feature）经确认后才发送 |

**自动触发的引导规则**（识别到用户受挫信号后，何时开口、怎么开口）：

- **时机**：先完成自救（重试/修复）。若已恢复且用户全程无感，不开口；若仍失败、需要向用户交代失败时，在交代失败的同一句话里顺带引导，不单独成段、不重复追问
- **开口话术三要素**：说明检测到了什么（哪个环节出的问题）、问一句是否提交反馈、说明"后续会先跟你确认分类和关联 feature"
- **两段式确认（硬约束）**：用户同意提交 ≠ 完成收集。同意后必须继续走 Step 2 的分类与 feature 收集，不得在用户说"好/可以"的那一刻跳过收集直接发送；收集完成后按 Step 3 准备草稿并确认实际接收地址与内容
- **节制**：同一问题一个会话只引导一次；用户拒绝 → 本会话不再就同一问题二次引导（同会话节流）；自救成功且用户无感 → 不引导
- 非交互 / loop（autopilot）上下文禁止自动上报：该场景下本 skill 直接以错误终止并说明原因，不收集、不发送

## User Input

```text
$ARGUMENTS
```

## 参数说明

| 参数 | 说明 | 示例 |
|------|------|------|
| `<反馈内容>` | 位置参数，反馈正文（1..4000 字符）。与 `--category` 可组合 | `propose 阶段生成的 spec 字段要求不清楚` |
| `--category <分类>` | 可选，`bug` / `docs_unclear` / `process_stuck` / `suggestion`，缺省 `uncategorized` | `--category docs_unclear` |
| `--feature <name>` | 可选，关联的 active feature 名；缺省时按交互确认 | `--feature early-repayment` |

## 执行流程

> 本 skill 是轻量反馈工具，**不做插件版本快速检查**（不同于业务工作流命令）：用户带着问题来，升级提示会打断反馈，且上报内容对插件版本零耦合。runtime preflight 硬门禁（python3 可用性）仍然保留。

### Step 1: 读取反馈协议

读取 `{plugin_root}/reference/feedback-protocol.md`（字段语义、transcript 裁剪与脱敏边界、服务端消费契约的唯一权威定义）；读取失败时停止准备和发送，并说明反馈组件不完整。

### Step 2: 输入收集（描述先行）

**`$ARGUMENTS` 带反馈内容时**：解析出正文与可选 `--category` / `--feature`，进入本步收集，随后按 Step 3 准备。正文以用户原话为准，不得改写、总结或补充。`--category` / `--feature` 缺哪项就只按本步规则补哪项。

**`$ARGUMENTS` 为空或模型自动触发时**：先问一句开放问题（如"说说遇到的问题吧"），拿到描述后再收集分类与 feature——描述是分类推断和 feature 定位的依据，必须先行。

**分类推断**：从用户描述推断分类并映射到枚举 `bug` / `docs_unclear` / `process_stuck` / `suggestion`，映射不上即 `uncategorized`。推断结果只作为推荐项呈现，由用户最终确认。

**分类与 feature 的收集——按宿主能力分派**。判定规则是确定性的，不得自行裁量：**当前会话工具列表里有 `AskUserQuestion` 就必须走首选路径**，没有才走降级路径；禁止在有该工具时降级为纯文本提问。

- **首选路径（会话工具列表含 `AskUserQuestion`，如 Claude Code）——必须使用**：一次调用同时问两问——
  - "反馈分类"：4 个选项（bug / docs_unclear / process_stuck / suggestion，label 用枚举值、description 一句话释义），推断项放第一位并在 label 标"（推荐）"；用户经 Other 自由输入时映射枚举，映射不上按 `uncategorized`
  - "关联 feature"：选项 = `不关联` + `docs/specs/active/` 下的 feature（最多再列 3 个，推断项或最近活跃优先并标"（推荐）"）；active 超出 3 个时经 Other 输入，**输入值必须校验存在于 active 列表**，不存在则提示重选；`docs/specs/active/` 不存在或为空时该问整体跳过（`feature=null`）
  - 选择完成后进入 Step 3 准备草稿；收集选项本身不替代对实际地址和完整脱敏内容的确认。
- **降级路径（仅当会话无 `AskUserQuestion`，如 Codex）**：对话式收集（下为摘要格式示例，实际值以本轮收集结果为准；在 Step 3 连同实际地址和完整脱敏内容确认后才发送）——

  ```
  当前收集的反馈信息（可直接说要改哪项）：
  - 分类：流程卡住
  - 描述：propose 到 P2 卡在确认项清理（你的原话）
  - 关联 feature：early-repayment
  ```

  无推断依据时相应项写"未分类"/"不关联"；用户口头改 feature 名同样必须校验在 active 列表内，不在则提示可选项。

**红线不变**：feature 值只允许来自 `docs/specs/active/` 实际列表 + 用户确认，任何情况下不得猜测或编造；用户显式传 `--feature` 时使用该值；"不关联"是有效答案，不二次确认、不暗示次优；非交互/loop（autopilot）上下文不收集、不自动上报；用户所选 feature 没有 eval-log 时 `related_entry_ids=[]`，合法。

### Step 3: 准备、确认并发送

`{plugin_root}` 必须来自当前 plugin skill resource（Claude 可用 `${CLAUDE_PLUGIN_ROOT}`，Codex 使用当前 plugin resource root），不得搜索安装目录。`{current_platform}` 必须使用 canonical host ID：Claude Code 固定为 `claude-code`，Codex 固定为 `codex`。

先准备草稿，不发送。参数必须安全转义：

```bash
python3 "{plugin_root}/scripts/feedback_submit.py" prepare --repo-root "{repo_root}" --platform "{current_platform}" --text "{反馈正文}" --category "{分类}"
```

按用户选择追加 `--feature "{feature}"`。默认不附带会话；只有用户明确要求附带当前会话时追加 `--include-transcript`，不搜索历史会话替代。

展示返回的实际 `endpoint`、完整 `item.document` 和附带的 `item.transcript_md`（如有），取得对地址和内容的明确发送确认。已有对同一完整内容及地址的明确授权时不重复确认；内容修改后重新准备。

```bash
python3 "{plugin_root}/scripts/feedback_submit.py" send --draft "{draft_path}" --digest "{digest}" --confirmed
```

`--confirmed` 必须由真实用户授权支持。脚本失败（exit 1）时向用户原样展示 stderr 中的错误 JSON 并终止，不得重试改写内容。非交互/loop（autopilot）上下文不得自动上报。

### Step 4: 回执输出（逐字使用 prepare/send 返回值，禁止手工编造字段）

**实时送达（send 的 `status: "delivered"`）**：编号与状态取 send 回执，document 和 transcript_stats 取同一已确认的 prepare 草稿 `item`，未附带会话时如实说明。

```
## HelixHarness feedback 已发送

- 反馈编号: {feedback_id}
- 状态: 已实时送达配置的平台看板（{endpoint}）
- 会话上下文: user {transcript_stats.user_messages} / assistant {transcript_stats.assistant_messages}，
  工具调用 {transcript_stats.tool_calls} 次，脱敏 {transcript_stats.redactions} 处
  （transcript_stats.supported 为 false 时，改为如实说明原因，如 codex 会话暂不支持采集）

### 本次发送的完整字段
{逐项列出 document 中的全部字段与值；这是透明披露，不得省略}
```

**进入本地队列（`status: "queued"`）**：如实说明看板暂不可达（`delivery` 字段），报告已进入本地待发队列（`queue_path`），下次提交反馈时自动补投，最长保留 30 天，只向原接收地址补投；如本次顺带补投了历史队列消息（`queue_flushed.flushed`），一并说明。

**平台拒绝（`status: "rejected"`）**：尚未送达，保留草稿并报告原因，不改写内容重发。用户要求重试时可执行 `feedback_submit.py retry --confirmed`，仅补投已确认且与当前配置地址一致的待发记录。

## 双宿主说明

- Claude Code 与 Codex 使用同一 CLI（`feedback_submit.py`），反馈流程本身无 hook 参与、无 consent 流程
- 会话上下文采集当前仅支持 claude-code 会话；Codex 宿主仍可上报反馈元数据（`transcript_stats.supported=false`），不是错误
- 未注册宿主（`{current_platform}` 无法给出 canonical ID 时）：`--platform unknown` 仍可上报，文档中 `host` 记为 `unknown`

## 注意事项

- **过程静默** — 内部动作（读取协议/preflight 文件、列出 feature 目录、校验路径与枚举值）不向用户逐条汇报；用户视角只出现业务话术（提问、选项、复述、回执）。读取必需协议失败时停止发送并说明反馈组件不完整
- **正文即用户原话** — 只做 shell 转义，不改写、不翻译、不截断；超 4000 字符时提示用户精简后重试
- **不读会话之外的数据** — 会话上下文仅取当前会话的对话文本与工具调用轨迹；工具原始输出、thinking、宿主注入内容一律排除
- **幂等** — 同一 `feedback_id` 与相同内容重复上报由服务端去重；相同 ID 的不同内容被拒绝，skill 侧不改写 ID 绕过
- **失败即止** — 校验失败（超长、非法分类、非法 feature 名）原样展示错误，不降级发送
- **不写仓库** — 反馈与上下文不落盘到业务仓库，不执行任何 git 操作
