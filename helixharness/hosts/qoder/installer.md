# 安装与更新

完整步骤见[安装指南](../../../docs/installation.md)。当前单仓源码使用仓库根目录的绝对路径；
独立插件副本可使用插件目录。记为 `HELIXHARNESS_SOURCE`。

Qoder 的插件市场与命令语法与 Claude Code 同构，但 CLI 名称不同。中文版 CLI 为 `qoderclicn`，
国际版为 `qodercli`；下文以中文版为例。本文只覆盖已验证的 CLI 路径：Qoder IDE 内的插件市场
界面可以完成同样的注册与安装，但本 Host Kit 尚未对其做 E2E。

## 从市场安装

```bash
qoderclicn plugins marketplace add "$HELIXHARNESS_SOURCE"
qoderclicn plugins install helix@helixharness-marketplace
qoderclicn plugins list
```

`"$HELIXHARNESS_SOURCE"` 可以是本地目录、Git URL（HTTPS 或 SSH，如
`git@github.com:owner/repo.git`）或 `owner/repo`。用 `plugins install --scope user|project|local`
选择声明范围，默认 `user`。

**Git 源只克隆默认分支。** Qoder 不对 `marketplace add` 提供 `--branch`/`--ref`。因此 fork 或
特性分支在安装前必须让**默认分支**包含根 `.qoder-plugin/marketplace.json` 与
`helixharness/.qoder-plugin/`；否则安装会读到不含 Qoder 清单的分支并失败。可先切换仓库默认
分支，或把改动合并进默认分支。

安装或更新后执行 `/plugins reload`（或在 IDE 中重新加载窗口），让运行中的会话加载新插件。

## 本地源码调试

不安装、直接从工作副本加载插件目录：

```bash
qoderclicn -p --plugin-dir "$HELIXHARNESS_SOURCE/helixharness" "<prompt>"
```

该方式同样会加载 `hosts/qoder/hooks.json`，适合验证命令面与 hooks 而不改动用户插件状态。

## 装载验收

安装后新开会话确认 9 个公开命令可见：`init`、`propose`、`apply`、`archive`、`bug-fix`、
`ut`、`update`、`status`、`config`。命令由薄 Skill 承载，限定名形如 `/helix:propose`，
会话内也可能接受平铺简写；可用 `qoderclicn skills list` 核对发现结果。

插件清单显式加载 `hosts/qoder/hooks.json`。按客户端流程审阅并信任该插件的 hook 定义后，
确认 `SessionStart` 注入 `qoder` 与当前安装路径，并在业务仓库生成
`.helixharness/host-session/qoder.json`。仅命令可见不能证明 hook 已执行。

单仓 Init 会在业务仓库生成 `.qoder/settings.json` 并合并 PostToolUse 检查；可用
`python3 scripts/project_hooks_cli.py --repo-root <repo> --platform qoder check` 校验。

## 更新与卸载

更新使用已配置 marketplace 与 `/helix:update`，或直接执行
`qoderclicn plugins update helix@helixharness-marketplace --scope user`。
卸载使用 `qoderclicn plugins uninstall helix@helixharness-marketplace`，随后
`qoderclicn plugins marketplace remove helixharness-marketplace` 移除市场。
插件卸载不删除业务仓库中的 `docs/harness/`、`docs/knowledge/`、`docs/specs/` 或业务代码。

## 已知限制

- 命令面为 `emulated`：`commands/*.md` 不被 Qoder 暴露为可调用入口，需经
  `hosts/qoder/skills/` 的薄 Skill 承载。
- 统计为 `emulated`：账本与异步 Supplement 正常，但 Qoder 会话记录不含 token 用量，
  因此 `usage` 为空（不估算），该账本不满足 `automation_eligible`。
- `/helix:ut` 的权限准备与精确回滚机制已验证，但未在真实构建工程上跑通完整流程。
