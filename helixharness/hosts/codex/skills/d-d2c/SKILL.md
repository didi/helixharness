---
name: d-d2c
description: "用户提到 d-d2c、设计稿还原、MasterGo、贴图比对、design-to-code、页面开发，或使用 d-d2c --create-page / --update-page / --component 时，按这个 skill 要求执行；支持新建整页、更新已有页、组件/弹层/卡片三种流程入口，后两者分别读取 rules/d-d2c-update-page.md / rules/d-d2c-component.md 作为流程覆盖层"
---

# Codex method projection: d-d2c

插件根固定为当前文件的 `../../../..`。先完整读取 `hosts/codex/host.json`、`reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 manifest 绑定的 `resources.runtime`。canonical 正文首次需要 Agent/Task 时，再完整读取 `reference/subagent-runtime-protocol.md` 与 `resources.subagent_protocol`，按 Codex 适配器注入 leaf/afk 边界。然后完整读取并执行唯一 canonical 方法 Skill `skills/d-d2c/SKILL.md`；不得复制、改写或补充其业务语义，也不得扫描 plugin cache。canonical frontmatter 不是 Codex 运行时元数据；Codex 只消费其正文，宿主能力按 Host Context 映射。
