---
protocol: codex-subagent-runtime
version: 2
platform: codex
---

# Codex 子代理适配协议

```text
apply_shared_write_mode: parent_finalize
ut_experience_write_mode: parent_finalize
result_encoding: helixharness_structured_text
```

Codex child 不直接更新共享 tasks/checkpoint、workspace 决策队列或 UT experience；父流程根据通过
终态、写域和产物验证的结构化 final 串行、原子落盘。

本资源只把 [公共子代理协议](subagent-runtime-protocol.md)（`reference/subagent-runtime-protocol.md`）
的 `spawn_job`、`wait_job` 和终态映射到
Codex。业务任务、依赖、写入范围、产物、验证、失败和完成条件全部来自公共协议、当前命令的
Dispatch Contract 与 agent prompt；本资源不得新增或覆盖业务语义，Claude Code 不得读取。

## 1. 工具与 schema

- `spawn_job` 映射为当前运行时直接暴露的 `spawn_agent`。已经直接暴露时直接使用；只有尚未暴露
  的能力才通过宿主提供的工具发现入口查找。
- 调用参数严格服从当前 `spawn_agent` schema，不绑定工具命名空间，也不传入 schema 未声明的
  类型、继承、队列、容量、身份或关闭字段。
- `task_name` 是 invocation 内的可读标签；需要唯一时使用 `<job_id>_<invocation-scoped suffix>`。
  runtime handle 只用于本轮消息和等待，不写入业务工件，也不跨会话恢复。
- schema 暴露 `fork_turns` 时默认显式使用 `fork_turns="none"`。只有 Dispatch Contract 明确需要最小
  对话片段时才传正整数；不得使用 `fork_turns="all"`。schema 未暴露时不得强传。

## 2. Spawn message

父流程按公共协议组装 message，至少传递当前 job 的 Dispatch Contract、工作目录、任务专属输入和
`prompt_source`。文件型 prompt 不复制全文，但必须要求子代理在首个业务动作前从共享 workspace
完整读取。不要把稳定公共协议、父任务长对话或其他 job 的结果全文重复注入。

父流程还必须把公共协议解析后的 `delegation_policy` 与 `interaction_mode` 原样写入 message。未声明时
显式写入 `delegation_policy=leaf`、`interaction_mode=afk`：leaf child 不得继续派生子代理；afk child
遇到需要用户选择、授权或补充事实时，返回具体缺口并让交互返回父流程，不得直接提问或代替用户
确认。只有 Dispatch Contract 明确声明 `may_delegate`/`hitl` 时才能放宽对应边界，且仍受当前 Codex
system、developer、sandbox 与公共 write/read scope 约束。

### 2.1 Resolved context packet

父流程在派发每个 ready job 前只解析一次任务上下文，并把结果放入 spawn message 的
`HELIXHARNESS_CODEX_CONTEXT_PACKET`。同一 batch 内共享的公共事实只放一份；packet 至少包含：

```text
version: 1
job_ids: [{job_id}]
prompt_source: {repo_relative_path#section}
source_sha256: {prompt/spec/tasks/verify-report source digest map}
resolved_read_paths: [{repo_relative_path}]
required_excerpts: [{source, heading_or_lines, verbatim_text}]
dependency_evidence_refs: [{ER-*}]
runtime_fingerprint: {value|not_applicable|unavailable}
```

packet 只是当前公共 command/spec 事实的有界投影，不是第二业务事实源，不写入仓库，也不得新增
任务、验收或门禁。父流程仍保留来源路径和 `source_sha256`；子代理首个业务动作先完整读取本 Codex
运行时协议，然后直接消费 packet。packet 已覆盖的 spec/tasks/verify-report 章节和
项目结构不得重新执行全仓 discovery，也不得重新读取整份父任务长对话；目标源码、按需
harness/knowledge 和缺失事实仍只能在 `resolved_read_paths`/公共 `read_scope` 内批量读取。

来源 hash 不一致、excerpt 缺少完成当前任务所需事实或路径已经变化时，packet 立即失效；子代理必须
把具体缺口返回父流程，由父流程在原 `read_scope` 内重新解析并生成一次新 packet。不得由子代理扩大
扫描范围、猜测旧摘要或把 packet 缺失当作降低验收的理由。

Codex system、developer、sandbox 和审批约束始终优先。宿主差异只作以下工具解释：

- prompt 中的 `Read`、`Write`、`Edit` 表示当前 Codex 实际提供的文件工具；编辑优先使用
  `apply_patch`，不得用 heredoc、重定向或其他方式绕过 sandbox。
- prompt 把 `CLAUDE.md` 用作宿主项目入口时映射到同级 `AGENTS.md`；明确列为普通业务输入时仍可
  只读消费，不做路径替换。
- 项目 settings 的写入者读取当前 Codex runtime/project 资源；`.codex/hooks.json` 保持
  parent-owned，不把该限制扩散到 Claude Code 或公共协议。

## 3. Capacity、等待与终态

- 不假定固定并发上限，也不自行计算 slot。宿主明确提供 queue 时才提交到 queue。
- 没有 queue 时逐个派发 ready job；已有本轮运行 job 后遇到容量暂满，保留其他 job 为 pending，
  等待任一真实终态事件后再派发。
- 首次派发即容量不足且没有本轮运行 job 时，只使用当前 schema 明确提供的容量/全局终态事件；
  没有安全等待能力时返回 `BLOCKED: codex_subagent_capacity_unavailable`。容量暂满不等于能力不可用。
- `wait_job` 映射到当前 Codex 暴露的 mailbox 或终态等待能力。不得用文件出现、固定轮询次数、
  静默计数或 HelixHarness 自定义总超时替代原生终态。
- job 是否成功完全按公共协议判断：Codex 原生终态、final 映射和父流程产物验证必须同时通过。

### 3.1 只读执行优化

- `batch_read`：同一 job 中彼此独立、已经落在 Dispatch Contract `read_scope` 内的文件清单、符号
  搜索和证据查询，可合并为一次 `rg --files`、多 pattern `rg` 或等价批量只读调用；必须使用
  bounded output，不能扫描仓库外路径、读取敏感文件或把失败吞成空结果。
- `parent_prefetch`：子代理运行期间，父流程只可预取下一 ready job 已声明的 prompt、公共协议与
  read_scope 内只读输入，并提前准备上述 context packet；不得写入任何业务或共享工件，不得提前执行
  验证/决策，也不得绕过 read_scope 或 depends_on。前序 job 的输出仍须等真实终态与父流程验证后
  才能注入后继 job。
- 两项优化只减少工具往返，不改变 Agent 数量、依赖、证据、用户确认、failure propagation 或
  完成条件；无法安全合并时保持公共命令的原始读取顺序。

## 4. Failure、fallback 与生命周期

- 原生失败、取消、等待中或容量暂满沿用公共协议，不在本适配器中自动 retry 或内联。
- 只有确认当前运行时没有暴露且无法发现 `spawn_agent`，并且 Dispatch Contract 允许时，才执行
  `unavailable_fallback`，同时记录 `WARN:codex_subagent_unavailable`。
- 不因静默或耗时自动调用 `interrupt_agent`；只有用户取消或明确发现 ownership/sandbox 越界时，
  才按当前宿主能力请求中断。
- 不查找、调用或模拟 `close_agent`。完成后的 agent 回收由 Codex 托管，不能成为业务完成门禁。
- 跨会话只恢复公共协议允许的逻辑 job 状态、输入摘要和产物路径，不恢复 runtime identity。

## 5. 结果映射

Codex 子代理完整读取 [结构化结果编码](../hosts/shared/subagent-result-encoding.md)，把当前命令要求的
业务事实编码到 final；父流程将真实终态和 final 映射为公共协议的通用结果，再执行 command-local
结构、写域和产物校验。本资源不重复定义各命令的业务门禁。

当前命令显式声明 `result_reencode_budget=1` 且满足共享编码的前置条件时，父流程把唯一一次纠正
映射为当前运行时真实提供的同一 Agent `format-only follow-up`；必须复用原 runtime handle，不得派生新 Agent、
重新 spawn、内联执行业务或增加逻辑 attempt。follow-up 只携带原始 final、精确校验错误和
canonical 模板，并明确禁止工具与文件访问。同一 Agent follow-up 能力不可用时返回父流程自主判断；
handle 已不可用或第二个候选仍无效时也按同一方式处理，不得转成 capacity blocked、自动 retry 或
新的公共强阻断。Claude Code 的 `native_agent_result` 不读取也不执行本映射。
