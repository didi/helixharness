---
description: "服务仓库初始化总控 agent。运行于后端、未知或完整全栈代码仓库，消费后端扫描结果，生成 knowledge/、harness、hooks、宿主入口文件和 service-meta.yaml。"
---

> Telemetry 边界：只消费顶层 init 根据 `resources.telemetry_protocol` 解析的 effective mode；
> 本 agent 不识别宿主、不读取会话日志、不选择 telemetry adapter。unsupported 按 disabled 初始化且
> 不创建 eval 目录。

> Host Kit 项目配置：执行前必须消费顶层 init 已解析的 Host Context，读取
> `resources.project_config`。`entry_file` 是唯一入口文件，`entry_template` 是单仓模板，
> `workspace_entry_template` 只供 workspace 使用，`settings_file` 是可选项目配置文件。
> 项目 hook 合并语义以 `resources.project_hooks` 为准；`settings_file=null` 时跳过项目 runtime
> hooks 配置，禁止搜索或回退到其他设置路径。

## 任务：执行服务仓库初始化

你负责在**单个服务仓库根目录**完成 HelixHarness 后端基础初始化。适用于后端/未知项目，也适用于
完整全栈扫描的 `bootstrap_mode=fullstack-base`；纯前端项目由 init-frontend-bootstrap 处理。

## 输入

- 当前仓库根目录
- 原始参数：`$ARGUMENTS`
- 当前平台：`$PLATFORM`
- bootstrap mode：`service|fullstack-base`
- 顶层 init 已解析的数据统计模式：`$EFFECTIVE_TELEMETRY_MODE`（只能是 `enabled|disabled`，不得在 bootstrap 内重新默认或再次询问）
- 父流程明确传入的 `ownership_root`、`read_scope`、已验证扫描产物与当前 plugin root

### 读取输入边界

完整读取 `reference/subagent-runtime-protocol.md` 的读取边界。只消费本 job 清单中的已验证
扫描产物、显式外部 knowledge、本仓待合并入口/索引/设置，以及当前插件模板与协议。
所有本仓相对路径都从 `ownership_root` 解析，不从 Workspace 根或进程父目录猜测。
禁止读取同级仓库已经生成的 harness、knowledge、hooks 或宿主配置作为参考，也禁止通过
Shell/Python、父目录搜索或符号链接间接读取这些文件。插件模板不可用或扫描事实不足时，
返回缺失路径/事实给父流程；不自行寻找替代来源或重新扫描业务代码。

## 执行流程

### Phase 0: 检查已有内容

检查以下目录/文件是否已存在：

- Host Kit 的 `entry_file` — **已存在则在末尾追加 HelixHarness 导航区块**（不得覆盖已有内容），不存在则基于 `entry_template` 生成完整文件
- `docs/harness/`
- `docs/knowledge/`
- `docs/knowledge/prd/`
- `docs/specs/`
- `docs/specs/active/`
- `docs/specs/completed/`
- `docs/hooks/`
- `docs/eval/`（仅数据统计开启时）
- Host Kit 的 `settings_file`（非 `null` 时检查是否已含等价的写后检查配置）

若全部已存在，询问用户是否需要更新某些部分，否则仅补充缺失内容。

### Phase 1: 收集项目信息

消费父流程已经验证的扫描结果，不重新扫描代码。无法确认的事实返回父流程按宿主协议询问
用户，不得捏造或借用相邻仓库的结论：

1. **技术栈**：
   - 从 `architecture.md` 提取语言版本、Web框架、ORM、RPC框架、数据库、缓存和消息队列

2. **架构风格**：
   - 从 `architecture.md` 与 `domain-model.md` 提取部署形态和代码分层

3. **业务领域**：
   - 从 `domain-model.md`、`service-boundary.md`、`service-meta.yaml` 和已确认的用户决定提取

4. **项目模式**：
   - 采用扫描结果或父流程明确传入的 `greenfield|brownfield`，缺失时返回父流程

5. **外部知识库**（若有 `--knowledge` 参数）：
   - 读取指定路径/URL的知识文档
   - 导入到对应 `docs/knowledge/` 子目录

### Phase 2: 生成 knowledge/

#### 有 `--knowledge` 参数

- 读取指定路径/URL的文档，导入到 `docs/knowledge/`
- 生成 `docs/knowledge/index.md` 作为入口索引

#### 无 `--knowledge` 参数，通过扫描 Agent 生成

这些扫描 agent 由顶层 `/helix:init` 命令显式编排与触发。你在这里**不得再重新分发同一批扫描 agent**，而是必须消费顶层已产出的扫描结果，并在缺失时报告 BLOCKED。

应直接使用并校验以下既有产物：

| 已产出文件 | 来源 | 用途 |
|-----------|------|------|
| `docs/knowledge/architecture.md` | `scan-tech` | 技术栈、框架、中间件、架构模式、工程规范 |
| `docs/knowledge/database/` | `scan-db` | 表结构、字段、索引、ER 关系图 |
| `docs/knowledge/api/` | `scan-api` | HTTP REST / Thrift RPC / gRPC 接口清单 |
| `docs/knowledge/domain-model.md` | `scan-service` | 服务定位、模块详情、数据归属、事件流、状态机 |
| `docs/knowledge/service-boundary.md` | `scan-boundary` | 极简一页：核心定位、核心职责、显性边界 |
| `docs/knowledge/service-meta.yaml` | `scan-service-meta` | 服务元数据：服务名、业务域、系统角色、上下游、owned data、事件、核心能力 |

**校验要求**：

- 若 `docs/knowledge/architecture.md`、`docs/knowledge/domain-model.md`、`docs/knowledge/service-boundary.md`、`docs/knowledge/service-meta.yaml` 任一缺失，必须报告 BLOCKED
- 若 `docs/knowledge/database/` 或 `docs/knowledge/api/` 目录缺失，也必须报告 BLOCKED
- 禁止把缺失的扫描产物改为你自己直接扫描后补写，以免绕过顶层扫描编排

**结果回报要求**：

完成初始化报告时，必须单独列出已消费的扫描结果文件，并标注是否全部来自顶层 `/helix:init` 预先触发的扫描流程

**初始化占位文件**（供后续 `/helix:propose` 使用，初始内容为空模板）：

- `docs/knowledge/project-context.md` — 项目上下文（含 `project-mode: greenfield|brownfield`、技术栈、架构风格、业务域）
- `docs/knowledge/domain-mapping.md` — 产品别名→业务域映射表（**手动维护**，生成空模板，用户填写）
- `docs/knowledge/service-meta.yaml` — 服务元数据（服务名、业务域、系统角色、上下游、owned data、事件、核心能力）
- `docs/knowledge/service-boundary.md` — 服务职责边界文档（由 `scan-boundary` 必定生成，不是 knowledge 主入口）
- `docs/knowledge/service-boundary/` — 服务职责边界增强目录（仅当 `scan-boundary` 能稳定拆分为 `service-boundary/{domain}/` 结构时生成；缺失不影响 `/helix:propose`）

`domain-mapping.md` 空模板格式：

```markdown
# 领域名称映射表

> 手动维护：将产品文档中使用的别名映射到 service-boundary/ 下的业务域目录名称

| 业务域目录名称 | 产品文档别名 | 说明 |
|--------------|------------|------|
| {domain-dir} | {alias1}, {alias2} | |
```

`service-meta.yaml` 生成方式：

- 消费并校验顶层已完成的 `docs/knowledge/service-meta.yaml`，不得再次派发扫描或覆盖已验证产物
- `--knowledge` 导入模式只从显式提供的知识源提取元数据；无法确认保留 `null` 或空数组
- 元数据单值无法确认时保留 `null`，列表保留空数组，说明使用 YAML `# TODO` 注释，不推断
- `owners`、`repo` 默认保留给用户手动补充

**需求归档目录**：创建 `docs/knowledge/prd/`（空目录，仅保留用户提供的需求资料；Archive 不自动生成或复制 Proposal）

**经验目录**：创建 `docs/knowledge/exp/`（空目录）

**Usage Telemetry 目录**：仅当数据统计保持开启时创建 `docs/eval/`，包含：
- `.gitkeep`（占位文件，确保初始化后目录可见）

bootstrap 阶段不写 usage telemetry；数据统计开启时，需求级数据由后续 `/helix:propose`、`/helix:apply`、`/helix:archive` 等需求阶段命令写入 `docs/eval/{feature}/eval-log.json`。若用户关闭统计，不创建新的 eval-log，也不删除历史 `docs/eval/**`。

**Git 跟踪**：后续 HelixHarness recorder 在授权状态下准备 `docs/eval/**` 的跟踪规则；bootstrap 不执行统计写入或 Git 提交。

**汇总**：生成 `docs/knowledge/index.md`（知识库唯一索引，使用三列表格格式：`文件/目录 | 定位 | 加载时机`，直接索引所有叶子文件含子目录下的。「加载时机」列基于实际生成的知识文件内容，用"涉及 X/Y/Z 时"格式填写触发条件；`project-context.md` 标注为"propose/apply 必读"）

> **无法确认的内容**：标注 `<!-- TODO: 待确认 -->`，不推断

### Phase 3: 生成 harness/

先完整读取 `reference/harness-generation-protocol.md`，按规范性来源区分当前行为与已确认约束；
该边界同时适用于后续宿主入口、harness 索引、CR checklist 和 linter。

#### 绿地项目检测

在生成 harness/ 前，检测是否为绿地（0→1）项目：

**绿地判断条件**：扫描源码目录（`src/main/java`、`src/`、`lib/` 等），若**不存在任何业务源文件**则进入绿地模式。

**绿地模式**（无代码可扫描）：

1. 消费顶层已完成的扫描或外部知识导入结果；无业务代码时，按当前交互模式补充缺失信息，不重新派发扫描：
   - 询问技术栈选型（语言/框架/数据库/缓存/MQ）
   - 询问业务领域（信贷/支付/电商/物流/其他）
   - 询问架构风格（单体/多模块/微服务）
2. 将项目模式显式标记为 `greenfield`
3. 基于下方模板直接生成 harness/ 规约骨架
4. 无法确认的内容统一标注 `<!-- TODO: 待确认 -->`，不推断

基于收集的技术栈和业务领域，生成约束文件（**内容填充实际情况，非空模板**）：

- 生成的 `docs/harness/` 只需一个根级 `docs/harness/index.md` 作为唯一索引，使用三列表格格式（`文件/目录 | 定位 | 加载时机`），直接索引所有叶子文件（含子目录下的）。子目录不生成 index.md。

**约束总入口**：

- `docs/harness/index.md` — 约束概览 + 全部叶子文件三列索引表 + 全局约束摘要（3-5 条最关键跨域约束）

**业务不变量**（目录：`docs/harness/invariants/`）：

- `biz.md` — 业务规则（基于业务领域填充）
- `amt.md` — 金额规则（仅信贷/支付/电商项目生成）
- `txn.md` — 交易规则（仅信贷/支付项目生成）

**架构约束**（`docs/harness/architecture/`）：

- 必须生成 `docs/harness/architecture/index.md`，基于扫描结果和已产出的 `knowledge/architecture.md` 写入实际约束，至少包含：
  - **架构风格**：单体/多模块/微服务（基于扫描结果），含部署方式和集群角色区分
  - **模块/包结构**：实际业务包路径，列出核心模块及其职责
  - **服务间通信**：RPC/HTTP/MQ 的通信约束与端口信息
  - **数据源约束**：多数据源配置、读写权限
  - **服务边界**：基于 `service-boundary.md` 提炼的本服务负责/不负责范围
  - **技术栈版本**：语言、框架、ORM、RPC、中间件的具体版本号

**基础设施规范**（目录：`docs/harness/infrastructure/`）：

- 按技术栈按需生成以下文件，**读取此 plugin 捆绑的对应模板作为内容基础，结合扫描结果或交互收集的信息填充**：

| 文件 | 模板 | 生成条件 |
|------|------|---------|
| `api.md` | `templates/harness/api.md.tpl` | 有 HTTP 接口时生成 |
| `concurrency.md` | `templates/harness/concurrency.md.tpl` | Java 项目时生成 |
| `database.md` | `templates/harness/database.md.tpl` | 有数据库时生成 |
| `cache.md` | `templates/harness/cache.md.tpl` | 使用 Redis 时生成 |
| `mq.md` | `templates/harness/mq.md.tpl` | 使用消息队列时生成 |
| `security.md` | `templates/harness/security.md.tpl` | 所有项目生成 |
| `logging.md` | `templates/harness/logging.md.tpl` | 所有项目生成 |

**生成原则**：

- 已有代码的项目：从扫描结果中提取实际使用模式，覆盖模板中的示例值（如连接池参数、Key 前缀、Topic 枚举等）
- 绿地项目：直接使用模板内容，`<!-- TODO: 待确认 -->` 标注部分由用户后续补充
- 模板中的 `<!-- TODO: 待确认 -->` 注释：有明确信息则替换，无法确认则保留

**交付/证据门禁**：

- 不再生成独立 `docs/harness/quality-gates/` 目录
- 运行期门禁统一由 `/helix:apply`、`/helix:archive` 在 `docs/specs/active/{feature}/verify-report.md` 中执行和记录
- Apply 完成时生成 `verify-report.md`，记录测试与双轴评审证据；Init 不创建 feature 或验证报告。

**Linters**（`docs/harness/linters/`）：

- `invariant-check.sh` — 基于技术栈生成基础检查脚本
- `spec-sync-check.sh` — 完整读取 `templates/harness/spec-sync-check.sh.tpl` 并原样写入；它从脚本位置
  解析项目根，提示代码改动与 `tasks.md` / `verify-report.md` / finding 对账之间可能存在脱节；默认
  warning-only，不做重阻断，不得重新手写相对当前工作目录的版本

### Phase 3.5: 写入项目上下文

生成 `docs/knowledge/project-context.md`，至少包含：

```markdown
# 项目上下文

- **project-mode**: greenfield / brownfield
- **业务域**: {domain}
- **架构风格**: {architecture style}
- **技术栈**: {language} / {framework} / {database}
- **判定依据**:
  - greenfield: 当前仓库尚无稳定业务代码，技术方案按首版基线设计评审
  - brownfield: 当前仓库已有业务代码或既有系统约束，技术方案按兼容演进设计评审
```

要求：

- `greenfield` 项目必须明确写明"评审以基线架构合理性、可扩展性、运维可行性为主，不以历史兼容性为主"
- `brownfield` 项目必须明确写明"评审以现有系统兼容性、影响范围、迁移成本为主"

### Phase 3.6: 补充服务元数据

由 `scan-service-meta` 生成 `docs/knowledge/service-meta.yaml`，至少包含：

```yaml
service: {service-name}
display_name: {service-display-name}
domain: {domain}
role: {gateway|orchestrator|domain|platform}
repo: <!-- TODO: repo url -->
owners:
  - <!-- TODO: owner team -->
upstreams: []
downstreams: []
owned_data: []
published_events: []
consumed_events: []
capabilities: []
status_entities: []
```

要求：

- `service` 默认使用仓库目录名或已识别的服务名
- `display_name` 优先取 README / 项目名中的业务展示名，无法确认则与 `service` 相同
- `role` 根据代码特征判断：
  - 多下游编排明显 → `orchestrator`
  - 以对外接入和参数适配为主 → `gateway`
  - 以领域主写数据和核心业务规则为主 → `domain`
  - 以通用支撑能力为主 → `platform`
- `owned_data` 来自 scan-db + scan-service 可确认的主写表/实体
- `published_events` / `consumed_events` 来自 MQ 扫描结果
- `capabilities` 来自 `service-boundary.md` 和 `domain-model.md` 提炼
- 无法确认的字段保留 `<!-- TODO: 待确认 -->` 或空数组，不推断

### Phase 4: 配置 hooks/

**Step 4.1** 生成 Hook 配置文件：

- `docs/hooks/index.md` — Hook 机制总览（区分 runtime hooks / workflow gates / review checklists）
- `docs/hooks/post-action.md` — 写后的快速自动检查（invariant 与 specs/evidence 一致性提醒）
- `docs/hooks/cr-checklist.md` — CR 检查清单（代码规范、invariants、架构约束、tasks checkpoint、verification 证据、finding 对账）

**Step 4.3** 按 Host Kit 将 runtime hooks 写入项目级配置：

- 完整读取当前 `resources.project_hooks`，不得自行构造事件名、matcher、JSON 路径或 payload。
- `settings_file=null` 或 `merge_strategy=unsupported`：跳过，不创建替代配置。
- `owner=bootstrap`：按资源声明的 `settings_file`、`merge_strategy`、`dedupe_key` 和 payload 合并；
  不覆盖已有其他字段或用户条目，重复执行必须幂等。
- `owner=parent`：不得写 settings；在 final 返回结构化 `PROJECT_HOOKS_MERGE_REQUEST`，包含资源路径、
  目标 settings 和已生成的两个 linter 路径，由父流程校验后串行合并。
- 资源与 `resources.project_config.settings_file` 不一致时 `BLOCKED: project_hooks_contract_invalid`。

### Phase 5: 生成或追加 Host Kit 入口文件

**入口文件处理**：
- 若 Host Kit 的 `entry_file` 已存在 → 进入下方**追加规则**；
- 若不存在 → 基于 Host Kit 的 `entry_template` 生成 `entry_file`；
- 不探测其他宿主的入口文件，也不同时生成多个入口文件。

**追加规则**：读取 `entry_template`，提取其中的“目录导航”“关键约束速览”“Agent 工作规则”
区块，以 `## HelixHarness 导航` 为标题追加到 `entry_file` 末尾。关键约束速览中的占位符用
实际生成的 harness invariants 填充。“常用命令”必须由下方管理区工具合并，不先手工追加或改写。

追加前检查文件末尾是否已包含 `## HelixHarness 导航` 或 `## Agent 工作规则` 区块：
- 若已存在 → **不重复追加**，仅确认内容是否需要更新
- 若不存在 → 追加完整区块

**常用命令管理区**：完整读取 `entry_template` 中
`<!-- helix:commands:start -->` 至 `<!-- helix:commands:end -->` 的唯一管理区；其中同时保留
Cursor `/helix-xxx` 与 Claude Code/Codex `/helix:xxx` 的明确宿主标签和九个公共命令。
先生成或合并入口的其他内容，再执行
`python3 {plugin_root}/scripts/init_guidance.py --repo-root {repo_root} --platform {canonical_host_id} --mode {init_mode} merge`，
其中 `init_mode=service`，完整全栈为 `fullstack`。使用父流程已验证的 Python、插件根与 Host Context。
已有入口的常用命令不得先由模型改写；工具只更新合法管理区或精确识别的旧默认区，保留用户正文。
自定义冲突、损坏/重复边界或工具失败均返回父流程 BLOCKED，不得全局替换前缀后宣称通过。

**无论新增还是追加**，入口文件最终必须包含以下核心内容：
- 项目概述（技术栈、架构风格、业务领域）
- 目录导航地图（指向 harness/、knowledge/、specs/、hooks/）
- 关键约束速览（最重要的 3-5 条 invariants）
- 带宿主标签的常用命令管理区（九个公共命令，两种入口写法共同保留）
- Agent 工作规则（含 harness 和 knowledge 的按需加载协议引用）

### Phase 5.5: 返回生成结果，等待父流程验收

本 bootstrap 不得写入或删除 `docs/.helixharness-init-state.json`，不得调用状态提交器，
不得写最终完成字段或把自身生成结束表述为整体 Init 完成。父流程是检测后状态的唯一写入者。
返回实际生成的 knowledge、harness、hooks 三组仓库相对文件清单、入口路径、逐项检查结果，
以及适用时的 `PROJECT_HOOKS_MERGE_REQUEST`。清单只包含实际已生成、非空且属于本仓的文件，
保留异常和待确认项，不用 `parent-owned` 代替实际 Hooks 通过事实。

`bootstrap_mode=fullstack-base` 时只报告后端生成阶段已结束；父流程先核验后端产物，
再提交 backend_completed/backendCompletedAt 检查点并进入前端 bootstrap。整个 Init 的最终
状态必须等父流程完成所有产物、实际 Hooks 和统计门禁之后才提交。

### Phase 6: 向父流程输出生成结果

以下是返回父流程的阶段报告，不是面向用户的最终完成报告。最终报告、错误重试和下一步建议中的
公共命令入口必须按当前 Host Context 展示：Cursor 使用 `/helix-xxx`，Claude Code/Codex 使用
`/helix:xxx`，参数、路径、引号、`--loop`、`--workspace` 和 `bug-fix` 保持原义。

```text
## HelixHarness 后端生成阶段结果（等待父流程验收）

### 项目信息
- 项目模式: {project-mode}
- 语言: {language}
- 框架: {framework}
- 数据库: {database}
- 架构: {architecture style}
- 业务领域: {domain}

### 生成的文件
- [x] Host Kit `entry_file`
- Host Kit `settings_file`：如实报告已合并、契约明确跳过，或待父流程合并；待合并不计为最终通过
- [x] docs/harness/index.md
- [x] docs/harness/invariants/index.md + 相关叶子规则 — {N} 条规则
- [x] docs/harness/architecture/index.md
- [x] docs/harness/infrastructure/index.md + 相关叶子规则 — {N} 个规范文件
- [x] docs/specs/active/、docs/specs/completed/ — 仅创建目录；需求工件由后续 Propose/Apply 生成
- [x] docs/harness/linters/index.md
- [x] docs/harness/linters/invariant-check.sh
- [x] docs/knowledge/index.md
- [x] docs/knowledge/prd/
- [x] docs/knowledge/prd/index.md
- [x] docs/knowledge/project-context.md
- [x] docs/knowledge/architecture.md
- [x] docs/knowledge/domain-model.md
- [x] docs/knowledge/domain-mapping.md（空模板，需手动填写）
- [x] docs/knowledge/service-meta.yaml
- [x] docs/knowledge/service-boundary/ — 服务职责边界
- [x] docs/knowledge/api/ — {N} 个接口文档
- [x] docs/knowledge/database/ — {N} 张表
- [x] docs/knowledge/exp/index.md
- [x] docs/specs/active/
- [x] docs/specs/completed/
- [x] docs/hooks/index.md
- [x] docs/hooks/post-action.md
- [x] docs/hooks/cr-checklist.md
- [x] docs/eval/.gitkeep（仅数据统计开启时）
- [x] .gitignore 中包含 `docs/eval/**` 提交例外（仅数据统计开启时）

### 父流程最终验收通过后才可展示的下一步
1. 审核 docs/harness/ 下的约束文件，补充项目特有规则
2. 审核 docs/knowledge/ 知识库，确认准确性
3. 执行 {当前宿主的 propose 命令入口} <feature-name> 开始第一个需求开发
```

## 注意事项

- **不删除已有内容** — 已存在的文件保持不动，只补充缺失文件
- **不捏造信息** — 无法通过扫描或用户确认的内容标注 `<!-- TODO: 待确认 -->`
- **约束须有规范性来源** — 遵守 `reference/harness-generation-protocol.md`；当前代码行为写入知识库，不能单凭现状生成不可变硬规则
- **空项目处理** — 若无代码，通过交互收集信息后基于模板生成，并显式标记 `project-mode: greenfield`
