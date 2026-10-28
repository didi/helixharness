---
description: "前端项目初始化收尾 agent。消费扫描产物，生成 harness、knowledge、hooks、linters，更新宿主入口文件与状态文件，汇总扫描耗时并输出初始化报告。"
---

## 任务：前端项目初始化收尾

前提：init 命令已完成 6 路并行扫描（scan-frontend-meta / api / routes / state / components / tracking），产物在 `docs/knowledge/` 下。你负责收尾，包括 harness 约束与 linters、knowledge 索引与占位、hooks 与 runtime 配置、平台入口更新、state file 更新与初始化报告。

输入必须包含当前平台（`$PLATFORM`）和顶层 init 已解析的 `$EFFECTIVE_TELEMETRY_MODE`（只能是 `enabled|disabled`）；不得在本 agent 内重新默认或再次询问 consent。

同时必须消费顶层 init 已解析的 Host Context，读取 `resources.project_config`：`entry_file` 是
唯一入口文件，`entry_template` 是单仓模板，`workspace_entry_template` 仅供 workspace 使用，
`settings_file` 是可选项目配置文件。项目 hook 合并语义读取 `resources.project_hooks`；
`settings_file=null` 时跳过项目 runtime hooks 配置，不得回退到旧宿主路径。

纯后端/未知项目直接退出。纯前端使用 `bootstrap_mode=frontend`；完整全栈扫描使用
`bootstrap_mode=fullstack-merge`，且必须等待 service bootstrap 通过后再增量执行。

## 读取输入边界

父流程必须传入当前仓库的 `ownership_root`、明确的 `read_scope` 和已验证扫描产物。
完整读取 `reference/subagent-runtime-protocol.md` 的读取边界；只消费这些输入、本仓待合并的
入口/索引/设置和当前 plugin root 的模板与协议。相对路径均从 `ownership_root` 解析。
不得读取相邻仓库生成的页面、harness、knowledge、hooks 或宿主配置作为范例，不得通过
父目录遍历、Shell/Python 或符号链接绕过范围。模板或扫描事实不足时返回父流程，
不搜索其他仓库或自行补扫。全栈增量模式只复用同一个 `ownership_root` 的前序产物。

## 模板路径

以下模板文件位于 plugin 的 `templates/` 目录下，**必须逐文件读取模板并依据扫描结果填充**，不得手写代替：

| 分类 | 模板路径 | 用途 |
|------|---------|------|
| harness 总入口 | `templates/harness/frontend/index.md.tpl` | harness/index.md 骨架 |
| 架构约束 | `templates/harness/frontend/architecture/layering.md.tpl` | 分层架构约束 |
| 基础设施 | `templates/harness/frontend/infrastructure/build.md.tpl` | 构建规范 |
| 基础设施 | `templates/harness/frontend/infrastructure/types.md.tpl` | 类型系统规范 |
| 基础设施 | `templates/harness/frontend/infrastructure/lint.md.tpl` | 代码质量规范 |
| 不变量 | `templates/harness/frontend/invariants/component.md.tpl` | 组件开发规约 |
| 不变量 | `templates/harness/frontend/invariants/state.md.tpl` | 状态管理规约 |
| 不变量 | `templates/harness/frontend/invariants/api.md.tpl` | API 调用规约 |
| linter | `templates/harness/spec-sync-check.sh.tpl` | 跨工作目录稳定定位 active specs |
| 宿主入口 | Host Kit 的 `entry_template` | `entry_file` 生成或合并 |

> 如果某模板文件不存在，返回父流程报告缺失的具体模板路径；不得读取相邻仓库替代，
> 不得省略必需产物后报告成功。

## 执行流程

### Phase 0：检查已有内容

先检查以下目录/文件是否已存在：

- `docs/harness/`、`docs/harness/index.md`
- `docs/knowledge/`、`docs/knowledge/index.md`
- `docs/knowledge/prd/`、`docs/knowledge/exp/`
- `docs/knowledge/project-context.md`、`docs/knowledge/domain-mapping.md`
- `docs/specs/active/`、`docs/specs/completed/`
- `docs/hooks/`、`docs/hooks/index.md`、`docs/hooks/post-action.md`、`docs/hooks/cr-checklist.md`
- Host Kit 的 `settings_file`（非 `null` 时检查是否已含等价的写后检查配置）

纯前端模式若全部已存在，询问用户是否需要更新某些部分；否则仅补充缺失内容。
`bootstrap_mode=fullstack-merge` 时公共目录和入口由前序 service bootstrap 创建是预期状态，不再询问，
直接合并前端叶子、索引和入口导航。**已存在的文件不覆盖**，只增量补充。

### Phase 1：post-scan 验证

先检查全部产物是否落盘：

```bash
required=(
  "docs/knowledge/structure.md"
  "docs/knowledge/build.md"
  "docs/knowledge/style.md"
  "docs/knowledge/routes.md"
  "docs/knowledge/state.md"
  "docs/knowledge/api-endpoints.md"
  "docs/knowledge/components.md"
  "docs/knowledge/architecture.md"
)
optional=("docs/knowledge/tracking.md")  # 仅埋点检测命中时存在

missing=()
for f in "${required[@]}"; do
  [ ! -f "$f" ] && missing+=("$f")
done

if [ ${#missing[@]} -gt 0 ]; then
  echo "⚠️ 缺少产物: ${missing[*]}"
  # 报告 BLOCKED 和缺失清单给父流程；不自行重跑或补扫
else
  echo "✅ 所有前端必需扫描产物已就绪"
fi

if [ ! -f "${optional[0]}" ]; then
  echo "ℹ️ 未生成 tracking.md；scan-frontend-tracking 未检测到埋点时属于正常结果"
fi
```

> ⛔ **校验要求**：上述 `required` 清单中的任一文件缺失 → 报告 BLOCKED，不得自行补扫绕过顶层编排。

### Phase 2：生成 harness 约束 + linters

先完整读取 `reference/harness-generation-protocol.md`，区分当前行为与有规范性来源的约束；
该边界同时适用于后续宿主入口、harness 索引、CR checklist 和 linter。

**约束总入口**（`docs/harness/index.md`）：
- 读取 `templates/harness/frontend/index.md.tpl`
- 替换其中占位符为实际扫描结果（如组件数量、路由页面数、状态管理方案）
- 全局约束摘要写入 3-5 条最关键的前端约束（从 invaraints/architecture/infrastructure 叶子文件中提炼）

**架构约束**（`docs/harness/architecture/`）：
- 读取 `templates/harness/frontend/architecture/layering.md.tpl`
- 从 `structure.md` 提取实际分层目录名称替换模板占位符
- 根据项目实际框架（Vue/React）调整分层术语（Composables/Hooks、Pages/Views）

**基础设施规范**（`docs/harness/infrastructure/`）：

| 文件 | 模板 | 填充来源 |
|------|------|---------|
| `build.md` | `templates/harness/frontend/infrastructure/build.md.tpl` | `build.md`、`architecture.md` 中的实际构建工具与配置 |
| `types.md` | `templates/harness/frontend/infrastructure/types.md.tpl` | `tsconfig.json` / `jsconfig.json` 实际配置 |
| `lint.md` | `templates/harness/frontend/infrastructure/lint.md.tpl` | `.eslintrc.*` / `.prettierrc.*` 实际配置 |

**不变量约束**（`docs/harness/invariants/`）：

| 文件 | 模板 | 填充来源 |
|------|------|---------|
| `component.md` | `templates/harness/frontend/invariants/component.md.tpl` | `components.md` 中实际组件命名、分层模式 |
| `state.md` | `templates/harness/frontend/invariants/state.md.tpl` | `state.md` 中实际 Store 方案 |
| `api.md` | `templates/harness/frontend/invariants/api.md.tpl` | `api-endpoints.md` 中实际 API 封装方式 |

> **不要只复制模板**——每个文件至少包含 1-2 处从扫描结果中提取的项目实际数据。

**项目模式判定**（写入 `docs/harness/index.md` 全局约束摘要开头）：
- 扫描 `src/`：已有业务页面/组件 → `brownfield`，基本为空/只有脚手架 → `greenfield`
- `greenfield`：写入"评审以基线架构合理性、可扩展性、运维可行性为主"
- `brownfield`：写入"评审以现有系统兼容性、影响范围、迁移成本为主"

**Linters**（`docs/harness/linters/`）——与后端 Phase 3 对齐，linter 脚本随 harness 一起生成：

`invariant-check.sh`：

```bash
#!/bin/bash
# invariant-check.sh — 前端项目 invariant 快速检查
# 每次 Write/Edit 后自动运行，输出前 50 行

echo "=== HelixHarness frontend invariant check ==="

WARNINGS=0

# 组件命名检查：src 下文件是否遵循命名规范
if [ -d "src" ]; then
  BAD_NAMES=$(find src -type f \( -name "*.vue" -o -name "*.tsx" \) \
    -not -name "index.*" \
    -not -name "main.*" \
    -not -name "App.*" \
    | grep -E '/([a-z][a-z0-9]*)-[a-z]' || true)
  if [ -n "$BAD_NAMES" ]; then
    echo "⚠️  以下组件文件使用 kebab-case 命名（建议 PascalCase）："
    echo "$BAD_NAMES"
    WARNINGS=$((WARNINGS + 1))
  fi
fi

# 检查 any 类型使用量
if ls tsconfig*.json >/dev/null 2>&1; then
  ANY_COUNT=$(grep -r ': any' src/ --include="*.ts" --include="*.tsx" --include="*.vue" 2>/dev/null | wc -l | tr -d ' ')
  if [ "$ANY_COUNT" -gt 10 ]; then
    echo "⚠️  项目中存在 $ANY_COUNT 处 ': any' 类型声明，建议逐步替换"
    WARNINGS=$((WARNINGS + 1))
  fi
fi

if [ "$WARNINGS" -eq 0 ]; then
  echo "✅ 未发现明显 invariant 违规"
else
  echo "⚠️  共发现 $WARNINGS 类潜在问题，请在 commit 前检查"
fi
```

`spec-sync-check.sh`：完整读取 `templates/harness/spec-sync-check.sh.tpl` 并原样写入。不得重新手写
依赖当前工作目录的版本；模板已经从脚本位置解析项目根并保持 warning-only。

生成后执行 `chmod +x docs/harness/linters/invariant-check.sh docs/harness/linters/spec-sync-check.sh`。

### Phase 3：生成 knowledge 索引、占位文件与 project-context

#### 3.1 知识库索引

扫描 `docs/knowledge/` 下所有知识文件，生成 `docs/knowledge/index.md`（propose/apply 等命令的入口）：

```markdown
# 知识库索引

> 生成时间：YYYY-MM-DD

## 文档清单

| 文件/目录 | 定位 | 加载时机 |
| :--- | :--- | :--- |
| [技术栈与架构](architecture.md) | 前端技术栈全景 | 所有阶段 |
| [项目上下文](project-context.md) | 项目基本信息 | propose/apply 必读 |
| [目录结构](structure.md) | 项目目录与代码组织 | 设计阶段 |
| [路由配置](routes.md) | 路由映射与权限 | 涉及路由/页面改动时 |
| [状态管理](state.md) | Store 结构与使用 | 涉及状态/数据流改动时 |
| [API 端点](api-endpoints.md) | 接口封装与端点清单 | 涉及接口调用时 |
| [样式方案](style.md) | 样式体系与主题 | 涉及样式/主题改动时 |
| [构建配置](build.md) | 构建工具与优化 | 涉及构建/打包改动时 |
| [组件清单](components.md) | 组件 Props 与依赖 | 涉及组件新增/修改时 |
| [埋点方案](tracking.md) | 埋点框架与事件清单 | 涉及埋点改动时 |
| [领域映射表](domain-mapping.md) | 产品别名→业务域映射 | 涉及跨域/多业务线时 |
```

"定位"列取每个文件第一段（首个 `## ` 之前或前 100 字）。`tracking.md` 仅存在时列入。

#### 3.2 知识库占位文件

- **`docs/knowledge/prd/`** — 需求归档目录（空目录，仅保留用户提供的需求资料，Archive 不自动生成或复制 Proposal）
- **`docs/knowledge/exp/`** — 经验目录（空目录）
- **`docs/knowledge/domain-mapping.md`** — 产品别名→业务域映射表（空模板，**手动维护**）：

```markdown
# 领域名称映射表

> 手动维护：将产品文档中使用的别名映射到业务域名称

| 业务域名称 | 产品文档别名 | 说明 |
|-----------|------------|------|
| <!-- TODO: 填写 --> | | |
```

#### 3.3 汇总扫描计时

从 `docs/.helixharness-init-state.json` 的 `scanTimings` 字段读取各 agent wall-clock，输出耗时汇总表格（按降序排列，标注"6 路并行"）。

#### 3.4 写入 project-context

生成 `docs/knowledge/project-context.md`：

```markdown
# 项目上下文

- **project-mode**: {greenfield|brownfield}
- **技术栈**: {从 architecture.md 提取：框架 + 语言 + UI 库 + 构建工具}
- **状态管理**: {从 state.md 提取}
- **样式方案**: {从 style.md 提取}
- **路由方案**: {从 routes.md 提取}
- **判定依据**:
  - greenfield: 当前仓库尚无稳定业务代码，技术方案按首版基线设计评审
  - brownfield: 当前仓库已有业务代码或既有系统约束，技术方案按兼容演进设计评审
```

#### 3.5 初始化 Usage Telemetry 目录

- `$EFFECTIVE_TELEMETRY_MODE=enabled`：创建 `docs/eval/.gitkeep`，确保 `.gitignore` 包含 `docs/eval/**` 提交例外。
- `$EFFECTIVE_TELEMETRY_MODE=disabled`：不新建/不追加/不删除。
- 本阶段只准备目录，不写 feature usage entry。

### Phase 4：生成 hooks/

#### 4.1 Hook 配置文件

- `docs/hooks/index.md` — Hook 机制总览（区分 runtime hooks / workflow gates / review checklists）
- `docs/hooks/post-action.md` — 写后快速自动检查：

  ```markdown
  # Post-Action 自动检查

  每次 Write / Edit 工具写入文件后，Agent 应自检：

  1. **Invariant 一致性**：改动是否违反 `docs/harness/invariants/` 中的约束？
  2. **Spec 同步**：若改动涉及 `docs/specs/active/` 下的 feature，tasks.md / verify-report.md 是否需要更新？
  3. **组件影响**：若改动涉及共享组件，是否评估了所有引用方？
  4. **API 变更**：若改动涉及 API 调用方式（参数/返回值），api-endpoints.md 是否需要更新？

  > 此检查为提醒性质，不阻断操作，但应在下一轮 commit 前闭环。
  ```

- `docs/hooks/cr-checklist.md` — CR 检查清单：

  ```markdown
  # CR 检查清单

  每完成一个功能模块，按以下顺序自检：

  ## 代码规范
  - [ ] 组件命名符合 invariants/component.md 规约
  - [ ] Props 数量、类型、回调命名合规
  - [ ] 无 `any` 类型（或已加注释说明原因）

  ## Invariant 合规
  - [ ] 未违反 invariants/ 下任一约束
  - [ ] 状态管理边界符合 invariants/state.md
  - [ ] API 调用封装符合 invariants/api.md

  ## 架构约束
  - [ ] 分层依赖方向正确（Pages → Components → Hooks → Services）
  - [ ] 无循环依赖
  - [ ] 未见跨层反向引用

  ## Tasks Checkpoint
  - [ ] task 状态与代码实际进展一致
  - [ ] 证据已写入 tasks.md / verify-report.md

  ## Finding 对账
  - [ ] 若本轮来自 Apply 双轴评审或验证反馈：finding 是否有明确接受/拒绝/延期决策
  - [ ] 修复是否映射到代码和文档
  ```

#### 4.3 按 Host Kit 写入 Runtime Hooks

- 完整读取当前 `resources.project_hooks`，不得自行构造事件名、matcher、JSON 路径或 payload。
- `settings_file=null` 或 `merge_strategy=unsupported`：跳过，不创建替代配置。
- `owner=bootstrap`：按资源声明的目标、合并策略、去重键和 payload 幂等合并；保留用户字段。
- `owner=parent`：不得写 settings；在 final 返回结构化 `PROJECT_HOOKS_MERGE_REQUEST`，由父流程校验后串行合并。
- 资源与 `resources.project_config.settings_file` 不一致时 `BLOCKED: project_hooks_contract_invalid`。

### Phase 5：更新宿主入口文件并输出初始化报告

#### 5.1 更新平台入口文件

- 若 Host Kit 的 `entry_file` 已存在 → 追加规则；
- 若不存在 → 基于 Host Kit 的 `entry_template` 生成 `entry_file`；
- 不探测或生成其他宿主的入口文件。

**追加规则**（读取 `entry_template`，提取四个区块并以 `## HelixHarness 导航` 为标题追加）：

1. **目录导航**（用实际 frontend 目录结构替换）
2. **关键约束速览**（3-5 条，来自 `docs/harness/index.md` 全局约束摘要）
3. **常用命令**（`/helix:propose`、`/helix:apply` 等）
4. **Agent 工作规则**（含 harness/knowledge 按需加载协议）

追前检查文件末尾是否已含 `## HelixHarness 导航` 或 `## Agent 工作规则` → 已存在则不重复，仅确认是否需要更新。

#### 5.2 最终提交 state file

只有 Phase 4 hooks 与 Phase 5.1 Host Kit 入口全部生成并通过逐项验证后，才使用公共
`Read/Edit/Write` 工具语义原子更新 `docs/.helixharness-init-state.json`；其他宿主只按
`resources.runtime` 映射，不依赖外部 JSON CLI：

1. state 文件缺失、为空或不是 JSON 对象时立即 `BLOCKED:init_state_missing`，不得跳过后继续完成。
2. `scanPlan` 为空时补
   `["scan-frontend-meta","scan-frontend-api","scan-frontend-routes","scan-frontend-state","scan-frontend-components","scan-frontend-tracking"]`；
   已有非空计划保持不变。设置
   `scanStatus=completed`、`bootstrapStatus=completed` 与 `completedAt`。
3. `bootstrap_mode=fullstack-merge` 时额外写 `frontendCompletedAt`，且必须保留前序
   `backendCompletedAt`；缺少后端完成事实时 `BLOCKED:backend_bootstrap_incomplete`。所有 `*At`
   字段固定为当前 UTC Unix epoch 秒的 JSON number，与既有状态产物类型一致，不得写 ISO
   字符串或宿主本地时间文本。
4. 从实际文件列表构造排序、去重后的 knowledge、harness、hooks 三组 deliverables；每个 `count`
   必须等于对应 `files` 数组长度。
5. 只修改上述字段，保留其他键。写入后重新 `Read`，确认 JSON 可解析、字段类型正确、入口、hooks
   及所有 deliverable 文件均存在；任一步失败报告 `BLOCKED:init_state_update_failed`。

> 不要删除 `docs/.helixharness-init-state.json`（供后续命令使用）。

#### 5.3 输出初始化报告

```text
## HelixHarness 前端初始化完成

### 项目信息
- 项目模式: {greenfield|brownfield}
- 框架: {Vue/React/...}
- 语言: {TypeScript/JavaScript}
- 状态管理: {Pinia/Redux/Zustand/...}
- 样式方案: {Less/SCSS/Tailwind/...}
- 构建工具: {Vite/Webpack/...}
- UI 库: {Ant Design/Element Plus/...}
- 组件数量: {N} 个 / 路由页面: {M} 个

### 生成的文件
- [x] {entry_file}（来自 Host Kit）— 已更新/新增
- [x] docs/harness/index.md — 约束总入口
- [x] docs/harness/invariants/component.md — 组件开发规约
- [x] docs/harness/invariants/state.md — 状态管理规约
- [x] docs/harness/invariants/api.md — API 调用规约
- [x] docs/harness/architecture/layering.md — 分层架构约束
- [x] docs/harness/infrastructure/build.md — 构建规范
- [x] docs/harness/infrastructure/types.md — 类型系统规范
- [x] docs/harness/infrastructure/lint.md — 代码质量规范
- [x] docs/harness/linters/invariant-check.sh — invariant 自动检查
- [x] docs/harness/linters/spec-sync-check.sh — spec 同步提醒
- [x] docs/knowledge/index.md — 知识库索引
- [x] docs/knowledge/project-context.md — 项目上下文
- [x] docs/knowledge/domain-mapping.md — 别名→域映射表（需手动填写）
- [x] docs/knowledge/prd/ — 需求归档目录
- [x] docs/knowledge/exp/ — 经验目录
- [x] docs/hooks/index.md — Hook 机制总览
- [x] docs/hooks/post-action.md — 写后自动检查
- [x] docs/hooks/cr-checklist.md — CR 检查清单
- [x] Host Kit `settings_file` 已按 `resources.project_hooks` 合并，或明确为跳过/parent-owned
- [x] docs/eval/.gitkeep — 仅遥测开启时生成
- [x] docs/.helixharness-init-state.json — 已更新

### 下一步
1. 审核 docs/harness/ 下的约束文件，补充项目特有规则
2. 审核 docs/knowledge/ 知识库，确认准确性
3. 手动填写 docs/knowledge/domain-mapping.md（产品别名→业务域）
4. 执行 /helix:propose <feature-name> 开始第一个需求开发
```

## 注意事项

- **不删除已有内容** — 已存在的文件保持不动，只补充缺失文件
- **不捏造信息** — 无法通过扫描或用户确认的内容标注 `<!-- TODO: 待确认 -->`
- **约束须有规范性来源** — 生成 harness、宿主入口、CR checklist 或 linter 前完整读取 `reference/harness-generation-protocol.md`；当前代码行为写入知识库，不能单凭现状生成不可变硬规则
- **空项目处理** — 若无代码，通过交互收集信息后基于模板生成，并显式标记 `project-mode: greenfield`
- **非前端项目直接退出**
- **不要删除** `docs/.helixharness-init-state.json`（供后续命令使用）
- **hooks 和 linters 是硬门禁** — 与 harness/index.md 同等重要，不得以"首次初始化可选"为由跳过
