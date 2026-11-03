# 安装与更新

完整步骤见[安装指南](../../../docs/installation.md)。当前单仓源码使用仓库根目录的绝对路径；
独立插件副本可使用插件目录。记为 `HELIXHARNESS_SOURCE`。

Qoder 的插件市场与命令语法与 Claude Code 同构，但 CLI 名称不同。中文版 CLI 为 `qoderclicn`，
国际版为 `qodercli`；下文以中文版为例。

```bash
qoderclicn plugins marketplace add "$HELIXHARNESS_SOURCE"
qoderclicn plugins install helix@helixharness-marketplace
```

本地源码调试可直接用插件目录加载：

```bash
qoderclicn -p --plugin-dir "$HELIXHARNESS_SOURCE/helixharness" "<prompt>"
```

安装后新开会话确认 9 个公开命令可见：`init`、`propose`、`apply`、`archive`、`bug-fix`、
`ut`、`update`、`status`、`config`。限定名形如 `/helix:propose`，会话内也可能接受平铺简写。

## 更新与卸载

更新使用已配置 marketplace 与 `/helix:update`；卸载通过 `qoderclicn plugins uninstall` 完成。
插件卸载不删除业务仓库中的 `docs/harness/`、`docs/knowledge/`、`docs/specs/` 或业务代码。

## Hook 装载验收

插件清单显式加载 `hosts/qoder/hooks.json`。安装后按客户端流程审阅并信任该插件的 hook 定义，
再开启新会话，确认 `SessionStart` 注入 `qoder` 与当前安装路径。仅命令可见不能证明 hook 已执行。

## 已知限制

- 项目级 hooks 目标路径 `.qoder/settings.json` 尚未经真实客户端 E2E 证实。
- 统计在 Qoder 下不可用：既不写 `docs/eval/**/eval-log.json`，也不做异步上报，
  因此平台看不到 Qoder 的用量数据。业务与质量门禁不受影响。
