# Codex UT 运行适配

本资源只把 [公共 UT 命令](../../commands/ut.md) 的 Claude 兼容工具语义映射到 Codex；不复制参数、
扫描、6 Loop、重试、统一验证或报告步骤。子代理的通用派发、容量、等待、终态和回退完整服从当前
`resources.subagent_protocol`。

```text
dispatch_mode: native_job
experience_write_mode: parent_finalize
runtime_handle_in_queue: false
permission_prepare: host_approval
permission_cleanup: not_required
```

## 权限与工具映射

- 不读取、创建或修改其他宿主配置，不自动批准权限，不绕过 sandbox，也不修改 Codex 全局配置。
- `Read/Write/Edit/Bash` 使用当前运行时真实工具；文件编辑优先使用 `apply_patch`。
- 需要额外权限时使用宿主审批流程；审批未获得时保存证据并按公共命令标记 blocked/failed。
- `permission_prepare` 只验证当前工作区、临时目录和必要构建命令是否可用；不写项目配置。
- `permission_cleanup` 固定返回 `not_required`。

## Worker Dispatch Contract

每个逻辑 Worker ID 建立 `ut_worker_<worker_id>` job：

| 字段 | 映射 |
|---|---|
| `prompt_source` | `agents/unit-test-worker.md`，worker 首个业务动作前完整读取 |
| `depends_on` | 目标测试路径或共享 fixture 写域冲突时依赖前一 job |
| `write_scope` | worker 临时目录、自己的 result/defects、唯一目标测试文件 |
| `forbidden_scope` | 业务源码、构建配置、共享 queue/session/summary/experience、其他 worker 路径 |
| `expected_artifacts` | result；成功时目标测试文件；有缺陷时 defects |
| `validation` | 原生终态、公共 final、写域、结果结构、6 Loop、测试和覆盖率 |
| `unavailable_fallback` | 仅确认子代理能力不可用时，父流程按同一 prompt 串行内联 |

- `dispatch_worker` 映射为 `spawn_job`；不假定固定并发上限，不预计算 slot，不把容量暂满当不可用。
- runtime handle 只保存在当前调用内存，不写业务工件；队列 `Agent ID` 列写 `-`。
- `wait_worker` 使用宿主真实 mailbox/终态事件，不按固定频率轮询文件，不因静默自动中断或重试。
- job 真实终态通过后，父流程再校验 result/defects、目标测试文件和 write scope；文本自报不等于完成。
- 原生失败、取消、越界写入或非法产物停止新任务准入，等待已派发 job 安全收口并输出部分结果。
- 重新执行只恢复逻辑队列和已验证产物；旧 `in_progress` 无可验证终态时重置为 `pending`。
- worker 不写共享经验；在 final 返回有界经验摘要，由父流程验证后串行、原子合并。

结构合法的 worker 业务失败（包括高危 Bug）仍按公共 UT 状态机处理，不等同于宿主原生 job 失败。
只有确认当前运行时没有暴露且无法发现子代理能力时才允许 inline fallback，并记录
`WARN:codex_subagent_unavailable`；容量暂满、等待中、单个 job 失败或取消不得触发 fallback。
