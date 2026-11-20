# HelixHarness

面向 Codex、Claude Code、Cursor 与 OpenCode（预览）的 HelixHarness 研发工作流。通过项目约束、知识库、Spec、任务和验证证据完成研发。

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

需要 Python 3.10+、Git，以及 Codex、Claude Code、Cursor 或 OpenCode（v2.2.2+）。Codex、Claude Code 可将本目录作为本地 marketplace 添加；Cursor 通过市场界面安装。宿主步骤见 [Codex](hosts/codex/installer.md)、[Claude Code](hosts/claude-code/installer.md)、[Cursor](hosts/cursor/installer.md) 和 [OpenCode](hosts/opencode/installer.md) 安装说明。OpenCode 将本插件目录加入 `plugins` 数组。

**OpenCode 预览支持（v2.2.2+）：** 插件注册原生 `helix:<command>` 命令、`helix-<skill>` 方法 Skill 和 `helix-<agent>` 角色，将 Host Context 绑定到当前仓库和会话。完整 Init → Propose → Apply → Archive 流程尚未完成真实模型验收。Workspace Apply 的 `may_delegate` RepoJob 使用 `helix-worker`，需要 `experimental.subagent_depth >= 2`；独立 reviewer 保持只读 leaf。深度不足时阻塞该 RepoJob，不得绕过评审。

OpenCode 没有原生 SessionStart/Stop/失败/会话结束 hooks，也没有原生计划工具（计划写入 `tasks.md`）。统计不支持（`unsupported_host`）；`/helix:update` 返回 `continue_silently`，请通过 OpenCode 插件管理手动更新。项目 hooks 不支持：Init 只合并 `AGENTS.md`，不创建 `.opencode/` settings。d-d2c 的 Claude skill-scoped Stop/PreToolUse guard 不执行，采用无 hook / `--bare` 语义，仍须遵守用户决策、视觉验收和完成门禁。

## 项目结构

`docs/harness/` 保存约束，`docs/knowledge/` 保存当前知识，`docs/specs/active/<name>/` 保存 Spec/Tasks/verify-report，归档后移至 completed。Workspace 根通过 `helixharness.workspace.yaml` 登记子仓，根 Spec 协调各仓完整 HelixHarness 生命周期。

## 可选配置

支持统计的宿主上，统计默认关闭，授权询问独立于异步地址。report 与 feedback 都由发布方在 `scripts/reporting_config.py` 设置默认地址，当前默认留空；用户可分别在 `~/.helixharness/telemetry/config.json` 与 `~/.helixharness/feedback/config.json` 中设置 `endpoint` 覆盖。用户文件优先，缺少或为空时使用发布默认值；无效配置不回退。地址环境变量不再生效。配置示例见 [README](README.md#可选配置)。未配置异步地址时静默跳过，授权后的同步账本仍正常记录；未授权时不新增同步账本，也不异步采集或投递。


前端保留设计解析、还原、预览和视觉验收能力。可用用户提供的设计素材；设计服务认证由用户配置。

## 验证

```bash
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p 'test_*.py'
python3 scripts/host_runtime_cli.py list
bun test opencode/tests
bun run typecheck
```

此发行版仅维护当前 HelixHarness 合同，不迁移其他工作流历史。循环不包含发布或部署。
