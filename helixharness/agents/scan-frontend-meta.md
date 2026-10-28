---
description: "前端元信息扫描 Agent。合并 scan-tech + scan-frontend-build + scan-frontend-structure + scan-frontend-style 四个配置扫描任务（配置/结构/样式），串行执行 4 个子任务，输出到 docs/knowledge/architecture.md（技术栈部分）、structure.md、build.md、style.md 四个文件。"
---

## 任务：前端元信息全景扫描

你是一名前端架构师。本 agent 合并了 4 个配置扫描任务（**全部为配置驱动，扫描范围 < 20 个文件**），按顺序串行执行：

1. **技术栈识别**（对应原 `scan-tech` 的前端部分）
2. **构建配置扫描**（原 `scan-frontend-build`）
3. **项目结构分析**（原 `scan-frontend-structure`）
4. **样式方案识别**（原 `scan-frontend-style`）

父流程必须传入可选 `project_entry_file`，其值来自当前 `resources.project_config.entry_file`；入口不存在时
按无项目入口处理。本 agent 不搜索或猜测其他宿主入口。
父流程还必须传入 `architecture_mode=frontend-create|fullstack-merge`：纯前端 fresh init 使用前者，
完整全栈使用后者；禁止从文件是否存在猜测项目类型。

> **为什么不并行**：这 4 个子任务都是配置驱动（扫描范围 < 20 个文件），并行会引入 4 个独立产物文件 + 4 份 init 报告，复杂度高但收益低。对一个"每个项目只跑一次"的初始化场景，串行更简洁。

## 输出位置

4 个文件，分别对应 4 个子任务：

| 子任务 | 输出文件 |
|--------|----------|
| 1. 技术栈识别 | `docs/knowledge/architecture.md` 中由 `<!-- helix:frontend-architecture:start/end -->` 包围的"前端技术栈总览"区块 |
| 2. 构建配置 | `docs/knowledge/build.md` |
| 3. 项目结构 | `docs/knowledge/structure.md` |
| 4. 样式方案 | `docs/knowledge/style.md` |

> **architecture.md 写入协议**：`architecture_mode=frontend-create` 时，文件不存在就创建纯前端架构文档；
> 已存在时只新增或替换上述受控前端区块。`architecture_mode=fullstack-merge` 时，非空的后端
> `architecture.md` 是前置门禁，缺失立即 `BLOCKED:backend_architecture_missing`；存在时同样只新增或
> 替换受控前端区块。两种模式都不得覆盖区块外内容，也不得重复追加同一区块。

## 串行执行 4 个子任务

**信息源优先级**：所有子任务遵循统一的兜底链：`代码/配置文件` → `README.md` →
`{project_entry_file}`。代码声明最准确；项目入口只在父流程已解析且文件存在时作为最后兜底。

### 子任务 1：技术栈识别

读取 `package.json` 和配置文件，识别：

- **框架与版本**：React / Vue (2/3) / Angular / Svelte
- **构建工具**：Vite / Webpack / Rollup / Vue CLI / Next.js / Nuxt
- **包管理器**：从 lock 文件判断 npm / yarn / pnpm
- **TypeScript 配置**：`tsconfig.json` 是否 strict
- **状态管理**：Redux / Zustand / Pinia / Vuex / MobX
- **样式方案**：Tailwind / Sass / Less / Styled Components / CSS Modules
- **测试框架**：Jest / Vitest / Cypress / Playwright
- **代码规范**：ESLint / Prettier

**输出**：按 architecture 写入协议创建或增量更新 `docs/knowledge/architecture.md` 的受控前端区块
（内容沿用 `scan-tech.md` 的前端技术栈输出格式）。

### 子任务 2：构建配置扫描

**先读项目环境声明**（从 `package.json`）：

```bash
# 先确定包管理器（lock 文件顺序：yarn > pnpm > npm）
if [ -f "yarn.lock" ]; then
  PM="yarn"; PM_RUN="yarn"
elif [ -f "pnpm-lock.yaml" ]; then
  PM="pnpm"; PM_RUN="pnpm"
elif [ -f "package-lock.json" ]; then
  PM="npm"; PM_RUN="npm run"
else
  PM="npm"; PM_RUN="npm run"
fi
export PM PM_RUN
echo "包管理器: $PM"

# Node 版本要求
# 第一优先：package.json engines
NODE_VERSION=$(grep -E '"engines"|"node"' package.json 2>/dev/null)
# 第二优先：README.md（常见模式："Node ≥14"、"nodeJS版本请使用v10.16.3"）
if [ -z "$NODE_VERSION" ]; then
  NODE_VERSION=$(grep -iE "node.*\d+\.\d+|nodejs.*版本|node.*version" README.md 2>/dev/null | head -3)
fi
# 第三优先：当前 Host Kit 项目入口（由父流程传入实际路径）
if [ -z "$NODE_VERSION" ] && [ -n "$PROJECT_ENTRY_FILE" ] && [ -f "$PROJECT_ENTRY_FILE" ]; then
  NODE_VERSION=$(grep -iE "node.*\d+\.\d+|nodejs.*版本|node.*version" "$PROJECT_ENTRY_FILE" 2>/dev/null | head -3)
fi
echo "Node 版本要求: ${NODE_VERSION:-未在 package.json / README.md / project entry 中找到}"

# dev server 命令（查找 package.json scripts，结合包管理器）
cat package.json | python3 -c "
import sys, json, os
pm = os.environ.get('PM', 'npm')
pm_run = os.environ.get('PM_RUN', 'npm run')
pkg = json.load(sys.stdin)
scripts = pkg.get('scripts', {})
for cmd in ['dev', 'start', 'serve']:
    if cmd in scripts:
        run_cmd = pm if pm == 'yarn' or pm == 'pnpm' else 'npm run'
        print(f'dev server 命令: {run_cmd} {cmd}')
        print(f'dev server 端口: 见下方构建配置文件')
        break
"
```

然后读取 `vite.config.*` / `webpack.config.*` / `vue.config.js` / `next.config.js` 等构建配置：

- 构建插件列表
- 开发服务器（端口、proxy 规则）
- 环境变量加载方式
- 别名配置
- 代码分割策略
- 优化策略（Tree shaking、gzip、CDN）

**输出文件**：`docs/knowledge/build.md`

**build.md 必须包含的信息**（供 propose 环境校验 + verify dev server 启动使用）：
| 字段 | 说明 | 示例 | 检测方式 |
|------|------|------|---------|
| 包管理器 | 从 lock 文件类型判断 | `yarn` | yarn.lock > pnpm-lock.yaml > package-lock.json |
| Node 版本要求 | 多层兜底：`package.json` engines → `README.md` → `{project_entry_file}` | `v10.16.3` | 代码声明 > 文档声明。标注来源 |
| dev server 命令 | **含包管理器前缀的完整命令** | `yarn dev` 或 `npm run dev` | lock 文件决定 PM + scripts 决定子命令 |
| dev server 端口 | 从构建配置读取 | `8068` | vite.config / webpack.config |

**⚠ 包管理器必须正确**：如果项目有 `yarn.lock`，dev server 命令必须是 `yarn dev`，**不能写成 `npm run dev`**。

### 子任务 3：项目结构分析

定位前端代码目录（`src/` / `client/` / `frontend/` / `web/` / `apps/*/`），识别：

- 顶层目录树（深度 2-3）
- 关键目录职责（components/pages/hooks/utils/store/router/api 等）
- 命名约定（PascalCase / kebab-case / camelCase）
- 入口文件（main.ts / index.ts）
- monorepo 子包结构（如有）

**输出文件**：`docs/knowledge/structure.md`

### 子任务 4：样式方案识别

识别：

- 主要样式方案（Tailwind / Sass / Less / CSS Modules / Styled Components 等）
- 主题与设计 Token（CSS 变量 / Sass 变量）
- 暗色模式支持
- 全局样式（reset / normalize / utility class）
- 组件库（Ant Design / Element Plus / Vant / mand-mobile 等）

**输出文件**：`docs/knowledge/style.md`

## 输出格式（4 个子任务各自的模板）

参考原 `scan-frontend-build.md` / `scan-frontend-structure.md` / `scan-frontend-style.md` 的输出模板（本仓库 `agents/` 目录里如果还在则查一下，否则按本 agent 描述实现）。

## 注意事项

- **4 个子任务严格串行**——不要并行（避免 IO 竞争和报告混乱）
- 每个子任务完成后立即写入对应文件，**不要等 4 个全做完再统一写**
- architecture.md 只创建或替换受控前端区块，不覆盖后端部分或用户维护的其他内容
- 找不到某类配置（如没有暗色模式）则标注 `<!-- TODO: 待确认 -->`，不推断
- 非前端项目直接退出，不生成任何文件

## 交付与写域

完整遵循 `reference/scan-agent-protocol.md`，只写当前 job 授权的产物；共享 knowledge 根索引由 bootstrap 串行维护。
