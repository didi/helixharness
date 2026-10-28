---
name: code-review
description: "为 HelixHarness Apply 评审固定比较点之后的代码变化；并行执行 Standards 与 Spec 两个独立评审轴，分轴报告结果，并按 HelixHarness 合同完成修复和复审。"
---

# Codex method projection: code-review

插件根固定为当前文件的 `../../../..`。先完整读取 `hosts/codex/host.json`、`reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 manifest 绑定的 `resources.runtime`。canonical 正文首次需要 Agent/Task 时，再完整读取 `reference/subagent-runtime-protocol.md` 与 `resources.subagent_protocol`，按 Codex 适配器注入 leaf/afk 边界。然后完整读取并执行唯一 canonical 方法 Skill `skills/code-review/SKILL.md`；不得复制、改写或补充其业务语义，也不得扫描 plugin cache。canonical frontmatter 不是 Codex 运行时元数据；Codex 只消费其正文，宿主能力按 Host Context 映射。
