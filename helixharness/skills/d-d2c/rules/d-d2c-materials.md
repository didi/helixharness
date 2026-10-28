---
description: d-d2c 自检基础物料生成规则（按轮次生成 actual、design-30、overlay）
alwaysApply: true
---

# d-d2c-materials（自检基础物料生成）

本文件定义代码还原完成后的基础视觉物料生成流程。该阶段按轮次生成渲染截图、30% 透明设计稿、叠图三类物料。

标准视觉物料必须使用同一个横向坐标系：以本轮 `*-actual.png` 的宽度为准，将设计稿等比缩放到该宽度后，再生成 `*-design-30.png` 和 `*-overlay.png`。这样可以避免设计稿原图宽度与页面截图宽度不一致时出现叠图比例错位。

视觉物料有两种合法坐标基准：

- `captureMode=full-page`：整页/视口截图；设计图按 `actual` 宽度等比缩放后叠到整页截图上。
- `captureMode=region`：局部 DOM 区域截图；必须由 `d2c-capture-page.cjs --capture-selector <selector> --design-scope <layoutMark>` 生成，并在 `capture-meta.json` 记录区域 CSS bbox 与唯一 design scope。region 物料只能与**同一 MasterGo 节点/根**做 `0,0` 对齐；整页设计图由脚本按 marked DSL scope bbox 确定性裁剪，禁止模型看图猜裁剪值或直接叠到局部 actual 上。

局部截图不是静态 mock：页面仍必须来自当前 workflow mode 明确允许的真实 Vue 运行入口。region 只是截图/量化坐标基准，不能绕过页面落地、模式覆盖层规定的访问策略、geometry/vertical/render 与阶段 2 联合闸。

DOM 量化自检也必须使用同一横向坐标系：`d2c-capture-page.cjs`、`d2c-geometry-check.cjs`、`d2c-vertical-check.cjs`、`d2c-render-check.cjs` 的 CSS 视口宽必须一致。截图命令须写 `capture-meta.json`；三项检查缺省会继承 `capture-meta.json.viewportWidthCss`。禁止截图与检查器混用不同宽度。

当 `capture-meta.json.captureMode=region` 时，geometry/vertical/render 会继续用 `viewportWidthCss` 打开真实页面，但三者必须收到同一个 `--scope`：量化换算使用“scope 根设计宽 / `regionBBoxCss.width`”，geometry 的期望 X 还会减去 scope 根绝对 X。region 缺 bbox、scope 缺失/多值/未命中 marked DSL 都必须非 0 退出，禁止退回整页坐标系。

宿主布局与叠图必须遵守 **`d-d2c-design-crop.md`**（`alwaysApply`）。先读取 `layout-h5.json.hostLayoutPolicy`：`immersive-overlay` 保留完整设计画布且裁顶必须为 0；`stacked-host` 才按 `hostChromeBottomY` 换算 `designCropTopRaster`。禁止按 `firstBusinessY` 裁切，也禁止把 DSL 坐标直接当栅格像素。

宿主控件始终不在 H5 还原，但沉浸式页面的顶部空间属于 H5 布局。`design-30` / `overlay` 会根据 `ignoredDesignRegions` 跳过原生控件区域的设计混合，不能通过裁掉顶部来让首业务模块假对齐。

## 前置检查：项目启动

- 在执行截图之前，先确认本轮页面的**真实完整访问 URL**（例如 `http://<host>:<port>/<route-path>`），不得猜测固定端口。
- 检测本地开发服务器是否已启动时，以该真实完整访问 URL 为准。
- 若未启动，先读取当前项目的 `package.json` 中 `scripts` 字段，识别正确的启动命令（常见如 `dev`、`start`、`serve` 等），然后在后台执行对应命令启动项目，并等待服务就绪后再继续。
- 阶段 1 必须已由 `d2c-runtime-health-check.cjs` 生成与当前源码摘要一致、`status="passed"` 且 `appMounted=true` 的 `compile-check.json`。源码在阶段 2 修复后，`d2c-capture-page.cjs` 会重新记录当前摘要和 `runtimeHealth`，因此旧截图不能冒充新代码物料。
- `Failed to compile` / `Errors while compiling` / `Module build failed` / browser page error / HTTP 4xx/5xx / `Cannot GET` / 空 SPA root 都属于 executor 可修复的运行时材料错误。截图会写 `materialStatus="invalid"`、`askUser=false`，checkpoint 要求覆盖同轮重采且不消耗视觉修复轮次。只有 `runtimeHealth.status="passed"` 后发生的目标路由重定向，才允许进入 browser handoff 分支。

### 横向溢出与截图宽度钳制（强制核对）

- `d2c-capture-page.cjs` 现会把 `*-actual.png` 宽度**钳制到设计视口宽**（`--width` × `--dpr`，默认 678×2=1356px）。若页面 `scrollWidth > 视口宽`（横向溢出），脚本会裁掉右侧溢出列并在 **stderr 告警**，同时在结果 JSON 标记 `widthClamped:true` 与 `horizontalOverflowCss`。
- ⚠️ **看到该告警必须当作页面真实 bug 修复**（如绝对定位/固定宽元素越界、印章/角标超出画板右缘），在代码侧加 `overflow-x:hidden`/约束宽度，而不是无视。
- 背景：若不钳制，`fullPage` 截图会按 `scrollWidth` 出图，叠图脚本以 `actualW/designW` 缩放设计稿时会**连带把设计稿纵向放大**，产生自上而下累积的「假纵向漂移」，污染 overlay 与还原度评测。

## 输入

- 设计稿原图：`d-d2c/temp/<page-dir>/image-source.png`
- 页面访问 URL：本次生成页面在本地开发服务器中的完整 URL，优先使用 `--url <full-url>`
- 页面标识：`<page-id>`，用于生成物料文件名前缀
- 当前有效轮次：`r<N>`，首轮为 `r1`；只有代码修复后得到一套新的有效 actual + geometry + vertical + render 才递增。invalid/source-material 或与最近有效轮四份证据完全相同的 no-progress 尝试覆盖同轮重采，不占 8 轮额度
- 输出目录：`d-d2c/temp/<page-dir>/r<N>/`
- **登录态 / URL 参数（可选）**：若目标项目**需要登录才能打开页面**，截图、geometry、vertical、render 四脚本必须使用完全相同的参数：
  - 命令行：给四脚本加 `--url-params 'token=<token>'`；
  - 环境变量：`export D2C_URL_PARAMS='token=<token>'`。
  四脚本都会脱敏日志。⚠️ 不要把 token 写进会提交的文件。
- **登录态（会话形式，flow-gated SPA 推荐）**：部分 SPA 的路由守卫 / 鉴权 Flow **不认 URL query token 当有效会话**（会把目标路由重定向到首页 / 登录页），只认 **cookie / localStorage 会话**。此时由**使用方提供一份有效会话快照**，三脚本在 `page.goto` **之前**把它放进浏览器（cookie 用 `setCookie`、localStorage 用 `evaluateOnNewDocument` 在文档创建时注入），让路由守卫**照常运行、验到有效登录态后正常放行**（等价于真人已登录后打开页面，**不是绕过守卫**）。两种传法（任选其一，三脚本须一致贯穿）：
  - 命令行：给截图、geometry、vertical、render 四脚本加 `--session-state '<json>'`；
  - 环境变量：`export D2C_SESSION_STATE='<json>'`。
  JSON 结构（值可为已编码字符串；为空 / 未提供则整段跳过，老 case 零影响）：

  ```json
  { "cookies": [{ "name": "SID", "value": "…", "domain": "127.0.0.1", "path": "/" }], "localStorage": { "auth": "…" }, "origin": "http://127.0.0.1:<port>" }
  ```

  cookie 的 `domain` / `path` 缺省时按访问 URL 的 origin 推断。⚠️ 具体注入哪些 key / 值属**外部输入**（由 case 提供），**skill 不写死任何 key 名 / 域名 / 单页逻辑**；敏感键值在日志里只打印 key 名并标 `(values not logged)`，不要把会话写进会提交的文件。会话注入优先于 URL token 用于 flow-gated SPA。
- **用户浏览器接管（update-page 默认）**：若已有页真实目标 URL 经 URL 参数/会话注入并重试 1 次后仍被 SPA flow 重定向，先创建 `browser-handoff.json`。交互环境让用户在托管可见 Chrome 中进入目标业务态，验证路由与目标 selector 后，四个物料脚本通过 CDP 复用同一 tab；headless/loop 环境暂停并由外层恢复。该入口记为 `accessMode=user-browser-handoff`，验证通过时属于真实页面集成证据。
- **模式能力边界**：运行入口与 fallback 只能由当前 mode 的覆盖层选择。`update-page` 读取 `d-d2c-update-page.md`；`create-page` 与 `component` 读取 `d-d2c-preview.md`。公共物料规则不得自行跨模式选择入口。

## 输出物料

必须生成以下六个文件：

```text
d-d2c/temp/<page-dir>/r<N>/<page-id>-<md5>-actual.png
d-d2c/temp/<page-dir>/r<N>/<page-id>-<md5>-design-30.png
d-d2c/temp/<page-dir>/r<N>/<page-id>-<md5>-overlay.png
d-d2c/temp/<page-dir>/r<N>/geometry-diff.json
d-d2c/temp/<page-dir>/r<N>/vertical-diff.json
d-d2c/temp/<page-dir>/r<N>/render-diff.json
```

其中 `<md5>` 必须由脚本基于设计稿 `image-source.png` 自动生成，禁止手工命名。它是稳定的**设计稿短哈希**，不是 actual 内容哈希；不同轮次文件名后缀相同是正常现象，禁止据此判断截图陈旧。
`capture-meta.json` schema v2 会分别记录 `designMd5`、最终裁切后的 `actualMd5` 与 `actualSha256`；判断本轮是否真的产生新渲染只认 `actualSha256`，并同时核对 geometry/vertical/render 的规范化测量摘要。旧字段 `md5` 仅是 `designMd5` 的兼容别名。
轮次只体现在目录名 `r<N>/` 中，禁止把轮次写进文件名。
`geometry-diff.json` 由 `d2c-geometry-check.cjs` 产出：对 DSL 每个 TEXT 节点在渲染 DOM 中实测宽度/左右缘/空白完整性/字号并与 DSL bbox 对比，是阶段 2 联合前置闸中的横向/尺寸**量化硬证据**。
`vertical-diff.json` 由 `d2c-vertical-check.cjs --page-flow` 产出：对各顶层业务根及其内部高风险纵向锚点（flex group、非流式文本行）实测 `top/height/bottom` 与 DSL 对账，同时捕捉页面拼装和容器内部坍缩，是阶段 2 联合前置闸中的纵向量化硬证据（`violationCount` 只计 primary 根因）。
`render-diff.json` 由 `d2c-render-check.cjs` 产出：按 `data-d2c-mark` 比较 DSL 与浏览器 computed style、字体加载/合成字重、字形裁切、圆角/颜色/渐变/描边/阴影及 `dsl-assets.json` 资源引用；资源可通过 asset id、文件名、`contentSha256` 或 SVG path fingerprint 证明内容等价。对已匹配资源还会做透明像素采样，并读取 SVG even-odd/mask/clip 与内置 opacity；可证明资源含透明区域时，同节点非透明 CSS 背景属于 `asset-background-contamination` hard violation。只有 high-confidence 项进入 hard violations；无法确定的视觉等价项写 warnings，留给 fidelity evaluator。

## 执行流程

1. 渲染页面并完成代码实现截图。使用 `d2c-capture-page.cjs` 打开已运行的本地页面，截图参数固定为 `dpr=2`；命令必须携带 `--design`、`--label` 和 `--meta-out d-d2c/temp/<page-dir>/r<N>/capture-meta.json`，且不传 `--round`，让脚本基于设计稿 md5 自动生成基础物料名，输出：

   ```text
   d-d2c/temp/<page-dir>/r<N>/<page-id>-<md5>-actual.png
   ```

   真实业务路由优先。先读取 `capture-meta.json.runtimeHealth`：未通过时按 `failureClass` 自动修复编译/runtime/dev-server 路由并覆盖重采当前轮，禁止询问用户。只有 `runtimeHealth.status="passed"` 且截图 JSON 显示 `redirected:true` 时，才按当前 mode 覆盖层规定的唯一路径继续；公共规则不得自行选择其它 fallback。

   update-page handoff 请求与恢复命令骨架：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-browser-handoff.cjs --request \
     --page-dir d-d2c/temp/<page-dir> --target-url '<real-url>' \
     --selector '<target-selector>' --capture-meta d-d2c/temp/<page-dir>/r<N>/capture-meta.json

   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-browser-handoff.cjs --start \
     --page-dir d-d2c/temp/<page-dir> --url '<real-url>' --width <viewport-width> --height <viewport-height> --dpr 2
   # 用户进入目标页后：
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-browser-handoff.cjs --verify --page-dir d-d2c/temp/<page-dir>
   ```

   `--request` 会确定性拒绝缺少 runtime health、material invalid、源码摘要过期或没有健康重定向证据的 capture。`--start` 若打开后检测到 compiler overlay / 空 SPA / `Cannot GET`，会关闭无效托管浏览器并返回 repair sentinel，不会把不可由用户解决的问题包装成 handoff。

   验证后，截图和三个检查命令都追加 `--handoff-state d-d2c/temp/<page-dir>/browser-handoff.json`；截图再追加 `--access-mode user-browser-handoff`。四份 JSON 中的 `browserSessionId` / `browserTargetId` 必须一致。

   若本次设计输入只要求还原已有页中的单个 banner/card/module，可使用 region 截图：给新增/改动块根元素添加与 DSL 一致的唯一 `data-d2c-mark` 和稳定选择器，并在截图命令追加 `--capture-selector '<selector>' --design-scope '<layoutMark>'`。无论 `image-source.png` 是局部导出还是整页导出，后续视觉/DOM 脚本都必须复用同一 scope。

2. 已有页更新模式（`--update-page`）必须生成源码落位证据，证明新增/改动块在目标 Vue 的宿主页上下文中；物料入口只允许真实路由或验证后的 user browser handoff。

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-code-placement-check.cjs \
     --page-dir d-d2c/temp/<page-dir> \
     --target <目标页面.vue> \
     --selector '<新增块根选择器>' \
     --anchor-text '<宿主锚点文案或省略改用 --anchor-selector>' \
     --relation after
   ```

   输出 `d-d2c/temp/<page-dir>/placement-check.json`。`--complete 2 --mode update-page` 会强制校验它存在且 `status="passed"`；新建整页与纯组件模式不受此规则影响。

3. 确认 `d-d2c/temp/<page-dir>/host-layout.json` 与 `layout-h5.json` 已存在（阶段 1 filter-dsl 产出）。其中：
   - `hostLayoutPolicy`：`immersive-overlay` / `stacked-host` / `none`
   - `hostChromeBottomY`、`firstBusinessY`、`businessTopGap`：宿主边界与必须保留的业务间距
   - `layoutShiftY`：实际 H5 坐标平移量；`contentOffsetY` 只是其兼容别名
   - `designCropTopRaster`：`round(layoutShiftY × image-source.height / designFrame.height)`
   - `ignoredDesignRegions`：沉浸式设计侧原生控件忽略区域
   - 可用 `d2c-resolve-design-crop.cjs --design image-source.png --print value` 核对

   **推荐：步骤 4、5 不传 `--design-crop-top`**，由脚本按 `layout-h5.json` 自动处理。沉浸式必须返回 0；手填冲突值会 stderr 报错。

   裁切只作用于设计稿，不裁 `actual`；不得补画宿主 UI 或平移截图凑对齐。

4. 解析第 1 步生成的 `*-actual.png` 绝对路径，后续 `design-30` 和 `overlay` 必须都使用这个具体文件。

5. 按 `hostLayoutPolicy` 处理设计稿：沉浸式保留完整高度并屏蔽 `ignoredDesignRegions`，stacked 才裁掉 `designCropTopRaster`；随后等比缩放到 `actual` 宽度并调整透明度为 30%。使用 `d2c-design-opacity.cjs` 输出：

   ```text
   d-d2c/temp/<page-dir>/r<N>/<page-id>-<md5>-design-30.png
   ```

6. 将 30% 透明设计稿覆盖在代码实现截图上。`d2c-diff-overlay.cjs` 会执行与第 5 步完全相同的 policy/crop/ignore 处理，再生成：

   ```text
   d-d2c/temp/<page-dir>/r<N>/<page-id>-<md5>-overlay.png
   ```

7. 运行几何自检 `d2c-geometry-check.cjs`（与截图同一 URL），输出：

   ```text
   d-d2c/temp/<page-dir>/r<N>/geometry-diff.json
   ```

   - 坐标类 violations（top/left 漂移、左右缘/宽高/上下缘偏差）会进入阶段 2 **联合前置硬闸**；当前有效轮未达最大修正轮次（固定 8 轮）且超出固定允差（8 设计px）时，禁止进入 `d2c-fidelity-evaluator`。禁止在 checkpoint 命令中附加 `--max-rounds` 或 `--geo-tolerance` 逃避坐标子闸；脚本会直接拒绝这类参数。
   - geometry 的非坐标 violations 不直接计入坐标允差；其中可确定的 font-size/空白/裁切等由 render 闸独立复核，其余仍作为 fidelity 输入。
   - `--complete 2` 会联合校验 `geometry-diff.json`、`vertical-diff.json`、`render-diff.json`，任一缺失或 hard violation 未清零且未达最大有效轮次时禁止进入评测。
   - 若同轮 `capture-meta.json` 标记材料无效，或 `captureMode=region` 却缺少有效 `regionBBoxCss`，脚本会直接失败。必须先修复截图材料，而不是解释 geometry 分数；checkpoint 会输出 `consumeRound=false nextRound=N`，要求覆盖重采同轮，不消耗有效修复轮次。

8. 运行纵向自检 `d2c-vertical-check.cjs --page-flow`（同一 URL），输出：

   ```text
   d-d2c/temp/<page-dir>/r<N>/vertical-diff.json
   ```

   - **create-page 必须显式传 `--page-flow`**，产物必须为 `mode="page-flow"`，并从 `page-flow-plan.json`（简单页）或 `component-plan.json`（复杂页）覆盖全部顶层业务根。缺计划、根 mark 未命中或报告退化为 `mode="page"` 时硬失败，禁止用普通文本锚点报告替代页面骨架检查。
   - 校验各顶层根及其内部高风险锚点的 top/height/bottom：既捕捉拼装纵向偏移，也阻断「顶层卡片外框正确但内部多行重叠/坍顶」；primary 坐标类违规会参与阶段 2 联合前置闸，`derived` 级联项与其它说明须带入评测阶段逐条核对。
   - checkpoint 会从 `items[].expected/measured` 重新计算横纵坐标差值；不得通过删改 `violations`、错误计数或相对文本锚点规避联合闸。
   - 与 geometry 一样，vertical 必须拒绝 invalid material；region 模式下使用同轮 `regionBBoxCss` 作为局部坐标基准。

9. 运行 render 自检 `d2c-render-check.cjs`（与截图/geometry/vertical 同一 URL、viewport、session），输出：

   ```text
   d-d2c/temp/<page-dir>/r<N>/render-diff.json
   ```

   - hard violations 包括：无效 DSL/CSS、可确定的字体/字重/字号/斜体/字距/颜色/装饰偏差、文字裁切、圆角/填充/渐变/描边/阴影偏差，以及静态资源未落盘、未引用、加载失败、透明资源被同节点 CSS 背景污染或 SVG/CSS opacity 重复衰减。
   - warnings 表示脚本无法可靠证明视觉等价，不参与硬闸，必须交给阶段 3 fidelity evaluator。
   - `repairClass=source-material` 输出 `consumeRound=false`，修复 DSL/字体/资源后覆盖同轮；其它 implementation hard violation 与坐标问题共享同一个 8 轮额度。

10. 若本轮持有 mode 覆盖层管理的临时运行会话，先保持会话 active 并执行阶段 2 checkpoint，让 checkpoint 判定联合闸是否需要自动修复。**不要在联合闸尚未确认可放行前 cleanup**：若输出 `D2C_AUTO_FIX_REQUIRED`，按 sentinel 的 `nextRound` 重采并复用同一已验证入口。

11. 确认六个物料文件都存在；mode 覆盖层要求的入口状态文件也必须齐全。已有页 user browser handoff 须确认 `browser-handoff.json.status="verified"` 且四份报告的 session/target id 一致，并确认 `placement-check.json.status="passed"`。随后必须执行阶段 2 检查点：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 2 --page-dir <page-dir> [--mode update-page]
   ```

   若联合闸或 placement 未通过，本轮停止在物料阶段。`coordinate-gate-failed` / `render-gate-failed` / `phase2-combined-gate-failed` 且 `consumeRound=true` 时按 `repairPlan` 改代码进入 `r<N+1>`；`render-source-material-invalid` / material-validation / no-progress 的 `consumeRound=false` 必须先修物料链并覆盖同轮。禁止询问用户是否继续或接受当前结果。只有联合过闸，或用满固定 8 个**有效**修正轮次并披露残余项后，才进入结果评测。

12. 若 checkpoint 已确认联合闸通过，或已达固定 8 轮最大修正轮次允许进入阶段 3，此时必须按 mode 覆盖层清理本轮持有的临时入口，然后再次执行 `--complete 2`。

   user browser handoff 使用：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-browser-handoff.cjs \
     --page-dir d-d2c/temp/<page-dir> \
     --cleanup --result completed
   ```

   最后统一复跑：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --complete 2 --page-dir <page-dir> [--mode update-page]
   ```

## 执行约束

- 截图使用 `dpr=2`（脚本默认值）；后续物料以实际截图宽度为准做设计稿等比对齐。
- 标准物料中的 `design-30` 必须与 `actual` 等宽，高度按设计稿原始比例等比缩放。
- 标准物料中的 `overlay` 画布尺寸必须与 `actual` 完全一致；设计稿缩放后的高度之外，叠图下方保留 `actual` 原图内容。
- `design-30` 与 `overlay` 必须使用**同一套**裁切（默认均由 `layout-h5.json` 自动解析；若手传 `--design-crop-top` 则两脚本须一致）。
- 评测时同时提供 `hostLayoutPolicy`、`hostChromeBottomY`、`firstBusinessY`、`layoutShiftY`、`designCropTopRaster` 与 `ignoredDesignRegions`。
- 叠图透明度固定为 30%（`opacity=0.3`）。
- 每轮截图物料统一存放在 `d-d2c/temp/<page-dir>/r<N>/`。
- 当前轮评测只能使用当前轮目录下、且已通过阶段 2 联合前置闸（或达到固定 8 个有效轮次并披露未达标项）的截图物料。
- 后续轮次必须新建 `r<N+1>/`，禁止覆盖旧轮次目录。
- `workflow-state.json.phase2Block.allowEvaluator=false` 是阶段 2 自动修复阻塞状态；它只允许修代码/清临时物料/生成下一轮物料，不允许进入 evaluator、用户决策或最终摘要。等待 browser handoff 验证不消耗有效修复轮次。
- 传给 `d2c-fidelity-evaluator` 的 `Overlay screenshot` 必须来自当前轮同宽规则生成的 `*-overlay.png`；如果发现宽度不一致，必须重新生成物料后再评测。
- mode 覆盖层登记的临时入口不能留在最终业务源码里；checkpoint 会拒绝不合法入口。`user-browser-handoff` 还必须有 verified/completed 状态、可见 selector 证据，并要求 capture/geometry/vertical/render 的 browser session 与 target id 完全一致。

## 核心命令

以下命令中的 `<page-id>`、`<page-dir>`、`<full-url>`、`<N>` 需替换为本次任务实际值。

0. 创建当前轮输出目录：

   ```bash
   mkdir -p d-d2c/temp/<page-dir>/r<N>/
   ```

1. 自动截图，生成 `*-actual.png`。推荐传完整 URL：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-capture-page.cjs \
     --url <full-url> \
     --design d-d2c/temp/<page-dir>/image-source.png \
     --label <page-id> \
     --out d-d2c/temp/<page-dir>/r<N>/ \
     --meta-out d-d2c/temp/<page-dir>/r<N>/capture-meta.json \
     --dpr 2
   ```

   如果只知道 route path，必须显式传入 host 与 port，或提前设置 `D2C_DEV_HOST` / `D2C_DEV_PORT`：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-capture-page.cjs \
     --host <host> \
     --port <port> \
     --path <route-path> \
     --design d-d2c/temp/<page-dir>/image-source.png \
     --label <page-id> \
     --out d-d2c/temp/<page-dir>/r<N>/ \
     --meta-out d-d2c/temp/<page-dir>/r<N>/capture-meta.json \
     --dpr 2
   ```

   也可以用 `D2C_CAPTURE_URL=<full-url>` 作为完整 URL 兜底。没有完整 URL，也没有可拼出完整 URL 的 host/port/path 时，截图脚本必须报错停止。

1a. 局部 region 截图（已有页局部需求；scope 必须唯一）：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-capture-page.cjs \
     --url <full-url> \
     --design d-d2c/temp/<page-dir>/image-source.png \
     --label <page-id> \
     --out d-d2c/temp/<page-dir>/r<N>/ \
     --meta-out d-d2c/temp/<page-dir>/r<N>/capture-meta.json \
     --capture-selector '<selector-for-implemented-region>' \
     --design-scope '<layoutMark>' \
     --dpr 2
   ```

   actual 是局部物料；design-30/overlay 在步骤 4/5 由同一 scope 生成。若要证明已有页插入位置，还必须补充结构化集成检查（目标 Vue diff、插入相邻上下文、无新增对外路由）或另行保留真实宿主页整页截图作为辅助证据。

1b. 已有页源码落位检查（仅 `--update-page`）：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-code-placement-check.cjs \
     --page-dir d-d2c/temp/<page-dir> \
     --target <目标页面.vue> \
     --selector '<新增块根选择器>' \
     --anchor-selector '<宿主锚点选择器>' \
     --relation after
   ```

   也可用 `--anchor-text '<宿主锚点文案>'` 替代 `--anchor-selector`。输出 `placement-check.json`；`status` 必须为 `passed`。

2. （可选）核对栅格裁切高度：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-resolve-design-crop.cjs \
     --design d-d2c/temp/<page-dir>/image-source.png \
     --print value
   ```

   输出为 `designCropTopRaster`（栅格 px）。**不要**把 `layout-h5.json` 里的 `layoutShiftY` 直接写入 shell 变量传给 `--design-crop-top`。

3. 解析当前轮 `actual` 的具体路径：

   ```bash
   actual_path="$(ls -1 d-d2c/temp/<page-dir>/r<N>/<page-id>-*-actual.png | head -n 1)"
   ```

4. 生成与 `actual` 等宽的 30% 透明设计稿（**推荐省略 `--design-crop-top`**，自动读 `layout-h5.json`）：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-design-opacity.cjs \
     --design d-d2c/temp/<page-dir>/image-source.png \
     --actual "$actual_path" \
     --page-dir d-d2c/temp/<page-dir> \
     --scope '<layoutMark-for-region-only>' \
     --name <page-id> \
     --out d-d2c/temp/<page-dir>/r<N>/ \
     --opacity 0.3
   ```

5. 生成画布尺寸与 `actual` 完全一致的叠图（同样推荐省略 `--design-crop-top`）：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-diff-overlay.cjs \
     --design d-d2c/temp/<page-dir>/image-source.png \
     --actual "$actual_path" \
     --page-dir d-d2c/temp/<page-dir> \
     --scope '<layoutMark-for-region-only>' \
     --name <page-id> \
     --out d-d2c/temp/<page-dir>/r<N>/ \
     --opacity 0.3
   ```

   full-page 模式省略两条 `--scope`；region 模式不可省略。region 的 opacity 命令还会在本轮输出 `*-design-source.png`，该文件是 fidelity 的设计输入，整页 `image-source.png` 只保留为来源证据。

6. 几何自检（DOM 实测 vs DSL bbox，URL 与第 1 步截图一致；puppeteer-core 解析不到时设 `NODE_PATH=$(pwd)/node_modules`）：

   `d2c-geometry-check.cjs` 会从同轮 `--out` 目录读取 `capture-meta.json` 并继承截图视口宽；若截图没有写 metadata，或几何输出目录不是截图轮次目录，必须显式传入与截图完全相同的 `--width`。

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-geometry-check.cjs \
     --url <full-url> \
     --page-dir d-d2c/temp/<page-dir> \
     --scope '<layoutMark-for-region-only>' \
     --out d-d2c/temp/<page-dir>/r<N>/
   ```

   输出 `geometry-diff.json`；坐标类违规会参与阶段 2 联合前置闸，stdout 的完整违规清单仍须原样带入结果评测阶段。

7. 纵向自检（DOM 实测 vs DSL top/height/bottom，组件根拼装流，URL 同上）：

   `d2c-vertical-check.cjs` 同样会继承同轮 `capture-meta.json.viewportWidthCss`；缺 metadata 或跨目录输出时必须显式传入同一个 `--width`，确保 `scaleCssToDesign` 与截图/叠图一致。

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-vertical-check.cjs \
     --url <full-url> \
     --page-dir d-d2c/temp/<page-dir> \
     --scope '<layoutMark-for-region-only>' \
     --out d-d2c/temp/<page-dir>/r<N>/
   ```

   region 使用唯一 `--scope`；create-page 则省略 `--scope` 并改传 `--page-flow`。create-page 产物必须声明 `mode="page-flow"` 且完整命中计划中的顶层根，并保留根内部自动收集到的高风险锚点，禁止静默降级为普通 page 模式或裁掉内部 items。primary 坐标类违规会参与阶段 2 联合前置闸，stdout 的 primary/derived 违规仍须原样带入结果评测阶段。

8. render 自检（computed style / font / clipping / asset，URL 与 viewport 同上）：

   ```bash
   node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-render-check.cjs \
     --url <full-url> \
     --page-dir d-d2c/temp/<page-dir> \
     --scope '<layoutMark-for-region-only>' \
     --out d-d2c/temp/<page-dir>/r<N>/
   ```

   输出 `render-diff.json`。hard violations 参与 phase2 联合闸；warnings 只作为 fidelity 输入。该脚本同样继承 `capture-meta.json` 的 viewport/region，并使用相同 URL 参数与会话。

生成后必须先执行 `d2c-workflow-checkpoint.cjs --complete 2`。只有 geometry + vertical + render 联合前置闸通过，或已达固定 8 个有效轮次并披露未达标项，才把当前轮截图与三份 diff 解析为具体绝对路径供 `d2c-fidelity-evaluator` 使用。

## 鉴权页面
需要登录态的页面，使用 `--session-state` 注入 cookie/localStorage。详见 [SKILL.md](../SKILL.md#鉴权页面--需要登录态)。
