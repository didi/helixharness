---
name: feedback
description: "HelixHarness 使用问题反馈通道。当用户在 /helix:* 命令与工作流中遇到问题并表现出困扰时主动使用：命令反复失败或报错无法推进、生成的文档/字段/结构与用户预期不符、流程在某一步卡住不动、用户对 HelixHarness 表达不满困惑或抱怨、放弃工作流改用手工方式，或明确要求提交反馈。此时向用户建议提交反馈，发送前必须经用户确认。不触发：用户业务代码的问题、与 HelixHarness 无关的报错、一般使用咨询、模型已自行修复的故障。"
---

# Codex method projection: feedback

插件根固定为当前文件的 `../../../..`。先完整读取 `hosts/codex/host.json`、`reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 manifest 绑定的 `resources.runtime`。canonical 正文首次需要 Agent/Task 时，再完整读取 `reference/subagent-runtime-protocol.md` 与 `resources.subagent_protocol`，按 Codex 适配器注入 leaf/afk 边界。然后完整读取并执行唯一 canonical 方法 Skill `skills/feedback/SKILL.md`；不得复制、改写或补充其业务语义，也不得扫描 plugin cache。canonical frontmatter 不是 Codex 运行时元数据；Codex 只消费其正文，宿主能力按 Host Context 映射。
