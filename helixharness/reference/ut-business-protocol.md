# HelixHarness UT 跨宿主业务索引

本协议是 `/helix:ut` 的跨宿主业务索引和最低不变量，不是对完整命令的摘要替代。
`commands/ut.md` 是现阶段唯一完整业务事实源；所有宿主都必须读取其中的参数、扫描细则、状态机、
Loop 0—5、缺陷、覆盖率、文件同步、恢复、汇总和清理契约。Host Kit 的 `resources.ut` 只能替换
权限、子代理派发、等待、生命周期和并发控制实现，不得删除或改写业务步骤。

冲突解释规则：业务结果以 `commands/ut.md` 为准。支持其原生后台 Agent 语义的 adapter 保持原有
权限、轮询、超时、锁和 writer ownership；其他 adapter 只能覆盖这些宿主机制。

adapter 必须声明 `dispatch_mode`、`experience_write_mode`、`runtime_handle_in_queue`、
`permission_prepare` 和 `permission_cleanup`，并实现公共命令定义的四个逻辑动作：
`permission_prepare`、`dispatch_worker`、`wait_worker`、`permission_cleanup`。

## 1. 输入与模式

- 支持 `--packages <pkg1,pkg2,...>`、`-p` 与无参数交互入口；参数缺失或非法时不退出，由父流程在进入扫描前展示候选包清单与候选类计数，用户选择包并指定并发后与显式传入等价。继续执行模式沿用已有队列，不重新选包，但必须按包展示队列内任务的完成状态，使已在任务列表中的包可区分；显式传入的包不在队列中时仅提示，不自动转增量。
- 有残留 session/queue/worker 文件时展示状态，由用户选择继续、全新开始或增量补充。
- 增量补充必须最终确定包名（显式提供或交互选择均可），按源文件路径与已有队列去重；无残留时直接全新开始。
- 只在模式选择、恢复确认、参数补全（包选择与并发确认）和全新任务清单确认这些业务门禁询问用户；worker 不询问用户；两个宿主使用相同的纯文本编号清单交互，不依赖任何结构化选择器。

## 2. 不变量

- 业务源码、构建配置和 wrapper 只读；只允许写测试源码和 UT 工件。
- 每个目标类对应一个逻辑 worker；目标测试文件或共享 fixture 冲突时必须串行。
- 每个 worker 完整执行 `agents/unit-test-worker.md` 的 Loop 0—5，不因已有测试而缩减。
- 构建、测试和覆盖率必须来自本轮真实执行；覆盖率目标不低于 70%。
- Low/Medium 缺陷记录后继续；高危 Bug 允许对应 worker 以完整失败结果退出，其他独立任务继续。
- 子代理文件存在和文本自报都不能单独代表完成；父流程必须验证宿主终态、结果结构和产物。

## 3. 任务发现与上下文

父流程读取 `docs/harness/`、`docs/knowledge/`、当前 active feature 和已有 UT 经验，识别项目测试
基础设施并完成预编译检查。扫描项目支持的业务源文件，排除生成代码、DTO/VO/POJO、纯常量、
接口声明、已有规则排除目录及不属于指定包的文件，按路径稳定排序生成类级任务清单。

每个 worker 在首个业务动作前读取公共知识、当前 feature 和 `agents/unit-test-worker.md`；任务输入只
传目标源码、目标测试、构建入口、临时工作区、ownership 和验证，不复制大段公共上下文。

## 4. 状态与产物

Feature 目录中的业务状态包括：

- `unittest-session.md`
- `unittest-queue.md`
- `unittest-experience.md`
- `worker-{ID}-result.md`
- 可选 `worker-{ID}-defects.md`
- `unittest-results.md`
- `unittest-defects.md`
- `unittest-summary.md`

session、queue、experience 和三个主报告的 writer ownership 由当前 UT 与 subagent adapter 定义：
`child_locked_append` 保持 worker 锁追加和父流程汇总语义；`parent_finalize` 由父流程独占共享写入、
worker 只返回经验。任何实现都不得无锁并发覆盖共享文件。worker result 第一行
必须为 `状态: 成功` 或 `状态: 失败`；同步失败、高危 Bug、6 Loop、测试、覆盖率和写入路径按
worker 协议提供证据。

## 5. 调度和恢复

- 只派发依赖满足且写域互斥的 pending job。
- spawn 成功后才把逻辑任务标记为 in_progress；业务工件不保存宿主运行时 handle。
- 父流程等待真实终态，校验公共 final、写域和产物后再更新队列和合并经验。
- 宿主原生失败、取消、越界写入或非法产物停止新任务准入，等待已派发任务安全收口并输出部分结果。
- 重新执行时只恢复逻辑状态和已验证产物；没有可验证终态的旧 in_progress 重置为 pending。
- 只有宿主明确报告子代理能力不可用时，才能按当前 UT adapter 的约定串行内联。

## 6. 汇总和完成门禁

全部已派发 job 进入真实终态、队列没有 pending/in_progress 且产物校验通过后，父流程串行：

1. 合并 worker results 和 defects；
2. 恢复经过验证的同步失败测试文件；
3. 去重合并 worker 返回的新经验；
4. 在主项目执行统一编译、测试和覆盖率验证；
5. 生成 summary，并更新 session/queue 终态；
6. 只清理本轮明确创建且 ownership 可验证的临时资源。

没有新鲜构建、测试和覆盖率证据时不得声称 UT 全部完成。最终输出必须列出模式、扫描数、完成、
失败、blocked、高危 Bug、覆盖率、调度方式和三个主报告路径。
