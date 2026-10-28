---
description: MasterGo DSL d-d2c 代码生成规范
alwaysApply: true
---

# d-d2c-generate（代码生成规范）

## 输入来源

d-d2c 只接受 **MasterGo 设计页链接** 作为设计输入源。生成流程固定为：

```text
MasterGo 设计页链接 -> 拉取 dsl_full.json -> filter-dsl.cjs -> Layer.md + dom.md + styles.md + mixed-text.json + text-whitespace.json + dsl_layout_marked.json + dsl_visual.json -> 基于布局 + 样式 + 设计图生成页面代码
```

若用户未提供 MasterGo 设计页链接，应先要求补充可解析到 fileId/layerId 的 MasterGo 链接。

## 复杂度分流（强制）

`filter-dsl.cjs` 自动产出 **`complexity-plan.json`**。写任何代码前先读其 `verdict`（脚本结论，禁止口头推翻）：`simple` → 按本文件现有路径一次性生成；`complex` → 转 **`d-d2c-complex-split.md`** 拆解为组件串行实现（每个组件的实现仍逐条遵守本文件全部规范，输入换成 `components/<id>/` 下的切片产物）。

## 生成输入约束

0. 若有设计图对照：将用户给出的设计原图置于 `d-d2c/temp/<page-dir>/`，文件名为 **`image-source.png`**（推荐）。
1. 生成代码前必须读取：
   - **`d-d2c/temp/<page-dir>/Layer.md`** — 布局树 + 功能区域（头部含「已剔除」「H5 布局坐标」；y 已对齐设计业务区，以文件为准）。
   - **`d-d2c/temp/<page-dir>/dom.md`** — DOM 树 + CSS 样式属性（位置/文字/字体/颜色/描边/特效/间距）。
   - **`d-d2c/temp/<page-dir>/styles.md`** — **样式规格表**（fills / strokes / effects / bbox / itemSpacing；文本 fontSize / fontWeight 等）。
   - **`d-d2c/temp/<page-dir>/mixed-text.json`** — **混排标记**（`hasMixedText`；为 `true` 时见 `items` 与 `styles.md` / `dom.md`）。
   - **`d-d2c/temp/<page-dir>/text-whitespace.json`** — **空白保真标记**（`hasWhitespaceRisk`；为 `true` 时按「文本空白与水平锁宽」实现）。
   - **`d-d2c/temp/<page-dir>/page-flow-plan.json`** — 顶层页面流 / 叠层判定（背景层、内容流、重叠关系、推荐实现方式）；写页面级结构前必须读取。
   - **`d-d2c/temp/<page-dir>/dsl_layout_marked.json`** — 带 `_layoutMark` 的完整 DSL（本地对照，勿全文载入 Agent）。
   - `d-d2c/` 目录下相关 `*.md` 文档。
   - `$HELIXHARNESS_PLUGIN/skills/d-d2c/rules/` 目录下相关规则文档（如存在）。
   - `$HELIXHARNESS_PLUGIN/skills/d-d2c/rules/d-d2c-limited.md`（实现边界，强制遵循）。
   - `d-d2c/components-mapping.md`、`d-d2c/assets-mapping.md`（组件/资源映射表，命中必须复用，未命中才可自写并回填映射表；新增的组件同步更新 `components-mapping` 与 `sort-of-d2c`）。

2. 静态图片落盘目录**必须等于代码 import 经别名解析后的真实目录**——「写盘路径」必须 ==「import 别名解析根 + 引用子路径」。二者不一致会触发 webpack/vite `Module not found` → 整页 `Failed to compile`、页面渲染成错误遮罩（曾实测整页 0 还原、28/28 文案 text-missing）。
   - **先定别名解析根再落盘**：从构建配置（`webpack.*.conf.js` 的 `resolve.alias`、`vite.config` 的 `alias`，或 `assets-mapping.md` 已记录的约定）解析出 import 前缀对应的真实目录。例：别名 `@` → `client/`，则 `@/assets/images/...` 的真实写盘目录是 `client/assets/images/...`，**不是**仓库根 `assets/images/...`。
   - **写盘后、引用前自检**：对每个被引用资源，确认「import 路径经别名解析后的绝对路径」在磁盘真实存在；缺失则改正写盘目录或改正 import，**禁止**带着无法解析的资源引用进入截图/物料阶段。
   - DSL 节点 `paint` / `fill` 为 `image` 且含可下载 URL 时，**必须下载**并引用本地路径。
   - 禁止忽略 `fill=image`。

2b. Vue SFC 的 `<style lang>` 必须使用项目已安装 loader 支持的语言，禁止默认写 `less`。
   - 写 `.vue` 前先看项目已有页面和 `package.json`：若项目大量使用 `lang="stylus"` / `stylus-loader`，则新文件也用 `lang="stylus"`；若没有 `less-loader`，禁止 `<style lang="less">`。
   - `d2c-workflow-checkpoint.cjs --complete 1` 会扫描本轮改动的 `.vue`：发现未安装 loader 的 `lang` 会直接阻断。必须先改成项目支持的 `stylus/css/scss` 并确认 dev server 不再出现 `Failed to compile` / `Module parse failed`，再进入截图阶段。
   - 不确定时优先复用同目录相邻页面的 style lang；不要从设计稿或个人偏好推断为 less。
   - **已有页的超长 legacy preprocessor block 不继续无界追加**：目标 SFC 已有很长的 Stylus/Less/Sass block、旧 loader/parser 或深层嵌套时，新增局部区域优先拆为项目内本地子组件，或使用独立的小型 `<style scoped lang="项目现有语言">` block，避免把新样式继续塞入同一个递归解析单元。不要用固定行数主观宣布失败；以 `d2c-runtime-health-check.cjs` 的真实构建结果为最终依据。若出现 parser `Maximum call stack size exceeded`，先隔离/拆分新增样式再重跑健康闸，不得把它解释为登录重定向。

2c. Vue SFC 的 `<script>` 禁止引入项目未安装的依赖，尤其是 decorator / class-component 生态。
   - 写 `.vue` 前先看 `package.json` 与 `node_modules` 是否存在对应裸包 import；没有就禁止新增 `import { Component, Vue } from 'vue-property-decorator'`、`vue-class-component` 等。
   - 新增静态还原页若没有业务逻辑，优先使用项目最小兼容写法：`<script>export default { name: 'Xxx' }</script>` 或项目已有 Composition/Options API；不要为一个纯展示页引入新的装饰器依赖。
   - `d2c-workflow-checkpoint.cjs --complete 1` 会扫描本轮改动 `.vue` 的裸包 import：依赖不在 `package.json` 且 `node_modules/<pkg>` 不存在时直接阻断。必须改成项目已有写法后再进入截图阶段。

3. **MasterGo DSL：优先脚本拉取，脚本失败再由 Agent 经 MCP 拉取；禁止编造 DSL。** 与本轮 `d-d2c/temp/<page-dir>/` 对齐落盘（`<page-dir>` 与 `d-d2c-workflow-self-check.md` 中临时产物目录一致）：
   （1） **拉取完整 DSL（首选）**：执行 `$HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/fetch-mastergo-dsl.cjs`（令牌等见脚本文件头）。用户显式提供 `业务线<name>` 时，命令必须附加 `--business-line <name>`；未提供时禁止自动补默认值。脚本先尝试本地令牌直连，无令牌或直连失败且已指定业务线时，走 Skill 内配置的转发地址（`config/transponder-routes.json` 的 `transponderUrl`，可用 `HELIXHARNESS_FRONTEND_ASSET_URL` 覆盖）；未指定业务线时不走转发。脚本会对 `503` / `10015` / 超时 / 5xx 自动重试，成功后原始 JSON 写入 **`dsl_full.json`**（`--out` 指向该路径）。
   （2） **脚本失败回退（退出码 2）**：读取 **`d-d2c/temp/<page-dir>/.dsl-fetch-fallback.json`**。先按其中 `mcp.arguments` 调用 **mastergo-magic-mcp** 的 **`mcp__getDsl`**，遇到 `10015` / 服务器繁忙 / 超时最多重试 3 次；成功后必须把 **MCP 原始响应**原封不动写入 **`mcp-getDsl-response.json`**，再执行 **`normalize-mastergo-dsl.cjs --input mcp-getDsl-response.json --out dsl_full.json`**。禁止手写、摘抄、压缩、补全或伪造 `dsl_full.json`。
   （2a） **section fallback（getDsl 仍失败时）**：若 `mcp__getDsl` 重试后仍不可用，但 `mcp__getDesignSections` 可用，必须按 section 工作流落盘原始响应：概览写入 **`mcp-sections/overview.json`**，每个 `sectionIndex` 响应写入 **`mcp-sections/section-<N>.json`**，`getDesignSvgs` 写入 **`mcp-sections/svgs.json`**，`getDesignTexts` 写入 **`mcp-sections/texts.json`**（工具存在时必调）。随后执行 **`assemble-mastergo-sections.cjs --overview mcp-sections/overview.json --sections-dir mcp-sections --svgs mcp-sections/svgs.json --texts mcp-sections/texts.json --out dsl_full.json`**。禁止由 Agent 用 heredoc/Write 拼 `dsl_full.json`。
   （2b） **空 DSL 硬阻断**：`dsl_full.json` 必须包含非空 `nodes` 或可抽取到非空 `dsl.nodes`。只有 `sections` / `rootMetadata` / `rootContainer` 的 metadata-only 文件不是 DSL；`filter-dsl.cjs` 会非 0 退出，必须回到第（1）/（2）/（2a）步，禁止写 Vue、注册路由、截图或把该 run 当作还原度问题处理。
   （3） **压缩 DSL / 布局**：执行 `filter-dsl.cjs`，产出 **`Layer.md`**、**`dom.md`**、**`styles.md`**、**`mixed-text.json`**、**`text-whitespace.json`**、**`flex-groups.json`**、**`page-flow-plan.json`**、**`dsl_layout_marked.json`**、**`dsl_filtered.json`**（与 layout_marked 同内容、单行压缩）、**`dsl_visual.json`**、**`dsl-assets.json`**（及 `stripped-chrome.json`、`host-layout.json`、`layout-h5.json` 等备查）。脚本会在删除宿主节点前，仅基于原始 DSL 的 bbox、层级、裁剪与跨界背景关系判定 `hostLayoutPolicy=immersive-overlay|stacked-host|none`；非多模态 executor 直接服从该产物，禁止自行看图猜测。生成代码以 **`Layer.md` 头部说明**与 **`page-flow-plan.json` 的顶层流/叠层结论**为准，勿还原已剔除的宿主控件；但 `immersive-overlay` 必须保留完整画布和业务原始 Y，禁止把“控件不生成”误写成“顶部空间也删除”。详见 **`d-d2c-design-crop.md`**。
   （3b） **复杂矢量**：`dsl-assets.json` 中 `status=needs_mastergo_export` 的项，**落盘前**至少按 `nodeName`、`Layer.md`、`dom.md`、`styles.md` 用 HTML+CSS 还原**可见文案与颜色**（`nodeName` 含空格时可拆多行；混排文本按 `mixed-text.json`），**禁止**仅留纯色块占位。DSL 的 `mask` / `Clip` 节点是裁切指令，不是独立可见资源，禁止作为 `<img>` 使用；已导出的复合 PATH SVG 必须保留 `opacity` 与孔洞填充规则，不得改成实心色块。使用导出 SVG 后，该资源是对应 PATH 标记节点的**唯一绘制源**：禁止同节点继续叠加由同一 PATH fill 派生的 `background/background-color`；SVG 已内置 `opacity` 时外层 opacity 必须为 1，只有资源未内置时才由承载节点应用 DSL opacity。
   （4） **省 token**：Agent 读 **`Layer.md` + `dom.md` + `styles.md` + `mixed-text.json` + `text-whitespace.json` + `page-flow-plan.json` + `flex-nesting.md` + `flex-groups.json` + `dsl-assets.json`**；**禁止**将 **`dsl_full.json` / `dsl_layout_marked.json` / `dsl_visual.json` 全文**载入对话。

4. 生成依据：基于 **`Layer.md` + `dom.md` + `styles.md` + 设计稿 + 规则文档**，不得只凭经验值还原。

5. 若是新建页面，须在项目对应的路由文件中添加路由。

## 混排文本

写代码前读取 **`mixed-text.json`**：`hasMixedText === true` 时，按 `items[].layoutMark` 在 **`dom.md`** 找 `data-span-index` 拆多个 `<span>`；各 span 的 **fontSize / fontWeight / fontFamily / color** 以 `items[].spans` 与 `styles.md` 混排表为准（脚本已按 `text[]` + `textColor[]` 字符边界合并；混排节点不在「文本样式」主表）。`--complete 1` 会静态核对 changed Vue：真实 TEXT 测量盒必须带对应 `data-d2c-mark`，并按索引顺序保留全部 `data-span-index` 与原文；拍平成普通文本会在截图前硬阻断。

## 文本空白与水平锁宽（强制）

> 背景：HTML 会把连续空格折叠为单空格（`white-space: normal/nowrap/pre-line` 均折叠）。DSL 文案含连续/前后/全角空格时，折叠会让文本渲染宽度小于 DSL bbox，**右端逐字累积横向漂移**（叠图表现为同一文本拖尾/双轮廓）。

写 `.vue` 前读取 **`text-whitespace.json`**：`hasWhitespaceRisk === true` 时，对 `items[].layoutMark` 命中的每个文本节点：

1. **保留空白序列**：单行文本用 `white-space: pre`（**替代** `nowrap`；`pre` 同样不换行且保留空白），或将连续空格逐个转为 `&nbsp;`。**禁止**用 `nowrap`/默认 `normal` 直接承载该文案。`dom.md` 中对应节点带 `data-preserve-whitespace="true"` 且内联样式已给出 `white-space: pre`，照抄即可。
2. **文案逐字保真**：包括空白字符在内逐字符来自 DSL（`items[].textVisible` 已将空白可视化，便于核对个数），禁止增删、合并空格。

**水平锁宽**（与「垂直 bbox 锁高」对称）：`dom.md` 中 TEXT 有明确 `width` 时，文本**内容**渲染宽度须 ≈ 该值（允差 max(4 设计px, 2%)）。写前验算：字号 × 字符宽度估算 ≈ DSL width；明显小于时优先排查空白折叠、字体回退（目标字体未注册时回退 PingFang 视觉偏窄）、letter-spacing 丢失。物料阶段会用 `d2c-geometry-check.cjs` 实测校验（见 `d-d2c-materials.md`），写代码时即按本规则自检可避免返工。

## Flex 组（禁止拍平）

写代码前**先读** **`flex-nesting.md`**（高风险嵌套清单），再对照 **`flex-groups.json`** 与 **`dom.md`**：

1. `dom.md` 中带 **`data-flex-group="true"`** 的节点 → Vue 中必须有对应 `display:flex` 包裹层（类名自定，结构不可省）。
2. **`data-flex-children`** 所列 layoutMark → 只能是该包裹层的**直接子级**，禁止提升为祖父容器（如 `coverage-table`）的同级列。
3. `flex-nesting.md` 里 **高风险拍平** 条目 → 生成代码后逐条自检。
4. `gap` / `flexDirection` 以 `dom.md` 内联样式为准，禁止凭 `justify-content: space-between` 把子 flex 组内的列拆成多列同级。
5. 表格/多列区须保留嵌套包裹层（如 753→752+756→750/751），禁止在表根拍成多列同级。

## 子级排序（按视觉位置，不按图层顺序）（强制）

> 背景：MasterGo/Figma 的**图层顺序（z-order / 数组下标，即 `_layoutMark` 末位序号）与元素的视觉先后（左→右、上→下）没有必然关系**。设计师可以把「¥」画在图层面板靠后、却摆在「8000」左侧。若把子级按 `Layer.md` / `dom.md` 里出现的图层顺序直接铺进 `display:flex` 正常流，视觉顺序就会被颠倒——实测金额 hero 把 `¥8000` 渲染成 `8000¥`（`￥`@relativeX=40 被排到 `8000`@relativeX=76 之后），并连带 `8000` 左缘 -40 设计px 漂移。

写正常流（`display:flex` / grid）容器时，子级**排列顺序以几何位置为准，禁止照抄图层/数组顺序**：

1. **横向 flex 行**：按各子节点 `layoutStyle.relativeX`（同一父级下的左缘）**从小到大**排列；`relativeX` 相同再比 `relativeY`。前缀性元素（`¥ $ ￥ + -` 等货币/正负/单位符号、前置角标/圆点、返回箭头等）在图层里常排在正文之后但视觉在左——**必须放到左侧**。
2. **纵向 flex 列 / grid 行**：按 `relativeY` **从小到大**排列。
3. **判据永远看 `Layer.md` 每个子节点的 `relativeX` / `relativeY`**（本页金额组 `L0.3.0`：`8000`@76、`￥`@40 → ￥ 在左），不要用节点在 `Layer.md` / `dom.md` 中出现的先后。
4. 写完对每个横向 flex 行自检一次：DOM 子级从左到右的 `relativeX` 是否**单调不减**？否则说明顺序照抄了图层序，需按 relativeX 重排。
5. 例外：父级 `flex-direction: row-reverse` 或 DSL/设计语义明确为倒序时，按 DSL 指定方向；此时目标仍是「命中视觉顺序」，只是方向相反。

> 本节与「Flex 组（禁止拍平）」正交：拍平管的是**层级**，本节管的是**同级顺序**。二者都要满足。

## 页面级叠层与背景层（强制）

> 背景：MasterGo/Figma 顶层节点常同时包含「大面积背景层」和「真实内容层」。它们的 `relativeY` 可以相同或重叠，例如背景矩形 `L0.0` 覆盖 y=0~750，而 header / 卡片也从 y=0 或 y=100 多开始。若把背景矩形写成普通文档流块（`position: relative; height: 750px`），会把后续内容整体推下，导致整页出现稳定的系统性纵向漂移。

写页面级根结构前，必须读取 `page-flow-plan.json`：

1. **`implementation="absolute-background-layer"` 的节点**：必须使用 `position:absolute` / root 背景 / `::before` 背景层实现；禁止作为普通文档流块占据高度。
   - 若在模板中保留该节点，CSS 必须类似：
     ```css
     .bg-layer { position: absolute; left: 0; top: 0; width: 750px; height: 750px; z-index: 0; }
     .page-content { position: relative; z-index: 1; }
     ```
   - 若合并到 root 背景或伪元素，可不单独渲染该 mark，但不得产生额外流式高度。
2. **顶层节点存在重叠或负 gap**：禁止简单按 `Layer.md` 图层顺序用 `margin-top` 串行堆叠。必须按 `page-flow-plan.json.items[].y/height/bottom/overlapsWith` 恢复重叠关系，可使用绝对定位、负 margin、CSS grid overlap 或局部包装层。
3. **顶层流式 content 的纵向位置**：以 DSL bbox 差值为准验算。写普通流时，`上一个流式内容 bottom + gap = 下一个内容 top`；若 DSL gap 为负数或与背景层重叠，不能用正 margin 替代。
4. **自动自检**：阶段 1 `d2c-workflow-checkpoint.cjs --complete 1` 会读取 `page-flow-plan.json` 并扫描本轮 `.vue`。若发现背景层 mark 对应元素写成 `position: relative/static` 且有大高度，将直接阻断，必须改成不占流的叠层实现后再进入截图。

反例（禁止）：

```stylus
.bg-top
  position relative
  height 750px
```

## 顶栏 / 底栏与页面级定位（禁止误用 fixed）

- **禁止**仅凭 H5 习惯或 `dom.md` 里的 `left`/`top` 坐标，对页面**顶栏、底栏、全宽主按钮**使用 `position: fixed` / `position: sticky` / `position: absolute` 脱离文档流。
- **默认**：顶栏、底栏、底部 tab、全宽操作区按 **`Layer.md` 功能区域顺序 + `dom.md` 的 `data-flex-group` 嵌套** 放在**正常文档流**（`display:flex` 的块级容器）中实现；与中间内容区用 `gap` / `margin` 分隔，勿吸底盖住卡片内按钮。
- **仅当** `Layer.md` / `dom.md` / `dsl_layout_marked.json` 中**明确**标注该模块为 sticky/fixed 或设计语义即为视口吸底/吸顶时，才可使用 `fixed`/`sticky`，并在代码注释中标明依据的 layoutMark。
- **禁止**把 `filtered-dsl-to-html.cjs` 预览里的 `position:absolute` 坐标嵌套照抄进 Vue；`dom.md` 上的 `left`/`top` 只作间距与对齐参考，布局以 Flex/Grid 为准（见上一节与 `d-d2c-limited.md`）。

## 垂直 bbox 锁高（强制）

`dom.md` 中 TEXT/FRAME 有明确 `height` 时，CSS 盒高须等于该值；仅 `line-height` 或 `flex + gap` 不算锁高。字号、`color` 等仍按下一节「样式还原约束」与 `styles.md`，本节不改配色。

- **多行列表/表格**：优先 **Grid**（`grid-template-rows` + `row-gap`）；写前验算 `Σ子项 height + (n-1)×gap = 容器 height`。
- **TEXT**：同时写 `height` 与 `line-height`（与 dom 一致）；`align-items: flex-start`；父 `padding` 已占位处勿再叠子项 `margin`。
- **右对齐**：用 `margin-left: auto` 或块级右对齐，勿仅靠 `text-align: right` 代替宽度/偏移。

### phase2 联合闸 repairPlan 执行优先级（强制）

阶段 2 `geometry + vertical + render` 联合闸失败时，`d2c-workflow-checkpoint.cjs` 会在 `workflow-state.json.phase2Block.repairPlan` 写入下一轮修复策略。该策略不是建议文本，而是下一轮自动修复的优先级：

- `page-flow-skeleton-rebuild`：说明至少两个不同顶层功能区的**区块锚点本身**发生纵向漂移。先按 `page-flow-plan.json` / DSL 顶层 bbox 锁定主区块 `top/height` 和重叠/负 gap；深层单节点偏差再大也不能单独触发整页重建。
- `card-internal-rhythm-rebuild`：说明同一卡片/列表的多个兄弟节点出现同向大漂移或逐行递增漂移。按 DSL row `top/height/gap` 改用 grid 或卡片内 CSS absolute；禁止凭单个深层节点重建整卡，也禁止 `.card padding + .row padding` 叠加撑高。
- `local-anchor-position-fix`：说明存在纵向失败，但没有顶层跨区或同卡多兄弟的高置信结构模式。只修偏差最大的最小共同父容器/真实 TEXT 测量盒，按 DSL bbox 校正 `top/height/line-height`，保留已经过闸的区块。
- `fixed-cta-misuse`：说明底部操作区/CTA 疑似 fixed/sticky 与页面流对账冲突。除非 DSL 明确 sticky/fixed，否则改为文档流或 DSL bbox 定位；禁止标记为检查器伪影后跳过。
- `text-horizontal-fix`：仅在没有高置信纵向结构问题时，才优先修文本宽度、左右缘、字体、letter-spacing、右对齐和 mark 落点。
- `render-source-material-fix`：DSL 样式含 `NaN/undefined`、字体/资源清单不可用或资源未导出。先修源物料并覆盖同轮重采，禁止消耗有效代码修复轮次。
- `render-style-fix`：按 `render-diff.json` 修复 computed style、字体、文本裁切和资源引用；与坐标失败并存时作为同轮 secondary strategy 一并执行。

若 `repairPlan.forbiddenActions` 与当前拟改动冲突，必须先改结构性布局；下一轮摘要须说明执行了哪些 `repairPlan.actions`。

## 纵向自检锚点 data-d2c-mark（强制）

纵向几何自检 `d2c-vertical-check.cjs` 按容器实测 `top/height/bottom` 与 DSL 对账（见 `d-d2c-materials.md`）。`--page-flow` 除顶层业务根外，还会在每个根内部继续检查 flex group 与非流式文本行，防止只对齐外框、内部行坍缩。为让脚本稳定定位容器，生成 Vue 时**关键 wrapper 须保留测量锚点**：

- **组件根节点**：带 `data-d2c-component="<组件id>"`（如 `c2`）。
- **所有 `data-flex-group="true"` 的 FRAME/GROUP/INSTANCE 包裹层**，以及卡片/表格/底栏/列表等影响文档流高度的容器：带 `data-d2c-mark="<layoutMark>"`，值**镜像 `dom.md` 的 `data-layout-mark` / `_layoutMark`**（如 `L0.2.2.1`），不要自创。
- **扁平/绝对定位 KV 卡（强制）**：当一个卡片/信息区的行不是 flex/正常流、而是靠绝对定位（`top`/`left`）逐行摆放时（如「车辆信息」「关系人信息」这类标签-值 KV 列表），**每一行的关键文本节点都须带 `data-d2c-mark="<该行 _layoutMark>"`**。这类行的纵向位置不由文档流保证、容器锚点覆盖不到；`d2c-vertical-check.cjs` 现已对「父非 flex 的文本行」逐行建锚对账，但稳定匹配依赖行级 mark（缺则退化为唯一文本回退，重复文案会失配）。
- ⚠️ **绝对定位坐标只能写在 `<style>`/scoped CSS 里**（经 postcss-pxtorem 等缩放），**禁止用模板内联 `style="top:Npx"`**——内联 style 不被 pxtorem 处理，px 不缩放会导致逐行累积纵向漂移（实测 +19~43px）。
- **TEXT / 混排 TEXT 的测量 mark 必须落在真实文本测量盒上**：若 DSL 节点是 TEXT（含需要拆 span 的混排文本），`data-d2c-mark="<layoutMark>"` 所在元素必须同时承载该 TEXT 的 `font-size`、`line-height`、`width`、`height`、`white-space` 等测量样式；子 `<span>` 只负责分段颜色/字重。禁止把 mark 放在无字体样式的外层 wrapper 上、再把字体写到内部 span，否则 geometry/vertical 会读取 wrapper 默认字号/高度，产生假 `font-size-mismatch` 与级联 top/height 偏差。
- **所有可确定视觉节点都须标记**：带 fill/gradient/stroke/borderRadius/effect/opacity 的真实绘制节点，以及 `<img>/<svg>` 资源节点，须把 `data-d2c-mark` 放在实际承载 computed style 或资源 URL 的元素上，禁止只标外层空 wrapper。`d2c-render-check.cjs` 会按该 mark 对账。
- 这些是 **debug/还原期** 属性：建议仅在 `d2cPreview` 等预览态保留，或在生产构建期 strip，避免污染线上 DOM。
- 缺标记不会硬阻断——脚本会回退按子树文本定位并产 warning；但回退对纯容器不稳，**强烈建议补齐** `data-d2c-mark`。

```vue
<div class="ic" data-d2c-component="c2" data-d2c-mark="L0">
  <div class="ic__detail" data-d2c-mark="L0.2">
    <div class="ic__table-wrap" data-d2c-mark="L0.2.2.1">…</div>
  </div>
</div>
```

## 样式还原约束（强制）

代码中的样式必须来自 **`styles.md` / `dom.md`**，重点关注：

| DSL 字段 | CSS / 实现 |
|---|---|
| `fill` / fills | `background` / `background-color` / `background-image` |
| `strokeColor` + `strokeWidth` | `border` / `border-width` / `border-color` |
| `effect` | 按属性落位：投影 → `box-shadow`；`backdrop-filter` / `filter` / `text-shadow` 保持原属性名 |
| `layoutStyle` / absoluteBoundingBox | 宽高、间距、模块 bbox |
| `flexContainerInfo.itemSpacing` | `gap` |
| `fontSize` | `font-size`（px，750 稿宽原值） |
| `fontWeight` | `font-weight`（使用 DSL 数值，如 400/500/600/900） |
| `lineHeightPx` | `line-height`（px；DSL 为 auto/-1 时用节点高度兜底） |
| `letterSpacing` | `letter-spacing`（auto 时不写；`-1px` 等原样保留） |
| `fontFamily` | `font-family`（PingFang 系列统一为 `PingFang SC, PingFangSC, sans-serif`） |
| `decoration` | `text-decoration-line`（如 `line-through`） |
| `textMode: single-line` | `white-space: nowrap` |

- **禁止**凭设计图目测改写字号、字重、行高、字间距。
- **禁止**把所有文字统一成 `font-weight: normal` 或固定 `line-height: normal`。
- **专用字重字体禁止二次加粗**：项目若把某个字重变体注册为独立 family 且 `@font-face` 声明为 `font-weight: normal`，使用该 family 时保持 `font-weight: normal`；不要再叠加 900/500 触发 synthetic bold。若项目按同一 family 注册不同 weight，则使用统一 family + 对应数值 weight。
- DSL 的 gradient/background 在写代码前必须通过浏览器 CSS parser；禁止保留 `NaN` / `undefined`，也禁止只取多层 gradient 的第一层。无法可靠表达时使用对应导出资源并在 `render-diff.json` 中保留证据。
- 特殊字体须映射到**本项目已注册字体**（见下表），禁止只写 MasterGo 原始 family 名导致回退 PingFang 后视觉偏小：

| DSL `fontFamily` | 项目 `font-family` | 说明 |
|---|---|---|
| 设计稿专用字体 + weight 900 / Black | 项目 `@font-face` 注册的 Black 变体 family | 以项目实际注册名为准 |
| 设计稿专用字体 + weight 500 / Medium | 项目 `@font-face` 注册的 Medium 变体 family | 同上 |
| `DINAlternate-Bold` | `DINAlternate-Bold`（无 `@font-face` 时回退 PingFang SC + `font-weight: 700`） | 同上 |

## 单行文本（强制）

固定宽按钮/标签等容器内文案，设计为单行时：

- `textMode: single-line` → 必须 `white-space: nowrap`；容器用 `inline-flex` + 居中。
- MG 内描边 ≠ CSS `border-box` 下的 `border`：写死 `width` 前验算 `width - padding - border ≥ dom.md 里 TEXT 的 width`，不够则加宽、`min-width` 或 `box-shadow` 模拟描边。
- 禁止在固定 TEXT bbox 上用 `overflow:hidden` 掩盖字体/字距错误；render 闸会比较字形 Range、scrollWidth 与容器边界，发生可见裁切即 hard violation。

## 标签-值（KV）行内的值节点防折行（强制）

> 背景：`label（左）+ value（右）+ 可选 chevron` 的 KV 行是详情页最常见结构。value 文本若未锁单行，一旦渲染宽度略超可用空间（字体回退偏宽、`letter-spacing` 丢失、被 label 挤压 flex 空间），value 会**折行并向下溢出压住相邻行**——实测「利率优惠 −118.39元」的「元」掉到第二行压住优惠券行、「年综合利率…提前还款无手续费」折成两行。此类缺陷换任何 KV/详情页都会复发。

对**任何 flex 行内承载单行文案的 value / content 文本节点**（设计上单行、非段落）：

1. **锁单行**：`white-space: nowrap`。判定「设计为单行」不要只依赖 DSL 的 `textMode: single-line` 标记（很多单行节点并不带该标记）——DSL bbox 高度 ≈ 单行 `line-height`（`height ≤ 1.5×fontSize`）即按单行处理。
2. **禁止被挤压折行**：value 节点 `flex-shrink: 0`；由 label / 前缀节点承担收缩（`flex-shrink: 1`，必要时 `min-width: 0` + 省略号），使字体回退带来的宽度增量不会把 value 推到第二行。
3. **数字与单位/符号后缀同一行**：value 含 `元 % ℃ 起` 等后缀或 `¥ + -` 等前缀时，把「符号 + 数字 + 单位」包进同一个 `nowrap` 容器，禁止单位作为可换行的相邻 inline 文本单独掉行。
4. 物料阶段 `d2c-geometry-check.cjs` 若对某 value 节点报 `text-missing`（规范化整段未匹配），优先排查是否发生本节所述折行/溢出，而非直接判为检查器误报。

## 文案与数据约束

- 所有可见文案必须**逐字**来自 **DSL / 设计稿**，禁止 Mock / 编造 / 占位 / 改写。
- DSL 优先于设计稿。若 DSL 与设计稿可视内容冲突，优先按 DSL 实现，并在备注或 MR 说明中标注取舍。

## 图标与项目资源

图标/位图：**优先 `dsl-assets.json`（`localPath`）**，再查 **`assets-mapping.md`** 与仓库已有资源；组件：**优先 `components-mapping.md`** 与仓库已有组件、token。映射表是检索入口，不是「写了就能直接用」。

- 复用前确认资源**存在**，并对照设计稿 + `styles.md` / `dom.md` / `Layer.md` 看**有无差异**。
- `dsl-assets.json` 已为 PATH/位图导出资源时，复杂轮廓、复合子路径、镂空、mask/clip 必须使用该资源；禁止降级为普通 `background`。资源节点必须保留相同 `data-d2c-mark`；若采用等价 inline SVG，额外写 `data-d2c-asset-id="<assetId>"` 供 render 闸核验。检查器也会用 `contentSha256` / SVG path fingerprint 接受改名或 data URI 的内容等价资源，不接受同名但内容错误的替代物。
- **资源绘制单一来源**：当 `data-d2c-mark` 已通过 `<img>`、inline SVG 或 `background-image:url(...)` 命中导出资源，禁止再在同一元素设置非透明 `background/background-color` 作为“兜底”，否则会填死透明区、孔洞或圆角。先检查 SVG 文件是否已写入 `opacity`；已写入则承载元素 `opacity:1`，未写入才应用 DSL 节点 opacity。`render-diff.json` 会以 `asset-background-contamination` 和有效 `opacity-mismatch` 硬阻断重复绘制。
- **有差异时**：评估 props / 样式覆盖 / 局部扩展能否消差；能则深穿透修改，不能则从 DSL/MasterGo 导出或单独实现；**禁止**仅用纯色块顶替未落盘图标（须带文案与 `styles.md` 中的字号/颜色）。
