---
name: helix-apply
description: "执行任务、验证并完成双轴代码评审。"
disable-model-invocation: true
---

# Cursor command projection: apply

入口类型：`command`。插件根固定为当前文件的 `../../../..`。完整读取 `hosts/cursor/skill-entry.md`，执行“公共绑定”后仅执行“命令入口”分支；完成 runtime preflight，以 `--platform cursor` 显式解析同一安装的 Host Context。

当前逻辑命令为 `apply`，原生 Skill 名为 `helix-apply`。按入口协议绑定本次用户参数为 `$ARGUMENTS`，然后完整读取并执行唯一公共契约 `commands/apply.md`；不复制业务步骤，不重新发现插件，不把建议命令当成自动授权。
