# OpenCode UT 运行适配

本资源只把 [公共 UT 命令](../../commands/ut.md) 的 Claude 兼容工具语义映射到 OpenCode；不复制
参数、扫描、6 Loop、重试、统一验证或报告步骤。子代理的通用派发、容量、等待、终态和回退完整
服从当前 `resources.subagent_protocol`。

```text
dispatch_mode: native_job
experience_write_mode: parent_finalize
runtime_handle_in_queue: false
permission_prepare: host_approval
permission_cleanup: not_required
```

## 权限与工具映射

- 不读取、创建或修改其他宿主配置，不自动批准权限，不绕过 OpenCode permission 规则，也不修改
  全局 `opencode.json(c)`。
- `Read/Write/Edit/Bash` 使用当前运行时真实工具：`read`、`edit`/`write`/`patch`、`shell`。
- `permission_prepare` 只验证当前工作区、临时目录和必要构建命令是否可用；需要额外权限时使用
  OpenCode 原生审批流程。未获得时保存证据并按公共命令标记 blocked/failed。
- `permission_cleanup` 固定返回 `not_required`，因为本适配器不写项目权限文件。

## 临时目录与原子写映射

- 公共命令的“系统临时目录”使用当前进程真实的 `TMPDIR` / 平台临时目录发现结果；
  不把项目内或其相邻的自建目录冒充系统临时目录。派发前确认每个 worker 的绝对路径与写域，
  只清理本次拥有的目录；系统临时目录权限不足时走原生审批，未获授权则保留 blocker。
- 公共命令要求原子写的 queue、session、主结果与共享经验等文件，先完整读取公共
  `reference/ut-artifact-protocol.md`，使用原生 `write` 写入
  **目标同目录的临时文件**，再用 `shell` 调用绑定插件的 `scripts/ut_artifacts.py write` 完成原子替换。
  插件必需的 `tool.execute.before` 会在原生 write/edit 执行前拒绝直接写最终 UT 状态文件，
  返回 `ut_atomic_write_required` 并给出脚本入口；按同一合同自行恢复，不需要操作者提醒。
  该保护覆盖 native write/edit，不声称是对任意 shell 或外部工具的通用文件系统沙箱。
  直接 `write` / `edit` 目标文件不构成该原子写证据。该映射同样适用于父流程最终报告收口，
  不能只对 worker result 或经验合并执行；具体适用文件仍服从公共 UT 命令。
- 发现先前使用直接写入时，如实保留不符合项，再按公共合同纠正后续操作；不得把最终
  原子替换追述为此前所有写入均合规。

## Worker Dispatch Contract

每个逻辑 Worker ID 建立 `ut_worker_<worker_id>` job：

| 字段 | 映射 |
|---|---|
| `prompt_source` | `agents/unit-test-worker.md`，worker 首个业务动作前完整读取 |
| `agent` | `helix-unit-test-worker`；不可用时使用内建 `general` 并传入同一完整 prompt |
| `depends_on` | 目标测试路径或共享 fixture 写域冲突时依赖前一 job |
| `write_scope` | worker 临时目录、自己的 result/defects/findings JSON 及同名 `.tmp` staging、唯一目标测试文件 |
| `forbidden_scope` | 业务源码、构建配置、共享 queue/session/summary/experience、其他 worker 路径 |
| `expected_artifacts` | result、findings JSON 与生成的分类 defects；成功时目标测试文件 |
| `validation` | 原生终态、公共 final、写域、结果结构、6 Loop、测试和覆盖率 |
| `unavailable_fallback` | 仅确认子代理能力不可用时，父流程按同一 prompt 串行内联 |

- `dispatch_worker` 映射为 `subagent`，按当前暴露的原生 schema 选择派发方式。在宿主支持的
  并行或后台执行方式下派发无写域冲突的 ready job；若仅支持前台执行，等待真实结果后再派发。
  不假定默认前台执行，不要求或传入 schema 不支持的 `background` 等参数；不假定固定并发
  上限，不预计算 slot，不把容量暂满当不可用。
- runtime handle 只保存在当前调用内存，不写业务工件；队列 `Agent ID` 列写 `-`。
- `wait_worker` 按当前 schema 等待前台真实结果，或后台 child 的原生完成通知并读取真实结果；
  `sessionID` 本身不是完成证据。不按固定频率轮询文件，不因静默自动中断或重试。收到终态后
  父流程再校验 result/defects、目标测试文件和 write scope。
- 原生失败、取消、越界写入或非法产物停止新任务准入，等待已派发 job 安全收口并输出部分结果。
- 重新执行只恢复逻辑队列和已验证产物；旧 `in_progress` 无可验证终态时重置为 `pending`。
- worker 使用 `parent_finalize`：不写共享经验，在 final 返回有界经验摘要，由父流程验证后串行、
  原子合并。

结构合法的 worker 业务失败（包括高危 Bug）仍按公共 UT 状态机处理，不等同于宿主原生 job 失败。
只有确认当前运行时没有暴露且无法发现 `subagent` 能力时才允许 inline fallback，并记录
`WARN:opencode_subagent_unavailable`；容量暂满、等待中、单个 job 失败或取消不得触发 fallback。

## UT 直接写入守卫范围

保留工件路径按大小写不敏感识别；原生 Write/Edit 不得直接覆盖。相对符号链接检查仅在已注册
`/helix:ut` 的活跃父会话或 `helix-unit-test-worker` 内解析当前 session 目录。普通会话的普通相对
编辑不查询 session API；绝对路径别名和显式保留路径仍受检查。父会话无后台任务而进入 idle，
或切换到其他 Helix 命令后释放 UT 范围；中断/失败后重新执行命令可重建范围，不缓存 cwd。
该守卫只覆盖原生 write/edit，不能当作 shell 沙箱；所有宿主仍须遵守公共原子发布合同。
