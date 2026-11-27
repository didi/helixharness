---
name: helix-status
description: "只读查看需求阶段、任务进度、阻塞原因和下一步命令。"
disable-model-invocation: true
---

# Cursor command projection: status

入口类型：`command`。插件根固定为当前文件的 `../../../..`。完整读取 `hosts/cursor/skill-entry.md`，执行“公共绑定”后仅执行“命令入口”分支；完成 runtime preflight，以 `--platform cursor` 显式解析同一安装的 Host Context。

当前逻辑命令为 `status`，原生 Skill 名为 `helix-status`。按入口协议绑定本次用户参数为 `$ARGUMENTS`，然后完整读取并执行唯一公共契约 `commands/status.md`；不复制业务步骤，不重新发现插件，不把建议命令当成自动授权。
