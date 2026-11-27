---
name: helix-ut
description: "单元测试生成与维护命令。基于知识库和活跃需求，逐层扫描代码并生成/维护单测，运行验证并检查逻辑缺陷，确保覆盖率达标。"
disable-model-invocation: true
---

# Cursor command projection: ut

入口类型：`command`。插件根固定为当前文件的 `../../../..`。完整读取 `hosts/cursor/skill-entry.md`，执行“公共绑定”后仅执行“命令入口”分支；完成 runtime preflight，以 `--platform cursor` 显式解析同一安装的 Host Context。

当前逻辑命令为 `ut`，原生 Skill 名为 `helix-ut`。按入口协议绑定本次用户参数为 `$ARGUMENTS`，然后完整读取并执行唯一公共契约 `commands/ut.md`；不复制业务步骤，不重新发现插件，不把建议命令当成自动授权。
