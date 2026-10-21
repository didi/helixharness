---
description: "单元测试生成与维护命令。基于知识库和活跃需求，逐层扫描代码并生成/维护单测，运行验证并检查逻辑缺陷，确保覆盖率达标。"
---

> HelixHarness 公共命令唯一契约：本文件同时承载 UT 的完整业务步骤和原生命令分发表面。所有宿主都必须通过当前 `resources.command_projection` 加载本文件，并使用预绑定 Host Context；完整读取 `reference/workflow-projection-protocol.md`、`reference/host-runtime-protocol.md`、`reference/tool-semantics.md`、当前 `resources.runtime`、`resources.ut` 和 `resources.subagent_protocol`，不得再读取其他 UT command/workflow 补充业务语义。

> UT 流程不变量：保持原命令 Phase 0—3、Agent 后台 worker、队列状态机、每类完整 6 Loop、失败重试、经验沉淀、主项目串行编译/测试/覆盖率和报告门禁。本文中的 `Agent`、`Read`、`Write`、`Bash`、`AskUserQuestion` 使用 `reference/tool-semantics.md` 的公共工具语义；当前 `resources.ut` 只映射权限准备、物理派发、等待、容量、运行时 handle 与清理，不得减少 worker、业务步骤、证据或完成条件。

> 参数宽容规则：解析 `$ARGUMENTS` 时先按 shell token 分词；每个 token 仅去除尾部常见中英文句读（`, . ; : ! ? ，。；：！？、` 及右侧闭合括号）后，再对 flag 做精确匹配。feature、数值、枚举和路径参数使用同一规范化结果；不得使用任意子串匹配。

# /helix:ut 命令执行手册

本文件是 UT 唯一完整业务事实源。`reference/ut-business-protocol.md` 只定义 adapter 可替换的运行机制
和不可降低边界，不能替代本文的扫描细则、状态机、6 Loop、缺陷、覆盖率、同步、恢复和报告契约。
所有宿主执行相同业务章节，并由当前 `resources.ut` 把本文的公共工具语义映射为真实能力。

## User Input

```text
$ARGUMENTS
```

## 参数说明

> 本命令支持两种入口：**显式参数入口**（`--packages` 与 `--concurrency` 一次给齐）和**交互补全入口**。
> 任一参数缺失或非法时，命令**不退出**：进入 Phase 1 后按 §4.2.0 扫描项目、展示候选包清单，
> 由用户选择包并指定并发；补全得到的值与显式传入完全等价。包名在执行模式为「继续执行」时可不
> 补全（沿用已有队列）；并发数在所有执行模式下都必须最终确定（`2 ≤ N ≤ MAX_CONCURRENCY`，
> `MAX_CONCURRENCY = 10`）。

| 参数 | 必填 | 说明 | 示例 |
|------|------|------|------|
| `--packages <pkg1,pkg2,...>` | 条件必填¹ | 指定目标包名集合（逗号分隔），仅扫描这些包及其子包下的源代码 | `/helix:ut --packages com.example.service --concurrency 5` |
| `-p <pkg1,pkg2,...>` | 条件必填¹ | `--packages` 的简写形式 | `/helix:ut -p com.example.service -c 5` |
| `--concurrency <N>` | 条件必填² | 单测维护 worker 并发数，必须是整数且 `2 ≤ N ≤ MAX_CONCURRENCY`（`MAX_CONCURRENCY = 10`） | `/helix:ut --packages com.example.service --concurrency 5` |
| `-c <N>` | 条件必填² | `--concurrency` 的简写形式 | `/helix:ut -p com.example.service -c 5` |
| （无参数） | — | 进入 §4.2.0 交互补全：列出候选包清单，由用户选择并指定并发 | `/helix:ut` |

> ¹ 全新开始 / 增量补充模式必须最终确定包名集合：可显式传入，也可由 §4.2.0 交互选择补全；继续执行模式可不补全（沿用已有队列）。
> ² 所有执行模式必须最终确定并发数：可显式传入，也可由 §4.2.0 交互输入补全。

## 参数解析与入口判断

解析 `$ARGUMENTS`：

### 第一步：参数提取（宽容提取，不校验必填、不退出）

1. **检测 `--packages` 或 `-p` 参数**：
   - 从 `$ARGUMENTS` 中提取 `--packages` 或 `-p` 后面的包名列表
   - 包名用逗号分隔，去除空格，去重
   - 示例：`--packages com.example.service, com.example.controller` → `["com.example.service", "com.example.controller"]`
   - 找到且非空 → 记为 `{packages_arg}`，状态 `已提供`；未找到或提取后为空列表 → `{packages_arg}` 状态 `未提供`
   - **不因缺失而退出**：`未提供` 与后续步骤正常衔接，由 §4.2.0 按执行模式补全

2. **检测 `--concurrency` 或 `-c` 参数**：
   - 从 `$ARGUMENTS` 中提取 `--concurrency` 或 `-c` 后面的整数值
   - 值为整数且 `2 ≤ N ≤ MAX_CONCURRENCY`（10）→ 记为 `{concurrency}`，状态 `已提供且合法`
   - 未找到 → 状态 `未提供`；值缺失、不是整数、小于 2 或大于 10 → 状态 `非法`，并**记录原始值**（用于 §4.2.0.2 的提示语）
   - **不因缺失或非法而退出**：与 `未提供` 同路径，由 §4.2.0.2 补全

3. **其他参数**：目前不支持其他参数，如有未知参数可忽略或提示用户

#### 参数补全入口协议

任一参数 `未提供` 或 `非法` 时，命令**不退出、不输出提示码**，继续执行第二步（执行模式判断）与 Phase 0；
在 Phase 1 入口由 §4.2.0 按所选执行模式补全仍缺的参数。补全得到的值与显式传入的值在整个后续流程中
**完全等价**（同一队列元数据、同一调度常量、同一门禁），同一轮执行内不再重复询问。

### 第二步：执行模式判断

进入 Phase 0 之前，先检查上次运行状态（§4.0）。此时第一步仅完成宽容提取，`{concurrency}` 可能尚未确定；如缺失或非法，由 §4.2.0 在进入 Phase 1 后补全，本步骤不因此退出：

- **存在残留文件**（`unittest-session.md`、`unittest-queue.md`、`worker-*.md`）：
  - 读取上次会话信息
  - 向用户展示当前状态
  - 询问用户选择：继续执行 / 全新开始 / 增量补充
  - **增量补充模式**：包名可来自显式参数，也可由 §4.2.0 交互选择补全；本步骤不因包名未提供而退出

- **无残留文件**：直接进入全新开始模式

### 第三步：增量补充模式特殊处理

若用户选择"增量补充"：
- 包名集合或已由显式参数提供，或将在 §4.2.0.1 通过交互选择确定；确定后与已有队列中的包名合并（去重）
- 保留已有队列中已完成的任务
- 仅对新增包名执行扫描，生成新的 pending 任务

---

## 文档摘要

本文档定义 `/helix:ut` 命令的完整执行流程：
- 扫描项目代码，识别需要单测的类
- 调度 `unit-test-worker` 并发处理单个类
- 汇总执行结果，主项目统一验证
- 临时资源和权限配置管理

**核心原则**：证据先于声明、发现缺陷不暂停、契约先于发挥、禁止修改业务源代码
**架构模式**：源外构建模式（直接使用主项目源码，零复制）

---

## 1. 核心原则与禁令

### 1.1 三条铁律
1. **证据先于声明**：必须展示实际输出才能声称完成
2. **发现缺陷不暂停**：Low/Medium 缺陷只记录并继续；High 高危 Bug 允许对应 worker 写完整失败结果后提前退出，ut.md 继续处理其他类
3. **契约先于发挥**：严格按知识库和本文档要求执行，不随意发挥

### 1.2 绝对禁令
| 禁令 | 说明                                                 |
|------|----------------------------------------------------|
| ❌ 修改业务源代码 | 主项目源码只可读，不可写                                       |
| ❌ 修改项目配置文件 | application.yml、build.gradle、pom.xml 等 (项目无单测框架除外) |
| ❌ 在业务执行阶段询问用户问题 | 仅允许 §4.0 模式选择、§4.2.0 参数补全和 §4.2.4 任务清单确认三类业务门禁交互；Phase 2 及之后所有操作直接执行，零确认 |
| ❌ 因已有测试简化流程 | 即使主项目已有测试，也必须完整执行                                  |
| ❌ 用已有测试结果替代新验证 | 必须在临时目录重新编写并运行                                     |
| ❌ 在主项目执行构建命令 | 仅 Phase 2-5 统一验证阶段可执行                              |
| ❌ 复制/修改敏感文件 | gradlew、gradle wrapper、mvnw 等文件禁止 cp、chmod、移动。调用构建工具时通过路径引用（worker 用绝对路径，主项目内用相对路径），不复制 |

---

## 2. 职责边界

### 2.1 ut.md vs unit-test-worker.md

| 职责项 | ut.md 负责 | unit-test-worker.md 负责 |
|--------|-----------|------------------------|
| 工作流管控 | ✅ 完全负责 | ❌ 不涉及 |
| 任务队列管理 | ✅ 完全负责 | ❌ 不涉及 |
| 并发调度 | ✅ 完全负责 | ❌ 不涉及 |
| 单个类单测生成 | ❌ 不涉及 | ✅ 完全负责 |
| 宿主权限/审批准备 | ✅ 通过 `resources.ut` 完全负责 | ❌ 只读，不修改 |
| 共享经验库访问 | ✅ 汇总或 `parent_finalize` 阶段串行合并 | ✅ Loop 0 完整读取；`child_locked_append` 时锁追加，`parent_finalize` 时只返回经验候选 |
| 主项目预编译 | ✅ 完全负责 | ❌ 不执行 |
| 主项目统一验证 | ✅ 完全负责 | ❌ 不执行主项目构建 |
| 全局临时资源清理 | ✅ 完全负责 | ✅ `child_locked_append` 仅清理自己持有的锁；`parent_finalize` 无锁动作 |

### 2.2 信号传递契约

- **ut.md → worker**：通过启动参数和 prompt 传递所有必要信息
- **worker → ut.md**：业务产物通过文件系统传递；物理终态由 `resources.ut` 映射。支持原有后台 Agent 文件信号的宿主保持以下语义，其他宿主必须先取得真实 `wait_job` 终态再校验同一文件契约
  - `worker-{ID}-result.md` 存在 = worker 已完成（使用原子写协议：`.tmp` → `mv`）
  - 文件第一行 = 基础状态（`状态: 成功` / `状态: 失败`）
  - 文件第二行（如存在）= 结构化子状态标记（`同步状态: 失败` / `退出原因: 高危Bug`）
  - `状态: 超时终止` 为 ut.md 内部使用（超时代写，仅 ut.md 写入此值，对应队列状态 `failed` + `备注: timeout`）
  - ut.md 根据第一行判定队列终态：`状态: 成功` → `completed`，`状态: 失败`（含 `状态: 超时终止`）→ `failed`；子状态信息记录到 `备注` 列

### 2.3 源外构建模式

核心原则：零复制，直接使用主项目源码，完全隔离构建

```
# 主项目（只读使用）
{主项目目录}/
├── build.gradle / pom.xml      # 🔴 只读：worker 读取配置
├── src/main/java/              # 🔴 只读：worker 直接引用
└── build/classes/java/main/    # 🔴 只读：ut.md 预编译的产物

# 临时区（完全隔离）
{系统临时目录}/{ProjectName}/
├── .gradle-cache/              # 🔴 共享 Gradle 依赖缓存（持久，运行间复用，不在 Phase 2-5 删除）
├── .m2-repository/             # 🔴 共享 Maven 依赖缓存（持久，运行间复用，不在 Phase 2-5 删除）
├── worker-{ID1}/               # 🔴 完全隔离的工作区 1
│   ├── build.gradle           # 最小化配置（引用主项目编译产物，指向共享缓存）
│   ├── src/test/java/         # 仅本 worker 的测试代码
│   ├── tmp/                   # 独立 JVM 临时目录（-Djava.io.tmpdir）
│   └── build/                 # 独立的编译输出
├── worker-{ID2}/               # 🔴 完全隔离的工作区 2
│   └── ...

---

**主项目（只读）**：`build.gradle/pom.xml`、`src/main/java/`、`build/classes/java/main/`
**临时区（隔离）**：`{系统临时目录}/{ProjectName}/` 下的 `worker-{ID}/` 独立工作区
**共享缓存**：`.gradle-cache/`、`.m2-repository/` 持久化，运行间复用

---

## 3. 跨平台临时目录设计

### 3.1 临时目录根路径选择
- macOS/Linux：优先 `$TMPDIR`，回退 `/tmp`
- Windows：优先 `%TEMP%`，回退 `%TMP%`
- 父流程解析为绝对路径后固定传入每个 Worker 的 `{系统临时目录}` 和 `{WorkerTempPath}`；Worker 不重新选择临时目录，权限检查、构建及清理均复用传入路径。

### 3.2 目录结构
- 格式：`{系统临时目录}/{ProjectName}/worker-{ID}/`
- 共享缓存：`{系统临时目录}/{ProjectName}/.gradle-cache/`、`.m2-repository/`

### 3.3 清理规则
- 只清理 `worker-*` 目录，保留共享缓存
- 使用跨平台删除方式，避免硬编码命令

---

## 4. Phase 0-3 完整流程

### 4.0 前置：执行模式确认与状态检测

**此步骤使用参数解析结果（见文档开头「参数解析与入口判断」）**

**前提**：第一步已完成宽容提取。`{concurrency}` 如已合法提供则作为最大并发常量贯穿后续所有 worker 调度；如未提供或非法，由 Phase 1 入口 §4.2.0 补全，本步骤不因此退出。

检查是否存在残留文件：`unittest-session.md`、`unittest-queue.md`、`worker-*.md`

**存在残留**：
- 读取上次会话信息，展示当前状态摘要
- 询问用户选择执行模式：
  - **继续执行**：从上次中断处继续，保留已完成工作
  - **全新开始**：清空所有状态，重新扫描
  - **增量补充**：基于上次运行，新增指定包名的扫描任务（包名可显式提供，也可由 §4.2.0 交互选择补全）
- 记录用户选择，进入对应分支

**无残留**：
- 直接进入全新开始模式
- 使用参数解析得到的包名列表（如有）

### 4.1 Phase 0：前置检查与上下文加载

#### 4.1.1 前置清理与初始化（根据用户选择）

| 分支 | 操作 |
|------|------|
| **继续执行** | 清理临时目录；仅 `child_locked_append` 清理符合 §7 所有权/超时条件的残留锁目录，`parent_finalize` 不检查、不创建、不删除锁。恢复 archived worker 文件，重置 in_progress 任务为 pending，保留已有 failed 任务（由 Phase 2-3 重试机制处理）。**重试行检测**：扫描队列 `备注` 列中已有 `retry:*|orig:W{ID}` 的行，这些为重试任务已在队列中，Phase 2-3 不再为其原 `failed` 行创建新重试行。保留经验库和已有结果 |
| **全新开始** | 生成新会话 ID，清理旧会话托管权限，归档旧状态文件，初始化新会话文件，保留经验库 |
| **增量补充** | 验证包名集合（已由显式参数或 §4.2.0 交互选择确定），清理残留文件，恢复 archived worker 文件，重置 in_progress 任务 |

#### 4.1.2 阅读项目完整知识库

读取 `docs/harness/` 和 `docs/knowledge/` 下所有文件，整理为完整公共知识库传递给每个 agent。排除：`docs/specs/active/`、`docs/specs/completed/`、`docs/hooks/`

**输出**：
```
Phase 0-1：项目完整知识库加载
- docs/harness/：{N} 个文件
- docs/knowledge/：{N} 个文件
- 知识库总大小：约 {X} 行
```

#### 4.1.3 推导 Feature 名称

**Feature 名称 = 当前 git 分支名，`/` 替换为 `_`**。

执行命令 `git rev-parse --abbrev-ref HEAD` 获取当前分支名，将其中所有 `/` 字符替换为 `_`，作为 `{feature}`。

示例：
- 分支 `feature_unit_test_command_upgrade_v1` → feature = `feature_unit_test_command_upgrade_v1`
- 分支 `feature/unit-test` → feature = `feature_unit-test`
- 分支 `bugfix/order/timeout` → feature = `bugfix_order_timeout`

`{feature}` 贯穿整个 ut.md 执行流程，用于定位 `docs/specs/active/{feature}/` 下的所有工作文件。

#### 4.1.4 阅读当前活跃需求

读取 `docs/specs/active/{feature}/spec.md` 和 `design.md`（如存在）作为独立需求上下文。如目录不存在，创建 `docs/specs/active/{feature}/` 目录。

#### 4.1.5 检查项目测试基础设施

**推导项目名**：当前工作目录 basename，替换特殊字符为 `-`，转小写
**全新开始时**：清理跨平台临时目录中的残留 worker 目录（保留共享缓存）

检查内容：项目类型、测试框架、Lombok 配置、测试目录结构、覆盖率配置

#### 4.1.6 预编译主项目（所有模块）

**强制执行**：无论任何执行模式，进入 Phase 1 前必须确保主项目已预编译
**继续执行/增量补充快速路径**：验证编译产物目录存在，存在则跳过，否则重新编译

**目标**：预编译所有模块主源码，避免每个 worker 重复编译；**同时提前发现并解决Java版本等环境级问题**，确保worker启动后环境可用。

**只编译主源码，不编译测试**：
- Gradle：`./gradlew clean classes --no-daemon --no-parallel --no-watch-fs -q`（如无 wrapper 则使用 `gradle`）
- Maven：`./mvnw clean compile --batch-mode -q`（如无 wrapper 则使用 `mvn`）

验证：检查 `build/classes/java/main` 或 `target/classes` 目录存在

**关键责任**：
- Phase 0 预编译是确保 worker 环境可用的第一道防线
- 若预编译失败（如Java版本问题、依赖问题等），必须先解决问题再启动 worker
- worker 启动后，不再接受"环境问题"作为跳过门禁的理由

### 4.2 Phase 1：代码扫描与任务列表生成

**前提**：§4.1.6 预编译已成功完成

**使用参数解析结果**：包名列表从文档开头的「参数解析与入口判断」步骤获取

**入口**：如仍有参数缺口（包名或并发数 `未提供`/`非法` 且所选执行模式需要它），先执行 §4.2.0 参数补全；§4.2.1 的扫描结果在补全与后续过滤之间复用，不重复扫描。

| 分支 | 操作 |
|------|------|
| **继续执行** | 读取已有任务队列，输出当前进度（含 §4.2.0.1-续 队列内包进度表），等待用户确认（并发数缺失/非法时先经 §4.2.0.2 确认） |
| **增量补充** | 读取已有任务队列，仅对新包名（显式提供或 §4.2.0.1 选定）执行扫描，排除策略，合并任务队列 |
| **全新开始** | 执行扫描与 §4.2.2 排除策略（§4.2.0.1 已完成时直接复用其排除后结果），再应用包名过滤（显式或 §4.2.0.1 选定），生成任务列表，用户确认 |

**所有分支在用户确认与任务队列生成之前必须通过 §4.2.3 任务数上限校验（`MAX_PENDING_TASKS = 100`），超限即阻断命令。**

#### 4.2.0 参数补全（包名发现与并发确认，仅当所选模式仍有缺口时执行）

**触发条件（缺口矩阵）**：

| 执行模式 | 包名是否必需 | 并发是否必需 | 需要补全的组合 |
|----------|--------------|--------------|----------------|
| **继续执行** | 否（队列已存在） | 是 | 只缺并发 → 按 §4.2.0.1-续 展示队列内包进度后，执行 §4.2.0.2 |
| **全新开始** | 是 | 是 | 缺什么补什么；两者皆缺 → 先 §4.2.0.1 选包，再 §4.2.0.2 定并发 |
| **增量补充** | 是（可交互补全） | 是 | 同上 |

无任何缺口（所选模式需要的参数均已合法提供）时，本节交互部分整体跳过，直接进入 §4.2.1 既有流程；但**继续执行模式仍必须执行 §4.2.0.1-续 的队列内包进度展示**（纯展示、无询问），保证用户始终能区分队列中已有的包及其状态。

**统一交互形式（硬约束，适用于本节全部交互）**：

- 纯文本编号清单，**完整列出、禁止截断、禁止只列前 N 个**；不使用任何结构化选择器或选项数受限的提问工具，所有宿主行为一致
- **禁止代用户选择**：不得自动选第一个、任务数最多或最近使用过的包
- 用户以「序号」或「包名」回复后必须**回显确认**所选内容；用户可直接回复多个序号/包名（逗号或空格分隔），逐个解析、去重、按字母序确认
- 同一轮执行内每个缺口只询问一次；用户明确拒绝补全（如回复"取消"）时输出当前进度说明并受控停止，不进入 Phase 2

##### 4.2.0.1 包名发现与选择（仅当所选模式仍需要包名且未提供）

**顺序契约（硬约束）：扫描 → 排除 → 再交互。** §4.2.1 全量扫描（不做包名过滤）与 §4.2.2 七条排除规则完整结束之前，不得输出候选包清单、不得发起任何用户交互；清单中的「候选类数」「共 {N} 个包」及统计行一律为**排除后口径**（排除后候选类数为 0 的包不进入清单），用户永远不会面对含排除项的清单。

1. 执行 §4.2.1 扫描（全部源码，**不做包名过滤**）
2. 对扫描结果**完整执行** §4.2.2 七条排除规则
3. 按"包名"分组聚合，得到每个包的候选类计数；按包名字母序输出：

```text
📋 可选包清单（共 {N} 个，请回复序号或包名，可多选）

| 序号 | 包名                 | 候选类数 | 备注           |
|:-----|:---------------------|---------:|:---------------|
| 1    | {package1}          |       {X} |                |
| 2    | {package2}          |       {Y} | 已在当前队列   |
| ...  | （全部列出，一个不漏，禁止截断） |  |                |

> 统计：扫描 {X} 个文件 → 排除 {Z} 个 → 候选 {Y} 个类，分布于 {N} 个包
> 执行模式：{全新开始/增量补充}
> 单次待调度任务上限：100 个类（§4.2.3 将在选定后校验）

请回复要维护的包（序号或包名，可多选）。
```

   「备注」列仅在**增量补充模式**下标注 `已在当前队列`（该包在既有队列中已有任务行），其余场景留空；重复类仍由 §4.2.2.1 按文件路径去重，本标注仅为提示。

4. 解析用户回复（序号/包名混用、逗号或空格分隔、去重）→ 得到 `{packages}` → 回显确认：`✅ 已选定包：{p1, p2, ...}（候选 {Y} 个类）`
5. **扫描复用**：本次扫描与排除结果保留复用，§4.2.2 之后的流程对同一结果应用所选包的前缀过滤，**不得重新扫描**（上下文截断恢复后如结果丢失，按本节重扫一次）
6. **零候选兜底**：排除后全项目候选类数为 0 → 输出以下提示并退出命令（无包可提供，不展示空清单、不询问并发）：

```text
ℹ️ 未发现需要单测维护的类
- 扫描文件：{X} 个 → 排除：{Z} 个 → 候选：0 个
- 已按 §4.2.2 排除测试代码、生成代码、接口、枚举、Mapper/DAO、配置类与纯数据载体

无可选包。请确认源码目录或包范围后重试。
```

##### 4.2.0.1-续 继续执行模式的队列内包识别（仅继续执行模式，在 §4.2.0.2 之前执行）

继续执行模式不重新选包（沿用已有队列），但必须让用户**区分出哪些包已在任务列表中及其当前状态**：

1. **读取队列并按包聚合**：读取 `unittest-queue.md` 全部任务行，按"包名"列分组统计各状态数量（`completed` / `failed` / `in_progress` / `pending`）
2. **输出队列内包进度表**（按包名字母序，完整列出，禁止截断）：

```text
📦 当前任务队列中的包（共 {N} 个包 / {M} 个任务，继续执行沿用此队列）

| 序号 | 包名                 | 总任务 | 已完成 | 失败 | 处理中 | 待处理 |
|:-----|:---------------------|------:|------:|----:|------:|------:|
| 1    | {package1}          |  {M1} |  {C1} | {F1} |  {I1} |  {P1} |
| 2    | {package2}          |  {M2} |  {C2} | {F2} |  {I2} |  {P2} |
| ...  | （全部列出，一个不漏，禁止截断） |     |       |     |       |       |

> 失败任务中可重试的将由 Phase 2-3 自动重试（timeout/critical_bug/retry_exhausted 除外）
> 继续执行不会新增队列之外的包；如需追加新包，请改用"增量补充"模式

{若用户显式传入了包名：⚠️ 你传入的 {p1, p2, ...} 不在当前队列中，继续执行不会处理它们；如需处理请改用"增量补充"模式}
```

3. **显式包名核验**：用户在继续执行模式下显式传入了 `--packages` 时，将其与队列内包名集合比对——
   - 传入包全部/部分不在队列中 → 在上表后输出上述 ⚠️ 提示，**逐个列出**不在队列中的包名；不退出、不自动转增量、不代用户决定，仅提示正确做法
   - 传入包全部已在队列中 → 无额外提示（继续执行本身就只处理队列内任务，包名仅作参考）
4. 本小节为**纯展示**，不询问选择、不等待确认；输出后直接进入 §4.2.0.2（并发数仍缺失/非法时）或按 §4.2 分支表"继续执行"行继续

##### 4.2.0.2 并发数确认（仅当 `{concurrency}` 未提供或非法）

1. 在包名确定**之后**询问（即使包名来自显式参数），便于用户参考任务数：

```text
⚙️ 请指定 worker 并发数
- 已选包：{p1, p2, ...}（候选 {Y} 个类）{或：沿用已有任务队列（见上方 📦 队列内包进度表）}
- 有效范围：2 ≤ N ≤ 10（MAX_CONCURRENCY = 10）
{若原值非法：- 你传入的 "{原值}" 无效：{非整数/小于 2/大于 10}，请重新指定}

请回复并发数（整数，2-10）。
```

2. 回复为整数且 `2 ≤ N ≤ 10` → 记为 `{concurrency}`，回显确认：`✅ 并发数：{N}`
3. **重试规则**：非法回复 → 输出 `❌ 无效并发数 "{回复}"，必须为 2-10 的整数` 并重新展示同一提示；**连续 3 次非法**后输出以下指引并受控停止（不设默认值、不静默取中值）：

```text
❌ 连续 3 次未获得有效并发数，命令停止。
- 用法：/helix:ut --packages <pkg1,pkg2,...> --concurrency <N>
- --concurrency：整数，2 ≤ N ≤ 10
- 示例：/helix:ut --packages com.example.service --concurrency 5
```

4. 并发数一经确定（显式或补全），写入队列元数据（§9.4 `worker 并发数`），上下文截断恢复后沿用此值调度

##### 4.2.0.3 补全完成后的衔接

`{packages}` 与 `{concurrency}` 视同显式传入，继续 §4.2.2 之后的排除/增量去重/门禁/确认流程；§4.2.3 任务数上限校验与 §4.2.4 任务清单确认的行为均不变。包选择回答"扫哪些包"，任务清单确认回答"这批类对不对"，两者是相互独立的业务门禁，不得合并或互相替代。

#### 4.2.1 扫描所有源代码文件

1. **扫描范围**：
   - 按语言扩展名扫描：`**/*.java`、`**/*.py`、`**/*.ts`、`**/*.js`、`**/*.go`
   - 按源码目录扫描：`src/main/**/*`、`app/**/*`、`lib/**/*`、`internal/**/*`

2. **去重与排序**：
   - 去重重复的文件路径
   - 按文件路径字母顺序排序

3. **提取信息**：
   - 从文件中提取类名、文件路径、包名
   - 不同语言的包名提取方式：
     - Java/Kotlin：从 `package` 声明提取
     - Python：从目录结构映射
     - JS/TS：从目录或 `package.json` 提取
     - Go：相对 `go.mod` 的路径

4. **包名过滤**：
   - 仅保留包名以指定包名开头的类。指定包名或来自显式参数，或来自 §4.2.0.1 的交互选择；未指定（继续执行模式沿用已有队列）时不过滤，直接进入排除策略
   - 显式参数可在扫描阶段即限定范围（见「参数说明」的仅扫描指定包及其子包）；§4.2.0.1 交互选定的包名按其步骤 5 在 §4.2.2 排除结果上做前缀过滤，不重新扫描，不改变排除后口径

#### 4.2.2 执行排除策略

按以下顺序排除，满足任一条件即排除：

| 排除顺序 | 排除标准 | 判断依据 |
|---------|---------|---------|
| 1 | 测试代码 | 文件路径包含 `src/test/`、`/tests/`、`__tests/` |
| 2 | 自动生成 | 文件内容注释包含 `@Generated`、`@AutoGenerated`、`generated by`、`DO NOT EDIT` |
| 3 | 接口类 | 类定义包含 `interface` 关键字（Java/TypeScript），或 `type` 定义且只有方法签名（Go） |
| 4 | 枚举类 | 类定义包含 `enum` 关键字（Java/TypeScript），或类继承自 `Enum`（Python），或文件中包含 `const` 且全大写常量组（Go） |
| 5 | Mapper/DAO（Java） | 文件名包含 `Mapper`、`Dao`、`DAO`，或类注解包含 `@Mapper`、`@Repository` |
| 6 | 配置/启动类（Java） | 类注解包含 `@Configuration`、`@SpringBootApplication`、`@EnableAutoConfiguration` |
| 7 | 纯数据载体 | 类名以 `DTO`、`VO`、`PO`、`BO`、`DO`、`Query`、`Req`、`Res`、`Request`、`Response` 结尾，且只有 getter/setter/toString/equals/hashCode/构造函数 |

#### 4.2.2.1 增量补充去重（仅增量补充模式）

合并前按文件路径对已有队列和新扫描结果去重：文件路径已在队列中存在的类不加入新任务列表。去重后再执行合并。

#### 4.2.3 任务数上限校验（用户确认与队列生成之前的强制门禁，所有执行模式）

**常量**：`MAX_PENDING_TASKS = 100`

**校验时机**：扫描、排除策略、包名过滤和增量去重完成之后，**任务列表展示与用户确认（§4.2.4）之前、任务队列文件生成之前**。所有执行模式（全新开始/继续执行/增量补充）都必须执行本校验；校验失败时用户根本不会被询问确认，队列也不会被创建或修改。

**待调度任务总数**按执行模式计算：

| 分支 | 待调度任务总数 |
|------|---------------|
| **全新开始** | 本次扫描生成的任务列表中所有类 |
| **继续执行** | 队列中所有非终态任务（`pending` + `in_progress`，含超时/孤儿对账后仍需处理的任务） |
| **增量补充** | 已有队列中的非终态任务（存量待执行）+ 本次新包名扫描去重后的新增任务 |

**注意**：`failed`（含 `timeout`、`critical_bug`、`retry_exhausted`）为终态，不计入待调度总数；Phase 2-3 的重试任务由本次执行内部创建，不参与本校验。

**校验失败（总数 > 100）时的阻断协议**：

1. 输出以下提示：

```text
❌ 任务数超限：当前待调度任务共 {N} 个，超过单次执行上限 MAX_PENDING_TASKS = 100 个，命令已阻断。

- 执行模式：{全新开始/继续执行/增量补充}
- 本次扫描新增任务：{X} 个
- 存量待执行任务：{Y} 个
- 包名范围：{package1, package2, ...}

请缩小包名范围（指定更具体的子包，或重新执行并选择更小的包集合，可显式传参或交互选择）后重试，确保单次待调度任务总数不超过 100 个类。
```

2. **直接退出命令**：不生成任务列表确认输出、不初始化/更新任务队列、不启动任何 worker、不执行 Phase 2-5、不生成汇总报告
3. 增量补充模式阻断后，已有队列文件保持原状不受影响，用户可换更小的包名重新执行增量补充
4. 继续执行模式如因遗留任务过多被阻断，建议用户改用更小范围的增量补充，或全新开始指定更小的包名

#### 4.2.4 生成任务列表并确认（仅全新开始）

**前提**：已通过 §4.2.3 任务数上限校验（待调度任务总数 ≤ 100）。

**任务排序**：按文件路径字母顺序排序

**输出格式**：
```markdown
## 单测维护任务列表

| 序号 | 类名 | 包名 | 文件路径 | 备注 |
|------|------|------|----------|------|
| 1 | {Class} | {package} | {path} | |

> 统计：扫描 {X} 个文件 → 排除 {Z} 个 → 包名过滤 {F} 个 → 需单测 {Y} 个
> 指定包名：{package1, package2, ...}
> worker 并发数：{concurrency}（MAX_CONCURRENCY = 10）
> 任务数上限校验：通过（{Y}/100）

> 显式传参路径：扫描范围已按 `--packages` 收窄，`包名过滤 {F}` 记 0（无再次过滤）；交互选择路径：{F} 为对排除结果做前缀过滤剔除的类数

共识别 {Y} 个类需单测维护。回复"继续"开始处理，或指出遗漏/调整。
```

### 4.3 Phase 2：单测维护执行（Ralph Loop 动态并行）

#### 4.3.1 Phase 2-1：初始化共享文件

| 分支 | 操作 |
|------|------|
| **继续执行** | 确保目录存在，验证主项目编译产物存在（缺失则重新执行 §4.1.6 预编译），托管权限，验证已有文件完整性，确定下一个 Worker ID |
| **全新开始** | 确保目录存在，会话文件已创建，初始化共享经验库；仅 `child_locked_append` 按 §7 确保锁目录不存在，`parent_finalize` 不检查、不创建、不删除锁。初始化空主结果文件，初始化任务队列，执行 §4.1.6 预编译，托管权限 |
| **增量补充** | 确保目录存在，验证主项目编译产物存在（缺失则重新执行 §4.1.6 预编译），托管权限，验证已有文件完整性，确定下一个 Worker ID |

**关键**：权限托管必须执行（见 §5）

#### 4.3.2 Phase 2-2：Ralph Loop 动态并行调度

**准入门禁**：进入主循环前必须已通过 §4.2.3 任务数上限校验（待调度任务总数 ≤ `MAX_PENDING_TASKS(100)`）。上下文截断后重新进入本阶段时，同样先按 §4.2.3 对队列非终态任务复核，超限则按阻断协议退出。
**核心参数（公共 Agent 语义）**：
- 最大并发度 = `{concurrency}`（由显式 `--concurrency` 参数或 §4.2.0.2 交互补全确定，`2 ≤ N ≤ MAX_CONCURRENCY(10)`，作为常量应用于本节全部 worker 调度）
- 任务超时时间 = 3600 秒（1 小时）
- 轮询间隔 = 2 秒

当前 `resources.ut` 声明 `dispatch_mode=background_agent` 时原样使用以上参数、文件轮询和 Agent ID；
声明 `dispatch_mode=native_job` 时由 adapter 映射 `spawn_job/wait_job`、真实容量和 runtime handle，
不得把容量暂满当能力不可用，也不得用固定文件轮询替代原生终态。两种模式的逻辑 Worker ID、
任务顺序、队列状态、3600 秒业务超时结果、重试与产物校验保持一致。

下方“主循环流程”的动态槽位（容量 = `{concurrency}`，上限 10）、`active_slots.agent_id`、result 文件轮询和 2 秒等待只属于
`dispatch_mode=background_agent` 的后台执行机制。`dispatch_mode=native_job` 必须保持同一逻辑
队列状态机，但 ready job 的准入数量、runtime handle 和等待完全使用 adapter 的真实
`dispatch_worker/wait_worker`：不预计算固定槽位，不把 handle 写入队列，不用文件出现代替原生终态；
每个 job 终态后仍校验相同 result/defects、目标测试文件、6 Loop、覆盖率和 write scope。

**内存跟踪**：`active_slots`（`{worker_id: {agent_id, start_time, class_name}}`），记录当前活跃的后台 agent。可通过队列文件 `in_progress` 行重建，**非事实来源**，仅为当前循环会话的便利缓存。

**真相源原则**：队列文件（`unittest-queue.md`）是 worker 状态的唯一事实来源；`active_slots` 是性能缓存，任何分歧以队列文件为准。每次对账时从队列重建 `active_slots`，运行时的状态变更先更新 `active_slots` 再原子写队列。

**队列原子更新**：每次 worker 状态变化时，必须用原子写协议（§6.2）更新 `unittest-queue.md`：读队列 → 修改目标行 → 写 `.tmp` → `mv` 重命名。同一迭代中多个 worker 完成时合并为一次批量写入。写入时机：worker 启动、worker 完成、超时/孤儿处理、对账修正。

**队列文件元数据头**：队列文件在原有表格基础上增加元数据块（见 §9.4 模板），`备注` 列在需要时填入结构化信息（如 `timeout`、`critical_bug`、`sync_failed`、`orphan_detected`、`retry_exhausted`、`retry:N|orig:W{ID}|reason:{text}`）。

---

**`dispatch_mode=background_agent` 主循环物理流程（后台 Agent 适配）**：

0. **循环启动对账**（首次进入时执行一次；后续每 5 次迭代执行增量对账）：
   - 扫描 `worker-*-result.md`，与队列对账：result 文件存在但队列行状态为 `pending`/`in_progress` → 读首行和结构化第二行解析状态，原子更新队列行为对应终态
   - 扫描 `worker-*` 临时目录：无 result 文件且已超时的 → 执行超时协议（见下方超时处理）
   - **锁超时检查**：仅 `experience_write_mode=child_locked_append` 时检查 `unittest-experience.lock` 是否存在且创建时间超过 10 分钟 → 如满足 ut.md 锁清理条件（见 §7），则清理锁目录；`parent_finalize` 跳过
   - 从队列 `in_progress` 行重建 `active_slots`；Agent ID 丢失的设为 `"unknown"`（后续通过轮询 result 文件检测完成）
   - 输出对账摘要：队列总数、已对账 result 文件数（含状态修正数）、孤儿目录数（含超时处理数）、活跃 worker 重建数、锁状态

1. **检测已完成 worker**：
   - 遍历 `active_slots`，检查 `worker-{ID}-result.md` 是否存在
   - **结果文件存在**：
     - 读取首行解析基础状态：
       - `状态: 成功` → 队列状态设为 `completed`。如第二行为 `同步状态: 失败`，`备注` 追加 `sync_failed`
       - `状态: 失败` → 队列状态设为 `failed`。如第二行为 `退出原因: 高危Bug`，`备注` 追加 `critical_bug`
       - `状态: 超时终止` → 队列状态设为 `failed`，`备注` 追加 `timeout`
     - 原子更新队列（设结束时间），从 `active_slots` 移除，输出进度
   - **结果文件不存在且已超时（>3600s）**：
     - 先复检避免竞态（文件可能刚好写入）
     - 不存在则原子写超时 result 文件（首行 `状态: 超时终止`），队列标记 `failed` + `备注: timeout`，从 `active_slots` 移除，输出超时警告

2. **填充空闲槽位**：
   - 可用槽位 = `{concurrency} - len(active_slots)`
   - 从队列取最多 `slots` 个 `pending` 任务（按序号升序）
   - 对每个任务：分配**单调递增 Worker ID**（永不重用）→ 启动后台 agent（`run_in_background: true`）→ 原子更新队列（`pending`→`in_progress`，记录 Worker ID、开始时间、Agent ID）→ 加入 `active_slots`
   - 输出：`[启动] Worker-{ID} 开始处理 {ClassName}（{N}/{Total}）`

3. **检查退出条件**：
   - 终态：`completed`、`failed`
   - 非终态（继续）：`pending`、`in_progress`
   - 队列中所有任务均为终态 → 退出循环，输出摘要，进入 Phase 2-3
   - 仍有非终态 → 继续

4. **输出进度，等待 2 秒后继续循环**

**进度输出**：
```
[进度] 迭代#{N} | 已完成={completed} | 失败={failed} | 处理中={in_progress} | 待处理={pending}
```

**阶段性维护报告（每完成 5 个类触发一次）**：

主循环内维护一个累计完成计数器；每当**新完成**（进入终态 `completed` 或 `failed`）的类数量跨过 5 的整数倍（第 5、10、15…个）时，在屏幕输出一份阶段性报告，包含整体进度和这 5 个类的维护情况。触发阈值的那次迭代中，若一次迭代内新增终态导致跨过多个阈值（如并发下同时完成 6 个），只输出一份报告，批次内容取**最近 5 个新终态的类**（从内存完成日志 `completion_log` 取最近 5 条）。

```text
📊 阶段性维护报告（已完成 {X}/{Total} 个类）

整体进度：
- 已完成：{completed} | 失败：{failed} | 处理中：{in_progress} | 待处理：{pending}
- 完成率：{percent}%（{X}/{Total}）
- 当前并发：{len(active_slots)}/{concurrency}

本批 5 个类维护情况：
| # | 类名 | Worker ID | 状态 | 覆盖率 | 发现缺陷 | 备注 |
|---|------|-----------|------|--------|----------|------|
| 1 | {ClassName} | W{ID} | 成功/失败 | {X}% | {N} 个 | {sync_failed/critical_bug/无} |
| ... |

> 下一次报告将在累计完成数达到 {X+5} 时输出（或全部完成时）。
```

**实现要点**：
- 内存维护 `completion_log`（按完成时间顺序追加 `{Worker ID, 类名, 状态, 覆盖率, 缺陷数, 备注}`），仅用于本报告，不是真相源（真相源仍为队列文件）
- 覆盖率、缺陷数从对应 `worker-{ID}-result.md` 的执行摘要表提取；提取失败时填 `-`
- 批次不足 5 个的尾部批次（总数非 5 的倍数时的最后一次）在主循环退出后并入 Phase 2-3 前的收尾输出，不单独触发
- 上下文截断恢复后，计数器从队列终态行数重建，已报告过的批次不重复输出

---

**超时与孤儿处理**：

- **超时协议**（任务 `in_progress` > 3600s 无 result 文件）：先复检避免竞态 → 原子写超时 result 文件（首行 `状态: 超时终止`）→ 原子更新队列（状态: `failed`，`备注: timeout`）→ 从 `active_slots` 移除 → 输出警告。Phase 2-4 汇总时如 worker 已覆盖超时文件，以 worker 实际结果为准
- **孤儿 Worker**（临时目录存在但无 result 文件，已超时）：执行超时协议，`备注` 设为 `orphan_detected`，不删除临时目录（Phase 2-5 统一清理）
- **陈旧孤儿**（临时目录存在但无队列条目、无 result 文件）：记日志，Phase 2-5 清理

---

**上下文截断恢复**：

会话上下文截断后，`active_slots` 丢失但队列文件和 result 文件均在磁盘保留。Phase 2-2 重新进入时执行对账（步骤 0），从磁盘重建全部状态后继续循环。Agent ID 丢失的 worker 通过轮询 result 文件跟踪；临时目录也不存在的 `in_progress` 任务标记 `failed`（`备注: lost_on_truncation`）。

---

**`dispatch_mode=background_agent` Agent 启动参数（后台 Agent 适配）**：

**Agent 工具调用方式**：
使用 Agent 工具启动后台 worker，参数如下：

```
Agent(
  description="Worker-{ID}: 为 {ClassName} 生成/维护单元测试",
  prompt="""你是 unit-test-worker agent。请按照 agents/unit-test-worker.md 的指引执行任务。

输入：
- 目标类文件路径：{path}
- Worker ID：{ID}
- 项目名：{ProjectName}
- 主项目路径：{MainProjectPath}
- 系统临时目录：{系统临时目录的绝对路径}
- Worker 临时目录：{WorkerTempPath}（父流程解析的 `{系统临时目录}/{ProjectName}/worker-{ID}/` 绝对路径）
- 主项目编译产物路径：{MainProjectPath}/build/classes/java/main 或 {MainProjectPath}/target/classes
- 共享经验库路径：docs/specs/active/{feature}/unittest-experience.md
- 共享经验库锁目录：docs/specs/active/{feature}/unittest-experience.lock（`child_locked_append` 使用；
  `parent_finalize` 只把它列为 forbidden scope，不检查、不创建、不删除）
- 独立输出文件：
  - 结果文件：docs/specs/active/{feature}/worker-{ID}-result.md
  - 缺陷文件：docs/specs/active/{feature}/worker-{ID}-defects.md
- Feature 上下文：{feature}
- 是否为重试：{否/是}
- 重试次数：{0/1/2}
- 原Worker ID：{无/原ID}
- 之前失败原因：{无/描述}

公共知识库（来自 docs/harness/ 和 docs/knowledge/ 完整内容）：
[插入完整的项目知识库内容]

需求上下文（来自 docs/specs/active/{feature}/，独立于知识库）：
[插入需求内容（如存在）]

开始执行。""",
  run_in_background=true
)
```

关键要点：
- `description`：简洁说明该 worker 的任务（包含 Worker ID 和类名）
- `prompt`：完整的 unit-test-worker 任务指令（见上方）
- `run_in_background=true`：在后台运行，不阻塞主循环
- 返回的 Agent ID 需记录到队列和 `active_slots` 中

**`dispatch_mode=native_job` 等价映射**：不得执行上方固定槽位、Agent ID 入队、2 秒文件轮询这些
物理步骤。父流程仍按同一排序为每个 pending 行分配单调递增 Worker ID，并把同一完整 prompt 交给
`dispatch_worker(job)`；adapter 根据真实容量准入，runtime handle 仅保留在内存。`wait_worker(handle)`
返回真实终态后，父流程再按相同产物、写域、6 Loop、测试与覆盖率门禁原子更新队列。容量暂满只等待
可用事件，不能改成能力缺失、失败或 inline fallback；上下文恢复时无法证明仍有真实 job 的旧
`in_progress` 行重置为 `pending`，不得模拟 handle 或沿用陈旧 Agent ID。

#### 4.3.3 Phase 2-3：失败重试机制

**重试规则**：
- 收集所有 `状态 = failed` 的任务行
- 检查 `备注` 列排除不应重试的：
  - `备注` 含 `timeout` → 不重试（ut.md 超时代写，非 worker 真实失败）
  - `备注` 含 `critical_bug` → 不重试（高危 Bug 阻断）
  - `备注` 含 `retry_exhausted` 或已有 `retry:2` → 不重试（已达上限）
- 其余 `failed` 任务进入重试队列
- 最多重试 2 次（总共执行 3 次）
- 重试也采用并行方式（每批最多 3 个）

**重试执行步骤**：
1. 读取队列文件，收集所有 `状态 = failed` 的任务行
2. 对每个 `failed` 任务，在队列中**新增一行**（新 `序号`，`状态 = pending`，`备注` 写入 `retry:{N}|orig:W{原ID}|reason:{原失败原因}`）
3. 将原 `failed` 行暂时保留（Phase 2-4 汇总时归档）
4. 使用原子写协议（§6.2）写入所有新增行
5. 返回 Phase 2-2，主循环将对新 `pending` 行分配新 Worker ID 并启动

**重试耗尽处理**：
- 重试后仍失败的 worker，由 Phase 2-2 主循环标记为 `failed`
- 当某任务重试次数已达上限（2 次），Phase 2-3 不再为其创建新 `pending` 行，在原 `failed` 行的 `备注` 追加 `retry_exhausted`

**重试 Worker ID 分配**：
- 重试任务由 Phase 2-2 主循环分配新的 Worker ID（永不复用原 ID）
- 启动参数必须包含：原Worker ID、重试次数、之前失败原因

#### 4.3.4 Phase 2-4：汇总所有 worker 输出

**执行步骤（单线程）**：
1. **先收集缺陷文件**：按 Worker ID 顺序检查 `worker-{ID}-defects.md`，存在则读取并暂存
2. **再收集结果文件**：按 Worker ID 顺序读取所有 `worker-{ID}-result.md`
3. **超时结果核验**：对队列中 `备注` 含 `timeout` 的任务，重新读取 result 文件首行，如 worker 已覆盖为成功/失败，以 worker 最新结果为准，并通过原子写协议更新队列中对应行的状态为 `completed` 或 `failed`，清除 `备注` 中的 `timeout`；即使仍为超时，也要检查 defects.md 是否存在
4. **重试记录去重**：对存在重试关系的任务（队列 `备注` 列含 `retry:N|orig:W{ID}`），按类名分组：
   - 同一类存在多条记录时，仅保留最终态的任务结果进入主文件（优先级：`completed` > `failed`）
   - 被去重的行在队列 `备注` 中追加 `（已归档）`，不在主结果文件中重复出现
   - Worker 输出文件（result + defects）均按最终态 Worker ID 引用，原始失败 Worker 的文件在归档步骤中一并处理
5. 汇总写入：将去重后的结果和缺陷汇总写入主文件
6. 经验库校验：`child_locked_append` 读取 `unittest-experience.md` 并统计本次已追加经验；`parent_finalize` 先校验各 worker final 的有界经验候选，再由父流程串行、去重、原子合并后统计
7. 归档 worker 文件：将已汇总的 worker 文件（含被去重的原始失败文件）重命名为 `archived-worker-*.md`
8. `child_locked_append` 按 §7 清理符合条件的残留锁目录（使用跨平台删除方式）；`parent_finalize` 不检查、不创建、不删除任何经验锁

#### 4.3.5 Phase 2-5：主项目统一验证（串行）

**关键**：这是唯一在主项目执行构建的阶段

**执行步骤**：
1. 从临时目录恢复缺失的测试文件（如同步失败）：
   - 读取 `备注` 含 `sync_failed` 或 result 文件第二行为 `同步状态: 失败` 的任务对应的 worker result 文件，提取「测试文件完整路径」和「临时目录备份路径」字段
   - 从临时目录 Read 测试文件内容，Write 到主项目目标路径
   - 验证文件存在性
2. **门禁 1：主项目编译验证**：
   - Gradle：`./gradlew clean compileTestJava --no-daemon --no-parallel --no-watch-fs -q`（如无 wrapper 则使用 `gradle`）
   - Maven：`./mvnw clean compile test-compile --batch-mode -q`（如无 wrapper 则使用 `mvn`）
3. **门禁 2：主项目测试运行**：
   - Gradle：`./gradlew test --no-daemon --no-parallel --no-watch-fs -q`（如无 wrapper 则使用 `gradle`）
   - Maven：`./mvnw test --batch-mode -q`（如无 wrapper 则使用 `mvn`）
4. **清理 worker 临时目录**：删除 `{系统临时目录}/{ProjectName}/worker-*`（保留共享缓存）
5. `child_locked_append` 清理残留锁目录 `docs/specs/active/{feature}/unittest-experience.lock`（如存在）；`parent_finalize` 不创建也不清理该锁

### 4.4 Phase 3：汇总报告与结果输出

#### 4.4.1 生成汇总报告

输出文件：
- `docs/specs/active/{feature}/unittest-results.md` - 单测执行结果
- `docs/specs/active/{feature}/unittest-defects.md` - 发现的逻辑缺陷
- `docs/specs/active/{feature}/unittest-summary.md` - 汇总报告

#### 4.4.2 输出最终结果

#### 4.4.3 清理宿主权限/审批准备（成功输出后）

**仅执行成功时清理**：调用当前 `resources.ut` 的 `permission_cleanup`。只撤销本次 UT 明确创建且
ownership 可验证的配置或临时授权，不删除用户已有配置，并把 adapter 返回的清理事实写入
`unittest-session.md`。不需要项目配置写入的 adapter 仍必须返回 `permission_cleanup=not_required`。

**执行失败、中断、或有未完成任务**：不清理托管权限

未执行主项目统一验证时不得报告 UT 完成；缺少本轮编译、测试、覆盖率验证的新鲜证据时同样不得报告完成。

---

## 5. 宿主权限与调度适配协议

所有执行模式都必须读取当前 `resources.ut`，并在 Phase 2 首个 worker 派发前完成下列逻辑动作：

1. `permission_prepare(main_project, temp_root, required_tools)`：使 worker 能准备临时目录、生成和运行
   测试、验证覆盖率、同步测试文件并写独立结果；不得绕过宿主 sandbox 或审批。
2. `dispatch_worker(job)`：把本文 `Agent(..., run_in_background=true)` 映射为真实派发；job 必须包含
   Worker ID、`agents/unit-test-worker.md`、`read_scope`、写域、禁写域、预期产物和验证。read_scope
   只能包含主项目被测源码/构建配置、目标测试文件、当前 UT session/queue 与已验证依赖结果；
   临时副本路径必须先解析，禁止未解析 glob、其他 worker 私有目录或仓库外任意读取。
3. `wait_worker(handle)`：取得当前宿主认可的真实终态，再校验 result/defects、目标测试文件和写域。
4. `permission_cleanup(session_record)`：仅在 Phase 3 成功路径撤销本轮新增项；失败、中断或有未完成
   任务时保留可恢复状态。

`resources.ut` 必须声明 `dispatch_mode`、`experience_write_mode`、runtime handle 是否允许写入队列、
权限准备和清理方式。支持原有项目配置与后台 Agent 的 adapter 必须完整保持配置追加不覆盖、
24 小时重新托管、系统临时目录与主项目绝对路径授权、成功后精确回收语义；具体项目路径和配置键
只存在于该 adapter，不进入公共命令。其他 adapter 使用当前宿主审批与原生 job 生命周期实现同一
业务结果，不能通过修改宿主全局配置、自动批准或模拟 Agent ID 达成。

---

## 6. 文件架构与状态机

### 6.1 文件架构

| 文件类型 | 命名模式 | 访问者 | 读写权限 | 说明 |
|---------|---------|---------|---------|------|
| **会话标识文件** | `unittest-session.md` | ut.md | ut.md 独占读写 | 记录当前会话 ID、开始时间、状态、托管权限 |
| **任务队列状态** | `unittest-queue.md` | ut.md | ut.md 独占读写 | 仅由 ut.md 主线程更新 |
| **Worker 独立结果** | `worker-{ID}-result.md` | 对应 Worker + ut.md | Worker 独占写，ut.md 只读 | 每个 Worker 只写自己 ID 对应的文件 |
| **Worker 独立缺陷** | `worker-{ID}-defects.md` | 对应 Worker + ut.md | Worker 独占写，ut.md 只读 | 每个 Worker 只写自己 ID 对应的文件 |
| **已归档 Worker 文件** | `archived-worker-*.md` | ut.md | ut.md 只读 | 汇总后不直接删除，重命名归档 |
| **共享经验库** | `unittest-experience.md` | 由 `experience_write_mode` 选择 Worker 锁追加或父流程串行合并 | Worker 始终可读；写入必须遵守 adapter | 永远不删除 |
| **经验库锁目录** | `unittest-experience.lock` | 仅 `child_locked_append` 的 Worker | 原子创建（mkdir）/ 删除（rm -rf） | `parent_finalize` 不创建 |
| **主结果文件** | `unittest-results.md` / `unittest-defects.md` / `unittest-summary.md` | ut.md | ut.md 独占读写 | 仅由 ut.md 汇总阶段写入 |
| **归档旧状态文件** | `unittest-*-{session_id}-{timestamp}.md` | ut.md | 只读 | 全新开始时归档 |

### 6.2 原子写文件协议

所有更新状态的文件操作必须使用临时文件 + rename 原子操作：
1. 先写临时文件：`{原文件名}.tmp`
2. 再 `mv` 原子重命名为目标文件名
3. 如目标文件已存在，根据场景决定是跳过还是覆盖

**适用文件**：`unittest-queue.md`、`unittest-session.md`、`worker-{ID}-result.md`（ut.md 超时代写时）、主结果文件

### 6.3 任务状态机

| 状态 | 说明 | 可转换到 |
|------|------|---------|
| `pending` | 待处理 | `in_progress` |
| `in_progress` | 处理中 | `completed` / `failed` |
| `completed` | 已完成 | — 终态 |
| `failed` | 失败 | `pending`（重试） |

**状态转换规则**：
```
pending → in_progress：启动 worker 时
in_progress → completed：检测到结果文件且状态为成功
in_progress → failed：检测到结果文件且状态为失败；或超时（ut.md 代写 `状态: 超时终止`）
failed → pending：Phase 2-3 创建重试任务
```

**`failed` 子类型（通过 `备注` 列区分）**：
- `备注` 含 `timeout` → 超时终止，不重试
- `备注` 含 `critical_bug` → worker 发现高危 Bug 退出，不重试
- `备注` 含 `retry_exhausted` → 重试次数耗尽（≥2 次），不再重试
- `备注` 含 `retry:N|orig:W{ID}|reason:{text}` → 第 N 次重试中

**`completed` 子类型（通过 `备注` 列或 result 文件第二行区分）**：
- result 文件第二行为 `同步状态: 失败` → 测试通过但文件同步失败，`备注` 记录 `sync_failed`，由 Phase 2-5 恢复

**超时终止处理**：
- ut.md 检测到超时，代写 result 文件首行 `状态: 超时终止`，队列状态设为 `failed`，`备注: timeout`
- Phase 2-4 汇总时需重新读取 `备注` 含 `timeout` 的 result 文件首行：
  - worker 已更新为 `状态: 成功` → 改为 `completed`，清除 `备注` 中 `timeout`
  - worker 已更新为 `状态: 失败` → 保持 `failed`，`备注` 更新为实际失败原因
  - 仍为 `状态: 超时终止` → 保持 `failed`，`备注: timeout`，不重试

---

## 7. 锁协议

本节只在 `experience_write_mode=child_locked_append` 时执行。当前仅有一个锁目录：
`unittest-experience.lock`，用于控制共享经验库的并发追加写入。`parent_finalize` 时所有 Worker 禁止
写共享经验库或锁目录，只在 final 返回经验候选，由父流程验证、去重后串行原子合并。

**锁超时时间**：10 分钟

**测试文件同步无需锁**：每个 worker 只负责自己目标类对应的测试文件（`{ClassName}Test.java`），不同 worker 写入的测试文件路径天然不同，不存在并发写冲突。

**锁超时清理**：`child_locked_append` 下 ut.md 可以删除锁目录，但必须满足以下任一条件；`parent_finalize` 不检查、不创建、不删除锁目录，本节不适用：
1. 所有 worker 都已完成（`active_slots` 为空，且队列中无 `in_progress` 或 `pending` 任务）
2. 锁目录创建时间已超过 10 分钟（与 worker 锁超时阈值一致，避免10-30分钟死区导致经验持续丢失）

---

## 8. 异常处理矩阵

| 异常类型 | 触发场景 | 处理策略 |
|---------|---------|---------|
| **任务数超限** | 待调度任务总数（新扫描 + 存量待执行）> 100 | 按 §4.2.3 阻断协议输出提示并退出命令，不询问确认、不生成队列、不启动任何 worker |
| **参数缺失或非法** | `--packages`/`--concurrency` 未提供、非整数或越界 | 不退出：按所选执行模式经 §4.2.0 交互补全（包名发现 + 用户选择 + 并发确认）；并发连续 3 次无效输入后输出用法指引并受控停止 |
| **上次运行中断** | 检测到存在残留状态文件 | 展示状态，询问用户选择（继续/重新/增量） |
| **Worker 超时** | in_progress 状态超过 3600 秒 | ut.md 代写超时终止结果文件（首行 `状态: 超时终止`），任务标记为 `failed`（`备注: timeout`，不重试）。Phase 2-4 汇总时检查是否有 worker 后续覆盖（状态变为 成功/失败），如有则以 worker 最新结果为准 |
| **锁目录残留** | `child_locked_append` 检测到存在过期的锁目录 | ut.md Phase 0-0 和 Phase 2-4 按 §7 负责清理；`parent_finalize` 不检查、不创建、不删除锁 |
| **权限阻断** | Worker 遇到权限被拒绝 | Worker 立即写入失败结果，ut.md 检查并补齐权限后重试 |
| **测试文件同步失败** | Worker 同步到主项目失败 | Worker 保留临时目录，ut.md Phase 2-5 检查并手动恢复 |
| **配额中断** | 执行时间或资源超限 | 保留临时文件和状态，下次可继续执行 |
| **结果文件格式错误** | worker 输出无法解析 | ut.md 尽力解析，失败则标记为 failed，归档无效文件 |
| **主项目源码在执行中变化** | 用户修改了主项目源码 | 记录警告，继续执行，最终统一验证时重新编译 |

---

## 9. 附录：输出模板

### 9.1 全新开始输出模板

```
🆕 全新开始模式
- 新会话 ID：{session_id}
- 归档旧状态文件：{A} 个
- 归档旧 worker 文件：{W} 个
- 清理旧会话托管 settings.local 权限：{已清理/无/跳过，原因}
- `child_locked_append` 清理锁目录：{0/1} 个；`parent_finalize`：当前模式不适用
- 清理临时目录：{M} 个
- 保留经验库记录：{E} 条
```

### 9.2 继续执行输出模板

```
🔄 继续上次执行模式
- 会话 ID：{session_id}
- 恢复 archived worker 文件：{R} 个
- 重置 in_progress 任务：{IP} 个
- `child_locked_append` 清理锁目录：{0/1} 个；`parent_finalize`：当前模式不适用
- 清理临时目录：{M} 个
- 保留经验库记录：{E} 条
```

### 9.3 增量补充输出模板

```
📈 增量补充模式
- 会话 ID：{session_id}
- 本次新增包名：{package1, package2, ...}
- 恢复 archived worker 文件：{R} 个
- 重置 in_progress 任务：{IP} 个
- `child_locked_append` 清理锁目录：{0/1} 个；`parent_finalize`：当前模式不适用
- 清理临时目录：{M} 个
- 保留经验库记录：{E} 条
```

### 9.4 unittest-queue.md 初始化模板

```markdown
## 任务队列状态

> 会话ID：{session_id}
> 最后更新、循环迭代计数等元数据以下方「元数据」块为唯一事实源，不在表头重复维护

| 序号 | 类名 | 包名 | 文件路径 | 状态 | Worker ID | 开始时间 | 结束时间 | Agent ID | 失败原因 | 备注 |
|------|------|------|----------|------|-----------|----------|----------|----------|----------|------|

### 元数据

- 会话ID：{session_id}
- 最后更新：{YYYY-MM-DD HH:MM:SS}
- 循环迭代计数：0
- 活跃Worker数：0
- worker 并发数：{concurrency}（本次会话的最大并发常量，上下文截断恢复后沿用此值调度）

### 状态说明
- pending：待处理
- in_progress：处理中
- completed：已完成（终态）
- failed：失败（终态，进入 Phase 2-3 判断是否重试）

> `failed` 和 `completed` 的子类型通过 `备注` 列区分：
> - `timeout` — 超时终止，不重试
> - `critical_bug` — 高危 Bug 退出，不重试
> - `sync_failed` — 测试通过但文件同步失败（completed 任务的子类型，Phase 2-5 恢复）
> - `retry:N|orig:W{ID}|reason:{text}` — 第 N 次重试
> - `retry_exhausted` — 重试次数耗尽
> - `orphan_detected` — 孤儿临时目录
> - `lost_on_truncation` — 上下文截断丢失

### 包名集合记录
- 指定包名：{package1, package2, ...}
```

### 9.5 unittest-experience.md 初始化模板

```markdown
## 会话问题经验知识库

### 已解决问题清单

| 序号 | Worker ID | 类名 | 问题类型 | 问题特征 | 解决方案 | 适用场景 | 写入时间 |
```

---

## 10. 附录：参数解析快速参考

### 10.1 参数解析流程图

```
$ARGUMENTS 输入
    ↓
宽容提取 --packages/-p（缺失或为空 → 标记"未提供"，不退出）
    ↓
宽容提取 --concurrency/-c（缺失/非整数/越界 → 标记"未提供或非法"并保留原值，不退出）
    ↓
分割包名列表（逗号分隔，去重）
    ↓
检查残留状态文件
    ↓
    ├─ 存在残留 → 询问用户选择（继续/重新/增量）
    │              ├─ 继续 → 进入 Phase 0
    │              ├─ 重新 → 进入 Phase 0（全新开始）
    │              └─ 增量 → 进入 Phase 0（包名可显式给，也可后续交互选）
    │
    └─ 无残留 → 进入 Phase 0（全新开始）
                ↓
      Phase 1 入口：按缺口矩阵仍有参数缺口？
        ├─ 是 → §4.2.0 参数补全
        │         ├─ 继续执行模式 → §4.2.0.1-续 输出队列内包进度表（识别已有包）
        │         ├─ 需要包名 → 扫描 + 排除 + 按包聚合 → 完整列出候选包清单
        │         │              → 用户选择（零候选 → 输出提示并退出）
        │         └─ 需要并发 → 提示 2-10 → 用户输入（连续 3 次无效 → 输出指引并停止）
        └─ 否 → 直接继续（继续执行模式仍输出 §4.2.0.1-续 队列内包进度表）
                ↓
      扫描/排除/包名过滤/增量去重后、用户确认前：
      待调度任务总数（新扫描 + 存量待执行）> 100？
        ├─ 是 → 输出超限提示，退出命令（不确认、不生成队列）
        └─ 否 → 生成任务列表 → 用户确认 → Phase 2 调度 → Phase 3 汇总
```

### 10.2 参数解析详细步骤

**步骤 1：原始输入获取**
- 输入源：`$ARGUMENTS`
- 输入格式：空格分隔的参数列表

**步骤 2：包名参数提取（宽容提取，不退出）**
- 查找 `--packages` 或 `-p` 参数
- 取该参数后面的值作为包名集合
- 包名集合格式：逗号分隔，允许空格（自动去除）
- 未找到参数或解析结果为空列表 → 标记 `{packages_arg}` 为 `未提供`，继续后续步骤，由 §4.2.0.1 补全
- 示例：
  - `--packages com.example.service, com.example.controller`
  - `-p com.example.service`

**步骤 3：并发数参数提取（宽容提取，不退出）**
- 查找 `--concurrency` 或 `-c` 参数
- 取该参数后面的值，必须解析为整数
- 校验规则：`2 ≤ N ≤ MAX_CONCURRENCY`（`MAX_CONCURRENCY = 10`）
- 未找到参数 → 标记 `未提供`；值缺失、非整数或越界 → 标记 `非法` 并记录原始值（用于提示语）；两者均不退出，由 §4.2.0.2 补全
- 校验通过后记为 `{concurrency}`，作为 Phase 2-2 worker 调度的最大并发常量

**步骤 4：包名解析**
- 按逗号分割字符串
- 对每个包名去除首尾空格
- 过滤空字符串
- 结果：字符串数组 `[pkg1, pkg2, ...]`

**步骤 5：状态检测**
- 检查文件：
  - `docs/specs/active/{feature}/unittest-session.md`
  - `docs/specs/active/{feature}/unittest-queue.md`
  - `docs/specs/active/{feature}/worker-*.md`
- 判断是否存在残留状态

**步骤 6：执行模式选择**
- **存在残留**：
  - 读取 `unittest-session.md` 获取上次状态
  - 向用户展示选项
  - 记录用户选择
- **无残留**：
  - 默认选择"全新开始"

**步骤 7：增量补充特殊处理**
- 若用户选择"增量补充"：
  - 包名或已显式提供，或由 §4.2.0.1 交互选定
  - 确定后新包名与旧队列合并（去重）

**步骤 8：参数补全（§4.2.0）**
- 按缺口矩阵判定所选执行模式仍缺的参数
- 缺包名 → §4.2.0.1：扫描 + 排除 + 按包聚合 → 完整列出候选包清单 → 用户选择 → 回显确认
- 缺并发 → §4.2.0.2：展示范围与已选包任务数 → 用户输入 → 连续 3 次无效则停止
- 补全值与显式传入完全等价

### 10.3 参数解析示例

**示例 1：完全无参数 → 交互补全**
```text
输入: $ARGUMENTS = ""
处理:
  - 包名 = 未提供；并发 = 未提供（均不退出）
  - 检查残留 → 假设无残留 → 全新开始
  - Phase 0 完成 → §4.2.0.1：扫描 + 排除 + 按包聚合 → 完整列出候选包清单 → 用户回复 "1,3"
  - 回显已选包 → §4.2.0.2 提示并发 → 用户回复 "5" → {concurrency} = 5
  - 复用扫描结果过滤所选包 → §4.2.3 校验 → §4.2.4 任务清单确认 → Phase 2
```

**示例 2：并发非法 → 交互纠正**
```text
输入: $ARGUMENTS = "--packages com.example.service --concurrency 12"
处理:
  - 包名 = ["com.example.service"]（有效）；并发 = 12 > 10 → 标记"非法"，不退出
  - 假设无残留 → 全新开始 → Phase 0 → §4.2.0.2：提示含「你传入的 "12" 无效：大于 10」
  - 用户回复 "8" → {concurrency} = 8 → 继续 §4.2.2 之后流程
```

**示例 3：参数齐全 → 直达**
```text
输入: $ARGUMENTS = "--packages com.example.service,com.example.controller --concurrency 5"
处理:
  - 包名 = ["com.example.service", "com.example.controller"]；concurrency = 5（合法）
  - 无残留 → 全新开始 → §4.2.0 整节跳过
  - 仅扫描指定包及其子包，Phase 2-2 最大并发 = 5
```

**示例 4：简写参数，增量补充**
```text
输入: $ARGUMENTS = "-p com.example.newpkg -c 3"
处理:
  - 包名 = ["com.example.newpkg"]；concurrency = 3（合法）→ §4.2.0 跳过
  - 假设存在残留文件
  - 询问用户后选择"增量补充"
  - 新包名加入已有队列
```

**示例 5：缺包名 + 残留 + 继续执行 → 展示队列包进度后只补并发**
```text
输入: $ARGUMENTS = ""
处理:
  - 包名 = 未提供；并发 = 未提供
  - 存在残留 → 用户选择"继续执行" → 包名无需补全（沿用已有队列）
  - Phase 0 → §4.2.0.1-续：输出队列内包进度表（每个包的 已完成/失败/处理中/待处理 计数，完整列出）
  - §4.2.0.2：只询问并发 → 用户回复 "5" → 继续 §4.2 分支"继续执行"
```

**示例 6：继续执行 + 显式传入不在队列中的包 → 提示不处理**
```text
输入: $ARGUMENTS = "--packages com.example.newpkg --concurrency 5"
处理:
  - 包名 = ["com.example.newpkg"]；concurrency = 5（合法）→ §4.2.0 交互部分跳过
  - 存在残留 → 用户选择"继续执行"
  - §4.2.0.1-续 仍执行：输出队列内包进度表；核验发现 com.example.newpkg 不在队列中
  - 追加 ⚠️ 提示："com.example.newpkg 不在当前队列中，继续执行不会处理它们；如需处理请改用增量补充模式"
  - 不退出、不自动转增量，继续执行队列内任务
```

### 10.4 常见场景处理

| 场景 | 处理方式 |
|------|---------|
| `--packages` 参数缺失或值为空 | 不退出：按所选模式经 §4.2.0.1 扫描并完整列出候选包，由用户选择（继续执行模式无需补全） |
| `--concurrency` 缺失、非整数、`< 2` 或 `> 10` | 不退出：经 §4.2.0.2 由用户重新指定，原值在提示中回显；连续 3 次无效输入后输出用法指引并受控停止 |
| §4.2.0.1 发现排除后候选类为 0 | 输出"未发现需要单测维护的类"提示并退出命令，不展示空清单、不询问并发 |
| 继续执行模式下识别队列中已有的包 | §4.2.0.1-续 输出队列内包进度表（每包的 总任务/已完成/失败/处理中/待处理 计数），完整列出、禁止截断；纯展示不询问 |
| 继续执行模式显式传入的包不在队列中 | 在包进度表后逐个列出该包名并 ⚠️ 提示"继续执行不会处理；如需处理请改用增量补充"，不退出、不自动转增量 |
| 用户在 §4.2.0 中拒绝选择包或并发 | 输出当前进度说明并受控停止，不进入 Phase 2 |
| 待调度任务总数（新扫描 + 存量待执行）> 100 | 按 §4.2.3 阻断协议提示后退出命令（发生在用户确认与队列生成之前） |
| 包名包含空格 | 自动去除空格，按逗号分割 |
| 同一个包名重复指定 | 自动去重，只保留一个 |
| 交互中用户混用序号与包名 | 逐个解析、去重、按字母序回显确认 |
| 参数中包含其他未知参数 | 忽略未知参数，只处理 4 个已定义参数 |
宿主资源声明：`resources.runtime`、`resources.subagent_protocol`、`resources.telemetry_protocol`、`resources.ut`。
