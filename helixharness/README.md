# HelixHarness

当前版本：**1.0.0**。

面向 Codex、Claude Code、Cursor 与 [OpenCode（macOS Stable）](../docs/opencode-testing.md)的 HelixHarness 研发工作流。通过项目约束、知识库、Spec、任务和验证证据完成研发。

## 命令

以下 `/helix:*` 为公共逻辑命令名，也是 Codex / Claude Code / OpenCode 的输入形式。Cursor 使用 `/helix-{command}`，从 `/helix-` 补全选择入口，参数和业务门禁相同。Cursor 默认通过市场安装，固定来源配置和托管 Local 作为可选方式；统计及更新的能力边界见 [Cursor 安装说明](hosts/cursor/installer.md)。

- `/helix:init`：初始化当前项目；`--workspace` 初始化多仓目录。
- `/helix:propose <name> <requirement-or-path>`：收敛需求并生成 Spec/Tasks。
- `/helix:propose <name> <requirement-or-path> --loop`：自动串联 Propose → Apply → Archive；同名 `propose <name> --loop` 续跑。
- `/helix:apply <name>`：逐项实现、测试和代码评审。
- `/helix:archive <name>`：核验收据、更新知识并归档。
- `/helix:bug-fix <name> <description>`：修订尚未归档的单仓需求。
- `/helix:ut`：扫描后选择包与并发，生成和维护单测；也支持显式参数。
- `/helix:update`：检查并更新当前宿主已配置的插件源。
- `/helix:status [name]`：只读查看需求进度、阻塞原因与建议命令；`--completed` 查已归档需求。
- `/helix:config`：查看配置；通过参数修改统计开关、范围、统计与反馈地址。

## 安装

需要 Python 3.10+、Git，以及 Codex、Claude Code、Cursor、OpenCode（macOS、2.3.2；见[支持矩阵](../docs/opencode-testing.md)）或 Qoder。Codex、Claude Code 可将本目录作为本地 marketplace 添加；Cursor 通过市场界面安装；OpenCode 将本插件目录加入 `plugins` 数组；Qoder 没有等价 marketplace CLI，需先用 `scripts/package_qoder_plugin.py` 生成 ZIP，再由 App 安装工具安装。宿主步骤见 [Codex](hosts/codex/installer.md)、[Claude Code](hosts/claude-code/installer.md)、[Cursor](hosts/cursor/installer.md)、[OpenCode](hosts/opencode/installer.md) 和 [Qoder](hosts/qoder/installer.md) 安装说明。

**OpenCode Stable 支持（macOS、2.3.2）：** 当前源码候选的支持范围与边界见[支持矩阵](../docs/opencode-testing.md)。OpenCode 2.2.2、2.3.0 仅有自动化兼容覆盖。

插件注册原生 `helix:<command>` 命令、`helix-<skill>` 方法 Skill 和 `helix-<agent>` 角色。命令、原生 `skill` 工具结果、Skill mention 与原生 Skill 激活均按请求会话绑定仓库目录；会话查询或目录校验失败时阻断未绑定的 Skill 正文。2026-11-20 已在 OpenCode 2.3.2 + DeepSeek 上完成一次合成单仓 Init → Propose → Apply → Archive 真实模型闭环，包含操作者确认和一次受控合同修订；2026-11-23 又完成了 OpenCode 2.3.2 + DeepSeek 的操作者辅助合成双仓 Workspace 与独立 UT 验证；后续已修复 UT 分类校验与原子发布保护，并通过无纠正提醒的真实模型复验。[合成 Workspace 的专项真实模型验证](opencode/tests/README.md#recovery-and-auxiliary-workflow-acceptance-2026-11-23)还覆盖了 Apply 与部分 Archive 原生取消后新会话恢复，以及注入原生 context hook 故障后阻断依赖派发。完整生产业务项目、无人干预时的协议遵守与完整 TUI/桌面 UI 验收仍未完成；已覆盖范围见[定向终端 UI 验收](opencode/tests/README.md#targeted-terminal-ui-acceptance-2026-11-23)。Workspace Apply 的 `may_delegate` RepoJob 使用 `helix-worker`，需要 `experimental.subagent_depth >= 2`；独立 reviewer 保持只读 leaf。深度不足时阻塞该 RepoJob，不得绕过评审。

OpenCode 适配使用原生工具和会话 Hook，但未提供等价的 SessionStart/Stop/失败/会话结束业务保证。任务跟踪通过 `scripts/task_plan.py` 原子更新同一份 `tasks.md`。统计在明确授权后记录阶段事实和工作流轨迹，不采集原始会话，token 和未观察到的工具次数保持未知。`/helix:update` 对有明确 Git upstream 的干净源码安装执行既有确认协议，锁定 commit 并仅快进；有本地修改、游离版本或包管理安装时不自动更新。Init 合并 `AGENTS.md`，不创建 `.opencode/` settings；插件在原生 write/edit/patch 成功后执行已有项目巡检，shell 修改仍需显式检查。d-d2c 的 Claude skill-scoped Stop/PreToolUse guard 不执行，采用无 hook / `--bare` 语义，仍须遵守用户决策、视觉验收和完成门禁。

## 独立方案探索与调试

- [brainstorming](skills/brainstorming/SKILL.md)：有代码库时结合相关实现比较方案；没有代码库时澄清目标用户、核心场景和最小可用版本。产出方案取舍、推荐方向、待确认问题与下一步。
- [systematic-debugging](skills/systematic-debugging/SKILL.md)：根据代码、日志、请求或测试复现异常、验证假设并定位根因；获授权修复后，重跑复现和回归检查，明确修复与验证状态。

在编码助手中明确要求“使用 HelixHarness brainstorming / systematic-debugging Skill”。两者作为独立工具使用，不由研发命令自动触发，也不启动研发命令；无需先执行 Init、创建需求、开启统计或启动 helixharness-platform。沿用原版方法和输出格式，不规定持久化文件。

有代码库时结合相关实现探索方案；没有代码库时先澄清想法与约束。Skill 交付讨论摘要或调试结果后结束；用户可自行将结果作为后续需求输入。两项 Skill 不创建 feature、Spec/Tasks 或 eval-log，不推进研发阶段。

这两项是辅助 Skill，不增加研发阶段，公开入口另含 status 与 config。Codex、Cursor 与 Qoder 使用各自生成的宿主薄入口（`hosts/<host>/skills/`），Claude Code 直接发现根 `skills/`，OpenCode 注册 `helix-<name>` 方法 Skill；详细示例见[使用指南](../docs/user-guide.zh-CN.md#从想法或异常开始)。

可选的 [git-diff-impact](skills/git-diff-impact/SKILL.md) 可独立分析 Git 改动的调用链、业务影响和兼容性。显式要求“使用 HelixHarness git-diff-impact，分析当前未提交改动，--no-save”即可，无需 Init 或 feature。默认保存 Markdown 报告到 `diff-impact-reports/`；日常 Apply 不自动调用，也不改变评审状态。`code-review` / `tdd` 当前仍依赖 HelixHarness 的 Spec 和执行上下文，没有独立模式。

## 项目结构

`commands/*.md` 保存公共入口和路由消费；Propose / Apply / Archive / Bug Fix 的单仓完整正文在 `workflows/single/`，跨仓协调正文在 `workflows/workspace/`。Router 根据 `execution_scope` 返回唯一工作流路径；Init / UT / Update / Status / Config 保持各自公共命令合同。

`docs/harness/` 保存约束，`docs/knowledge/` 保存当前知识，`docs/specs/active/<name>/` 保存 Spec/Tasks/verify-report，归档后移至 completed。Workspace 根通过 `helixharness.workspace.yaml` 登记子仓，根 Spec 协调各仓完整 HelixHarness 生命周期。

## 可选配置

随时使用 `/helix:config` 查看生效值。显式设置示例：`/helix:config --telemetry disabled --scope project`、`/helix:config --report-endpoint http://127.0.0.1:7860/report`。反馈地址用 `--feedback-endpoint`；`--reset-report-endpoint` / `--reset-feedback-endpoint` 恢复对应发布默认地址，不等于关闭统计。详见[配置命令](commands/config.md)。

支持统计的宿主上，统计默认关闭，但**无论是否配置异步地址，Init / 规定的 Propose 入口都按授权缓存和提醒周期正常询问**。选择开启后，同步写入 `docs/eval/<feature>/eval-log.json`，平台可本地读取或经 Git 同步获取；没有有效异步地址时仅跳过后台采集和投递。未授权或关闭时，同步账本不新增记录，异步也不采集或投递；历史记录不会自动删除。账本 `completed` 只表示阶段完成，不能声称平台已收到异步明细。

未配置异步统计地址时，正常流程静默跳过，不额外显示地址缺失、未异步上报或配置提醒；仅在用户主动查询或排障时解释。

report 与 feedback 统一采用两种配置方式：发布方在 `scripts/reporting_config.py` 设置 `DEFAULT_REPORT_ENDPOINT` 和 `DEFAULT_FEEDBACK_ENDPOINT`，用户可用本机配置文件覆盖。两个发布默认值当前均为空，后续需要时由发布方填写实际地址并重新发布或刷新插件；用户无需修改安装缓存中的脚本。

| 用途 | 用户配置文件 | 发布脚本中的默认值 |
| --- | --- | --- |
| 异步统计 | `~/.helixharness/telemetry/config.json` | `DEFAULT_REPORT_ENDPOINT` |
| feedback | `~/.helixharness/feedback/config.json` | `DEFAULT_FEEDBACK_ENDPOINT` |

创建父目录并在文件中设置 `endpoint`；已有文件合并修改。例如统计配置：

```json
{
  "endpoint": "http://127.0.0.1:7860/report"
}
```

feedback 使用相同结构，地址为实际平台的 `/feedback/report`。**用户文件非空地址优先于发布默认值**；缺少文件、字段或值为空时使用默认值。损坏文件、错误字段类型或非空无效地址不回退，防止误投到其他接收方。两条通路独立配置，删除覆盖文件或清空 `endpoint` 可恢复默认值。

地址环境变量 `HELIXHARNESS_TELEMETRY_ENDPOINT` / `HELIXHARNESS_FEEDBACK_ENDPOINT` 不再生效，请迁移至配置文件。`HELIXHARNESS_TELEMETRY_HOME` / `HELIXHARNESS_FEEDBACK_HOME` 仍可改变各自私有目录。地址必须为 HTTP(S)，不能包含凭据、查询参数或 fragment；配置不代表授权，也不写入业务账本。完整发布脚本与配置文件示例见[中文使用指南](../docs/user-guide.zh-CN.md#2-配置异步上报与-feedback-地址)。

异步流程：阶段结束 → Runtime 采集授权窗口 → 脱敏 → 私有 Outbox → `/report` → 服务端确认 → 关联账本。网络或临时错误退避重试，永久拒绝进入隔离；仅匹配的 `accepted` / `duplicate` 表示接收成功。未配置或上报失败不阻塞研发。详见[异步统计协议](reference/eval-supplement-protocol.md)。


前端保留设计解析、还原、预览和视觉验收能力。可用用户提供的设计素材；设计服务认证由用户配置。

## 验证

```bash
python3 scripts/run_tests.py
python3 scripts/host_runtime_cli.py list
bun test opencode/tests
bun run typecheck
```

此发行版仅维护当前 HelixHarness 合同，不迁移其他工作流历史。循环不包含发布或部署。

## 问题反馈

[feedback 辅助 Skill](skills/feedback/SKILL.md) 可提交 HelixHarness 自身的使用问题，独立于研发命令和统计授权。先启动 helixharness-platform，并按上方说明配置 `/feedback/report`；仅配置统计地址不会启用 feedback，反馈地址缺失时停止准备并提示配置方法。

在编码助手中要求使用 HelixHarness feedback Skill：准备脱敏草稿 → 展示完整内容和实际地址 → 用户确认 → 立即发送 → 服务端确认。准备阶段不联网，草稿只保存在本机私有目录；可选附带脱敏后的当前会话摘录。确认绑定内容与地址，修改后重新审核。

只有匹配 feedback ID 的 `accepted` / `duplicate` 才算送达，可在平台 `/feedback` 查看。连接失败或临时错误显示“待发送”，私有队列最长保留 30 天；下次向同地址发送或显式重试时补投，不使用统计定时 Worker，不因配置变化改投其他地址。永久拒绝保留草稿供检查。

反馈不会写入业务仓库或自动开启统计；统计关闭时仍可单独确认发送 feedback。平台存储路径由 `HELIXHARNESS_PLATFORM_FEEDBACK_DB_PATH` 配置。详见[反馈协议](reference/feedback-protocol.md)。

## 开源与贡献

项目使用 [Apache-2.0](LICENSE)。单仓发布的根目录提供市场入口；独立插件副本仍支持本目录安装。详见[安装指南](../docs/installation.md)与[贡献指南](../CONTRIBUTING.md)。[安全政策](../SECURITY.md)和[社区行为准则](../CODE_OF_CONDUCT.md)在仓库根目录统一维护。普通社区问题在公开发布后通过 [GitHub Issues](https://github.com/didi/helixharness/issues) 提交；未配置平台地址时，反馈 skill 仅保留入口，不能发送。
