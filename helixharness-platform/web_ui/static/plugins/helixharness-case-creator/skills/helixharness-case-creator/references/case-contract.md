# Scenario v2 Case Source 契约

以目标 HelixHarness Platform 当前 `bench/scenarios.py` 为最终事实源。本页只是生成检查表。

## 必需文件

- `case.json`
- `initial-request.md`
- `clarification-policy.json`
- `gold.json`
- `provenance.json`（当 `case.json.provenance_path` 声明时）
- `hidden-tests.patch`（单仓 `hidden_test_patch.path` 或多仓 `hidden_test_patches[].path` 声明时；多仓可逐仓拆分文件）
- `workspace-fixture.patch`（当 `case.json.workspace_fixture_patch.path` 声明时）

## case.json

- `schema_version`: `framework-eval-scenario.v2`
- `case_kind`: `REPOSITORY_CASE`
- `execution_intent`: `FLOW_THROUGH` 或 `BLOCKING_PROBE`
- 使用安全相对路径引用 initial request、Gold、澄清策略和可选 provenance。
- `runtime.language` 必填；工具链版本必须来自冻结仓库事实。
- 单仓继续使用旧 `repository`，只冻结 `repository_id`、40 位 `base_commit`、40 位 `solution_commit` 和真实 binary full-index diff 的 `solution_patch_sha256`；不要求迁移已有 Case。
- 多仓声明 `repository_mode=MULTI_REPOSITORY`，不再声明顶层 `repository`；使用 `workspace.primary_repository_id` 与 `workspace.repositories`。每个成员包含唯一 `repository_id`、唯一单层 `mount_path`、`impact=WRITE|READ` 及独立的 Commit 区间和 patch SHA256。至少两个成员、至少一个 `WRITE`，所有成员都必须可重放。`READ` 成员必须冻结为 `base_commit == solution_commit`，并使用空 diff 的 SHA256，防止把应由 Candidate 修改的仓库误标成只读上下文。
- 多仓必须声明 `workspace_path`，且其中至少保存执行时复制到 Workspace 根目录的 `helixharness.workspace.yaml`；注册表的 repo key/path 必须与 Case 成员和 mount_path 一一对应。
- 可选 hidden patch 声明安全路径与 SHA256。
- 可选 `workspace_fixture_patch` 只能声明构建/工具配置路径（如 `build.gradle`、`pom.xml`、`package.json`、`gradle/` 或 `.mvn/`）与 SHA256。它在 Agent 执行前应用并进入冻结工作区基线；不得修改业务源码、测试断言或需求语义。

`hidden_test_patch` 只保留工作流后才注入的隐藏断言。如果 Base 中的无关测试、旧 Lombok/Mockito 版本或源码集范围会让 Agent 在实现前就无法构建，应将对应构建隔离拆入 `workspace_fixture_patch`，而不是继续藏在 hidden patch。

多仓使用 `hidden_test_patches` 数组，每项必须包含所属 `repository_id`、路径、SHA256 和 `protected_paths`；同一仓库最多一个隐藏补丁。多仓不接受旧 `workspace_fixture_patch`。

Case 不得包含 Run 才能决定的字段：`profile`、`profile_version`、`workflow_driver`、`workflow_policy`、`workflow_commands`、`scoring_profile`。双方执行命令、阶段产物来源和统一评分口径由发起评测时冻结。

## initial request 与 provenance

initial request 至少包含背景、目标、范围、功能需求和验收标准；已冻结的澄清事实放入 `clarification-policy.json`，不得依赖评测执行时临时询问业务方。

澄清 facts 必须给出具体业务事实、边界答案和可追溯来源；禁止使用“行为已冻结”“按需求实现”等空壳事实，也禁止把 Review、归档、commit 等 Run 流程选择写成业务事实。

每条 source evidence 必须能从冻结 commit 或历史需求产物追溯。不要伪造 commit、文件哈希、测试结果或业务事实。

## gold.json

- `schema_version`: `framework-eval-scenario-gold.v2`
- `requirements`、`source_evidence` 必须非空，标识全局唯一且相互引用有效。
- 不再声明 `hidden_checks`；确定性业务校验统一放入 `execution_tests`。
- `execution_tests` 必须同时包含 F2P（Base 失败、Solution 通过）和 P2P（两侧均通过）。
- execution tests 使用 argv 数组、安全 runner、1..3600 秒超时。
- 多仓 execution test 使用 `working_directory` 指向 `.` 或某个已声明 `mount_path`；仓库来源证据必须携带 `repository_id`。
- F2P 与 P2P 的构建和源码集合必须隔离；P2P 不得编译 F2P 专用测试或 stub。
- 断言可观察行为；只有冻结需求明确要求时，才可检查私有字段名、构造器、注入方式、注解或唯一 accessor 名。
- Gold 不得包含 `artifact_paths`、`stage_artifact_contract`、`milestones`、`judge_guidance` 或 `score_axes`；这些属于 Run 的工作流和评分契约。

## 每次修改后重算

修改任何被引用文件后，重新运行 Scenario v2 Preflight 并以返回的 `source_hash` 为唯一当前内容哈希。更新已发布 Case 会使当前校准失效，必须重新执行确定性校准。

`CALIBRATED` 不证明完整工作流闭环；同一 `source_hash` 的正式 Run 结果必须单独报告。
