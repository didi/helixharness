# HelixHarness 子代理公共协议

本协议定义所有宿主都必须保持的子代理业务不变量。命令负责提供当前命令的 Dispatch
Contract；`resources.subagent_protocol` 只负责把 `spawn_job`、`wait_job`、终态和结果通道映射到
当前宿主。共享命令不得根据平台名称选择子代理语义。

## 1. 派发前置条件

当 `subagents=native|emulated` 且命令需要派发任务时，父流程必须依次读取：

1. 本协议；
2. Host Context 的 `resources.subagent_protocol`；
3. 当前命令的 Dispatch Contract；
4. 当前 job 的 `prompt_source`。

`subagents=unsupported` 时，只能使用 Dispatch Contract 明确允许的 inline fallback。容量不足、
等待中或单个 job 失败不等于能力不可用。

## 2. Dispatch Contract

每个 job 至少声明：

| 字段 | 语义 |
|---|---|
| `job_id` | 当前命令内稳定的逻辑任务标识 |
| `prompt_source` | 子代理必须完整读取的任务说明 |
| `depends_on` | 派发前必须通过的依赖 job |
| `read_scope` | 本 job 可读取的明确路径、符号或已验证输入；动态路径必须在派发前解析 |
| `write_scope` | 本 job 独占的允许写入路径；只读 job 使用 `none` |
| `forbidden_scope` | 禁止写入的共享或越界范围 |
| `expected_artifacts/result` | 必须产生的文件或结构化结果 |
| `validation` | 终态后由父流程执行的验证 |
| `unavailable_fallback` | 宿主确认 spawn 不可用时的唯一降级路径 |
| `delegation_policy` | 可选；默认 `leaf`，子代理不得再派生子代理。只有业务契约明确需要递归分解时才可声明 `may_delegate` |
| `interaction_mode` | 可选；默认 `host_native`，保留当前宿主既有 Agent/Task 交互语义；宿主不支持 child 直接交互时映射为 `afk` 并回传父流程 |

现有 Dispatch Contract 未声明这两个可选字段时，父流程必须在 spawn message 中注入
`delegation_policy=leaf` 与 `interaction_mode=host_native`；这只收紧递归分发边界，不改变 job 数量、
依赖、产物、验收语义或宿主既有提问时点。`leaf` 子代理不得继续派生子代理。Host Kit 只有在 child
无法直接交互时才把 `host_native` 映射为 `afk`；afk child 遇到缺失输入时返回具体缺口，由父流程按
当前命令的 blocked/交互规则处理，不得自行扩大上下文或绕过用户确认。

依赖 job 只有在“宿主原生终态 + final 成功 + 产物/结果验证通过”后才算满足。共享索引、决策、
计划、checkpoint、timing 和最终报告必须保持单写者语义；物理写入者由当前
`resources.subagent_protocol` 的写入模式决定。`child_scoped_write` 只允许 Dispatch Contract 显式
授权的 child 写域，`parent_finalize` 则由 parent 根据已验证 final 串行落盘；并行 job 的写域不得重叠。

## 3. 派发与等待

- 使用 `resources.subagent_protocol` 声明的真实工具和 schema，不虚构 `Agent`、`Task`、
  `spawn_agent`、queue、slot、handle 或 close 能力。
- 每次派发都显式传递已解析的 `delegation_policy` 与 `interaction_mode`。`host_native` 严格保持当前
  宿主基线和 command/prompt 已有交互，不新增问题；`afk` 的待决策事实必须返回父流程后才能继续。
- 宿主提供 queue 时可以提交全部 ready job；没有 queue 时逐个准入，容量暂满的 job 保持
  pending，并等待已经派发 job 的真实终态后再尝试。
- 没有正在运行的本轮 job 且首次派发即容量不足时，只能使用宿主明确提供的容量事件；没有
  安全等待能力时返回宿主协议定义的 capacity blocked。
- 共享写入和有依赖 job 必须按所选写入模式串行提交。只读或写域互斥的 job 才能并行。
- 不使用文件出现、文本自报或固定轮询次数代替宿主原生终态。
- 失败、取消、等待和容量不足不得自动转成内联重试。

## 4. 通用最终结果

`resources.subagent_protocol` 声明 `result_encoding=native_agent_result` 时，子代理保持宿主原生 result，
父流程把真实结果映射为下列逻辑字段；不得要求原生 Agent 输出文本 marker。声明
`result_encoding=helixharness_structured_text` 时，子代理完整读取
`hosts/shared/subagent-result-encoding.md` 并使用下列线性编码：

```text
HELIXHARNESS_JOB_ID: {job_id}
HELIXHARNESS_JOB_STATUS: completed|blocked|failed
HELIXHARNESS_WRITTEN_PATHS:
- {path 或 none}
HELIXHARNESS_VALIDATION:
- {check}: pass|fail
HELIXHARNESS_ERROR: {none|error}
```

宿主不能原生返回该格式时，Host Kit 必须提供等价映射，父流程仍要独立校验真实终态、写入
范围和预期产物。只读 job 必须通过命令约定的结构化结果返回，不能由子代理写共享报告。

格式纠正不是 job retry。只有当前命令显式声明 `result_reencode_budget=1`、当前 Host Kit 使用
`helixharness_structured_text`，并且 job 原生终态成功但 final 的物理编码或 command-local schema
不合格时，才按 `hosts/shared/subagent-result-encoding.md` 对同一 Agent 执行一次无工具、无写入的
final 重编码。`native_agent_result`、未显式声明预算的命令、原生失败/取消和业务产物失败均不进入
该分支。能力不可用或纠正仍失败时交回当前命令按既有产物门禁和父模型判断处理，不得新增通用强
阻断、自动内联或派生 Agent。

## 读取边界

`read_scope` 是当前 job 的完整读取授权清单；`forbidden_scope` 只描述禁止写入，不能据此推断
其他路径可读。执行读取、搜索、目录遍历或通过脚本间接读取前，先将目标按当前 job 的 cwd
规范化并解析符号链接；目标必须是清单中的文件，或位于清单明确授权的目录内。不得用字符串
前缀判断归属，不得跟随链接读到范围外，也不得从 Workspace 根或父目录发起宽泛搜索。
扫描结果中的链接、相邻仓库已生成的文件和“只读参考”建议都不会自行扩大授权。

父流程向子任务传递明确的 `ownership_root` 和解析后的 `read_scope`；递归派发的 reviewer
只能使用其子集，不能因为主机工具可访问其他路径就扩大范围。缺少必要输入时，在读取前返回
`BLOCKED: read_scope_input_missing` 和具体路径/用途，由父流程核实任务合同后重新派发明确范围。
父流程核验原生工具记录时发现超出声明范围的读取，应拒绝该 job 的成功结果并报告
`BLOCKED: agent_read_scope_violation`；不能只凭产物正确就接受。

## 5. 失败与恢复

- job 原生失败或取消后按 `BLOCKED: agent_failed` 收口，不自动重试。
- 越界写入、final 无效或产物校验失败时拒绝结果，并使用命令声明的 blocked 语义。
- 只有宿主明确确认 spawn 能力不可用，且 Dispatch Contract 允许时，父流程才读取同一 prompt
  内联一次，并执行相同 ownership 和验证。
- 跨会话只持久化逻辑 job 状态、输入摘要和产物路径，不持久化宿主 handle。
- 子代理的关闭和资源回收由宿主管理；公共命令不假设存在 close 工具。
