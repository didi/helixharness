# HelixHarness 工作流架构

HelixHarness 是独立的 Lite 工作流插件。运行时只读取当前安装的插件和用户当前项目，
不依赖其他 HelixHarness checkout、历史版本或内部平台。

## 公共入口与唯一正文

九个公共命令是 `init`、`propose`、`apply`、`archive`、`ut`、`bug-fix`、`update`、`status`、`config`。
`commands/` 保存唯一公共入口；Propose、Apply、Archive 和 Bug Fix 通过 Router 加载当前正文。

| 范围 | 正文 | 职责 |
| --- | --- | --- |
| Single | [`workflows/single/`](../helixharness/workflows/single/) | 单仓需求、实现、评审、归档和修订 |
| Workspace | [`workflows/workspace/`](../helixharness/workflows/workspace/) | 多仓合同、依赖编排与根 Spec 归档 |
| Init / UT / Update | [`commands/`](../helixharness/commands/) | 初始化、单测维护和当前安装源更新 |

`propose --loop` 串联 Propose → Apply → Archive；两种执行范围共享相同业务合同。
Workspace 子任务从公共入口进入 Single 正文，根目录不写子仓业务工件或项目 linters/hooks。

`status` 只读检查已有需求工件并给出进度、问题和建议命令；`config` 只查看或修改用户配置。两者直接使用各自命令合同，不进入阶段 Router、不产生研发 Stage。

## 宿主边界

[`Host API`](../helixharness/reference/host-api.md) 定义 manifest 和适配器合同。
Claude Code 使用原生命令，Codex 与 Cursor 使用各自的 Skill 投影；OpenCode 由插件注册原生命令、方法 Skill 和 Agent，绑定当前仓库与会话的 Host Context；宿主机制由 `hosts/{host-id}/` 提供。
Claude Code、Codex 与 Cursor 的原生插件清单显式选择各自的 hook 配置；OpenCode 不发布 hook manifest。宿主入口负责固定身份和事件转换，
共享生命周期逻辑不得根据平台名称、环境变量或缓存目录猜测宿主。

宿主 Init 入口加载 `project_config`、`project_hooks`；UT 入口加载 `ut`。
支持项目 hooks 的宿主，单仓 Init 必须实际生成并校验当前 Host Kit 的项目 hooks，重复初始化保留用户配置并去重。
业务阶段显式记录 start/finish；会话 Stop 不是业务完成证据。

**OpenCode Stable 支持（macOS、2.3.2）：** 当前源码候选的支持范围与边界见[支持矩阵](opencode-testing.md)。OpenCode 2.2.2、2.3.0 仅有自动化兼容覆盖。

插件注册原生 `helix:<command>` 命令、`helix-<skill>` 方法 Skill 和 `helix-<agent>` 角色。命令、原生 `skill` 工具结果、Skill mention 与原生 Skill 激活均按请求会话绑定仓库目录；会话查询或目录校验失败时阻断未绑定的 Skill 正文。2026-11-20 已在 OpenCode 2.3.2 + DeepSeek 上完成一次合成单仓 Init → Propose → Apply → Archive 真实模型闭环，包含操作者确认和一次受控合同修订；2026-11-23 又完成了 OpenCode 2.3.2 + DeepSeek 的操作者辅助合成双仓 Workspace 与独立 UT 验证；后续已修复 UT 分类校验与原子发布保护，并通过无纠正提醒的真实模型复验。[合成 Workspace 的专项真实模型验证](../helixharness/opencode/tests/README.md#recovery-and-auxiliary-workflow-acceptance-2026-11-23)还覆盖了 Apply 与部分 Archive 原生取消后新会话恢复，以及注入原生 context hook 故障后阻断依赖派发。完整生产业务项目、无人干预时的协议遵守与完整 TUI/桌面 UI 验收仍未完成；已覆盖范围见[定向终端 UI 验收](../helixharness/opencode/tests/README.md#targeted-terminal-ui-acceptance-2026-11-23)。Workspace Apply 的 `may_delegate` RepoJob 使用 `helix-worker`，需要 `experimental.subagent_depth >= 2`；独立 reviewer 保持只读 leaf。深度不足时阻塞该 RepoJob，不得绕过评审。

OpenCode 适配使用原生工具和会话 Hook，但未提供等价的 SessionStart/Stop/失败/会话结束业务保证。任务跟踪通过 `scripts/task_plan.py` 原子更新同一份 `tasks.md`。统计在明确授权后记录阶段事实和工作流轨迹，不采集原始会话，token 和未观察到的工具次数保持未知。`/helix:update` 对有明确 Git upstream 的干净源码安装执行既有确认协议，锁定 commit 并仅快进；有本地修改、游离版本或包管理安装时不自动更新。Init 合并 `AGENTS.md`，不创建 `.opencode/` settings；插件在原生 write/edit/patch 成功后执行已有项目巡检，shell 修改仍需显式检查。d-d2c 的 Claude skill-scoped Stop/PreToolUse guard 不执行，采用无 hook / `--bare` 语义，仍须遵守用户决策、视觉验收和完成门禁。

## 独立性与数据边界

- 只支持当前 Host API 和当前账本、轨迹 schema；不迁移旧版本数据，不接受已退役的工作流正文路径。
- 不支持的数据返回 unavailable 或明确阻断，原文件保持不变。
- 普通 Git `main` / `master` 分支识别属于用户仓库支持，不依赖历史插件主分支。
- 新宿主通过独立 Host Kit、分发配置、专项测试和真实客户端验收接入，不在共享核心增加平台分支。
- 安装可选的平台服务不是命令运行前提；关闭统计时研发流程可独立执行。

## 验证

统一回归入口为 `python3 scripts/run_tests.py`，另执行 Host Kit、Codex 与 Cursor 投影校验。
Cursor 采用 Host API 1.2 的扁平项目 hooks 策略；1.1 的嵌套策略保持兼容。Cursor 桌面宿主按已声明的能力边界正式支持，发布范围和验证边界详见 [Cursor 验证说明](cursor-testing.md)。
原生客户端验收分别核查插件装载、启动身份、真实子任务、测试和覆盖率、
完整工件、归档状态与清理；单测通过、命令退出成功或模型回复完成均不能单独证明闭环通过。
