# OpenCode capability adapters / 能力适配

These are HelixHarness adapters over OpenCode 2.3.2. An unsupported adapter is not a claim that the host has no such APIs. Native tool/session hooks are already used. Session idle is not Stop, session end or stage completion.

这些能力由 HelixHarness 适配实现，不支持项不等于 OpenCode 没有相关 API。插件已使用原生工具和会话 Hook；会话空闲不代表 Stop、会话结束或阶段完成。

| Capability / 能力 | Implementation / 实现 | Boundary / 边界 |
|---|---|---|
| Task tracking / 任务跟踪 | `scripts/task_plan.py` reads and atomically updates the existing Feature `tasks.md`, with a content receipt and dependency validation. / 读取同一份任务表，凭内容回执校验并原子更新进度。 | Does not invent a second plan store, change the Spec, close a stage or replace verification. / 不增加第二份计划，不修改 Spec，不代替阶段验收。 |
| Telemetry / 交付统计 | Existing consent, stage ledger, workflow trace, asynchronous outbox and archive seal. / 复用授权、阶段账本、工作流轨迹、异步队列和归档封口。 | Stage facts only. No transcripts; usage/execution remain unknown. Consent never authorizes commit/push. / 仅阶段事实，不读取会话日志；未采集数据保持未知；授权不包含提交或推送。 |
| Project checks / 项目巡检 | Plugin `execute.after` runs existing `invariant-check` and `spec-sync-check` linters after successful native write/edit/patch, with bounded time and output. / 原生编辑成功后运行已有巡检，限制时间和输出。 | No `.opencode/` settings merge. Failed/non-edit tools do not trigger checks. Failures are feedback, not automatic stage failure or rollback. Shell changes still need explicit checks. / 不创建项目 Hook 设置；失败反馈不撤销编辑，也不自动改变阶段状态；shell 修改仍需显式检查。 |
| Source updates / 源码更新 | Existing `check → prepare → decide → install`; exact approved commit, clean attached checkout, explicit upstream, fast-forward only, hooks disabled and version/revision verification. / 复用确认链路，只更新干净且有 upstream 的分支，锁定批准 commit，仅快进并核验。 | No automatic update of dirty/detached/package-managed copies, no force/reset/stash/commit/push. Restart OpenCode afterwards. / 不自动处理本地改动、游离版本或包管理缓存，不重置、暂存、提交或推送；更新后重启。 |

## Plan operations / 计划操作

Run Python preflight first. Use the requesting session's repository and the routed Feature; do not guess another Feature. / 先执行 Python preflight，使用当前会话仓库和 Router 确定的 Feature。

```text
{helix_python} "{plugin_root}/scripts/task_plan.py" --repo-root "{repo_root}" --feature example show
{helix_python} "{plugin_root}/scripts/task_plan.py" --repo-root "{repo_root}" --feature example update --task T-01 --status in_progress --expected-receipt {receipt_sha256}
```

A stale receipt, unfinished dependency, ambiguous row, unsafe path or closed plan blocks mutation. A read-only `show` creates no state files. Existing Apply/Archive checks remain authoritative. / 旧回执、未完成依赖、歧义行、不安全路径或关闭的计划均阻断写入；show 不创建状态文件，Apply/Archive 门禁仍为最终依据。

## Identity and collection / 身份与采集

Invoke the installed plugin's telemetry scripts by absolute path through native `shell`. The plugin binds `HELIXHARNESS_SESSION_ID` and `HELIXHARNESS_SESSION_CWD` to that shell invocation from `session.get`, without changing the server environment. Missing/foreign identity skips collection. Direct external CLI callers must explicitly bind these same values from their own native session; an inherited other-host identity is rejected. / 通过原生 shell 以绝对路径调用本安装统计脚本；插件从 session.get 绑定本次进程的真实会话和目录，不修改服务器全局环境。身份缺失或不匹配时跳过。外部 CLI 调用者须显式绑定，不能借用其他宿主身份。

Consent is off by default. The adapter never reads native conversation storage. Stage-only supplements carry empty `usage`/`execution` objects and explicit unavailable conversation evidence, including after Archive. / 默认关闭统计；不读取原生会话存储，归档前后均保持未采集指标未知。

## Updating / 更新

Use `/helix:update --check` to inspect. Checks use version-policy.json releases; source changes without a version release do not trigger an upgrade. Checks may fetch the configured upstream into a temporary Git ref and inspect an isolated snapshot; they do not switch HEAD or alter source files. `/helix:update` requires the existing structured user confirmation before install. The approved target stays pinned if upstream advances. Local changes, divergence, changed branch/HEAD or target-version mismatch block installation. / 检查按 version-policy.json 的发布版本判断升级，同版本源码变化不触发升级；可拉取已配置 upstream 的临时引用并检查隔离快照，不切换 HEAD 或修改源码。安装前须按现有协议确认；上游后续变化不改变批准目标，本地修改、分叉、分支/HEAD 漂移或目标版本不符时停止。

Standalone package installations can still use OpenCode's own package management. This adapter deliberately does not infer their specifier or read private global configuration. / 独立包仍可使用 OpenCode 自身的包管理；本适配不猜测包标识，也不读取私有全局配置。
