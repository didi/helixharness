# Codex compatibility verification / Codex 兼容验证

## Scope and evidence / 范围与证据

Cursor integration shares workflow contracts, Init validation and update infrastructure with Codex. Recorded command, lifecycle, update and shared Init regression passed. The latest productization suite and accepted Cursor release scope are summarized in the [Cursor report](cursor-testing.md). Tests use synthetic repositories, artifacts, host outcomes and consent choices.

Cursor 接入与 Codex 共用流程合同、Init 校验和更新基础设施。已记录的命令、生命周期、更新及共享 Init 定向回归通过。最近产品化回归和 Cursor 发布范围见 [Cursor 报告](cursor-testing.md)。测试使用合成仓库、产物、宿主结果及授权选择。

Native Codex CLI 0.144.3 samples covered source resolution and entry loading. A later visible-notice enable/disable sequence completed with independent readback, while an initial unavailable readback and native error events remained unexplained. An earlier bounded status/config attempt reached its deadline before business execution. Those samples retain their limited scope and do not establish zero-anomaly native compatibility.

Codex CLI 0.144.3 的实机样例覆盖来源解析和入口加载。后续可见说明后的统计开启/关闭完成并有独立读回，但首次 unavailable 读回及原生错误事件未完成归因。更早的有界 status/config 尝试在业务执行前到达期限。各样例保留原范围，不据此认定全部原生交互无异常。

## Historical update findings / 历史更新问题

Independent probes reproduced two outcomes on both the current source at that time and the original baseline: the native updater returned success but left the old version installed, or installed a higher version than approved after its source advanced. The wrapper reported success and consumed authorization. Normal successful-update tests passed. The finding concerns validation of abnormal native outcomes.

独立探针在当时源码和原基线均复现两类结果：原生更新器返回成功但仍保留旧版，或来源推进后安装了高于批准版本的版本。包装脚本误报成功并消费授权，正常升级用例通过。问题在于异常原生结果的安装后校验。

The 2026-11-20 pre-merge review reran the probe against the current source: both abnormal update outcomes still failed. Standard-suite pass counts exclude this explicitly selected diagnostic. Reassess the current [Codex adapter](../helixharness/scripts/update_hosts/codex.py) and [shared updater](../helixharness/scripts/version_update.py) with the commands below. Installed-version checks must consider versioned-cache switching rather than assuming the executing old plugin directory has changed.

2026-11-20 合并前评审在当前源码重跑探针，两类异常升级结果仍失败；常规测试通过数量不包含这项显式诊断。通过下方命令复核当前 [Codex 适配器](../helixharness/scripts/update_hosts/codex.py) 和[公共更新器](../helixharness/scripts/version_update.py)。安装结果校验需考虑版本缓存切换，不能假定正在执行的旧插件目录已变为新版。

## Reproduce / 复跑

Run from the combined repository root. Use synthetic projects and keep raw runtime evidence in ignored `output/`. The deterministic workflow tests supply prepared Spec/Tasks/review receipts; they verify routing, persistence and gates. They do not prove native discovery, model implementation quality or complete UT worker behavior.

在组合仓库根运行，使用合成项目，原始运行证据保存在忽略的 `output/` 中。确定性流程测试预置 Spec/Tasks/评审收据，验证路由、持久状态和门禁，不证明原生发现、模型实现质量或完整 UT worker 行为。

```bash
PYTHONPATH=helixharness/scripts:helixharness/scripts/tests \
python3 -m unittest test_codex_command_scenarios test_codex_lifecycle_scenarios \
  test_codex_update_scenarios test_codex_local_update

# Independent historical failure probe / 独立历史失败探针
python3 helixharness/scripts/tests/test_codex_update_scenarios.py --probe-postconditions
```

The probe exits nonzero while its modeled failure remains. Full reproduction and environment requirements are in the [Cursor verification guide](cursor-testing.md#reproduce--复跑). See the [Codex installer](../helixharness/hosts/codex/installer.md) for normal installation.

探测问题仍存在时返回非零。插件全量复跑与环境要求见 [Cursor 验证说明](cursor-testing.md#reproduce--复跑)，正常安装见 [Codex 安装指南](../helixharness/hosts/codex/installer.md)。
