# 安装与更新

完整步骤见[安装指南](../../../docs/installation.md)。当前单仓源码使用仓库根目录的绝对路径；独立插件副本可使用插件目录。公开发布后也可使用 `https://github.com/didi/helixharness.git`，记为 `HELIXHARNESS_SOURCE`。

```bash
claude plugin marketplace add "$HELIXHARNESS_SOURCE"
claude plugin install helix@helixharness-marketplace
```

安装后新开会话确认 9 个公开命令可见：`init`、`propose`、`apply`、`archive`、`bug-fix`、`ut`、`update`、`status`、`config`。更新使用同一已配置 marketplace，`/helix:update` 调用宿主更新适配器。

## 更新与卸载

更新使用已配置的 marketplace 与 `/helix:update`。卸载通过当前宿主的插件管理完成，先核对安装来源和目标；插件卸载不删除业务仓库中的 `docs/harness/`、`docs/knowledge/`、`docs/specs/` 或业务代码。

安装、更新或卸载后，按宿主要求重新加载会话，并核对公开命令是否符合预期。

## Hook 装载验收

插件清单显式加载 `hosts/claude-code/hooks.json`。安装后按客户端原生流程审阅并信任该插件的 hook 定义，
再开启新会话，确认 SessionStart 注入 `claude-code` 与当前安装路径。仅命令可见不能证明 hook 已执行。
更新后 hook 内容变化时需按客户端要求重新审阅，不复用旧定义的信任摘要。
项目 hooks 在单仓 Init 后单独检查；重复 Init 应保留用户 settings 且不增加重复命令。
