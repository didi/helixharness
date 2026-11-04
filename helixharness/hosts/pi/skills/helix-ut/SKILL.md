---
name: helix-ut
description: "单元测试生成与维护命令。基于知识库和活跃需求，逐层扫描代码并生成/维护单测，运行验证并检查逻辑缺陷，确保覆盖率达标。"
---

# HelixHarness ut

插件根固定为当前文件的 `../../../..`。先完整读取 `hosts/pi/host.json`、manifest 绑定的 `resources.runtime`、`resources.command_projection`、`reference/host-runtime-protocol.md` 和 `reference/tool-semantics.md`，并按 `reference/runtime-preflight.md` 执行一次
`sh {plugin_root}/scripts/runtime_preflight.sh check`。

然后以唯一 `commands/ut.md` 为命令契约，完整读取并按其中显式声明的 adapter 顺序执行。
不得扫描 plugin cache，不得读取其他宿主的入口，也不得为该命令补充本文件之外的业务语义。
