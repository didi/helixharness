# Cursor 工作流跳转适配

模式：`inline`。Propose `--loop` 在当前主 agent 内按 Router 返回值依次执行唯一
`workflows/single/*.md` 或 `workflows/workspace/*.md`。保留 feature、execution_scope、trigger
和 workflow_arguments；不依赖原生 slash namespace，不嵌套调用另一入口 Skill。

Apply 的双轴评审完整执行 `skills/code-review/SKILL.md`，按当前子代理协议派发 Standards 与 Spec
两个独立只读角色。Review 属于 Apply，不创建另一个阶段。TDD、修复、复审与归档证据保持完整。
统计关闭、缺少会话身份或采集失败仅影响可选统计；阶段工件、依赖和实际测试仍决定完成与续跑。
blocked/失败按公共合同停止，不把 `stop` 或 `sessionEnd` 当作业务完成，也不自动重试失败 job。
