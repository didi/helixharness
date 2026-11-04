# Pi UT 运行适配

本资源只把 [公共 UT 命令](../../commands/ut.md) 的工具语义映射到 pi；不复制参数、扫描、6 Loop、
重试、统一验证或报告步骤。子代理的通用派发、容量、等待、终态和回退完整服从当前
`resources.subagent_protocol`。

```text
dispatch_mode: inline_serial
experience_write_mode: parent_finalize
runtime_handle_in_queue: false
permission_prepare: not_required
permission_cleanup: not_required
```

## 权限与工具映射

- 不读取、创建或修改其他宿主配置，不自动批准权限，不修改 pi 全局或项目设置。
- `Read/Write/Edit/Bash` 使用当前运行时真实工具；文件编辑使用精确文本替换或整文件写入。
- `permission_prepare` 只验证当前工作区、临时目录和必要构建命令是否可用；不写项目配置。
- `permission_cleanup` 固定返回 `not_required`。

## Worker Dispatch Contract

本宿主 `subagents=unsupported`，每个逻辑 Worker 按依赖顺序串行 inline 执行：

| 字段 | 映射 |
|---|---|
| `prompt_source` | `agents/unit-test-worker.md`，worker 首个业务动作前完整读取 |
| `depends_on` | 目标测试路径或共享 fixture 写域冲突时等待前一 worker 完成 |
| `write_scope` | worker 临时目录、自己的 result/defects、唯一目标测试文件 |
| `forbidden_scope` | 业务源码、构建配置、共享 queue/session/summary/experience、其他 worker 路径 |
| `expected_artifacts` | result；成功时目标测试文件；有缺陷时 defects |
| `validation` | 写域、结果结构、6 Loop、测试和覆盖率 |
| `unavailable_fallback` | inline 串行执行，记录 `WARN:pi_subagent_unavailable` |

- 队列 `Agent ID` 列写 `-`；不假定并发上限，不预计算 slot。
- inline 结果不是宿主原生终态：父流程必须独立校验 result/defects、目标测试文件和 write scope，
  文本自报不等于完成。
- 写域越界或产物非法时停止新任务准入，输出已完成部分结果。
- 重新执行只恢复逻辑队列和已验证产物；旧 `in_progress` 无可验证终态时重置为 `pending`。
- worker 不写共享经验；在 final 返回有界经验摘要，由父流程验证后串行、原子合并。

结构合法的 worker 业务失败（包括高危 Bug）仍按公共 UT 状态机处理，不等同于宿主运行失败。
