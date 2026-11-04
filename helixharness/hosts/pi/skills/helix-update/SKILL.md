---
name: helix-update
description: "检查并升级 HelixHarness plugin。通过 Host Kit 识别安装版本、展示更新内容，并在用户授权后执行当前宿主的更新适配器。"
---

# HelixHarness update

插件根固定为当前文件的 `../../../..`。先完整读取 `hosts/pi/host.json`、manifest 绑定的 `resources.runtime`、`resources.command_projection`、`reference/host-runtime-protocol.md` 和 `reference/tool-semantics.md`，并按 `reference/runtime-preflight.md` 执行一次
`sh {plugin_root}/scripts/runtime_preflight.sh check`。

然后以唯一 `commands/update.md` 为命令契约，完整读取并按其中显式声明的 adapter 顺序执行。
不得扫描 plugin cache，不得读取其他宿主的入口，也不得为该命令补充本文件之外的业务语义。
