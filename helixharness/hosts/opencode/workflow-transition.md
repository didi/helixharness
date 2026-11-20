# OpenCode 工作流跳转

模式：`inline`。Propose `--loop` 按 Router 返回值，在当前主会话中依次执行
`workflows/single/` 或 `workflows/workspace/` 下的唯一阶段正文。已路由的内部阶段复用保留
feature、execution_scope 与 trigger，跳过重复入口路由，不递归调用 `session.command` 或其他
`/helix:*` 命令。

Apply 的双轴评审完整执行 `skills/code-review/SKILL.md`，通过原生 `skill` 工具加载
`helix-code-review`，按当前子代理协议派发 Standards 与 Spec 两个独立只读角色。评审属于同一个
Apply Stage，不创建独立 Review 阶段，也不提前 finish coding entry。

当前阶段的 start、trace-event、finish 均以所选工作流及 `resources.telemetry_protocol` 为准。
OpenCode 的 `telemetry=unsupported`，统计脚本返回 `unsupported_host` 时跳过统计并继续业务；
不得因此跳过评审、验证或完成门禁。
