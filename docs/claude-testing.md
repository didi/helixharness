# Claude Code compatibility verification / Claude Code 兼容验证

## Scope and evidence / 范围与证据

Cursor integration shares command contracts, Init output validation and update infrastructure with Claude Code. Focused command, lifecycle, update and shared Init regression passed in the recorded runs. The latest productization suite and accepted Cursor release scope are summarized in the [Cursor report](cursor-testing.md). Historical source versions and overlapping test counts are kept in local evidence.

Cursor 接入与 Claude Code 共用命令合同、Init 产物校验和更新基础设施。已记录的命令、生命周期、更新及共享 Init 定向回归通过。最近产品化测试及 Cursor 发布范围见 [Cursor 报告](cursor-testing.md)。历史源码版本与重叠测试数量保留在本地证据。

Native samples on Claude Code 2.1.252 covered plugin discovery, selected status/config execution and visible-notice enable/disable. Earlier isolated Hook tests and authenticated command tests used different execution setups, so those samples do not establish a complete Hook-to-model chain. Some earlier interaction attempts timed out or stopped at `notice_required`; later successful samples retain that history.

Claude Code 2.1.252 的实机样例覆盖插件发现、所选 status/config 执行、可见说明后的统计开启/关闭。早期隔离 Hook 与已认证命令使用不同执行配置，不能据此认定完整 Hook 到模型链路通过。部分早期交互超时或停在 `notice_required`，后续成功样例保留这些历史结果。

## Historical diagnostic findings / 历史诊断问题

- Update probes found that a synthetic host returning success without installing the approved version, or installing a later version, could produce a success result and consume authorization. Both outcomes also reproduced on the original baseline. / 更新探测发现，合成宿主返回成功却未安装批准版本、或实际安装更高版本时，包装脚本可能误报成功并消费授权；两种情况也在原基线复现。
- A synthetic native usage probe found omitted cache-read/write fields and an incorrectly precise total. Cache semantics must be normalized in the Claude adapter because other hosts may already include cache in input totals. / 合成原生用量探测发现缓存读写字段遗漏并错误标记精确总量；应在 Claude 适配层归一化缓存语义，其他宿主的 input 可能已包含缓存。

These are retained historical findings, not new Cursor defects. The 2026-11-20 pre-merge review reran both independent probes: the two abnormal update outcomes still failed, and the native cache-usage probe still omitted cache fields on both the current parser and the original baseline. These explicitly selected diagnostics are not included in standard-suite pass counts.

这些是保留的历史发现，不是 Cursor 新引入缺陷。2026-11-20 合并前评审重跑两项独立探针：两类异常升级结果仍失败，原生缓存用量探针在当前解析器和原基线仍遗漏缓存字段。这些显式诊断不包含在常规测试通过数量中。

## Reproduce / 复跑

Run from the combined repository root. Use synthetic projects and keep runtime evidence in ignored `output/` directories. Native testing should authenticate normally without copying credentials. Synthetic routing/state tests do not establish model-generated code quality or every native interaction path.

在组合仓库根执行，使用合成项目，将运行证据保存在忽略的 `output/` 中。原生测试通过正常机制认证，不复制凭据。合成路由/状态测试不证明模型生成质量或全部原生交互路径。

```bash
PYTHONPATH=helixharness/scripts:helixharness/scripts/tests \
python3 -m unittest test_claude_command_scenarios test_claude_lifecycle_scenarios \
  test_claude_update_scenarios test_claude_local_update

# Independent historical failure probes / 独立历史失败探针
python3 helixharness/scripts/tests/test_claude_update_scenarios.py --probe-postconditions
python3 helixharness/scripts/tests/test_claude_update_scenarios.py --probe-postconditions --baseline
PYTHONPATH=helixharness/scripts:helixharness/scripts/tests \
python3 helixharness/scripts/tests/test_claude_lifecycle_scenarios.py --probe-native-cache
```

The probes exit nonzero while their modeled failure remains. Full plugin reproduction and environment requirements are in the [Cursor verification guide](cursor-testing.md#reproduce--复跑). See the [Claude Code installer](../helixharness/hosts/claude-code/installer.md) for normal installation.

探测问题仍存在时返回非零。插件全量复跑与环境要求见 [Cursor 验证说明](cursor-testing.md#reproduce--复跑)，正常安装见 [Claude Code 安装指南](../helixharness/hosts/claude-code/installer.md)。
