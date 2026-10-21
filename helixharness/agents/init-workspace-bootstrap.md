---
description: "多仓 workspace 初始化 agent。生成 helixharness.workspace.yaml、workspace knowledge 基线和 specs 目录，不替代 repo-local 初始化。"
---

## 任务：执行 workspace 初始化

你负责在**多 repo workspace root**完成 HelixHarness workspace 初始化。

执行前必须消费顶层 init 已解析的 Host Context，读取 `resources.project_config`：
`entry_file` 是 workspace 根唯一入口文件，`workspace_entry_template` 是本流程使用的模板；
`entry_template` 仅供单仓 init 使用，`settings_file` 在 workspace 模式不写。宿主工具语义读取
`resources.runtime`，禁止按平台名硬编码入口文件。

## 输入

- workspace 根目录
- 原始参数：`$ARGUMENTS`
- repo 发现结果
- 每个 repo 的 HelixHarness 初始化状态

## 产物边界

workspace init 只写 workspace 根目录下的协调层产物：

- Host Kit 的 `entry_file`
- `helixharness.workspace.yaml`
- `docs/knowledge/service-catalog.yaml`
- `docs/knowledge/service-graph.md`
- `docs/knowledge/integration-boundaries.md`
- `docs/knowledge/index.md`
- `docs/specs/active/`
- `docs/specs/completed/`

不得自动进入子 repo 执行单仓 `/helix:init`，也不得改写 repo-local `docs/harness/`。

强制要求：

- 所有上述产物必须落在 workspace root，即当前命令执行目录下
- 子 repo 已存在 HelixHarness 初始化产物时，只读取它们，不重新生成、不覆盖、不刷新、不迁移
- 子 repo 未初始化时，只在 `helixharness.workspace.yaml` 标记 `initialized: false`，不自动补初始化
- 不得只在各服务子目录下生成 `.helixharness/` 文件后就报告完成
- `.helixharness/telemetry.json`、`.helixharness/upload-state.json` 是 repo-local 运行期/遥测文件，不属于 workspace init 目标产物
- 若只能生成 repo-local `.helixharness/` 文件而无法生成 workspace root 产物，必须报告 `BLOCKED: workspace_root_outputs_not_created`

## Phase 0：确认 workspace root

检查当前目录是否为 workspace root：

- 当前目录本身可以不是 git repo
- 当前目录下一层或 `--repos` 指定路径中应包含一个或多个 git repo
- 若没有发现任何 repo，报告 BLOCKED，并提示用户使用：

```text
/helix:init --workspace --repos <repo-paths...>
```

## Phase 1：生成 `helixharness.workspace.yaml`

对每个 repo 写入 registry 信息。

格式：

```yaml
version: 1

workspace:
  name: {workspace-directory-name}
  root: .
  mode: multi-repo

repos:
  {repo-key}:
    path: ./{repo-path}
    remote: {remote-url-or-null}
    default_branch: {default-branch-or-current-branch-or-TODO}
    role: {role-or-TODO}
    helix:
      initialized: true|false
      service_meta: docs/knowledge/service-meta.yaml|null
```

规则：

- repo key 默认取目录名；冲突时停止并询问用户，不要自动改名
- `remote` 优先取 `origin` fetch URL，无法确认写 `null`
- `default_branch` 优先取远端默认分支，无法确认用当前分支并标注 TODO
- `role` 优先从 `service-meta.yaml` 的 `role` 或 README/部署配置推断；无法确认写 `TODO`
- `helixharness.initialized` 只表示当前宿主入口、harness/knowledge index 与 specs 目录是否齐备，不代表业务服务是否可运行
- `service_meta` 是后端/服务仓增强项；纯前端 repo 可为 `null`，不得据此把已初始化前端仓降为 missing

## Phase 1.5：生成 workspace 根入口文件

在 workspace root 生成 Host Kit 的 `entry_file`，作为当前宿主的 workspace 入口规则。

优先读取此 plugin 捆绑模板：

```text
Host Kit 的 `workspace_entry_template`
```

若 workspace root 已存在 `entry_file`：

- 不覆盖用户已有内容
- 只在文件末尾追加或更新 `HelixHarness Workspace` 导航区块
- 不写入 repo-local harness 规则

workspace 入口文件必须明确：

- 当前目录是 HelixHarness workspace root
- 发现 `helixharness.workspace.yaml` 时，Propose/Apply/Archive 由 Router 选择 Workspace scope；其他命令遵循各自的适用范围
- Workspace 根 feature 只保存概要设计 `docs/specs/active/{feature}/spec.md`，协调跨仓影响范围与依赖
- 各子仓独立执行完整 Single Propose/Apply/Archive：Propose 生成 `spec.md`、`tasks.md`，Apply 生成 `verify-report.md`；Archive 校验并移动这三份工件
- 不生成独立 proposal、design、verification、apply-report 或 repo-plan 文件
- 不得绕过 workspace spec 直接进入 repo 子目录生成 feature spec
- repo-local Host Kit 入口文件只在 repo agent 执行本 repo 任务时读取，不作为 workspace propose 的主入口

## Phase 2：聚合 service catalog

生成 `docs/knowledge/service-catalog.yaml`。

优先读取每个 repo 的：

- `docs/knowledge/service-meta.yaml`
- README
- OpenAPI / Swagger / RPC IDL
- package / module / artifact 名称
- 部署配置中的服务名

示例结构：

```yaml
version: 1
services:
  - repo: order-api
    path: ./order-api
    service: order-api
    display_name: Order API
    domain: order
    role: domain
    initialized: true
    evidence:
      - ./order-api/docs/knowledge/service-meta.yaml
```

无法确认的字段保留 `TODO` 或空值，不得捏造。

## Phase 3：生成 service graph

生成 `docs/knowledge/service-graph.md`，包含：

- 服务清单
- 已确认调用关系
- MQ topic producer / consumer
- 数据 ownership
- 不确定关系

不确定关系必须带 evidence、confidence、needs_confirmation：

```yaml
uncertain_edges:
  - from: order-api
    to: coupon-api
    type: http|rpc|mq|data|unknown
    evidence: "CouponClient found in ./order-api/src/..."
    confidence: medium
    needs_confirmation: true
```

## Phase 4：生成 integration boundaries

生成 `docs/knowledge/integration-boundaries.md`，至少包含：

- workspace 中每个 repo 的职责摘要
- repo 间 API/RPC/MQ/DB 边界
- 已确认 ownership
- 待确认边界
- 后续 propose 阶段如何使用这些信息做 feature-specific impact scan

## Phase 5：生成 workspace knowledge index

生成或更新 `docs/knowledge/index.md`，使用三列表格：

```markdown
| 文件/目录 | 定位 | 加载时机 |
|----------|------|----------|
| service-catalog.yaml | workspace 服务目录 | workspace propose/apply/archive 按阶段读取 |
| service-graph.md | workspace 服务关系图 | 涉及跨服务调用、MQ、DB ownership 时 |
| integration-boundaries.md | 跨 repo 集成边界 | 识别影响面、设计跨服务契约时 |
```

## Phase 6：输出报告

输出报告前必须执行后置检查：

```text
test -f ./helixharness.workspace.yaml
test -f "./{entry_file}"
test -f ./docs/knowledge/service-catalog.yaml
test -f ./docs/knowledge/service-graph.md
test -f ./docs/knowledge/integration-boundaries.md
test -f ./docs/knowledge/index.md
test -d ./docs/specs/active
test -d ./docs/specs/completed
```

若任一检查失败，不得输出“完成”，改为输出：

```text
BLOCKED: workspace_init_outputs_missing

缺失：
- {path}

已发现但不计入成功条件的 repo-local 运行期文件：
- {repo}/.helixharness/telemetry.json
- {repo}/.helixharness/upload-state.json
```

```text
## /helix:init --workspace 完成

### 已生成
- [x] Host Kit 入口文件：`{entry_file}`
- [x] helixharness.workspace.yaml
- [x] docs/knowledge/service-catalog.yaml
- [x] docs/knowledge/service-graph.md
- [x] docs/knowledge/integration-boundaries.md

### Repos
| Repo | Path | HelixHarness | service-meta | 备注 |
|------|------|-------------|--------------|------|
| {repo} | {path} | initialized/missing | yes/no | {TODO/uncertain} |

### 待确认
- {uncertain_edges 或 role/TODO}

### 下一步
/helix:propose <feature> <requirement-or-path>
```

## 约束

- `helixharness.workspace.yaml` 是 repo registry，不是完整系统知识库
- 服务关系基线要有证据；没有证据就进入 uncertain_edges
- 不覆盖用户已有 workspace 文档；refresh 时尽量保留人工补充字段
