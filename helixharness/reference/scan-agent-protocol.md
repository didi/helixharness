# Scan Agent 交付协议

适用于 init 的所有 `scan-*` job。派发、依赖、终态和写域由 `commands/init.md` 的 Dispatch
Contract 及当前 Host Kit 决定；扫描角色只补充各自产物内容。

## 执行者身份

你就是当前扫描 job 的 leaf 执行者。直接完成父流程授权的读取、分析、写入与回读验证，
不再调用同名角色，不派发 Agent/Task 或其他子代理；工具可用不等于获得递归派发授权。
父流程已负责依赖与任务编排。依赖未就绪、输入缺失或写入被阻断时返回具体缺口，
不得自行创建等待子任务或把当前 job 再转交给其他执行者。

## 写入边界

- 只写当前 job 的 `write_scope`；不修改共享 knowledge 根索引、init state、timings 或其他 job 产物。
- 父流程传入当前 Host Context；Read/Write/Edit 使用 `resources.runtime` 映射的真实文件工具。
- 输出必须实际落盘并回读验证非空。已有文件按当前 prompt 的增量合并或覆盖规则处理；
  尤其 architecture 的受控前端区块不得覆盖后端内容。
- 首次写入失败可在同一写域内修正路径或工具使用后再尝试一次；再次失败返回具体错误。
  不通过扩大权限、派发新 Agent 或越界写入绕过失败。

## 完成与失败

final 返回实际写入路径、验证结果与必要的索引建议，编码遵循宿主子代理协议。
`helixharness_structured_text` 时，完整读取父流程传入的同一插件编码资源，逐项返回
`HELIXHARNESS_JOB_ID`、`HELIXHARNESS_JOB_STATUS`、`HELIXHARNESS_WRITTEN_PATHS`、
`HELIXHARNESS_VALIDATION`、`HELIXHARNESS_ERROR`；普通 `STATUS: SUCCESS` 不能替代该回执。
只编码实际执行事实；收到 format-only 纠正时仅重述已有事实，不调用工具或重新扫描。
`native_agent_result` 保持原生结果，不要求这些文本 marker。
仅当当前 job 允许可选产物时，才可明确返回无产物原因，例如未检测到埋点；不得声称文件已写入。
父流程先等待原生终态，再校验产物与写域；文件存在或文字自报均不能代替终态。
产物缺失、越界或校验失败时报告 BLOCKED；bootstrap 只消费已验证产物，不重派扫描或自行补扫。
全部扫描通过后，由 bootstrap 在其授权范围内串行汇总共享 knowledge 索引。
