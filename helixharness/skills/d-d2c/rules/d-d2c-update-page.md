---
description: d-d2c 已有页面更新流程覆盖层。用户使用 d-d2c --update-page，或要求在已有页面上更新、迭代、新增一块内容时读取；只定义流程差异，不改变通用还原逻辑。
alwaysApply: false
---

# d-d2c-update-page（已有页更新模式）

> 🔧 Vue 2 项目 `el` 替换了 `#app`？使用 `--app-selector "#rootApp"` 指定正确的挂载点。

本文只描述已有页更新模式相对默认新建整页流程的差异。除本文列出的覆盖项外，仍严格遵循：

- `d-d2c-workflow-self-check.md`
- `d-d2c-generate.md`
- `d-d2c-materials.md`
- `d-d2c-design-crop.md`
- `d-d2c-limited.md`
- `d-d2c-init.md`

不得因为更新已有页修改 DSL/filter、布局/样式生成、geometry/vertical/render 判定算法、phase2 联合闸或 fidelity 评分口径。

`update-page` 的运行入口只有真实业务路由与 `user-browser-handoff`。本模式不读取
`d-d2c-preview.md`，也不允许创建 preview harness、isolated Vue mount 或使用
`accessMode=vue-route-preview`；无 GUI/headless 环境同样必须暂停等待 browser handoff，
不得视觉降级。

## 1. 适用范围

适用于以下输入：

```text
d-d2c --update-page <页面 vue 的相对路径> <MasterGo 设计链接> + [Image #1]
```

也适用于用户表达为：

- 在某个已有页面上更新/迭代；
- 在已有页面里新增一块、替换一块、改一个 banner / card / module；
- 明确给出了目标页面 Vue 文件路径。

若用户只要求开发独立组件、弹层、卡片且不验证宿主页位置，走 `rules/d-d2c-component.md`。

## 2. 落位与路由

- 代码写入用户命令给定的目标 Vue，或写入该页面按项目约定管理的局部组件后由目标 Vue import。
- 复用已有页面路由，禁止为 update-page 新增对外业务路由。
- 若把新增/改动块拆为独立组件，只借用 `d-d2c-component.md` 的组件落位/文件组织建议；最终验收仍按本文的物料与宿主页集成口径。
- 宿主 Vue 已包含超长的旧 Stylus/Less/Sass block 时，新增区块优先落到局部子组件或独立小 style block，不继续扩大同一个 legacy parser 单元；最终必须通过通用 `compile-check.json` 健康闸。
- `sort-of-d2c.md`、`assets-mapping.md`、`components-mapping.md` 仍是项目约定来源；新增复用组件时按既有规则更新映射。

## 3. 输入与 DSL

### 3.1 落位声明（阶段 2 硬输入）

已有页模式必须在阶段 2 前明确三件事，并通过 `placement-check.json` 落盘，不能只靠局部截图判断：

- `targetVue`：用户指定的宿主页面 Vue 文件，或实际 import 新组件的宿主页面 Vue。
- `selector`：新增/改动块根元素稳定选择器，优先 `data-d2c-component="<kebab-name>"` 或 `data-d2c-mark="L0"`。
- `anchor` + `relation`：用户要求的宿主页上下文，例如“已有卡片下部”可表达为 `--anchor-text '<已有卡片内稳定文案>' --relation after`，或 `--anchor-selector '<已有卡片稳定选择器>' --relation inside/after`。

代码实现后、截图前/后均可运行：

```bash
node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-code-placement-check.cjs \
  --page-dir d-d2c/temp/<page-dir> \
  --target <目标页面.vue> \
  --selector '<新增块根选择器>' \
  --anchor-text '<宿主页锚点文案或省略改用 --anchor-selector>' \
  --relation after
```

该脚本只做**源码结构落位证据**：它证明新增块已经写进指定 Vue，并且相对锚点的代码顺序/包含关系满足需求。它不替代真实页面截图、geometry/vertical/render 或 fidelity，也不能在真实业务路由打不开时单独构成可评分物料。

- MasterGo 设计可以是整页，也可以是页面内某个区块；但最终验收必须能证明该区块在宿主页中的位置正确。
- 若只拿到区块级设计，执行前需明确其在目标页面中的插入位置和上下文；无法判断页面最终位置时，应向用户补充确认。
- 阶段 1 仍使用当前分支既有 DSL/filter 与生成规则，不为 update-page 引入新的还原算法。

## 4. 物料与验收口径

已有页模式的核心风险有两个：新增/改动块自身是否还原，以及它是否正确落在宿主页里。因此先按设计输入选择视觉材料口径，再补充宿主页集成检查。

### 4.1 整页设计输入

若 MasterGo 设计和 `image-source.png` 表达的是完整页面最终效果，继续使用整页材料：

- `Rendered screenshot`：目标页面真实业务路由整页截图；真实路由无法稳定落地时，按本文第 4.3 节使用用户浏览器接管并等待用户完成业务操作。
- `Design screenshot`：能表达最终页面效果的整页设计图。
- `Geometry diff` / `Vertical diff`：按整页页面流运行，检查新增/改动块是否挤乱页面其它模块。
- 产物目录使用普通 page 目录：`d-d2c/temp/<page-dir>/r<N>/`，不使用 `component/<name>`。

### 4.2 局部设计输入（banner / card / module）

若 MasterGo 链接和 `image-source.png` 只对应已有页中的一个局部节点/根，允许使用 region 视觉材料，但必须满足以下条件：

- actual 必须来自真实业务路由或验证通过的 `user-browser-handoff` 真实 Vue DOM，不得来自静态 HTML、DOM replay、手写截图壳、组件离线 mock 或任何 isolated preview。
- 使用 `d2c-capture-page.cjs --capture-selector '<selector>' --design-scope '<layoutMark>'` 截取新增/改动块根元素；根元素必须同时带相同的 `data-d2c-mark` 与稳定 selector（推荐再带 `data-d2c-component`）。一个 region 只能对应一个 scope，禁止用多个 mark 或省略 scope 后退化为整页。
- `capture-meta.json` 必须包含 `captureMode:"region"`、`regionBBoxCss`、`designScope` 和 `overlayBasis.mode:"region-to-design-root"`。geometry/vertical/render 必须全部传同一 `--scope`；脚本以 scope 根宽度作为设计换算宽度，geometry 还会把 X 重置到 scope 根 `0,0`。
- region overlay 只能把该局部 actual 与同一 MasterGo 节点/根导出的设计图 `0,0` 对齐。若用户提供的是整页设计图，给 `d2c-design-opacity.cjs` / `d2c-diff-overlay.cjs` 同时传 `--page-dir` 与 `--scope`，由脚本按 marked DSL bbox 生成 `*-design-source.png`；禁止让模型看图猜裁剪值，或把整页设计图直接叠到局部 actual 上。
- 仍需生成 `geometry-diff.json` / `vertical-diff.json` / `render-diff.json`，且 phase2 联合闸仍生效；invalid capture material 直接失败，不进入 evaluator。

局部视觉材料之外，已有页模式还必须给出宿主页集成证据：

- 目标 Vue 路径确实被修改，且未新增对外业务路由。
- 新增/改动块插入在用户指定上下文位置（例如“已有卡片下部、底部按钮上方”），必须先用 `d2c-code-placement-check.cjs` 产出 `placement-check.json`；若真实路由可打开，可再用 DOM 相邻节点或辅助整页截图增强证据。
- `capture-meta.json.accessMode` 只允许 `real-route` 或 `user-browser-handoff`。验证通过且四项物料复用同一 tab 的 handoff，再加 placement check 通过，才可写成“已有页内效果已验证”。
- `d2c-workflow-checkpoint.cjs --complete 2 --mode update-page` 会强制读取 `placement-check.json` 且要求 `status="passed"`；缺失、锚点缺失、相对位置不满足或 status 为 `failed/unverified` 时禁止进入阶段 3。

登录态、URL 参数、会话注入、真实路由重定向和 browser handoff 仍按 `d-d2c-materials.md` 的当前规则处理；不得在 update-page 规则里写死 token key、域名、端口或业务守卫绕过逻辑。

### 4.3 真实路由重定向时的用户接管

真实路由第一次重定向后先按通用规则补会话/等待并重试一次。第二次仍重定向时：

1. 确认 generation、lint、DSL、placement-check 与阶段 1 `compile-check.json` 均已通过；编译/runtime/dev-server 路由失败必须先自动修复，不消耗修复轮次，也不属于用户操作边界。
2. 用 `d2c-browser-handoff.cjs --request` 写 `browser-handoff.json`，必须提供目标 URL、目标组件 selector 与第二次**运行健康且源码摘要新鲜**的重定向 `capture-meta.json`。`Cannot GET`、compiler error、空 SPA root 或 material invalid 会被脚本拒绝，禁止绕过后直接 `--start`。
3. 有真人交互时，用 `--start` 打开托管可见 Chrome，请用户完成登录/业务操作并进入目标页，然后用 `--verify` 确认路由稳定且 selector 可见。
4. headless/loop 环境不得等待或自行 fallback，输出 `EXECUTOR_BLOCKED reason=user-browser-handoff-required page-dir=<page-dir>`，让外层保留 worktree 后恢复。
5. 验证通过后，capture/geometry/vertical/render 均传同一 `--handoff-state`，capture 使用 `--access-mode user-browser-handoff`。禁止重新 goto、刷新、换 tab 或启动无状态 Chrome。
6. handoff verification 会记录 selector 根的 `data-d2c-mark`；调用方显式 `designScope` 优先，否则只允许使用该唯一 mark。两者不一致时停止修物料契约，不得继续猜 scope。

browser handoff 后的 phase2 repair 只能修改命令声明的目标 Vue 与本 case 的生成资源目录。禁止为了热更新或样式生效修改 `client/inject_*`、bootstrap、router、store、dev-server 配置或其他宿主页文件；这类动作会让真实页状态失效，并由外层 containment guard 拒绝。
6. 联合闸未放行时保持浏览器 active 供后续轮次热更新复测；放行或达到轮次上限后执行 `--cleanup --result completed`，再复跑 checkpoint 确认状态与物料一致。

用户明确取消、无法进入目标业务态或等待超时时，分别用 `--abort` / `--expire` 清理，并按 `BLOCKED_NEEDS_INPUT` 披露；不得拿重定向页或局部模拟入口继续评分。headless/loop 环境必须保持 `WAITING_FOR_USER_BROWSER`，直到用户完成操作或明确取消。

## 5. 阶段 2 与用户决策

- phase2 `geometry + vertical + render` 联合前置闸保持当前分支语义。
- 当 `--complete 2` 输出 `D2C_AUTO_FIX_REQUIRED` 或 `workflow-state.json.phase2Block.allowEvaluator=false` 时，仍停在物料阶段自动修复；禁止询问用户是否继续或接受当前结果。
- 只有阶段 3 evaluator 完成并进入阶段 4 后，才允许按既有用户决策流程询问是否继续优化。

已有页模式的所有 checkpoint 命令必须带 `--mode update-page`（第一次带上即可写入 `workflow-state.json`，后续建议继续带，避免复制状态文件时丢失模式）。

## 6. 最终摘要补充

最终摘要中需说明：

- 目标 Vue 路径；
- 是否复用已有路由且未新增对外路由；
- 截图与评测使用 `full-page` 还是 `region`；若是 region，列出 selector、`regionBBoxCss` 与 overlay basis；
- 对宿主页插入位置的集成证据；
- `placement-check.json` 的 status、selector、anchor/relation；若使用 `user-browser-handoff`，列出同 tab/session 验证结果；
- 若局部设计无法完整覆盖整页上下文，需披露该限制。
