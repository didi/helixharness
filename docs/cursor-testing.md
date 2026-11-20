# Cursor support and verification / Cursor 支持与验证

## Release scope / 发布范围

Cursor desktop support is `stable` and release-ready within the documented limits. On 2026-11-20 the project owner accepted the remaining verification gaps for this release. Commands use `/helix-xxx`. Single-repository and Workspace workflows, unit-test generation, status/configuration and consent-based statistics are implemented. Managed Local automatic updates require a registered installation. Installation options are documented in the [installation guide](installation.md#cursor-preview).

Cursor 桌面接入按已声明的能力边界标记为 `stable`、可发布。项目负责人于 2026-11-20 接受本次剩余验证缺口。命令使用 `/helix-xxx`，已实现单仓与 Workspace 流程、单测生成、状态/配置管理及授权后统计。托管 Local 自动升级需要已登记的安装。安装方式见[安装指南](installation.md#cursor-preview)。

| Capability / 能力 | Evidence and boundary / 证据与边界 |
| --- | --- |
| Commands and workflows / 命令与流程 | Synthetic contract tests cover all nine entries. Native samples cover Single and Workspace implementation/archive, active Single BugFix, frontend Init and Java UT. / 合同测试覆盖九个入口；实机样例覆盖单仓及多仓实施/归档、活跃单仓 BugFix、前端 Init 和 Java UT。 |
| Recorded Local candidate / 已验 Local 候选 | 387 reviewed files matched the installed package. Two identity rounds and normal status/preflight on Python 3.9.6 passed. The unregistered Local updater returned the expected source warning. / 387 个审核文件与安装包匹配；两轮身份、Python 3.9.6 下的 status/预检及未登记 Local 更新警告通过。 |
| Native fixed source / 原生固定来源 | An earlier candidate was fetched by Cursor with 386 matching files; status, identity and partial statistics passed. / 较早候选由 Cursor 获取，386 文件匹配，status、身份及有限统计通过。 |
| Managed Local upgrade / 托管 Local 升级 | A synthetic 1.0.0 → 1.0.1 native upgrade and post-reload version recognition passed. Abnormal installation outcomes are covered by automated checks. / 合成版本原生升级及重载后识别通过，安装异常结果由自动化检查覆盖。 |
| Statistics and cleanup / 统计与清理 | Real tool events, one loopback acknowledgment, repeat-collection deduplication and consent-revocation cleanup passed in isolation. Usage remained partial. / 隔离环境真实工具事件、本机接收、重复采集去重与关闭授权清理通过，usage 为 partial。 |
| D2C | A synthetic local page and owner-bound checkpoint gate passed selected checks. The full MasterGo-to-delivery workflow remains unaccepted. / 合成本地页面与 owner checkpoint 门禁通过所选检查，完整 MasterGo 到交付流程未完整验收。 |

The 2026-11-20 pre-merge regression ran 727 plugin tests: 726 passed, and one process-inspection case was blocked by the sandbox's `ps` restriction; that case passed an authorized isolated retry. All 132 related platform tests passed. Cursor and Codex Skill projections and platform Ruff checks passed. These results cover the cleaned source; native candidate results above refer to earlier recorded packages. Native replies were partly supplied by the user; installed hashes and selected artifacts were independently checked.

2026-11-20 合并前回归运行 727 项插件测试，726 项通过，1 项因沙箱禁止 `ps` 而报错；该用例获准单独重跑后通过。相关平台测试 132 项全部通过，Cursor/Codex Skill 投影与平台 Ruff 检查通过。这些结果覆盖清理后的源码，上方实机结果对应此前记录的候选包。部分原生回复由用户提供，安装哈希及所选产物独立核验。

## Accepted limitations and follow-up / 已接受限制与跟进项

- Native fixed-SHA automatic upgrades, colon-prefix completion, Cloud Agents and native Skill-scoped stop/question interception are outside this release's capabilities. / 原生固定 SHA 自动升级、冒号补全、Cloud Agents 及原生 Skill 级停止/提问拦截不在本期能力范围。
- Token attribution can remain partial/unavailable. Full token precision, default background delivery and production statistics ingestion have not completed native acceptance. / Token 可为 partial/unavailable；完整精度、默认后台投递与生产统计入库未完成实机验收。
- Window-close cleanup is best effort. Explicit consent revocation cleans the current repository; later startup can retry scoped cleanup. / 关窗清理尽力执行，主动关闭授权清理当前仓库，后续启动可补扫。
- Fresh remote loading of the productized candidate, native update-entry behavior, invalid-source/recovery and the general marketplace UI flow remain accepted follow-up verification. / 产品化候选的新一轮远程加载、原生更新入口、错误来源/恢复及普通市场 UI 完整流程作为已接受的后续验证。
- Cursor Agent CLI, full D2C and all native cancellation/concurrency boundaries remain outside complete acceptance. / Cursor Agent CLI、完整 D2C 及全部原生取消/并发边界未完整验收。
- Mixed imported/Local copies can select different Skill and Hook roots. Resolve source conflicts without bypassing binding checks. Imported Claude hooks may duplicate checks. / 混合导入与 Local 副本可能选中不同 Skill/Hook 来源，需解决来源冲突并保留绑定检查；导入 Claude hooks 可能重复检查。

Historical failures remain recorded in local evidence. Earlier Hook timeouts, missing identity and empty-cwd tool accounting led to repairs and subsequent focused/native checks. Earlier upgrade runs also exposed a test-launcher lock issue and a model's failure-to-stop deviation; successful final installation does not erase those observations. Original-host historical diagnostics are summarized in the [Claude Code](claude-testing.md) and [Codex](codex-testing.md) reports.

历史失败保留在本地证据中。此前 Hook 超时、身份缺失和空 cwd 工具计数问题促成修复，并有后续定向/实机复验。升级历史还记录了测试 launcher 锁识别问题及模型未遵守失败停止的偏差，最终安装成功不会覆盖这些观察。原宿主历史诊断见 [Claude Code](claude-testing.md) 与 [Codex](codex-testing.md)。

## Reproduce / 复跑

Run from the combined repository root with Python, Git and Node.js. The process-timeout fixture needs permission to inspect its own child process. Use synthetic projects and keep raw runtime evidence in ignored `output/` directories.

在组合仓库根运行，需要 Python、Git 和 Node.js。进程超时夹具需能检查自身子进程。使用合成项目，将原始运行证据保存在忽略的 `output/` 中。

```bash
GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null \
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/dev/null \
python3 helixharness/scripts/run_tests.py

PYTHONPATH=helixharness/scripts:helixharness/scripts/tests \
python3 -m unittest test_cursor_command_scenarios test_cursor_lifecycle_scenarios \
  test_cursor_native_install test_cursor_native_update test_cursor_managed_update

python3 helixharness/scripts/cursor_skill_projection.py --check
python3 helixharness/scripts/codex_skill_projection.py --check
python3 helixharness/scripts/host_runtime_cli.py list
```

Automated contract tests supply synthetic workflow artifacts. They verify routing, persistence, isolation and gates; native model behavior and external service delivery require their own recorded evidence. Read the [Host API](../helixharness/reference/host-api.md) for the capability contract.

自动化合同测试提供合成流程产物，验证路由、持久状态、隔离与门禁；原生模型行为和外部投递需各自的实际证据。能力合同见 [Host API](../helixharness/reference/host-api.md)。
