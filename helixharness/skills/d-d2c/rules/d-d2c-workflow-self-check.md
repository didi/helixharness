---
description: d-d2c 工作流（初始化→编码→物料生成→结果评测→用户决策→摘要）, 严格按照步骤执行，禁止自做主张跳步执行
alwaysApply: true
---

# d-d2c 工作流（d-d2c-workflow-self-check）
本文件定义 d-d2c 生成页面后的自检评测工作流；执行时必须按本流程串行推进。

> **阶段状态追踪**：全流程通过 `d2c-workflow-checkpoint.cjs` 脚本追踪阶段状态。每个阶段入口必须调用 `--enter`，出口必须调用 `--complete`。脚本会校验前置阶段是否完成，校验不通过时返回非 0 退出码并阻断流程。**必须阅读脚本输出中的"⚠️ 下一步"指令并严格执行。**

> **完整执行与终止守卫**：用户调用任一 `d-d2c --create-page|--update-page|--component` 命令，即授权执行完整 workflow 和所需的本地 dev server。阶段 0/1/2/3 完成都不允许询问“是否继续”。checkpoint 会同步 `d-d2c/temp/.active-workflow.json` 并输出 `D2C_CONTINUE_REQUIRED`；Claude Code 的 skill-scoped Stop Hook 会在模型准备结束 turn 时读取该状态，未到合法暂停或阶段 6 完成时确定性拒绝停止。

合法停止当前 turn 的状态只有：`browser-handoff` 等用户操作、阶段 4 等交互式 fidelity 决策、用户取消、带 `--reason` 且带 `--detail/--evidence` 的 `external-input` 暂停、阶段 6 完成。其它错误必须先自动排查；确需外部输入时使用：

```bash
node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs \
  --pause external-input --reason <稳定原因码> --detail '<缺少什么以及已验证的证据>' \
  --page-dir <page-dir>
```

外部输入到位后先执行 `--resume --page-dir <page-dir>`，再继续当前阶段。禁止用显式暂停代替普通 dev server 启动、阶段迁移、自动修复或 fidelity。

## 0. 初始化阶段（硬阻断）
- 规则见：`d-d2c/rules/d-d2c-init.md`
- ⛔️ 必须先确认本地 Agent 环境可用 `d2c-fidelity-evaluator` skill；若缺失，按初始化规则自动安装，安装失败或仍不可检测时禁止进入下一阶段。
- ⛔️ 必须实际产出 `d-d2c/sort-of-d2c.md`、`d-d2c/assets-mapping.md`、`d-d2c/components-mapping.md` 三份文档；缺一禁止进入下一阶段。
- 进入下一阶段前必须显式 `ls -la` 三份文件作为放行证据。
- ⛔️ 阶段完成时必须执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 0 --page-dir <page-dir>
  ```

## 1. 代码还原
- 规则见：`d-d2c/rules/d-d2c-generate.md`
- ⛔️ 进入此阶段前必须执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --enter 1 --page-dir <page-dir>
  ```

  校验不通过（非 0 退出码）时禁止继续。

- **DSL 落盘（脚本优先，失败走 MCP，禁止伪造）**：在同一 `d-d2c/temp/<page-dir>/` 下先 `fetch-mastergo-dsl.cjs` 写出 **`dsl_full.json`**；用户显式指定业务线时传 `--business-line <name>`，未指定时不得自动补默认值或走转发。脚本会自动重试 transient MasterGo 错误。若退出码 2，必须按 `.dsl-fetch-fallback.json` 调用 MCP，并把 **MCP 原始响应**写入 `mcp-getDsl-response.json` 后执行 `normalize-mastergo-dsl.cjs`；若 `getDsl` 仍失败但 `getDesignSections` 可用，则把 overview/sections/svgs/texts 原始响应落到 `mcp-sections/` 后执行 `assemble-mastergo-sections.cjs`。禁止手写、摘抄或拼接 `dsl_full.json`。再执行 `filter-dsl.cjs` 产出 **`Layer.md`**、**`dom.md`**、**`styles.md`**、**`mixed-text.json`**、**`text-whitespace.json`**、**`page-flow-plan.json`**、**`dsl_layout_marked.json`**、**`dsl_visual.json`**、**`complexity-plan.json`** 等；细则见 `d-d2c-generate.md` 第 3 条。
- ⛔️ **空 DSL 禁止进入代码还原**：`filter-dsl.cjs` 必须产出非空 `dsl_visual.json.nodes` 与非空 `dsl_layout_marked.json.nodes`。若 `dsl_full.json` 只有 `sections/rootMetadata/rootContainer` 或节点数为 0，视为 DSL 拉取/落盘失败，必须重新拉取或走 section fallback 组装；禁止写 Vue、注册路由、截图或把该 run 当作还原度问题处理。
- ⛔️ **复杂度分流（写任何代码之前）**：读取 **`complexity-plan.json`** 的 `verdict`（脚本结论，禁止口头推翻）：
  - `simple` → 按 `d-d2c-generate.md` 现有路径一次性生成整页。
  - `complex` → 按 **`d-d2c-complex-split.md`** 执行：Agent 落盘 `component-plan.json` → 逐组件「切片 → 只读切片实现 → 挂载 → `d2c-geometry-check.cjs --component <id>` → `--component-complete <id>` 过闸」串行小闭环；全部组件过闸后才允许 `--complete 1`（checkpoint 硬校验）。
- **省 token**：本阶段读 **`Layer.md` + `dom.md` + `styles.md`**；**勿**把 **`dsl_full.json` / `dsl_layout_marked.json` / `dsl_visual.json` 全文**载入对话。
- ⛔️ **写页面级根布局之前**：读取 **`page-flow-plan.json`**。若其中存在 `implementation="absolute-background-layer"` 的顶层节点，必须按 `d-d2c-generate.md`「页面级叠层与背景层」实现为不占文档流的背景层；禁止把大背景矩形写成 `position: relative/static + height` 的普通块。`--complete 1` 会静态扫描本轮 `.vue` 并阻断这类错误。
- ⛔️ **写任何 `.vue` 之前**：读取 `mixed-text.json`；若 `hasMixedText` 为 `true`，按 `d-d2c-generate.md`「混排文本」对照 `dom.md` 实现。`--complete 1` 会硬校验 changed Vue 中对应 `data-d2c-mark` 下的 `data-span-index` 数量、顺序和静态文案；拍平成普通文本必须在截图前修复。
- ⛔️ **写任何 `.vue` 之前**：读取 `text-whitespace.json`；若 `hasWhitespaceRisk` 为 `true`，按 `d-d2c-generate.md`「文本空白与水平锁宽」实现（命中节点 `white-space: pre` 或 `&nbsp;`，禁止 `nowrap` 折叠连续空格）。
- ⛔️ **写任何 `.vue` 之前**：扫描 `dom.md` 是否命中「垂直 bbox 锁高」触发条件；若命中，须完成验算并按 `d-d2c-generate.md` 实现，否则禁止 `--complete 1`。
- ⛔️ **`--complete 1` 之前必须通过确定性编译健康闸**：启动真实项目 dev server 后，用能够挂载该项目 SPA shell 的完整 URL 执行（目标业务 URL 若先被外部登录页截走，可用同一 dev server 的已知可挂载入口做本阶段编译检查；阶段 2 仍必须验证真实目标 URL）：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-runtime-health-check.cjs \
    --url '<真实项目完整URL>' \
    --page-dir d-d2c/temp/<page-dir>
  ```

  脚本输出 `compile-check.json`，同时检查 webpack/vite compiler 信号、浏览器 page error、错误遮罩、HTTP 4xx/5xx、`Cannot GET` 与 SPA 根节点挂载，并绑定当前 changed implementation source digest。默认识别 `#app/#root/[data-v-app]/[data-server-rendered]/#__nuxt/#__next`；项目使用其它根时显式传 `--app-selector '<selector>'`。**端口可连接或 `curl` HTTP 200 不算编译成功。**任一源码再次修改后旧证据自动失效，必须重跑。编译/runtime/dev-server 路由失败会输出 `D2C_COMPILE_REPAIR_REQUIRED` / `D2C_RUNTIME_REPAIR_REQUIRED`，均为 `askUser=false consumeRound=false`：先自动修复，禁止进入截图或 browser handoff。`--complete 1` 会硬校验该文件、源码摘要和 `appMounted=true`。
- 编译失败时**先修复**（常见为资源 import 路径无法解析、`.vue` 使用项目未安装 loader 的 `<style lang>`，或旧预处理器无法解析被继续扩大的超长 style block；见 `d-d2c-generate.md` 第 2/2b 条）再继续；禁止带着编译失败/空 DOM 进入物料阶段。
- ⛔️ **真实目标页落地由阶段 2 截图闸处理**：阶段 1 的健康闸只证明项目真实构建链可编译、SPA 可挂载，不把登录/业务重定向当视觉物料。阶段 1 不允许为绕过 SPA 重定向去修改业务基础设施（路由守卫 / `flow` 链 / `login` / `bootstrap` / `init-data` 等）。若真实业务路由被重定向，进入阶段 2 后严格按当前 mode 覆盖层选择唯一后续路径，禁止在阶段 1 把错页当成目标页完成截图判断。
- ⛔️ 代码还原完成后必须执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 1 --page-dir <page-dir>
  ```

  **阅读输出中的"⚠️ 下一步"指令。**

- ⛔️ `--complete 1` 输出 `D2C_CONTINUE_REQUIRED ... nextPhase=2 askUser=false` 后，必须在同一个 assistant turn 自动执行 `--enter 2` 并开始物料阶段。启动或重启本地 dev server 属于 d-d2c 已授权流程，不得把代码实现摘要当最终回复，也不得询问用户是否继续。

## 2. 物料生成
- 必须按 `d-d2c/rules/d-d2c-materials.md` 执行。
- ⛔️ 进入此阶段前必须执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --enter 2 --page-dir <page-dir> --round <N>
  ```

  已有页更新模式必须追加 `--mode update-page`，例如 `--enter 2 --page-dir <page-dir> --round <N> --mode update-page`；首次记录后会写入 `workflow-state.json`，后续 `--complete` 仍建议显式携带。

  校验不通过（非 0 退出码）时禁止继续。

- 进入此阶段前请确认阶段 1 已完成页面代码还原，且本地页面可通过开发服务器访问。
- 宿主坐标与叠图遵守 **`d-d2c-design-crop.md`**：阶段 1 必须产出 `host-layout.json` 和 schema v2 `layout-h5.json`。
  - `immersive-overlay`：宿主控件不生成，但保留完整 H5 顶部空间；`layoutShiftY=0`、`designCropTopRaster=0`，业务原始 Y 必须过 geometry/vertical 闸。
  - `stacked-host`：只按 `hostChromeBottomY` 平移/裁切，禁止按 `firstBusinessY` 删除业务顶部留白。
  - 生成 `design-30` / `overlay` 时推荐不传 `--design-crop-top`，由脚本自动应用 policy、栅格换算和 `ignoredDesignRegions`。
- 首轮物料输出到 `d-d2c/temp/<page-dir>/r1/`；若用户后续选择继续优化，第 N 轮物料输出到 `d-d2c/temp/<page-dir>/r<N>/`。
- 每轮生成以下六个基础自检物料，文件名不带轮次，轮次只体现在目录中：
  - 渲染截图：`<page-id>-<md5>-actual.png`
  - 30% 透明设计稿：`<page-id>-<md5>-design-30.png`
  - 叠图比对：`<page-id>-<md5>-overlay.png`
  - 几何自检：`geometry-diff.json`（`d2c-geometry-check.cjs` 产出，DOM 实测 vs DSL bbox 的量化 diff）
  - 纵向自检：`vertical-diff.json`（create-page 必须由 `d2c-vertical-check.cjs --page-flow` 产出且 `mode="page-flow"`，顶层根及根内部高风险纵向锚点的 top/height/bottom 与 DSL page flow 的量化 diff；普通 `mode="page"` 文本锚点报告不得替代）
  - render 自检：`render-diff.json`（`d2c-render-check.cjs` 产出，DSL visual spec vs computed style/font/text clipping/assets）
- ⛔️ 截图完成后必须运行 geometry、vertical、render 三项检查。`--complete 2` 会执行**联合前置硬闸**：坐标按固定 8 设计px，render 只采用 high-confidence hard violations；任一失败且当前有效轮次小于 8 时，禁止进入阶段 3。invalid/source-material/no-progress 不消耗轮次；实现修复共享同一 8 轮额度。达到上限仍失败时可以进入最终 fidelity，但必须披露残余项，禁止记录为通过。
- ⛔️ `captureMode=region` 时，capture、design-opacity、overlay、geometry、vertical、render 必须共享同一个唯一 design scope。三项 DOM 闸只能检查该 scope 子树并使用 scope 根宽度/原点；fidelity 只能使用本轮 `*-design-source.png` 的确定性设计裁片。缺 scope、整页设计直叠局部 actual、或整页节点混入 region diff 都属于无效物料，不得消耗修复轮次。
- ⛔️ **阶段 2 自动修复协议（禁止问用户是否接受）**：当输出 `D2C_AUTO_FIX_REQUIRED` 时，读取 `reason/consumeRound/nextRound/repairPlan`。coordinate/render/combined gate failure 且 `consumeRound=true` 时修改代码进入下一有效轮；source-material/material-validation/no-progress 且 `consumeRound=false` 时修 DSL、字体、资源、dev server 或截图链并覆盖同轮。若同时存在坐标与 render 问题，必须在同一轮执行 primary strategy 和 secondary render strategy。自动修复期间保持当前 mode 覆盖层管理的已验证运行入口 active，禁止询问用户是否接受当前效果。`user-browser-handoff-required` 是目标 DOM 外部交互，不是效果决策，不消耗修复轮次。
- ⛔️ **目标页落地校验闸（截图 + geometry/vertical/render 后、进入阶段 3 前）**：出现下述任一「未落地到目标页」信号时，**禁止**进入阶段 3：
  - 截图脚本 `d2c-capture-page.cjs` 输出 `redirected: true`（导航后最终 URL 的 path 被改写）；或
  - `geometry-diff.json` 近乎整页失配——`matchedCount` 极低（如 `matchedCount/checkedCount < 0.5`）且绝大多数 violation 的 `kind` 为 `text-missing`，即设计稿的关键业务文案在 DOM 中系统性缺失（残留的少量 matched 多为「元/千」等跨页误命中）。

  这类信号表明落地页很可能**不是目标业务页**（SPA 路由守卫/鉴权 Flow/`init-data` 首屏改写、登录态缺失或截图过早），而**非「还原度低」**——此时的低分无意义。必须按顺序处理：
  1. 核对最终 URL，按需等待跳转完成 / 注入已有登录态 / 修正目标路由后**重跑一次真实路由截图 + 几何自检**（**至多重试 1 轮**，不得反复触发）。
  2. 若真实业务路由仍持续重定向，严格执行当前 mode 覆盖层；update-page 必须创建 browser handoff 并暂停等待用户，不得自行切换其它运行入口。
  3. 静态快照、手写 HTML、直接复制 DOM、截图壳或只加载 CDN Vue 的模板不得作为阶段 2 完成物料，也不得传给 fidelity。只有当前 mode 明确授权且通过脚本验证的真实 Vue 运行入口可进入后续阶段。
  - ⛔️ **落地阻塞的排查边界（通用，换任何页面都成立）**：允许的补救包括截图脚本支持的手段（等待跳转完成、注入已有登录态 / 补全访问参数、修正目标路由），以及当前 mode 覆盖层明确授权的后续路径。禁止自行跨模式调用其它入口，禁止为真实业务路由绕守卫做广域改动。
  - ✅ **登录态优先用会话注入、而非 URL token（通用，换任何页面都成立）**：flow-gated SPA 的路由守卫常**不认 URL query token 当有效会话**（`?token=` 已注入仍被重定向），却认 **cookie / localStorage 会话**。若已有有效会话，优先按 `d-d2c-materials.md` 用 `--session-state` / `D2C_SESSION_STATE` 在 `page.goto` **之前**注入（由外部输入提供，skill 不写死任何 key / 域名），让守卫照常验到登录态后放行——等价真人已登录，非绕过守卫。若已提供有效会话仍复现同一重定向，按当前 mode 覆盖层继续；仅拼 URL token 被重定向时先补会话注入，别急着升级。
- ⛔️ **组件完整性闸（截图 + geometry 后、进入联合闸自动修复前，通用）**：落地页正确（非重定向、非整页失配），但**目标组件的关键子区域可能为空**——DOM 存在但 `v-if` 条件块未渲染。此信号极易被忽略（页面"看起来正常"），必须显式检查。

  **触发条件**：落地校验闸通过（`redirected: false` 且非整页失配），但 `geometry-diff.json` 中存在 `matched: false` 的节点。

  **检查流程**：
  1. 从 `geometry-diff.json` 提取所有 `matched: false` 的节点，列出其 `layoutMark` / `text` / `name`
  2. 对照目标组件的 `<template>`，判断这些未匹配节点是否对应 `v-if` / `v-show` 条件块内的内容
  3. 若未匹配节点涉及以下典型信号 → 判定为**组件数据缺失**，不得进入自动修复轮：
     - 业务文案节点（姓名、联系电话、金额数字等）在 DOM 中不存在
     - 列表类节点的匹配数为 0
     - 同一 `v-if` 块下的多个兄弟节点集体 `matched: false`

  **处置**：
  - 判定为数据缺失 → 按 [SKILL.md#注入 Mock 数据](../SKILL.md#-agent-主动检测--自动-mock-截图后必须执行) 走 mock 生成流程，**不消耗修复轮次**
  - 判定为还原度问题（节点存在但位置/样式偏差）→ 走正常自动修复轮
  - 无设计稿 DSL → 使用 `page.evaluate` 直接检查组件 DOM：读模板中 `v-if` 块的预期选择器，逐条 `document.querySelector` 验证是否存在

  **严禁**：对数据缺失导致的 `matched: false` 进行 CSS/布局修复——源头上组件没渲染，改样式无效。
- ⛔️ **临时入口/浏览器接管清理与入口类型闸**：所有 mode-specific 临时入口必须登记并 cleanup；`user-browser-handoff` 必须通过 `d2c-browser-handoff.cjs` 管理，四项物料复用同一 verified tab，联合闸放行或达到上限后执行 `--cleanup --result completed`。checkpoint 会按 workflow mode 校验入口类型与证据。
- ⛔️ **已有页落位证据闸（仅 update-page 生效）**：如果本次是 `--update-page`，截图/geometry/vertical/render 完成后、`--complete 2` 前必须生成 placement-check：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-code-placement-check.cjs \
    --page-dir d-d2c/temp/<page-dir> \
    --target <目标页面.vue> \
    --selector '<新增块根选择器>' \
    --anchor-text '<宿主锚点文案>' \
    --relation after
  ```

  `--complete 2 --mode update-page` 会要求 `placement-check.json.status="passed"`。验证通过的 `user-browser-handoff` 可与 placement 共同证明真实页集成；任何其它入口都会按 workflow capability violation 拒绝。
- 当前轮后续评测只能使用当前轮 `r<N>/` 下的截图物料，禁止复用旧轮次截图。
- ⛔️ 物料生成完成后必须执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 2 --page-dir <page-dir>
  ```

  已有页更新模式必须追加 `--mode update-page`。如需指定非默认 placement 文件，可追加 `--placement-check d-d2c/temp/<page-dir>/placement-check.json`。

  **阅读输出中的“⚠️ 下一步”指令。** 联合闸失败时严格按 `nextRound` 重跑截图/geometry/vertical/render，直到联合过闸或用满最大有效轮次。

## 3. 结果评测
- ⛔️ 进入此阶段前必须执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --enter 3 --page-dir <page-dir>
  ```

  校验不通过（非 0 退出码）时禁止继续。

- 本阶段只在阶段 2 联合前置闸通过、或已达最大有效修正轮次并记录未达标项后进入；不得用主观报告绕过三份确定性 diff。
- 本阶段将阶段 2 生成的当前轮物料整理为 `d2c-fidelity-evaluator` 需要的输入，然后调用 `d2c-fidelity-evaluator` skill 生成评测报告。
- 所有输入必须落到具体文件的**绝对路径**，不得使用相对路径、通配符、占位符或口头描述。
- 进入评测前必须解析出当前轮的九类输入（update-page placement check 为条件必需）：

| d2c-fidelity-evaluator 输入 | 是否必须 | 路径来源 |
|---|---:|---|
| Design screenshot | 必须 | `<workspace>/d-d2c/temp/<page-dir>/image-source.png` |
| Design DSL | 必须 | `<workspace>/d-d2c/temp/<page-dir>/Layer.md` + `dom.md`（`dsl_layout_marked.json` 本地对照） |
| Implementation source | 必须 | 当前实时生成页面或组件的 `.vue` 源码绝对路径；页面用页面 `.vue`，组件用组件 `.vue` |
| Rendered screenshot | 必须 | `<workspace>/d-d2c/temp/<page-dir>/r<N>/<page-id>-<md5>-actual.png` |
| Overlay screenshot | 推荐 | `<workspace>/d-d2c/temp/<page-dir>/r<N>/<page-id>-<md5>-overlay.png` |
| Geometry diff | 必须 | `<workspace>/d-d2c/temp/<page-dir>/r<N>/geometry-diff.json` |
| Vertical diff | 必须 | `<workspace>/d-d2c/temp/<page-dir>/r<N>/vertical-diff.json` |
| Render diff | 必须 | `<workspace>/d-d2c/temp/<page-dir>/r<N>/render-diff.json` |
| Update-page placement check | update-page 必须 | `<workspace>/d-d2c/temp/<page-dir>/placement-check.json` 或当前轮 `r<N>/placement-check.json`；若 `workflow-state.json.updatePagePlacement.scoreScope="component-visual-only"`，必须把该限制传给 evaluator |

- `Overlay screenshot` 使用 d-d2c 标准生成的 30% 叠图；`d2c-fidelity-evaluator` 只要求透明叠图，不关心具体透明度。
- Geometry / Vertical / Render diff 是确定性硬证据。调用 evaluator 时仍须传入剩余 violations 与 warnings；禁止以“叠图看起来对齐”推翻硬测量，也禁止用 render warning 冒充 hard pass。
- 若存在宿主壳层，调用 `d2c-fidelity-evaluator` 时必须说明 `hostLayoutPolicy`、confidence/evidence、`hostChromeBottomY`、`firstBusinessY`、`businessTopGap`、`layoutShiftY`、`designCropTopRaster` 和 `ignoredDesignRegions`。宿主控件不参与评分；但 `immersive-overlay` 的顶部保留空间与首业务原始 Y 属于 H5 布局，缺失时必须扣分，禁止当成统一裁切偏移豁免。
- `Implementation source` 始终指向实时源码文件，不为每一轮生成代码快照。
- `Design DSL` 默认使用 **`Layer.md` + `dom.md` + `styles.md`**；仅当评测需要定位图标/path 等精细节点时，才在本地参考 `dsl_full.json` / `dsl_visual.json`，不要把整份载入上下文。
- ⛔️⛔️⛔️ **原子操作约束 — evaluator 返回后必须在同一个 assistant turn 内连续完成到 `AskUserQuestion`，中间禁止向用户输出任何文字、禁止结束回复。**

  `d2c-fidelity-evaluator` 的输出只是阶段 3 的中间产物，不是面向用户的最终回复。evaluator 返回后，必须在**同一个回复内**按顺序执行以下全部步骤：

  **步骤 A** — 从 evaluator 输出中解析 `报告绝对路径` 和 `还原度分数`。

  **步骤 B** — 立即执行（会自动打印中文摘要）：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 3 --page-dir <page-dir> --report <报告绝对路径> --score <还原度分数>
  ```

  **步骤 C** — 立即执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --enter 4 --page-dir <page-dir>
  ```

  **步骤 D** — 立即使用 `AskUserQuestion` 工具向用户提问（问题模板见阶段 4）。

  A → B → C → D 是**不可中断的原子序列**。如果在步骤 D 之前就向用户输出了文字或结束了回复，视为违反 SOP。

- ⛔️ 未执行 `--complete 3`、未看到中文摘要输出，禁止进入阶段 4。
- 本阶段只负责评测与报告生成，不自动修改代码。

### Headless / eval-loop 阶段 3 收尾

`D2C_EVAL_LOOP=1` 等无交互执行环境没有可等待的 `AskUserQuestion`，但仍不得停在 phase 3/4：

- evaluator 返回后仍须原子执行 `--complete 3` 与 `--enter 4`。
- 分数达到 `D2C_SCORE_THRESHOLD`（未设置按 90）时，自动记录 `cancel`，完成阶段 4，再进入并完成阶段 6。
- 分数未达阈值且当前有效轮次小于 8 时，自动记录 `continue`，按 evaluator 与三项 diff 进入下一轮；禁止向无人值守环境提问。
- 分数未达阈值但已达 8 轮时，记录 `cancel` 并完成阶段 6，如实披露残余项。
- 只有缺少无法由 skill 生成的外部输入时，才允许记录 `external-input` 显式暂停；browser handoff 继续使用专用 sentinel 交给外层恢复。

## 4. 用户决策与迭代
- ⛔️ 进入此阶段前必须已在阶段 3 的原子序列中执行过 `--enter 4`（见上方步骤 C）。若未执行，先补执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --enter 4 --page-dir <page-dir>
  ```

  校验不通过（非 0 退出码）时禁止继续。

- ⛔️ 交互式环境必须使用 `AskUserQuestion` 工具向用户提问，让用户决定是否继续优化；`D2C_EVAL_LOOP=1` 按上一节自动决策，不等待用户：
  - 问题示例：「R<N> 还原度 <score>%，是否继续优化？」
  - 选项：「继续优化」（description: 基于评测报告修复问题，进入下一轮）/「取消」（description: 结束迭代，输出最终摘要）
- 注意：本阶段的效果决策只适用于**阶段 3 已完成、已有 evaluator 报告和分数**的情况。若还停在阶段 2 联合闸、运行入口或物料校验失败，必须按阶段 2 自动修复协议处理；update-page 的 browser handoff 只让用户把真实页面带到目标状态，不询问是否接受效果。
- 拿到用户选择后，将结果传给脚本记录：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-loop-decision.cjs --choice <continue|cancel>
  ```

- 必须读取命令输出的最后一行作为唯一决策值。
- ⛔️ 拿到决策值后必须立即执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 4 --page-dir <page-dir> --decision <continue|cancel>
  ```

  **阅读输出中的"⚠️ 下一步"指令。**

- ⛔️ **联合达标硬闸（阶段 2 已前置，阶段 4 收尾兜底）**：`--complete 4 --decision cancel` 会再次读取 geometry / vertical / render，确认联合 hard violations 已清零或当前已达固定 8 个有效修正轮次：
  - **未达标且未达上限** → 非 0 拒绝 cancel，继续下一轮并同时修复坐标与 render 契约。
  - **达标 或 已达最大修正轮次** → 放行收尾；已达上限仍不达标时，脚本会列出未达标项与残余偏差，阶段 6 最终摘要必须如实披露，**禁止**静默当"已通过"。
  - **已达最大修正轮次且仍未达标** → 坐标修复循环已经结束，禁止再选择 `continue` 生成 `r<N+1>` 非终止状态；必须选择 `cancel`，随后进入阶段 6 并执行 `--complete 6`，让下游 `d2c-run-summary.json.completeRun=true`。若仍需人工继续打磨，应在本次完整收尾后另起新任务。
  这把布局与可确定视觉契约统一下沉为**阶段 2 evaluator 前置闸 + 阶段 4 收尾兜底闸**。

- 只有脚本输出最后一行**精确等于** `continue`，才允许进入下一轮优化。
- 若决策为 `continue`：
  1. 优先以当前轮 geometry / vertical / render 的联合失败项和 `repairPlan` 作为下一轮硬依据，再结合 fidelity 报告处理脚本无法确定的感知问题。
  2. 修改实时页面/组件 `.vue` 源码。
  3. 轮次加 1，重新执行 `阶段 2. 物料生成 → 阶段 3. 结果评测 → 阶段 4. 用户决策与迭代`。
  4. 新一轮物料必须输出到新的 `r<N+1>/` 目录，禁止覆盖上一轮物料。
- 若决策为 `cancel`：
  - 退出迭代 loop。
  - 进入 `阶段 6. 最终完成摘要`。
- 若 `AskUserQuestion` 不可用（如非 Claude Code 环境），可退而在 shell 中直接运行 `node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-loop-decision.cjs 2>&1` 尝试 TTY 交互；若脚本输出 `manual_required`、脚本非 0 退出、无输出或输出无法识别，则必须暂停流程，在对话中要求用户明确选择"继续优化"或"取消"。
- ⛔️ 未获得用户明确选择前，禁止修改代码、禁止生成下一轮物料、禁止再次调用 `d2c-fidelity-evaluator`。
- ⛔️ 禁止因为评测分数低、报告中存在优化建议、预期收益较高、或 Agent 自己判断"应该继续"，而绕过用户决策进入下一轮。

## 5. 临时产物目录
- 全部临时产物根目录：`d-d2c/temp/<page-dir>/`
- 根目录保存设计与 DSL 基础输入：
  - `image-source.png`
  - `dsl_full.json`
  - `dsl_layout_marked.json`
  - `dsl_filtered.json`
  - `Layer.md`
  - `dom.md`
  - `styles.md`
  - `text-whitespace.json`
  - `dsl_visual.json`
  - `complexity-plan.json`（复杂度判定，filter-dsl 自动产出）
  - `component-plan.json`（仅复杂页；Agent 落盘的拆解方案）
  - `components/<id>/`（仅复杂页；组件 DSL 切片 + `component-meta.json` + 组件级 `geometry-diff.json`）
- 每轮截图物料保存到独立轮次目录：

  ```text
  d-d2c/temp/<page-dir>/r1/
  d-d2c/temp/<page-dir>/r2/
  d-d2c/temp/<page-dir>/r3/
  ```

- 每个轮次目录内命名格式：`<page-id>-<md5>-<类型>.png`；另含本轮 `geometry-diff.json`
- `d2c-fidelity-evaluator` 评测报告保存到目标项目根目录的 `d-d2c/temp/d2c-fidelity-evaluator-report/` 下，具体路径以 evaluator 输出为准。
- 工作流状态文件：`d-d2c/temp/<page-dir>/workflow-state.json`（由 `d2c-workflow-checkpoint.cjs` 自动管理）

## 6. 最终完成摘要
- ⛔️ 进入此阶段前必须执行：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --enter 6 --page-dir <page-dir>
  ```

  校验不通过（非 0 退出码，即阶段 4 未完成或 decision 不为 cancel）时禁止输出摘要。

全流程完成并退出迭代 loop 后，必须向用户输出一份结构化最终摘要，包含以下五部分：

### 6.1 改动了哪里
- 以表格形式列出所有**新建**和**修改**的文件，区分类型（新建/修改）
- 每个文件附简要说明其作用

### 6.2 访问地址
- 给出本地开发服务器的完整可访问 URL（如 `http://localhost:<port>/<path>`）

### 6.3 最后一轮自检物料
- 列出最后一轮以下文件的**绝对路径**，方便用户直接打开查看还原效果：
  - 渲染截图（`*-actual.png`）
  - 30% 透明设计稿（`*-design-30.png`）
  - 叠图比对（`*-overlay.png`）
  - 设计稿原图（`image-source.png`）

### 6.4 最后一轮评测结果
- 列出最后一轮 `d2c-fidelity-evaluator` 评测报告 Markdown 文件的绝对路径。
- 列出最后一轮还原度分数。

### 6.5 迭代信息
- 列出总迭代轮次。
- 若执行过多轮优化，简要说明最终使用的是哪一轮物料和哪一轮评测报告。
- 若最后一轮是因为达到最大修正轮次而带着联合闸未达标项进入评测/收尾，必须同时列出 `workflow-state.json.geometryGate.top` 与 `workflow-state.json.renderGate.top` 中的残余项，禁止写成"联合闸已通过"。

### 6.6 标记流程完成（硬收尾，强制）

- ⛔️ 输出上述五部分摘要后，必须执行以下命令标记全流程结束：

  ```bash
  node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 6 --page-dir <page-dir>
  ```

  未执行 `--complete 6` 时，`workflow-state.json` 会永远停留在阶段 6 `in_progress`、运行日志缺少 `phase.complete(6)` 事件、`d2c-run-summary.json` 的 `completeRun` 恒为 `false`，下游完整性判定会把健康的 run 误判为「未完成 / RUN_INVALID」。此为通用收尾要求（换任何页面都成立）。

> 未输出此摘要即宣布完成，视为流程不完整。
