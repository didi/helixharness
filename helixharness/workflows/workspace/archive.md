---
description: "Workspace Archive：逆依赖调度各仓完整 Single Single Archive，最后归档根概要设计。"
---

# Workspace Archive · Finalize

仅当公共 Router 返回 `execution_scope=workspace` 时执行本工作流。各 repo 独立归档自己的三工件；Workspace 根最后只归档一份概要设计 `spec.md`。

Workspace 根不维护独立 HelixHarness Ledger；各 repo 的统计由各自完整 Single Archive 收口。根 feature 已 completed 时公共 Router 直接返回 `feature_already_archived`，不得借用单仓 `lite_archive_telemetry_recovery` 重启业务或统计。

第一次派发子仓前，必须完整读取并执行 `reference/workspace-repo-dispatch.md`、`reference/subagent-runtime-protocol.md` 与当前 Host Context 的 `resources.subagent_protocol`。`workspace-plan` 返回的结构化 RepoJob 是本流程的 Dispatch Contract；必须通过 Host Kit 的 `spawn_job` / `wait_job` 映射实际派发、校验 Router identity、等待原生终态并执行父级 completion check，不能把 slash-command 字符串当 shell 命令或仅展示给用户。

## 1. 聚合门禁与计划

执行：

```bash
python3 "{plugin_root}/scripts/workspace.py" --workspace-root "{workspace_root}" workspace-archive-preflight --feature "{feature}" --review-choice
python3 "{plugin_root}/scripts/workspace.py" --workspace-root "{workspace_root}" workspace-plan --feature "{feature}" --command archive --review-choice
```

门禁要求根 Spec 为 `implemented|completed`，且每个子仓都满足 Single Archive preflight，或已经存在身份一致的 completed 三工件。根仍为 `implemented` 时，`--review-choice` 允许将仅有评审后内容变化的子仓派发到 Single Archive 处理用户选择；聚合结果会返回 `ready=false`、`decision_needed=review_scope_changed` 和 `pending_review_repos`，这只允许继续派发，不能据此移动根目录。其他门禁失败仍阻断。各子仓展示 1～3 条变更摘要并等待明确选择，Loop 和提交／推送授权均不能代替直接归档确认。计划按 repo 依赖的逆序返回，避免先归档仍被消费者校验依赖的上游仓库。

生成 `workspace-plan --command archive` 时，必须从本轮 `workflow_arguments` 提取显式 `--commit` / `--no-commit` 并原样追加到上面的计划命令；没有 flag 时不追加。计划脚本把选择写入每个 RepoJob 的 `arguments` 与 `command`，不得在派发时剥离或用默认值覆盖。

`--no-commit` 由各子仓按 Single Archive 处理：将提交决定延后到归档后的最终选择，本身不授权 commit 或 push。已 completed 的子仓仍只校验并跳过，不补交历史文件。

## 2. 调度完整 Single Archive

对每个尚未完成的 repo，按 RepoJob 的 `ownership_root` 和 RepoJob Dispatch Contract 执行完整公共命令：

```text
{RepoJob.command}
```

子仓公共 Router 必须选择 `workflows/single/archive.md`。实现提交选择、精确 Apply Scope、知识授权、Git operation 检查、三工件移动、telemetry ledger 与 push 事实全部由该子仓自己的 Single Archive 负责。Coordinator 可以按 repo 分组汇总需要用户作出的同类选择，但授权边界仍是每个 repo 的精确 scope；不得跨仓合并 commit、使用 `git add -A` 或把某个 repo 的授权扩展到其他 repo。

本轮执行 Single Archive 的子仓必须处理其“最终交付选择”，不因统计关闭、记账失败或 Ledger 已提交而省略推送选项。子仓已 move 但交付选择尚未回答时仍返回 `needs_input`；父 Coordinator 按当前 Host Kit 取得真实回答后续跑同一个 job 的交付段，不因 completed 目录已存在而跳过这次待回答选择。只有选择已处理或明确取消交付后，子任务才返回业务归档完成及真实交付结果；用户暂不提交／推送和交付操作失败不回滚业务归档。

每个子仓 move 后立即用 completed preflight 校验。中断重跑时，已 completed 的 repo 只校验并跳过；未完成 repo 从自身 Archive 状态继续。失败不回滚已经完成的 repo，也不提前移动 Workspace 根 Spec。

## 3. 最后归档 Workspace 概要设计

只有所有子仓都已 completed，且本轮派发的 job 均返回已验证终态、没有待回答的最终交付选择时，才把 Workspace Spec 状态更新为 `completed`，再次运行不带 `--review-choice` 的 `workspace-archive-preflight`，要求 `ready=true` 并确认 finalizing 状态，然后原子移动：

```text
{workspace_root}/docs/specs/active/{feature}/
→ {workspace_root}/docs/specs/completed/{feature}/
```

移动后执行：

```bash
python3 "{plugin_root}/scripts/workspace.py" --workspace-root "{workspace_root}" workspace-completed-preflight --feature "{feature}"
```

completed 根目录仍必须只包含 `spec.md`。Workspace 根不是 Git repo 时，不伪造 commit、HEAD 或 push；如实汇总各 repo 的真实归档与交付状态即可。

本流程不生成根 `tasks.md`、根 `verify-report.md`、`repo-plan.yaml` 或任何额外状态工件。根 Spec 的最终 move 与子仓 completed receipts 共同构成 Workspace Archive 的恢复事实。
