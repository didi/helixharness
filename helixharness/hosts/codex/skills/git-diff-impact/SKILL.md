---
name: git-diff-impact
description: "分析指定 Git 改动的调用链、业务影响和兼容性风险，生成影响报告。用户主动要求影响分析时单独使用，无需 HelixHarness feature。"
---

# Codex method projection: git-diff-impact

插件根固定为当前文件的 `../../../..`。先完整读取 `hosts/codex/host.json`、`reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 manifest 绑定的 `resources.runtime`。canonical 正文首次需要 Agent/Task 时，再完整读取 `reference/subagent-runtime-protocol.md` 与 `resources.subagent_protocol`，按 Codex 适配器注入 leaf/afk 边界。然后完整读取并执行唯一 canonical 方法 Skill `skills/git-diff-impact/SKILL.md`；不得复制、改写或补充其业务语义，也不得扫描 plugin cache。canonical frontmatter 不是 Codex 运行时元数据；Codex 只消费其正文，宿主能力按 Host Context 映射。
