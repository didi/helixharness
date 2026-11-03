# Qoder 工作流跳转适配

模式：`inline`。命令面由 Skill 承载，无法可靠地在一次会话内递归调用另一个公开命令入口，
因此 Propose `--loop` 按 Router 返回值，在当前主 Agent 中依次执行
`workflows/single/` 或 `workflows/workspace/` 下的唯一阶段正文。

- 已路由的内部阶段复用保留 `feature`、`execution_scope` 与 `trigger`，跳过重复入口路由。
- 不把 slash-command 字符串当 shell 命令执行，也不伪造嵌套命令调用。
- Apply 的双轴评审完整执行 `skills/code-review/SKILL.md`，按 `resources.subagent_protocol`
  派发 Standards 与 Spec 两个独立只读角色；评审属于同一个 Apply Stage，不创建独立 Review 阶段。
- 当前阶段的 start、trace-event、finish 以所选工作流与 `resources.telemetry_protocol` 为准；
  统计关闭时业务流程仍执行。
