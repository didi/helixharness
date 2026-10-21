---
feature: {feature-name}
spec_receipt_sha256: {reviewed-spec-receipt-sha256}
tasks_receipt_sha256: {reviewed-tasks-receipt-sha256}
review_fixed_point: {resolved-full-fixed-point-commit-id}
review_head: {review-head-commit-id}
review_scope_sha256: {frozen-review-bundle-sha256}
review_mode: parallel_subagents
standards_verdict: APPROVE
spec_verdict: APPROVE
must_fix_rounds: {任意非负整数}
stage_trace:
  propose: completed
  apply: completed
  archive: pending
---

# {feature-name} · Evidence

Repair revision: `{none|R-NN}`

{有 repair revision 时记录最新修复 Tasks 与旧 Review seal 已失效；本报告只保留最新验证和复审事实。}

## AC → Task → Result

| AC | Task | Result | Evidence |
|---|---|---|---|
| AC-01 | T-01 | {PASS|MANUAL} | {实际测试或人工验证引用} |

## TDD Evidence

| Task | AC | Test | Test File | RED | GREEN |
|---|---|---|---|---|---|
| T-01 | AC-01 | {failing test name} | {仓库相对测试文件路径} | {failing output summary} | {passing output summary} |

## Verification

| Command | Exit Code | Result | Evidence |
|---|---|---|---|
| {实际执行命令} | {0|N/A} | {PASS|MANUAL} | {执行时点与结果摘要} |

## Review

Fixed point: `{resolved-full-fixed-point-commit-id}`

Review HEAD: `{review-head-commit-id}`

Review scope: `{frozen-review-bundle-sha256}`

Reviewed paths: `{review-scope changed_paths}`

Excluded pre-existing paths: `{review-scope excluded_preexisting_paths}`

### Standards

Verdict: `APPROVE`

{Standards sub-agent 原始或轻度清理后的分轴报告；不得与 Spec findings 合并或重排。}

### Spec

Verdict: `APPROVE`

{Spec sub-agent 原始或轻度清理后的分轴报告；不得与 Standards findings 合并或重排。}

### Advisory Resolution

Choice: `{none|keep_unfixed|fix_all|fix_selected}`

Advisory remediation rounds: `{advisory_remediation_rounds，任意非负整数}`

{稳定 Advisory ID、用户授权范围、保留或修复状态，以及修复后的验证和完整双轴复审证据。无 Advisory 时写 none。}

## 前端视觉还原验证

{可选：仅 spec 已声明视觉验收的项目填写；未声明时删除本节。}

| UI Task | d2c 命令 | Hard Violation | Overlay | 豁免记录 |
|---|---|---|---|---|
| {T-NN} | {d2c 命令摘要} | {0|N} | {存在/不存在} | {无|豁免边界说明} |
