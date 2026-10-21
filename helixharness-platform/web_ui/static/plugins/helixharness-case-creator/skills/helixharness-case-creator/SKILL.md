---
name: helixharness-case-creator
description: 从 Git 历史生成 HelixHarness Platform Scenario v2 REPOSITORY_CASE，执行只读 Preflight 与冲突检查，并在明确授权后发布和确定性校准。
---

# HelixHarness Case Creator

## 目标

从一个或多个真实 Git 历史变更生成与工作流无关、可复现的 Scenario v2 Case。默认只发现、生成到 staging、校验和给出计划；不得默认覆盖 Case、发布、校准或创建评测 Run。

只有 `publication_status=PUBLISHED` 且 `calibration_state=CALIBRATED` 才能报告“Case 可用于正式评测”。Base/Candidate 命令、阶段产物来源和统一评分规则都由 Run 冻结，不写入 Case。

## 必须遵守

1. 读取目标仓库 `AGENTS.md`、平台开发说明和 [case-contract.md](references/case-contract.md)。
2. 候选必须来自 Git commit；忽略未提交内容、符号链接目标、凭据、`.env` 和缓存。
3. 默认只在目标 `case-sources-v2` 之外的 staging 生成；不得覆盖现有目录。
4. 不同内容更新必须得到用户对具体 `case_id` 的明确授权，并说明需要重新校准。
5. 每个 Case 独立报告；一个失败不得隐式修改其他 Case。
6. 不伪造 commit、测试结果、SHA256、校准状态或业务事实；证据不足标记 `NEEDS_REVIEW`。
7. 行为测试优先于源码文本/字段存在检查；F2P 要求 Base 失败、Solution 通过，P2P 要求两侧通过。
8. 代码改动为 0 行，或只有需求/归档资料变化的候选不得生成 Case。
9. `CALIBRATED` 只表示当前 Source 的确定性校准通过；只有同一 `source_hash` 的正式 Run 完整闭环，才能报告工作流可闭环。
10. 不得要求 Candidate 复跑只存在于隐藏 Solution 中的历史测试；强制测试必须在 Base 可见证据中有可枚举身份和命令。
11. Initial Request 若引用 Base/Solution、F2P/P2P、hidden patch 或 JUnit 计数，必须声明候选可见性边界：这些由平台独立校准，不是 Candidate 的 Review/Verify/Archive 前置证据。
12. Agent 开始前必需的构建隔离或工具链兼容修复必须显式冻结：单仓使用 `workspace_fixture_patch`，多仓使用 `workspace_path` 下的 Workspace 执行资产；不得藏在工作流后才注入的 hidden patch 中。
13. 单仓 Case 保持旧 `repository` 契约；只有需求明确跨两个及以上仓库、且每个仓库都有冻结 Commit 证据时，才生成 `MULTI_REPOSITORY`。不得仅凭依赖关系把只读依赖仓库加入多仓 Case。

## 工作流

### 1. 发现与冻结

先运行 `scripts/discover_repositories.py`，按当前归档合同检查 `spec.md`、`tasks.md`、`verify-report.md` 三份文档，再人工选择有可靠历史需求和行为证据的候选。若同一归档需求在多个仓库均有实现变更，先按 feature/需求证据归组，并逐仓确认是 `WRITE` 还是仅 `READ`。用 `git rev-parse --verify <ref>^{commit}`、`git merge-base --is-ancestor` 和真实源码 diff 冻结每个仓库的 Base/Solution 40 位 commit；计算逐仓 binary full-index diff SHA256。

### 2. 生成 Scenario v2 Source

每个 Case 使用稳定 ID `<repository-id>-<feature-slug>`，按 [case-contract.md](references/case-contract.md) 生成：

```text
initial-request.md
clarification-policy.json
provenance.json
gold.json
hidden-tests.patch
workspace-fixture.patch  # 仅在 Agent 前需要构建/工具链基线时
case.json
```

多仓 Case 额外用 `workspace/helixharness.workspace.yaml` 冻结 HelixHarness Workspace 注册表；每个仓库使用独立子目录、独立 Commit 区间和独立 Git 基线。`execution_tests[].working_directory` 明确测试在哪个仓库运行，`source_evidence[].repository_id` 和 `hidden_test_patches[].repository_id` 明确证据及隐藏补丁归属。多仓当前不使用单仓的 `workspace_fixture_patch`；需要的 Workspace 配置放入 `workspace_path` 声明的冻结执行资产。

Case 只保存业务场景、冻结仓库区间、澄清答案和确定性业务校验；不得写入 Profile、工作流命令、阶段产物来源或评分轴。

澄清策略必须冻结可追溯的具体业务事实和边界答案。Review、归档、commit、是否继续等流程选择属于 Run 执行策略，不得伪装成业务事实。空壳、同义复述 initial request、无法定位证据的事实均标记 `NEEDS_REVIEW`。

### 3. Case 质量门禁

按 [portability-and-regression.md](references/portability-and-regression.md) 审计生成结果：

```bash
python3 <skill-dir>/scripts/audit_case_quality.py \
  --case-dir <staged-case> --repository <repository-id>=<source-repo>

# 多仓：每个仓库重复绑定
python3 <skill-dir>/scripts/audit_case_quality.py \
  --case-dir <staged-case> \
  --repository <repo-a-id>=<repo-a-checkout> \
  --repository <repo-b-id>=<repo-b-checkout>
```

必须消除所有 `BLOCKER`；`RISK` 必须逐项用冻结契约或重放证据解释。隐藏测试只验证可观察行为，不绑定未冻结的私有字段名、构造器形态、注入方式、注解或等价 accessor 名。P2P 必须独立构建和运行，不得因 F2P 专用源码或 stub 失败。若 Agent 在注入 hidden tests 前就会因 Base 构建基线失败，先用只触及构建/工具配置的 `workspace_fixture_patch` 修复可见基线，再保持 hidden patch 仅包含隐藏断言。

至少重放三种实现：pristine Base（F2P 失败、P2P 通过）、历史 Solution（全部通过）、一个与历史实现结构不同但业务等价的替代实现（全部通过）。无法构造替代实现时标记 `NEEDS_REVIEW`，不得把历史 Solution 的实现细节固化为唯一答案。

### 4. 只读 Preflight 与冲突保护

```bash
python3 <skill-dir>/scripts/preflight_case.py \
  --platform-root <helixharness-platform> --case-dir <staged-case> --repository <repository-id>=<source-repo>

python3 <skill-dir>/scripts/case_guard.py \
  --platform-root <helixharness-platform> --case-dir <staged-case> --repository <repository-id>=<source-repo> \
  --database <target-evaluation.db> --case-source-root <target-case-sources-v2> \
  --repository-registry <target-registry.json>
```

单仓和多仓统一使用 `--repository <repository-id>=<checkout>`；多仓为每个仓库重复传入一次，ID 必须与 Case 清单一致。

Guard 只读目标 Scenario v2 DB/Source snapshot，返回 `READY_NEW`、`NO_CHANGE`、`UPDATE_REQUIRES_CONFIRMATION` 或阻断状态。只有用户明确允许该 ID 更新后，才能加 `--update-existing` 重查。

### 5. 明确授权后的状态变更

发布前再次检查 source hash、活动 Scenario v2 Run、repository 归属和 publication status。发布不同 hash 会使校准回到待执行；随后用 Scenario v2 确定性校准服务验证 Base/Solution。创建正式评测 Run 是单独授权。

### 6. 正式闭环验证与报告

发布更新、分支或冻结文件变化后必须重新生成 Candidate integrity bundle；不得复用旧摘要。确定性校准成功后，优先用一个低并发正式 Run 做 canary。只有 Run 的 Base/Candidate、工作流、Verify、Review/Archive 和评分均按当前冻结契约结束，才记录“正式闭环已验证”。

逐 Case 输出仓库、commit 区间、staging/目标路径、Preflight、冲突决策、source hash、发布状态、校准状态和下一动作。状态只使用：

```text
DISCOVERED -> GENERATED -> PREFLIGHT_PASSED -> PUBLISHED -> CALIBRATED
```

另行报告 `FORMAL_RUN_NOT_CHECKED`、`FORMAL_RUN_FAILED` 或 `FORMAL_RUN_COMPLETED`，不得把它们混入校准状态。

批量任务汇总成功、跳过、需确认、阻断和失败数量，并保留可重跑清单。
