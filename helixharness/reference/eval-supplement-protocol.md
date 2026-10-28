# HelixHarness 异步统计协议

当前工作流使用两类互不覆盖的数据：

- `docs/eval/{feature}/eval-log.json`：`helixharness-eval-ledger/1.0` 固定证据，可随代码提交。
- Stage Supplement：`helixharness-stage-supplement/1.0`，只在既有 consent 已开启时由阶段结束后的异步 Runtime 生成和上报。

业务流程不等待采集或网络；采集、脱敏、上传与重试全部由 Runtime 在阶段外完成。

Ledger 保存 command、标准化 stage kind、attempt、每次 Entry 实际使用的 HelixHarness version、时间、状态、工件、质量、128-bit 匿名 session 指纹，以及代码的 commit/分支引用。新 Entry 不再写可由 `entry_id` 一对一派生的 `logical_stage_run_id`，也不写 command 可直接映射的 `phase`；Schema 1.0 reader 仍接受旧日志中的这两个字段。`attempt` 只在用户明确重新执行同一 requirement 的同一 command 时递增，重试保留独立 Entry；一次命令内部的提问、回答和继续执行保持同一个 Stage Entry，不以 `decision_needed` 收口。Recorder 自动把阶段结束时真实存在的 Spec/Tasks/Evidence 固定路径写入 `artifacts`，调用方不传自由格式路径。Checkpoint 默认只记录阶段起止 commit 与业务 worktree dirtiness；只有代码按分支解析时才追加 source/target/merge-base，Apply/Archive 的 commit 或 Review Scope 事实按需追加。

Ledger 不保存 raw session ID、Token、工具原始输入输出、对话文本或上传状态。一个 requirement 可以包含多个主会话；每个 Stage Entry 只绑定本次实际会话，不同 Entry 的 session 指纹共同形成需求会话集合。Supplement 保存 `platform + raw session_id + session_hash` 关联、精确阶段 Token 差值、工具/子 Agent/上下文读取结构统计，以及经过筛选脱敏的完整 `helixharness-evolution-evidence/1.0`。其中 `interactions[]` 按发生顺序保存脱敏后的 `role + text`，`interaction_summary` 保存用户轮次与 Agent 消息数；兼容字段 `user_inputs[]`、`assistant_replies[]` 继续保留。成功工具不带原始参数或输出；失败工具只保留类型化摘要；Thinking、源码、完整 Diff、附件内容和原始日志不上传。平台以 `entry_id + requirement_id + feature` 连接固定证据，并以 `platform + session_id` 关联会话记录；未关联数据不得进入正式统计或自动自进化。

Supplement 还携带 `helixharness-workflow-trace/1.0`。该 Trace 在 consent enabled 时按 Stage Entry 私有保存于 `.helixharness/telemetry-v2/traces/{entry_id}.json`，不进入可提交 Ledger；它只接受受控 identifier，记录 HelixHarness 版本、Router/阶段正文/recorder 的路径与 SHA-256、Router decision code、阶段起止迁移、确定性业务门禁结果、受控恢复 fallback 和最终 outcome。它不接受自由文本，不记录 Agent Thinking、推荐理由正文、工具参数/输出、源码、Diff 或日志。Trace 缺失时 Supplement 显式写 `status=unavailable + reason_code=trace_unavailable`，不得补猜。该数据用于把“哪一版组件、哪条路由、哪个门禁或 fallback 与结果相关”转成可验证的进化假设；它不是假设正确性的证明，也不授权自动修改或发布框架。

Archive 完成时，以私有 `helixharness-lite-collection-seal/1.0` 冻结 Ledger 中最终 Stage Entry 清单，并为每个可用 Session 源记录当时的字节读取上限；Seal 不保存 raw session ID、日志路径或会话正文。归档后的 Worker 可以继续解析冻结内容、重试同一份 Outbox 数据并等待 `/report` 确认，但不得读取字节上限之后的补写、登记新 Session 或重新打开需求。日志在归档前变化时仍可生成递增 Supplement `revision`；Archive 封口后内容身份不再变化，只允许相同冻结数据的幂等传输。

每个 Stage finish 同时在 `.helixharness/telemetry-v2/inputs/{entry_id}.json` 私有冻结 `helixharness-lite-collection-input/1.0`：只包含该 Entry 生成 Supplement 所需的 Requirement 标识、固定 Ledger Entry 和可选 Seal Boundary，不包含 raw session ID、日志路径或会话正文。Collector 优先读取 Collection Input；缺失时仍须按现有校验读取可验证的 Ledger 与 Locator，不能猜测或补造；因此已登记 Stage 的异步采集不依赖 Worker 运行时当前 checkout 的分支。Archive finish 会用最终 Ledger 与 Seal Boundary 刷新该需求全部 Entry 的 Collection Input。

私有采集状态按以下单向合同收口：未归档 Entry 首次生成 Supplement 后写 `collected`，源或 Workflow Trace 变化时仍可生成 revision；Archive Seal 冻结后成功生成最终 Supplement 即写 `sealed_collected`，后续定时、唤醒和 backfill 都只跳过，不再读取 Session 或重新生成。最终 Supplement 的 Outbox envelope 携带 `final_for_entry=true`；服务端返回 accepted/duplicate 后，Runtime 完成 `delivered` 清理，保留 Git 跟踪的 Ledger，但删除对应 Locator、Trace、Stage、Collection Input 与本地完成标记，全部 Entry delivered 后删除 Requirement Seal。永久缺少旧 Ledger、冻结输入损坏、Locator 永久无效等不可重建情况写 `quarantined + reason_code`，从正常采集和熔断计数中退出；服务端 rejected 或损坏 Outbox 仍进入用户级 Quarantine，不再自动重试。

自进化证据优先保留精确阶段窗口内经过 DLP 的完整用户输入与有效 Assistant 回复，不以单条固定字符数截断；密钥、个人信息、系统路径、源码与 Diff 仍先脱敏或整体省略。传输按 gzip 后字节数和 item 数双重分批：默认每个请求不超过 1 MiB 且不超过 100 个 Supplement，单并发发送，批次间暂停 500 ms。单个 Supplement 自身超过目标大小时仍作为独立批次完整发送，不丢弃正文；网络失败继续使用持久 Outbox 与指数退避。

代码证据优先使用已验证、属于当前分支历史的 `implementation_commit_id`。没有准确 commit 时保存 `source_branch + target_branch + merge_base_commit + observed head + dirty state`；`target_branch` 优先解析 `origin/HEAD`，缺失时只回退本地或 `origin` 中唯一存在的 master/main 名称，同名引用固定优先 remote-tracking commit。平台稍后读取功能分支相对该目标分支的 Diff；分支未 push 或修改尚未提交时保持 pending。HelixHarness 不上传源码/Diff，也不因统计 consent 自动 push。普通非强制 push 由 Archive 的最终交付选择授权：先完成本次获授权的精确提交，再验证功能分支与待推送 HEAD；不存在可提交 Ledger 时，允许按明确展示并获授权的代码交付范围推送，不把 Ledger 当作代码交付前置门禁。

Telemetry consent 只授权记录和上报，不单独授权 Git commit 或 push。Archive 在业务归档完成后始终进入 `workflows/single/archive.md` 的“最终交付选择”；普通入口、`--loop`、Bug Fix 与 Workspace 子仓共用该合同。统计 disabled、unsupported、finish 跳过／失败或 Ledger 已提交都不能吞掉代码提交／推送选择。按实际待提交代码与可提交 Ledger 展示选项：有待提交项时选择“提交并推送／只提交／暂不提交”；没有待提交项时选择“立即推送／暂不推送”。已有覆盖本轮精确范围的明确决定直接复用，不重复询问。选择发出后必须等待真实回答；工具受理或超时不构成决定。

最终 Ledger 仍由 `commit-ledger --commit-authorized` 创建一个只包含 `docs/eval/{feature}/eval-log.json` 的独立提交 `chore(eval): record {feature}`；它保留实现 commit 作为 Ledger 的代码引用，不夹带其他 staged/unstaged 文件。未授权、disabled 或 finish 未 recorded 时不得新增 Ledger 提交；已经完整提交的 Ledger 不创建空提交。用户已选择的 Ledger 提交失败时，不得静默降级为只推代码；没有可提交 Ledger 的路径则不调用 `commit-ledger`，按事先展示的范围独立交付代码。推送验证使用本次获授权操作后的完整 HEAD，而非 Ledger 的历史 commit；含推送的选项只授权一次普通非强制 push，不得扩张为部署或 force push。代码推送成功不代表统计成功，只有有效最终 Ledger 已随远端 HEAD 验证成功时才能报告平台可继续关联。

统计开启且 Archive 成功记账后，finish CLI 返回 `NOTICE:lite_telemetry_delivery_required`，工作流在最终交付选择前原样展示一次：过程统计 Supplement 自动异步上报，但最终 eval-log 必须提交并随功能分支 push 才能完成平台关联。同一需求的本次交付已经提醒时，恢复、继续执行或复用已有授权均不重复展示。`commit-ledger` 成功或幂等跳过时不返回该 NOTICE；工作流也不重复展示旧版 CLI 返回的同名提示。用户选择后的最终回执只报告实际提交／推送结果：有效最终 Ledger 已提交并随远端 HEAD 验证成功时说明“平台可继续关联统计”；未提交或未推送时说明对应待完成动作及“平台暂时无法关联”；失败时说明真实原因，不把选择等同于执行成功，也不重复通用 NOTICE。

业务合同与统计支持集相互独立：业务正文和三工件不记录宿主，同一需求可以跨宿主继续；`current_platform` 只用于本次 Telemetry Adapter 选择。当前只有 Claude Code 与 Codex 注册 Adapter。CLI 输入对大小写和宿主包装标签宽松：只要包含 `claude` 或 `codex` 子串，就分别规范化为 `claude-code` 或 `codex`；同时包含两类子串时因歧义拒绝，公开 Ledger 与 Supplement 始终只落 canonical ID。其他未注册统计宿主按 `unsupported_host` 跳过统计，但业务必须继续。新增统计宿主只在 `scripts/telemetry_hosts/` 实现并注册 `HostAdapter`（含统一 Supplement 窗口），不得修改业务正文，也不得在 Ledger、Collector 或 Schema 中增加平台分支。

同一 feature 允许跨宿主接力。Ledger 的每个 entry 独立记录本次实际 `platform` 与 locator；不同 entry 可以来自不同已注册 Adapter，Supplement 仍按 `entry_id` 精确关联。未注册宿主只缺少该次统计，不阻塞业务。

Runtime 位于 `${HELIXHARNESS_TELEMETRY_HOME:-$HOME/.helixharness/telemetry}`。它是用户级全局 Runtime，不为每个需求建立 Worker：一次 Worker 扫描所有已登记仓库，仅处理尚未采集或上报完成的 Stage；已经完成且内容未变化的需求只命中本地完成标记，不再生成 Supplement。持久调度支持 macOS launchd 与 Windows Task Scheduler：阶段结束非阻塞唤醒同一个受管任务，十分钟定时触发只做漏跑补偿。Windows 任务使用 `IgnoreNew` 拒绝重叠实例；Runtime 还以用户级 OS 文件锁保证定时触发、阶段唤醒和手工运行全局只有一个 Worker，已有 Worker 时新调用立即成功跳过。单次 Worker 最长运行 15 分钟，超时退出后保留 Ledger、采集状态和 Outbox，由下一轮续跑；正常退出、超时或崩溃后锁均由操作系统自动释放，不依赖残留 PID 文件。

熔断按故障域分层。Collector 对每个 `repository + platform` 独立累计实际、可重试的采集异常；连续 6 次失败只暂停该作用域 1 小时，冷却后放行一次半开探测，其他仓库和宿主继续采集。永久不可恢复 Entry 已进入 `quarantined`，不计入采集熔断。Delivery 对每个 Endpoint 独立累计网络异常或 `retryable`，连续 6 次失败只暂停该 Endpoint 1 小时；已生成 Supplement 继续保留在 Outbox，Collector 不因发送熔断停止或重新解析 Stage。服务端 `rejected` 与损坏 Outbox 属于单项永久隔离，不触发 Endpoint 或全局熔断。

只有 Runtime 导入失败、Worker 未正常收口、用户级存储不可用等系统性异常才累计用户级全局熔断；连续 6 次后暂停整个 Worker 1 小时，并按小时限频向同一个 `/report` 尝试发送一条 `helixharness-telemetry-runtime-health/2.0` 告警。发送全程持有一个明确 repo/platform/consent epoch 的 lease，授权失效时不发送。该事件只含随机匿名 Runtime ID、受控错误码、失败次数、OS、Runtime 版本以及 `scope_kind=runtime`、`scope_id=runtime`、`circuit_open` 等健康状态，不含仓库数量、Outbox 数量、仓库路径、feature、Session、对话或源码。`/report` 不可达时无法通过同一地址即时告警，本地状态会保留并在后续触发重试；`telemetryctl repair` 可在修复 Runtime/Scheduler 后清零全局及各作用域熔断状态。

macOS LaunchAgent 由 `RunAtLoad` 在用户登录后恢复；Windows 任务持久保存，并通过 LogonTrigger 与 `StartWhenAvailable` 在登录/漏跑后恢复。Worker 异常由下一轮补偿继续，缺失或不健康的 Scheduler 会在后续 SessionStart/命令入口自动重建；若系统任务被删除后用户不再启动任何 HelixHarness 会话，则不存在能脱离操作系统任务自行运行的进程，必须等待下一次入口或手工 `telemetryctl repair`。其他系统不安装持久调度器，只保留阶段结束的一次性唤醒和手工入口。Outbox 对 accepted/duplicate 删除，对网络/5xx 退避，对永久 schema 错误 quarantine。授权关闭后，下一次 hook、命令入口或后台 worker 会移除未发送私有证据并停止新增采集；用户级关闭同时撤掉调度器。

维护入口不是 HelixHarness command，而是：

```bash
~/.helixharness/telemetry/bin/telemetryctl status
~/.helixharness/telemetry/bin/telemetryctl run --all
~/.helixharness/telemetry/bin/telemetryctl backfill --since 7d --all
~/.helixharness/telemetry/bin/telemetryctl repair
```

默认接收地址为空。report 与 feedback 共用 `scripts/reporting_config.py` 的解析器；发布方在该脚本配置 `DEFAULT_REPORT_ENDPOINT` / `DEFAULT_FEEDBACK_ENDPOINT`，用户分别通过私有目录 `config.json` 的非空 `endpoint` 覆盖。缺少文件、字段或值为空时使用发布默认值；损坏文件、字段类型错误或非空无效地址不回退。统计接收地址须指向实际 helixharness-platform 的 `/report`。只支持发布脚本与用户文件这两种地址来源，地址环境变量不再生效；HOME 环境变量仍可指定私有目录。配置不进入 `docs/eval`，默认脚本随 Runtime 一起更新。批次使用 gzip，服务端逐项返回 accepted、duplicate、retryable 或 rejected。

同一阶段补充内容未变化时，Runtime 先用宿主源与 Workflow Trace 的组合指纹跳过重复解析；归档前日志延迟补齐、Trace 延迟收口或重修导致内容变化时，生成递增 `revision` 并用 `supersedes` 指向上一包。Archive 后宿主源指纹切换为 Seal 的固定身份，并继续与冻结的 Trace 内容组合；首次处理只读取冻结字节，完成标记写入后即使宿主日志继续增长、轮转或删除也不重开采集。手工 `run/backfill` 只要所选范围仍有待投记录就返回非零，不把退避中的数据误报为完成。

## 授权、入口与异常边界

授权入口及固定告知见 `reference/eval-protocol.md` 第 2 节。工作流通过 `scripts/telemetry_consent.py` 处理用户选择；recorder/Runtime 通过 `scripts/telemetry_consent_state.py` 读取同一缓存并校验 consent epoch，不依赖其他统计实现。

只有 Init 和规定的 Propose 入口处理授权提醒；Apply/Archive 只读当前有效授权。统计 disabled、unsupported 或发生异常时不得阻断业务，不要求不存在的 entry_id，也不伪造统计成功。等待业务问题回答仍处于同一次阶段调用。

`archive --no-commit` 将提交决定延后到归档后的最终选择，本身不授权 commit 或 push；无论是否生成 eval-log，都按当前 Archive 正文处理真实待提交项及推送选择。consent 本身不构成提交或推送授权。

未支持的 Ledger 格式只跳过统计并保留原文件，不迁移或改写历史数据。Schema 及约束以 `reference/eval-ledger-schema-v1.json` 和当前 `scripts/telemetry_protocol.py` 为准。字段不可用时保留 unavailable，不通过兼容路径生成历史阶段。

未配置或配置无效时，Runtime 不自动安装/唤醒，不采集会话、不投递补充数据或健康报告，也不把配置缺失计为采集失败。已安装 Worker 每轮执行前同样检查；历史待发送队列保留并暂停，不使用其中保存的地址绕过检查。配置恢复后按原授权、授权代次和队列地址继续处理。授权询问和提醒周期不受此检查影响；用户确认 enabled 后正常记录同步账本。未授权或 disabled 时，不新增账本，也不异步采集或投递。
