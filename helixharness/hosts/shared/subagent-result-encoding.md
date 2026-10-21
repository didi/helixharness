# HelixHarness 结构化子代理结果编码

本资源只供 `result_encoding=helixharness_structured_text` 的 Host Kit 使用。公共 command/agent prompt
定义必须返回的业务事实，本资源只规定这些事实在线性文本通道中的物理编码；不得新增业务门禁。
`result_encoding=native_agent_result` 的宿主不读取本资源，继续使用原生 Agent/Task result，由父流程
映射并独立验证真实产物。

## 通用终态

使用公共子代理协议的以下 canonical 模板，替换占位符为真实执行事实；状态使用小写枚举，
不能以普通 `STATUS: SUCCESS` 或自然语言摘要替代。

```text
HELIXHARNESS_JOB_ID: {job_id}
HELIXHARNESS_JOB_STATUS: completed|blocked|failed
HELIXHARNESS_WRITTEN_PATHS:
- {path 或 none}
HELIXHARNESS_VALIDATION:
- {check}: pass|fail
HELIXHARNESS_ERROR: {none|error}
```

## 有界格式纠正

只有当前命令显式声明 `result_reencode_budget=1`，并且原生 job 已正常结束、原始 final 已包含本次
job 的业务事实，但线性终态或 command-local payload 因格式/schema 不合格而无法消费时，父流程才可
请求一次格式纠正。当前命令未显式启用时预算为 0，继续
沿用公共失败语义。

```text
result_reencode_budget: 1
```

格式纠正必须发送给同一 Agent，并同时提供原始 final、首次校验的精确错误和本文件中的 canonical
模板。纠正消息只能要求重新编码已返回事实：不得调用任何工具、读取或写入文件，不得重新执行业务
任务，不得新增、删除或改变 finding、证据、状态、路径、干预事件或 move 事实，也不得派生新 Agent。
格式纠正不是 job retry，不增加业务 attempt，也不改变原生终态。

父流程对原始和纠正后的每个候选 payload 只调用一次相同 validator/结构检查，最多校验两个候选 payload；
禁止第三次纠正、父流程猜测字段或另起 Agent 重做。纠正能力不可用或第二个候选仍不合格时，
保留原始 final 与两次以内的真实错误，交回当前命令按既有产物门禁和父模型判断处理；不得因为格式
纠正机制本身不可用而新增强阻断或把未知结果伪造成成功。

## Evidence facts

命令要求证据索引时追加 `HELIXHARNESS_EVIDENCE_INDEX`。每项编码 command 声明的 `claim`、
`supports` 与可定位 `source`；允许集合、条数和消费方式仍以当前 command contract 为准。

## Apply Review facts

Standards 与 Spec 的结果完整遵循 `skills/code-review/SKILL.md`。两个轴分别返回报告、finding
证据及 verdict：`APPROVE|MUST_FIX|DECISION_NEEDED`；finding disposition 为
`ADVISORY|MUST_FIX|DECISION_NEEDED`。不得映射为旧的 product/technical/quality 角色或
pass/concerns/fail 评分，也不得在宿主层引入第二套 Review schema 或验证门禁。

## Workspace RepoJob facts

Workspace 子仓执行完整公共命令，结果身份、预期产物、阶段完成状态和父级校验以
`reference/workspace-repo-dispatch.md` 为准。Archive 使用同一 RepoJob 结果，不追加第二套
archive-business marker。子仓完成后父流程验证 completed 工件，最后归档 Workspace 根 Spec。
