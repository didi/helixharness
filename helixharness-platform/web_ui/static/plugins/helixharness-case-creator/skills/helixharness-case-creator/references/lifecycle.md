# Scenario v2 Case 生命周期

1. 发现 Git 历史候选并冻结 commit 区间。
2. 在 staging 生成 Scenario v2 Source。
3. 运行只读 Preflight 和目标状态冲突检查。
4. 经明确授权后原子发布到 `case-sources-v2`，写入 `evaluation.db` 当前版本。
5. 对冻结 Base/Solution 执行确定性校准。
6. 状态达到 `PUBLISHED + CALIBRATED` 后，才进入正式评测 Case 选择列表。
7. 用同一 `source_hash` 发起低并发正式 canary Run，单独验证工作流、Verify、Review/Archive 和评分闭环。

不同 source hash 的更新会产生新版本并使当前校准失效；旧版本只作为不可变 Run 快照的一部分存在，不作为兼容入口。正式 Run 的双方工作流命令、阶段产物来源、Judge 和统一评分规则均在创建时冻结。

校准状态和正式闭环证据必须分开：`CALIBRATED` 是确定性校准结果，`FORMAL_RUN_COMPLETED` 才表示当前 Source 曾完整跑通。Candidate 内容或冻结目录集合变化后，重新生成 integrity bundle，不能复用旧摘要。
