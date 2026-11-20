# Cursor 子代理适配

遵守 `reference/subagent-runtime-protocol.md` 与当前命令 Dispatch Contract。Cursor 使用本轮实际
暴露的 Task/子 agent 工具，不复制其他宿主的参数。角色 prompt 来自当前插件的 `prompt_source`，
不依赖自动导入的同名全局角色。官方支持独立上下文和前台/后台任务，不构成当前安装的验收证据。

```text
apply_shared_write_mode: parent_finalize
ut_experience_write_mode: parent_finalize
result_encoding: native_agent_result
```

- 显式传递 `job_id`、prompt、依赖、`ownership_root`、read/write/forbidden scope、预期产物与验证。
  子 agent 默认不继承会话；必须提供完整必要上下文，不能让它搜索父会话或猜测资源来源。
- 每次派发保留当前命令 Dispatch Contract 解析后的 `delegation_policy`；未声明时使用
  `delegation_policy=leaf`，此时子 agent 不得继续递归。命令明确声明
  `delegation_policy=may_delegate` 时保留该授权，例如 Workspace Apply 的每仓 job 必须执行
  本仓 Single Apply 并派发两个独立 Review 轴，不能被宿主适配器一律降为 leaf。
  交互映射为 `interaction_mode=afk`，待决策事实回传父流程，子 agent 不直接提问或扩大权限。
  共享文件由父流程验证后串行写入。
- `may_delegate` 只允许当前命令明确要求的子任务，不是任意递归授权。子任务的 `read_scope`
  必须是该 job 已解析范围的子集；reviewer 的 `write_scope=none`，reviewer 自身使用
  `delegation_policy=leaf`。依赖仓库的只读权限不能转成写入权限，也不能读取未授权的兄弟仓库。
  父流程发现越界读取时拒绝成功结果并返回 `BLOCKED: agent_read_scope_violation`；越界写入同样
  拒绝结果并按当前命令的 blocked 语义收口。
- 通用角色按当前 Cursor schema 选择 general-purpose 等价能力；不硬编码 Claude 类型名。
  原生只读开关可用时为 reviewer 开启，并始终声明 `write_scope=none`。
- 保留 command-local job 数量和两个独立 Review 轴。只有读任务或互斥写域可以并行；没有安全的
  并行能力时遵守命令的 fallback，不把多角色合并成一份自评。
- 前台任务返回后立即验证真实终态和 result；后台任务使用实际提供的完成事件/结果读取工具。
  不用文件出现、静默时长或普通文字替代终态，不重复派发仍运行的 job。
- 父流程从原生 result 映射业务身份、状态、written paths、validation 与错误，并复核 diff 和产物。
  文本自报不能覆盖失败/取消；未知终态保持未完成。运行 handle 仅保存在 invocation 内存。
- 原生失败或取消停止新的任务准入并按 `BLOCKED: agent_failed` 安全收口。容量暂满不触发内联重试；
  只有明确能力不可用且 command-local contract 允许时，才按同一 prompt 内联并执行相同验证。
  `may_delegate` 也不证明当前 Cursor 模式实际提供嵌套 Task 能力。必要 reviewer 无法派发且没有
  命令允许的等价降级时返回 `BLOCKED: cursor_subagent_unavailable`；不能把两个独立 Review 轴合并
  为子流程自评、伪造完成结果，或仅凭已有报告跳过真实终态和验证。

参考：[Cursor Subagents](https://cursor.com/docs/subagents)。
