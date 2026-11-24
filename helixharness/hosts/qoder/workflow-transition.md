# Qoder 工作流跳转适配

模式：`inline`。本宿主的斜杠命令不暴露为 Agent 可编程入口：宿主 UI 侧的会话提交与命令回注能力
对 Agent 不可执行。薄入口虽然以 `helix:{command}` 的 Skill 形式存在，阶段推进仍不重新进入入口——
`propose --loop` 按 Router 返回值，在当前主 Agent 中依次读取并执行
`workflows/single/` 或 `workflows/workspace/` 下的唯一阶段正文，不尝试触发 `/helix:apply`，也不
调用 `helix:apply` 技能重跑入口路由。

已路由的内部阶段复用保留的 feature、execution_scope 与 trigger，跳过重复入口路由。

Apply 的双轴评审完整执行 `skills/code-review/SKILL.md`，按 `resources.subagent_protocol` 派发
Standards 与 Spec 两个独立只读角色。评审属于同一个 Apply Stage，不创建独立 Review 阶段，也不
提前 finish coding entry。`telemetry=unsupported` 时不产生阶段记账，统计开关不改变业务完成门禁，
阶段终态仍以仓库中的真实证据为准。
