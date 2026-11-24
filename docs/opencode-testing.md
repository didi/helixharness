# OpenCode support and verification / OpenCode 支持与验证

## Release decision / 发布结论

The 2026-11-23 maintainer-directed assessment classifies the current source candidate as **Stable within the scope below**. The primary verified environment is macOS 15.7.1 arm64 with OpenCode **2.3.2**, local CLI/server and selected PTY interactions. This is a scoped integration-support decision based on the recorded acceptance, not a claim that every model, operating system or UI path is accepted. The candidate includes local uncommitted fixes; this decision does not publish a release, tag or remote commit.

2026-11-23 按维护者授权完成支持范围评定：当前源码候选在下列范围内为 **Stable（稳定支持）**。主要已验环境为 macOS 15.7.1 arm64、OpenCode **2.3.2**、本地 CLI/server 与所列 PTY 交互。这是根据已有验收作出的集成支持声明，不代表任意模型、平台和 UI 路径均已验收。候选包含本地未提交修复；本次评定不创建发布、Tag 或远端提交。

OpenCode 2.2.2 and 2.3.0 passed automated native-server compatibility tests. Those results are retained separately; they do not extend the primary real-model/installation Stable envelope to every version from 2.2.2 onward. Unlisted versions require compatibility checks before that envelope is extended.

OpenCode 2.2.2、2.3.0 已通过自动化原生服务兼容检查；该证据单独保留，不将主要真实模型/安装验收范围外推为“2.2.2 起所有版本均 Stable”。未列版本须先验证兼容性，再扩展支持矩阵。

## Supported scope / 支持矩阵

| Area / 范围 | Supported behavior and evidence / 已验行为与证据 | Boundary / 边界 |
| --- | --- | --- |
| Environment / 环境 | macOS 15.7.1 arm64, OpenCode 2.3.2, Python 3.10+ contract (actual runs: 3.14.3), Git; JVM samples used Java 17. / Python 合同为 3.10+，已记录执行版本为 3.14.3，JVM 样本使用 Java 17。 | No native Windows/Linux, desktop GUI or unlisted-version acceptance. / Windows、Linux 原生会话、桌面图形与未列版本未验收。 |
| Installation / 安装 | A checked-out nested plugin configured by its absolute package directory loads without runtime npm dependencies. Independent copies resolve nine commands, eight method Skills and seventeen roles to their installed roots. / checkout 后配置绝对包目录，无运行时 npm 依赖；独立副本的命令、Skill、角色及路径绑定通过。 | This repository's monorepo Git subdirectory spec and native CLI add of an absolute directory are unsupported on the tested build; use the [installation guide](installation.md). / 已测版本采用安装指南中的目录配置路径。 |
| Single workflows / 单仓流程 | Recorded synthetic real-model Init, Propose, Apply, Archive, active Single BugFix, readonly Status and independent native review. Existing Java tests/build files remain protected. / 合成真实模型覆盖核心阶段、活跃 BugFix、只读 Status 和独立评审，并核验既有测试/构建文件。 | Normal product, task, Advisory and knowledge-scope choices remain interactive. This is not an unattended-production reliability guarantee. / 正常产品、任务、Advisory 和知识范围决策仍需交互，不保证无人干预生产可靠性。 |
| Workspace / 多仓 | Synthetic two-repository workflows, actual native ownership-root moves, readonly repositories, dependency blocking, Apply cancellation/fresh-session recovery and partial Archive recovery are recorded. / 双仓流程、原生切仓、只读仓库、依赖阻断与定向恢复已记录。 | Workspace Apply requires effective nested delegation depth at least 2 and actual admission of independent leaf reviewers. Unknown depth is not a pass; blockers must not trigger inline/self-review fallback. / depth 至少 2，须实际准入；未知不算通过，不内联或自审。 |
| Unit tests / 单测 | Java 17 Maven/Gradle samples, existing tests, incremental precompile, structured classification and single-file atomic publication were checked. / Java 17 Maven/Gradle、既有测试、增量预编译、结构化分类与单文件原子发布已验证。 | Default class discovery is root-project-only; custom/multi-module paths require explicit verification. Real Kotlin/Groovy compilation and a naturally elapsed one-hour timeout remain unaccepted. Native write/edit guards are not a shell sandbox or multi-file transaction. / 不承诺自动发现多模块；未验真实 Kotlin/Groovy、一小时自然超时或多文件事务。 |
| Native surfaces / 原生入口 | Nine commands, eight method Skills and seventeen roles are registered. Direct mentions, Skill activation, cross-session binding, resource confinement and prerequisite failures have automated native coverage. / 九命令、八方法 Skill、十七角色，原生 mention/激活、跨会话绑定和失败阻断有自动化覆盖。 | Config still needs separate full interaction acceptance; source update, stage telemetry and task adapters have their own scoped checks. / Config 完整设置交互另需验收；源码更新、阶段统计和任务适配分别有专项验证。 |
| Targeted PTY / 定向终端 | Native question choice, permission Reject, double-Escape shell interruption, session-history reattachment, command menu and Status were exercised through a real PTY client. / 真实 PTY 验证问答、拒绝权限、双 Escape 中断、历史重连、菜单与 Status。 | Terminal byte streams/reconstructed text are interaction evidence, not pixel screenshots or full desktop/TUI coverage. / 文本重建仅证明交互，不是像素或完整图形界面验收。 |
| Models / 模型 | Recorded real-model runs used DeepSeek with tool support; other native tests used a loopback scripted model. / 真实模型使用支持工具调用的 DeepSeek，其余原生回归使用回环脚本模型。 | Arbitrary provider/model behavior is not guaranteed. A correct script receipt can still be misdescribed by a model; Status has a closed empty-output contract but no final-text interceptor. / 不保证任意模型行为；Status 展示受合同约束，不是最终文本硬拦截。 |

## Assessment gates and evidence / 评定门槛与依据

The scoped decision requires: (1) native registration/binding and confinement checks pass; (2) core workflow and independent-review acceptance is recorded; (3) selected failure/cancellation paths fail closed and preserve completed work; (4) the documented installation path works independently of the development checkout; (5) current relevant regressions pass; (6) unsupported and unverified capabilities remain explicit. No unresolved high/medium finding in the recorded reviews was waived by renaming it an unsupported feature.

本次门槛为：原生注册/绑定/边界检查通过；核心流程和独立评审有记录；所列故障/取消场景保留完成进度并阻断后续错误执行；文档安装路径可脱离开发工作树运行；当前相关回归通过；不支持和未验收项保持明确。没有将已记录审查中未解决的高/中等级问题改名为“不支持”而放行。

Public summaries are in the [adapter verification record](../helixharness/opencode/tests/README.md), especially [recovery and auxiliary acceptance](../helixharness/opencode/tests/README.md#recovery-and-auxiliary-workflow-acceptance-2026-11-23), [targeted terminal acceptance](../helixharness/opencode/tests/README.md#targeted-terminal-ui-acceptance-2026-11-23), and [existing Java-project acceptance](../helixharness/opencode/tests/README.md#existing-java-project-acceptance-2026-11-23). Raw synthetic traces remain local and ignored; no credentials, session transcripts or runtime dumps are published here. The manifest's optional machine-E2E receipt remains null; this document is a scoped support assessment, not a fabricated new machine receipt.

公开摘要见上述适配验收记录及三个专项章节。原始合成轨迹只保存在本地忽略目录，不在此发布凭据、会话正文或运行时转储。manifest 的可选机器 E2E receipt 保持 null；本文是支持范围评定，不伪装为新生成的机器验收回执。

Historical checkpoints include 888 Python cases (881 passed, seven skipped), and 136 Bun tests / 483 assertions on each of 2.2.2, 2.3.0 and 2.3.2. The final delivery follow-up reran 136 native-enabled Bun tests on 2.3.2, 18 readonly/user-command tests and 34 host/reference tests, plus typecheck and links. These suites overlap and must not be added together. The release-declaration change receives fresh related checks; it does not relabel old logs as newly executed full suites.

历史检查点包括 Python 888 项（881 通过、7 跳过），三个版本各 136 项 Bun 测试/483 次断言。交付收尾复跑 2.3.2 的 136 项原生开启 Bun 测试、18 项只读/命令及 34 项宿主/引用检查，另有类型与链接检查。检查有重叠，不累加；本次声明调整另跑相关检查，不把旧日志写成新全量。

## Explicit exclusions / 明确排除

- [Helix-owned capability adapters](../helixharness/hosts/opencode/capabilities.md) cover consented stage facts, canonical task progress, plugin-level edit checks and consented pinned fast-forward source updates. Native project settings hooks and equivalent Stop/failure/session-end business hooks remain unsupported. / 已补齐授权阶段统计、canonical 任务进度、插件编辑后巡检和确认后锁定 commit 的源码快进更新；原生项目设置 Hook 和等价 Stop/失败/会话结束业务 Hook 仍未提供。
- Full D2C/MasterGo/browser provenance and visual-fidelity acceptance are outside this Stable scope. Existing explicit evidence gates remain available with their documented audit limits; remaining D2C work stays paused. / 完整 D2C、浏览器来源认证与视觉还原不在本次 Stable 范围，既有门禁保留，剩余工作暂停。
- Controlled standalone Git-package install/update/rollback used local synthetic Git transport. Public GitHub transport, arbitrary registries, marketplace GUI and every source-conflict combination are not accepted. / 独立根包 Git 验收使用本地受控传输，不外推公开网络、市场 GUI 或任意来源冲突。
- Existing Java acceptance used controlled synthetic planning inputs and a clean copy after an unborn-Git dirty-baseline blocker. It is not an uninterrupted unattended production run. / 存量 Java 验收包含夹具准备、正常决策及干净副本，不是连续无人干预生产运行。

## Reassessment rules / 后续升级与降级

Reassess before expanding operating systems, OpenCode versions, model claims, build layouts, distribution channels or unsupported capabilities. A reproducible break in the supported scope's repository/session binding, readonly ownership, independent-review admission, publication integrity or completed-work recovery blocks release until repaired or explicitly removed from the support scope. Feature-specific evidence gaps outside the matrix do not silently broaden the Stable promise. Preserve failed attempts and distinguish test-environment repairs from product fixes.

扩展平台、OpenCode 版本、模型声明、构建布局、分发渠道或原不支持能力前重新评定。已支持范围内若复现目录/会话绑定、只读所有权、独立评审准入、发布完整性或完成进度恢复缺陷，应阻断发布，修复后验证或明确收缩支持范围。矩阵外的缺口不自动扩大 Stable 承诺；保留失败记录，区分夹具排错与产品修复。


## Declaration-check rerun / 声明检查复跑

The 2026-11-23 declaration change passed 11 OpenCode Host Kit tests, 20 Host Registry tests, 136 native-enabled Bun tests on OpenCode 2.3.2 (483 assertions), and typecheck. Business runtime and capability flags were unchanged. These are new declaration/compatibility checks, not new paid-model lifecycle acceptance.

本次声明变更复跑通过 11 项 OpenCode Host Kit、20 项 Host Registry、OpenCode 2.3.2 下的 136 项原生开启 Bun 测试（483 次断言）及类型检查；业务运行代码与能力标志未改。这是新的声明/兼容检查，不是新增付费模型全流程验收。

From repository root with Python 3.10+ / 在仓库根目录运行：

```bash
PYTHONPATH=helixharness/scripts python3 -m unittest discover -s helixharness/scripts/tests -p test_opencode_host.py
PYTHONPATH=helixharness/scripts python3 -m unittest discover -s helixharness/scripts/tests -p test_host_runtime_registry.py
```

Then from the plugin directory with development dependencies available / 然后在插件目录、具备开发检查依赖时运行：

```bash
cd helixharness
HELIXHARNESS_OPENCODE_BIN=/absolute/path/to/opencode-2.3.2 HELIXHARNESS_OPENCODE_E2E=1 bun test opencode/tests
bun run typecheck
```

Native fixtures use isolated homes and scripted loopback models and clean their own services. Use the declared runtime version; checking a different version does not automatically expand this Stable matrix. / 原生夹具隔离 home、使用回环脚本模型并清理自有服务；使用已声明的宿主版本，其他版本的单次检查不会自动扩展本矩阵。
