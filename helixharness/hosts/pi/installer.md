# 安装与更新

完整步骤见[安装指南](../../../docs/installation.md)。pi 包根是仓库根目录，`package.json` 通过
`pi` 清单装载 `helixharness/hosts/pi/` 的扩展与 Skills。

```bash
pi install git:github.com/didi/helixharness
```

安装后新开会话确认 9 个公开入口可见：`/helix-init`、`/helix-propose`、`/helix-apply`、
`/helix-archive`、`/helix-bug-fix`、`/helix-ut`、`/helix-update`、`/helix-status`、`/helix-config`，
以及对应的 `/skill:helix-{name}`。更新使用 `/helix-update` 调用宿主更新适配器，或直接使用
`pi update git:github.com/didi/helixharness`。

## 更新与卸载

更新使用已配置的包来源与 `/helix-update`。卸载通过 `pi remove git:github.com/didi/helixharness`
完成，先核对安装来源和目标；插件卸载不删除业务仓库中的 `docs/harness/`、`docs/knowledge/`、
`docs/specs/` 或业务代码。

安装、更新或卸载后按 pi 要求 `/reload` 或重开会话，并核对公开入口是否符合预期。

## 生命周期装载验收

pi 包清单显式装载 `hosts/pi/extension.ts`。安装后新开会话，确认 SessionStart 注入的 Host Context
里 `canonical_host_id` 为 `pi`、`plugin_root` 指向当前安装目录。仅命令可见不能证明扩展已执行。
项目级 hooks 在 pi 上不支持，重复 Init 不得新增或改写任何 pi 设置文件。
