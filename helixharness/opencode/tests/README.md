# OpenCode adapter verification / OpenCode 适配验收

Current release support: **Stable on macOS with OpenCode 2.3.2**, within the [support matrix](../../../docs/opencode-testing.md). OpenCode 2.2.2 and 2.3.0 have automated compatibility coverage only. The dated checkpoints below retain their original acceptance boundaries and historical Preview decisions; they are not fresh test runs.

当前发布支持：**macOS 上的 OpenCode 2.3.2 为 Stable**，范围以[支持矩阵](../../../docs/opencode-testing.md)为准。OpenCode 2.2.2、2.3.0 仅有自动化兼容覆盖。下列历史检查点保留当次验收边界与 Preview 决定，不代表本次重新执行了这些测试。

Run these commands from the plugin directory (`helixharness/`).

```bash
bun install --ignore-scripts
bun test opencode/tests
bun run typecheck
```

The default suite skips native-server tests. To exercise an installed OpenCode V2 binary:

```bash
HELIXHARNESS_OPENCODE_E2E=1 bun test opencode/tests/e2e.test.ts
```

If it is not on `PATH`, select the executable explicitly:

```bash
HELIXHARNESS_OPENCODE_BIN=/absolute/path/to/opencode \
  HELIXHARNESS_OPENCODE_E2E=1 bun test opencode/tests/e2e.test.ts
```

`native-fixture.ts` starts the real OpenCode server with temporary project/home/config/data/cache/state
directories and a loopback OpenAI-compatible scripted model. It forwards no model-account environment
variables and uses only synthetic messages and fixture authentication. It stops both servers and removes
the temporary directories when each fixture finishes. An OpenCode account or paid model is not needed.

The tests verify registration and command admission, per-session directory binding, command rejection
after a directory disappears, successful Skill binding and complete replacement of failed Skill output
before the next model call. They also verify native worker → two independent reviewer dispatches,
leaf-role denial and depth-limit failures. The assertions observe actual native execution results;
a tool's presence in the advertised catalog is not proof that it can execute.

These are adapter integration tests. Scripted model responses do not establish that a real model can
complete Init → Propose → Apply → Archive, produce correct business code, or follow all ownership rules.
A separate assisted run on 2026-11-20 used OpenCode 2.3.2 and DeepSeek to complete one synthetic single-repository lifecycle, including a controlled contract amendment and fresh parallel Standards/Spec review. The project suite passed 13 tests; an independent acceptance suite passed 14. Operator confirmations were supplied through the native form API. This does not establish TUI/desktop UI or real-model Workspace acceptance.

A separate assisted run on 2026-11-23 completed one synthetic two-repository Workspace Init → Propose → Apply → Archive on OpenCode 2.3.2 with DeepSeek. Both RepoJobs moved their native sessions to their ownership roots; nested leaf reviewers exercised depth 2. A provider MUST_FIX was repaired and both axes rerun. The repository suites passed 10 and 19 tests, plus 5 independent cross-repository acceptance groups. Standalone Maven UT used two concurrent native workers with full six-loop evidence and a second run under the actual OS temporary directory: 40 tests passed with 100% line/branch coverage for both classes; business sources and pom.xml were unchanged. The acceptance harness supplied synthetic choices through native forms/prompts. It also corrected report classification and parent atomic finalization; this is not unattended protocol-compliance evidence. Workspace read-only repositories, cancellation/recovery, UT Gradle/incremental/retry paths, D2C and TUI/desktop UI remain outside this run.

At that checkpoint, OpenCode remained Preview. On 2026-11-23, regression tests verified the fixes for Skill mention repository binding and symbolic-link resource containment. Unit tests cover all four request hooks (context, compaction, generate, title), source-message preservation, literal input, registration failures, and internal/external/dangling/cyclic resource links. Native tests on OpenCode 2.2.2, 2.3.0 and 2.3.2 inspect actual model requests for direct mentions, command attachments, native activation, cross-session roots and literal preservation. Fault-injected session lookup/validation failures must stop before the model receives a request. Auxiliary request hooks have unit-level coverage; those scripted regressions alone do not establish real-model workflow or TUI/desktop UI acceptance.

## UT classification and atomic-state regression

`scripts/ut_artifacts.py` validates structured observation evidence, separates confirmed defects, risks and optional improvements, and generates classification counts/reports. State publication reuses the same-directory fsync/replace writer. OpenCode requires an execute.before guard that blocks native write/edit of final UT state, including directory/file aliases within active registered UT parent/canonical worker scope and absolute aliases; reserved names match case-insensitively. Ordinary relative editing outside UT does not query the session API; staging and the explicit publisher remain available. This is not a sandbox for arbitrary shell code, and source-reference validation does not prove every business interpretation.

After the fixes were frozen on 2026-11-23, a fresh OpenCode 2.3.2 + DeepSeek Maven UT run used two native workers and full six-loop evidence. The operator answered only the normal task-list form. All 29 tests passed, both classes reached 100% line/branch coverage, and main sources/pom.xml stayed unchanged. The generated counts were 0 confirmed defects, 2 risks and 2 optional improvements. All 15 observed atomic publication receipts succeeded; there were no direct-target write attempts and no corrective prompts. The fixture had no prior tests or experience. This is one synthetic acceptance case, not a reliability guarantee for all projects.

After the seven compatibility fixes, the suite passes 122 Bun tests (435 assertions), including 21 native tests on each of 2.2.2, 2.3.0 and 2.3.2. The Python suite passes 821 tests with 7 skips, including 27 classification/publication regressions. Coverage includes legacy/timeout unknown totals, coherent active/archived cohorts, saved-record corruption, evidence cleanup, reproduced ClassCastException and non-JVM source references, existing POSIX modes, host ownership, ordinary editing under session faults and casing. JDK/Python runtime probes retain actual ClassCastException/ZeroDivisionError as confirmed High observations. Windows matching is unit-only; ACL/xattr and multi-file transactions are not guaranteed. Typecheck passes.


A second fresh OpenCode 2.3.2 + DeepSeek acceptance run after these seven compatibility fixes completed both native workers and all six loops: 38 test executions passed, both classes reached 100% line/branch coverage, and source/pom hashes stayed unchanged. Counts were 0 confirmed defects, 2 risks and 3 optional improvements. All 15 atomic publication receipts succeeded, with no direct-target write attempts or corrective prompts; only the normal task confirmation was answered. Published schema-2 snapshots remained valid after worker cleanup. The three synthetic sessions were archived and the isolated server stopped.

## 中文说明

在插件目录运行上面的 Bun 单元测试与类型检查。默认跳过真实宿主测试；设置
`HELIXHARNESS_OPENCODE_E2E=1` 后，使用 `PATH` 上的 OpenCode，或通过
`HELIXHARNESS_OPENCODE_BIN` 指定实际可执行文件。最低验证版本为 OpenCode 2.2.2。

测试启动真实 OpenCode 服务，但模型是本机回环地址上的脚本模型；项目、宿主测试 home、配置、数据、缓存和
状态均使用临时目录，不传递模型账号环境变量，不需要真实账号或付费模型。测试结束后关闭
服务并清理临时目录。

验收覆盖注册、命令提交、跨会话目录绑定、目录失效后的阻塞、Skill 正文绑定与失败结果替换，
以及 worker 的两路独立派发、leaf 角色拒绝委派和深度不足拦截。必须检查真实调用结果，不能仅凭
工具出现在列表中判断权限有效。

另一次于 2026-11-20 执行的辅助验收使用 OpenCode 2.3.2 + DeepSeek，完成了合成单仓的
Init → Propose → Apply → Archive 闭环，期间有操作者原生问答确认、一次受控合同修订和
新一轮并行 Standards/Spec 评审。项目测试 13 项、独立验收 14 项通过。这不覆盖 TUI/桌面 UI
或多仓 Workspace 的真实模型验收，也不能外推全部业务场景与所有权规则。

2026-11-23 另一次操作者辅助验证在 OpenCode 2.3.2 + DeepSeek 上完成合成双仓 Workspace
Init → Propose → Apply → Archive。RepoJob 先切换真实会话目录到 ownership_root，depth=2
派发独立 leaf reviewer；provider 的 MUST_FIX 修复后重新双轴评审通过。两仓分别通过
10/19 项测试，外部独立验收通过 5 组。独立 Maven UT 用两个并发原生 worker 执行完整
6 Loop，并在实际 OS 临时目录重跑：40 项测试通过，两类行/分支覆盖率 100%，业务源码和
pom.xml 未变。验收脚本通过真实 form/prompt 提供合成选择，并纠正了报告误报分类与父流程
原子写收口；这不能证明无人干预时始终遵守协议。本轮未覆盖 read-only repo、取消/恢复、
UT Gradle/增量/失败重试、D2C 或 TUI/桌面 UI。

当次验收时，支持级别保持 Preview。2026-11-23 的回归已验证 Skill mention 绑定与资源符号链接边界修复：
单元测试覆盖 context/compaction/generate/title 四类请求 hook、源消息保留、用户字面输入、
注册失败，以及内部/外部/悬空/环状资源链接。OpenCode 2.2.2、2.3.0、2.3.2 的原生测试
检查直接 mention、命令附件、原生 Skill 激活、跨会话目录与用户字面输入的实际模型请求；
故障注入时，查询或目录校验失败必须在模型收到请求前阻断。辅助请求 hook 为单元级覆盖；
上述脚本模型回归本身不构成真实模型业务闭环或 TUI/桌面 UI 验收。

## UT 分类与原子状态回归

`scripts/ut_artifacts.py` 校验结构化观察的来源/反例，分别统计已证实缺陷、未确认风险与可选改进，
生成分类报告；UT 状态通过同目录 fsync/replace 原子发布。OpenCode 的必需 execute.before
保护在 native write/edit 执行前拒绝直接写最终 UT 工件，保留路径按大小写不敏感匹配；绝对别名及活跃注册 UT 父会话/规范 worker 内的相对别名受检，
非 UT 普通相对编辑不查询会话 API；不声称是任意
shell 的文件系统沙箱，引用校验也不等于机械证明所有业务语义。

2026-11-23 冻结修复后，使用 OpenCode 2.3.2 + DeepSeek 在无旧测试/经验的全新 Maven 夹具
完成两个原生 worker 的完整 6 Loop。操作者只回答任务清单确认：29 项测试通过，两类行/分支
覆盖率 100%，业务源码与 pom.xml 不变；分类为 0 缺陷 / 2 风险 / 2 改进。15 次原子发布回执
均成功，无直接目标写入尝试，无分类或原子写纠正提示。这是一轮合成场景验收，不外推全部项目。

七项兼容修复后：122 项 Bun 测试、435 个断言；2.2.2/2.3.0/2.3.2 各 21 项原生测试通过。
Python 全量 821 项（7 跳过）通过，含 27 项分类/发布回归：旧会话/超时未知计数、当前与归档
整组选择、旧记录损坏、证据清理、ClassCastException 与非 JVM 源码引用、原文件 mode、宿主
写域、会话查询故障下的普通编辑、大小写绕过。JDK/Python 实际异常探针均保留已证实 High。
Windows 仅单元级覆盖；不承诺 ACL/xattr、多文件事务。类型检查通过。

七项兼容修复后的第二轮全新 OpenCode 2.3.2 + DeepSeek 验收：两个原生 worker 完成全部 6 Loop，
38 次测试执行通过，两类行/分支覆盖率 100%，业务源码与 pom 哈希不变。分类 0 缺陷 / 2 风险 /
3 改进；15 次原子回执全部成功，无直接目标写入或纠正提示，仅回答正常任务确认。worker 清理后
schema-2 分类快照仍校验通过；3 个合成会话已归档，隔离服务已停止。


## Recovery and auxiliary workflow acceptance (2026-11-23)

The follow-up suite ran 852 Python tests: 845 passed and 7 skipped, including 31 new recovery, precompile and read-only regressions. Each of OpenCode 2.2.2, 2.3.0 and 2.3.2 passed 122 Bun tests (435 assertions); typecheck passed.

Real-model OpenCode 2.3.2 + DeepSeek acceptance verified a read-only Workspace repository, native Apply cancellation followed by fresh-session recovery, partial Archive cancellation followed by recovery that skipped the already archived repository, and an injected native context-hook failure that blocked dependent dispatch without inline fallback. Active Single BugFix reproduced an injected Unicode regression, preserved the original Apply baseline, obtained new independent reviews and archived with 9 tests passing. Status covered empty, active, blocked and completed fixtures without changing files.

Gradle UT preserved an existing sentinel test: the first run passed 14 test executions, and the incremental run passed 31 across three classes with 100% line/branch coverage. A synthetic imported ordinary failure was retried under a new Worker ID; an imported timeout remained terminal and unretried. Those historical records are synthetic inputs, not claims of prior native execution or an actual one-hour wait. Incomplete classification remained unknown. The combined UT/Status trace contained 33 successful atomic publication receipts and no native direct-final-target write attempts. Main sources/build files/wrappers and existing tests stayed unchanged within each request.

The [UT precompile guard](../../scripts/ut_precompile.py) runs the build tool on every invocation. Default output validation is limited to standard root-project directories; active multi-module/custom directories must be verified by the parent and supplied explicitly using repeated `--class-directory` arguments. It does not recursively guess active modules or establish test success. Real Kotlin/Groovy compilation, Windows execution, TUI/desktop UI and a naturally elapsed one-hour worker timeout remain outside this acceptance. At that checkpoint, support remained Preview.

## 恢复与辅助流程补充验收（2026-11-23）

Python 全量 852 项：845 通过、7 跳过，新增 31 项恢复、预编译和只读回归。OpenCode 2.2.2、2.3.0、2.3.2 各通过 122 项 Bun 测试（435 断言），类型检查通过。

OpenCode 2.3.2 + DeepSeek 真实模型验证了只读仓库、Apply 原生取消后新会话恢复、部分 Archive 取消后跳过已归档仓库，以及原生 context hook 故障时阻断依赖、不降级内联。Active Single BugFix 复现注入的 Unicode 回归，保留原 Apply 基线，取得全新独立双轴评审并完成无提交归档，9 项测试通过。Status 的空仓、活跃、阻塞、已归档四种状态均未修改文件。

Gradle UT 保留既有哨兵测试：首轮 14 次测试执行通过，增量最终 31 次通过，三个类行/分支覆盖率均为 100%。合成导入的普通失败由新 Worker ID 重试成功，导入的 timeout 保持终态、不重试；这些历史工件不代表实际旧原生执行，也不代表实等一小时。分类不全时继续显示未知。UT/Status 合计观察到 33 次成功原子发布，无 native 直接写最终目标的尝试；每次请求内业务源码、构建文件、wrapper 与既有测试不变。

[UT 预编译门禁](../../scripts/ut_precompile.py) 每轮均调用构建工具；默认仅验证根项目标准输出目录，多模块/自定义目录须由父流程核实后通过重复的 `--class-directory` 显式传入。不递归猜测活动模块，不把编译就绪当测试通过。真实 Kotlin/Groovy 编译、Windows、TUI/桌面 UI 和自然经过一小时的 worker 超时仍未验收；当次验收时，支持等级保持 Preview。


## Explicit D2C evidence gate and entry preflight (2026-11-23)

The OpenCode adapter now exposes an [explicit D2C gate](../../hosts/opencode/runtime.md), reusing the canonical checkpoint/guard. A new request enters phase 0 and immediately claims it using the actual native session identity. Completion checks require the same owner, current invocation, healthy source/capture fingerprint, valid PNG, canonical measurements and the fenced JSON in the Markdown fidelity report. This is an explicit audit, not a native Stop interceptor or proof that screenshots came from an authenticated browser/MasterGo session. Audit limits are documented; large supported captures receive a capacity diagnostic rather than a visual-failure verdict.

Canonical marker/state writers hold one nonblocking lock from read through validation/persistence. Concurrent contenders can both receive busy and retry. Unique PID entries allow the next writer to recover from a killed process without deleting a new owner's lock. Dedicated regressions cover stale writers, simultaneous claims, explicit exits, SIGTERM/SIGKILL recovery, malformed images, zero-placeholder measurements and source-path confinement. Existing Cursor projection and canonical D2C tests remain required.

Mutation command entry observes allowlisted registered native tools and session-move API availability, blocking known missing read/shell before prompt admission. Unavailable metadata remains unknown; Status/Config do not depend on the lookup. Registration does not establish permissions or actual execution. Effective nesting depth remains unknown because the SDK has no direct accessor; native delegation admission and independent review remain mandatory. Skill result/mention identity binding changes only complete owned bodies, preserving surrounding literals and unrelated Skills.

OpenCode 2.2.2, 2.3.0 and 2.3.2 each passed 136 Bun tests, including 23 scripted native tests. OpenCode 2.3.2 + DeepSeek used its real native identity for a fresh owner claim and accepted a controlled completed synthetic checkpoint; missing screenshot and another native owner were rejected. A real mutation entry observed the catalogue and unknown depth, then stopped at the actual Router prerequisite blocker without creating business artifacts. These are real-model checks using synthetic evidence, not a complete design-to-code or TUI/desktop acceptance. At that checkpoint, support remained Preview.

## D2C 显式证据门禁与入口预检（2026-11-23）

OpenCode 适配器新增 [D2C 显式门禁](../../hosts/opencode/runtime.md)，复用 canonical checkpoint/guard。新请求先进入 phase 0，再用实际原生会话身份立即认领；完成检查校验同一 owner、本次调用、健康的源码/截图指纹、有效 PNG、canonical 测量和 Markdown 报告中的 fenced JSON。它是主动调用的审计，不是原生 Stop 拦截，也不能证明截图来自已认证浏览器/MasterGo。容量边界见运行说明；超大截图返回容量诊断，不作视觉失败结论。

canonical marker/state 写入从读取、校验到落盘使用同一非阻塞锁，并发竞争可双方均返回 busy 后重试。唯一 PID 条目支持后续写入恢复已终止进程的遗留，不删除新 owner 的锁。专项回归覆盖旧写入、并发认领、显式退出、SIGTERM/SIGKILL 恢复、损坏图片、空占位测量和源码路径边界；仍须运行原有 Cursor projection 与 canonical D2C 测试。

写入型命令入口只观察允许名单中的原生工具登记和会话移动 API；已知缺少 read/shell 时在 prompt 准入前阻断。元数据不可用保持未知，Status/Config 不依赖此查询。登记不等于权限或执行成功；SDK 无直接查询有效嵌套深度的接口，因此深度保持未知，继续保留原生委派准入和独立评审要求。Skill 结果/mention 仅绑定完整的本插件正文，不改外围字面值和其他 Skill。

OpenCode 2.2.2、2.3.0、2.3.2 各通过 136 项 Bun 测试，其中包含 23 项脚本原生测试。OpenCode 2.3.2 + DeepSeek 用真实原生身份完成新 owner 认领，并接受受控的合成完成检查点；缺失截图和另一原生 owner 均被拒绝。真实写入型入口取得工具登记和未知深度后，按 Router 的实际前置条件阻断，未创建业务工件。这些是真实模型消费合成证据的检查，不构成完整设计稿还原或 TUI/桌面验收；当次验收时，支持等级保持 Preview。


## Targeted terminal UI acceptance (2026-11-23)

OpenCode 2.3.2 loaded an independent plugin copy without installing development dependencies. A controlled standalone Git-root package passed native install, update, uniquely pinned rollback and removal, with business sentinel files preserved. Its Git transport used isolated rewrites to a local synthetic repository; this does not establish public GitHub transport or monorepo subdirectory installation. For this repository's nested plugin, use a checkout and configure its absolute package directory as described in the [installer](../../hosts/opencode/installer.md). The native CLI does not accept an absolute directory as `plugin add` input in this tested version.

A real OpenCode PTY terminal client with DeepSeek exercised native question selection, permission rejection, double-Escape interruption of a running shell command, reattachment with the same native session/history, the nine-command menu and readonly Status. Permission refusal left the target file absent; cancellation recorded native `interrupted=true`; Status preserved project file hashes. Terminal byte streams and reconstructed text states are interaction evidence, not desktop GUI or pixel-layout acceptance. Setup attempts that allowed a probe or let a command finish are retained separately and are not counted as refusal/cancellation passes.

The first empty Status receipt was correct, but its model response appended a filter suggestion absent from the receipt. The canonical command now separates empty and nonempty presentation: empty responses end after scope, no requirements and the no-rerun note. Three fresh isolated Git-root real-model cases (default query, completed query, and a terminal default query) followed that branch; a nonempty fixture retained its actual `next_command`. This is a prompt-contract mitigation, not deterministic final-output interception. At that checkpoint, support remained Preview; production projects, unattended reliability, complete terminal coverage and desktop GUI remain unverified.

OpenCode 2.3.2 在未安装开发依赖的情况下加载了独立插件副本。受控的独立 Git 根包通过原生安装、更新、唯一版本固定后的回退和卸载，业务哨兵文件保持不变。Git 传输使用隔离规则改写到本地合成仓库，不代表公开 GitHub 链路或本仓子目录安装通过。本仓嵌套插件采用 checkout 后配置绝对包目录的方式，见[安装说明](../../hosts/opencode/installer.md)；该已测版本的 native `plugin add` 不接受绝对目录。

真实 OpenCode PTY 终端客户端与 DeepSeek 验证了原生问答选择、权限拒绝、双 Escape 中断运行中 shell、同原生会话及历史重连、九命令菜单和只读 Status。拒绝后目标文件不存在，中断产生原生 `interrupted=true`，Status 项目文件哈希不变。终端字节流和重建文本只证明交互，不构成图形桌面或像素布局验收；准备阶段曾允许探针或正常结束命令，已单列保留，不计入拒绝/取消通过。

首次空 Status 回执正确，模型却追加了回执中没有的筛选建议。公共合同已拆分空/非空展示分支，空结果在查询范围、没有需求、未重跑说明后结束。三个全新独立 Git 根目录真实模型场景（默认查询、completed 查询、终端默认查询）均遵守该分支；非空样本仍保留真实 `next_command`。这属于指令合同缓解，不是确定性最终输出拦截。当次验收时支持等级保持 Preview，生产项目、无人干预可靠性、完整终端覆盖和图形桌面仍未验收。


## Existing Java project acceptance (2026-11-23)

A synthetic existing Java17/Maven library began with one business class and four sentinel tests. Real-model Init/Propose completed. Its deliberately unborn Git fixture correctly blocked review when the implementation overlapped the dirty baseline; that case remains a blocker, not an Apply pass. A fresh controlled copy restored all 34 original baseline files byte-for-byte and received an operator-created initial test commit and the same approved ready planning inputs, without old private Apply/Review state. Real-model Apply then passed seven tests and two independent native reviewer axes (both APPROVE); an external Java probe passed 65 assertions. Existing tests and pom.xml remained byte-identical. Archive confirmed and authorized five exact knowledge paths, archived without model commits/pushes, and the readonly completed gate returned archived with archive_check=passed. The reviewed code seal and initial fixture HEAD stayed unchanged through Archive.

This is a synthetic, multi-session controlled acceptance with normal product, Advisory and knowledge-scope choices. The clean copy's planning inputs and initial test history were fixture preparation, not a claim of an uninterrupted unattended production lifecycle. Production services, real Kotlin/Groovy/multi-module compilation, Windows and naturally elapsed one-hour timeouts remain outside this run.

合成 Java17/Maven 存量类库初始包含一个业务类和四个哨兵测试，真实模型完成 Init/Propose。刻意无 Git 初始历史的夹具因实现与脏基线重叠而正确阻断评审，该场景不计 Apply 通过。随后在全新受控副本中逐字节恢复 34 个原基线文件，由操作者建立仅限测试的初始提交并预置同一已确认 ready 规划输入，不复制旧 private Apply/Review 状态。真实 Apply 通过七个测试和两个独立原生评审轴（均 APPROVE），外部 Java 探针通过 65 个断言；原测试与 pom.xml 字节不变。Archive 按确认的五个精确知识路径先授权后更新，模型不提交/推送；只读 completed 门禁返回 archived、archive_check=passed，代码封存哈希与初始夹具 HEAD 保持不变。

这是含正常产品、Advisory、知识范围选择的多会话合成受控验收。clean 副本的规划输入与初始测试历史属于夹具准备，不代表连续无人干预生产流程。生产服务、真实 Kotlin/Groovy/多模块编译、Windows 及自然经过一小时的超时仍未覆盖。


## Helix-owned capability adapters (2026-11-23)

The [capability guide](../../hosts/opencode/capabilities.md) documents the new adapters: consented stage facts/workflow traces without native transcript collection, atomic progress updates over canonical tasks.md, bounded post-edit project linters, and confirmation-gated pinned fast-forward source updates. Project settings hooks and equivalent Stop/failure/session-end business hooks remain unsupported; idle does not finish a stage. Uncollected usage/execution remains unavailable, including after Archive.

Python regression coverage includes synthetic source updates through the entire check/prepare/decide/install authorization chain, stale/dirty/divergent/detached/no-upstream/version-mismatch rejection, disabled Git hooks, idempotent installation-step retry, task receipts/dependencies/path confinement, consent/session isolation, stage-only supplements and archive seals. The complete suite runs 910 tests: 903 pass and 7 skip. OpenCode 2.2.2, 2.3.0 and 2.3.2 each pass 146 Bun tests (515 assertions), including actual native edit feedback and consent-off → synthetic grant → stage start/finish; typecheck passes. The two older clients retain automated-compatibility status only.

A fresh OpenCode 2.3.2 + DeepSeek synthetic acceptance used the same native session to update task progress, observe an intentionally failing project check, repair the code and finish both tasks. Disabled telemetry created no ledger. After a harness-supplied synthetic consent grant, a new stage started and finished with the correct native session hash; the model did not modify consent. An unmanaged source copy returned a real update warning and was not changed. The session was archived and its isolated server stopped. This is targeted adapter acceptance, not a new full Init → Archive or production acceptance run, and it does not establish unattended model reliability. Actual source installation is covered by the synthetic Git authorization tests, not by updating the user's checkout.

本次补齐阶段统计、canonical 任务进度、编辑后巡检和确认后锁定 commit 的源码快进更新。全量 Python 910 项中 7 项跳过；三个 OpenCode 版本各通过 146 项 Bun 测试，类型检查通过。真实模型定向验收验证了任务更新、巡检失败修复、关闭统计时不建账本、合成授权后的同会话阶段收口，以及更新拒绝修改不受管理的副本。该授权仅属于隔离测试，不修改用户授权；未重新宣称完整工作流或生产验收。
