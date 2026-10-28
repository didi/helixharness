---
description: 复杂页拆解 SOP（复杂度判定 → 组件化串行实现 → 拼装），complexity-plan.json verdict=complex 时强制执行
alwaysApply: true
---

# d-d2c-complex-split（复杂页拆解 SOP）

复杂页整页一把梭会导致：上下文稀释（长 DSL 后半段细节丢失）、级联偏移（前面模块误差向下传染）、迭代粒度粗（整页评测整页改）。本规则把复杂页拆成组件级小闭环，逐个达标后再拼装。

## 1. 复杂度判定（脚本结论，禁止口头推翻）

`filter-dsl.cjs` 会自动产出 **`d-d2c/temp/<page-dir>/complexity-plan.json`**（也可单独运行 `scripts/d2c-complexity-plan.cjs --page-dir <dir>`）。

- ⛔️ **`verdict` 以该文件为准**：`simple` → 按 `d-d2c-generate.md` 现有路径一次性生成，本规则其余部分不适用；`complex` → 必须按本规则拆解，**禁止**以"页面看起来不复杂"等理由口头推翻。
- 判定指标与默认阈值（命中任一即 complex；漏判复杂掉还原度、误判简单只是多走流程，故取敏感方向）：

| 指标 | 含义 | 阈值 |
|---|---|---:|
| `flexGroupCount` | flex 组数量（结构嵌套信号） | ≥ 10 |
| `mixedTextCount` | 混排文本数量 | ≥ 4 |
| `uniqStructSigs` | 结构多样性（节点类型+子类型序列签名去重；重复卡片列表只算一种） | ≥ 30 |
| `nodeCount` | 节点总数（体量兜底护栏） | ≥ 250 |

**校准基准**（2026-06-12，file=188838996770122，用户标注；核心结论：节点总数/深度/文本数区分不开简单复杂——重复卡片列表对生成是简单的，嵌套表格/键值区/混排才是还原度杀手）：

| 样本 | 标注 | nodeCount | textNodeCount | flexGroupCount | mixedTextCount | uniqStructSigs |
|---|---|---:|---:|---:|---:|---:|
| 等待报价 `layer_id=3:30490` | 简单 | 68 | 10 | 4 | 0 | 19 |
| 首页工作台 `layer_id=24:324938` | 简单 | 154 | 59 | 4 | 1 | 22 |
| 核保中 `layer_id=24:334243` | 复杂 | 156 | 68 | **30** | **8** | **36** |

> 出现误判样本时：对该页跑 `fetch-mastergo-dsl.cjs` + `filter-dsl.cjs` 取指标，连同标注补进上表，重新选阈值并更新 `d2c-complexity-plan.cjs` 的 `DEFAULT_THRESHOLDS`；三个基准样本的 verdict 必须保持 simple / simple / complex。

## 2. 拆解方案 `component-plan.json`（Agent 落盘）

基于 `complexity-plan.json` 的 `suggestedComponents`（按功能区切分，单组件超 80 节点自动下钻）微调后，落盘 **`d-d2c/temp/<page-dir>/component-plan.json`**：

```json
{
  "components": [
    { "id": "c1", "name": "头部身份模块", "layoutMarks": ["L0.0"], "vueFile": "components/HeaderIdentity.vue" },
    { "id": "c2", "name": "报价卡片", "layoutMarks": ["L0.1.0"], "vueFile": "components/QuoteCard.vue", "adjustReason": "与建议不同时必填" }
  ]
}
```

微调约束：

- 组件边界必须是 DSL **子树整体**（一个或多个完整 layoutMark 子树），禁止把一个子树拆一半。
- ⛔️ 禁止拦腰拆开 `dom.md` 中 `data-flex-group="true"` 标记的 flex 组（其 `data-flex-children` 必须留在同一组件内）。
- 与 `suggestedComponents` 不一致的项必须带 `adjustReason`。
- 语义优先：一个卡片/表格/导航条是一个组件；过细（单文本一个组件）与过粗（>80 节点不下钻）都不可取。

## 3. 串行小闭环（每个组件依次执行，禁止并行/跳步）

> 组件级验证用 **DSL 量化自检**，不裁切设计图；`image-source.png` 只在整页终验（阶段 2/3）使用。

对 `component-plan.json` 中每个组件 `<id>`，按顺序：

1. **切片**：`node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-slice-dsl.cjs --page-dir d-d2c/temp/<page-dir> --component <id>` → 产出 `components/<id>/`（Layer.md / dom.md / styles.md / mixed-text.json / text-whitespace.json / flex-groups.json / flex-nesting.md / component-meta.json）。
2. **实现**：按 `d-d2c-generate.md` 全部规范写该组件 `.vue`。⛔️ **只读 `components/<id>/` 下的切片产物 + 公共映射表（components-mapping / assets-mapping / 字体表）；禁止读整页 `dom.md` / `styles.md` / `Layer.md` 正文**（整页 Layer.md 仅用于拼装时确认区域顺序）。
3. **挂载**：把组件加进页面（渐进拼装，按 Layer.md 功能区域顺序、正常文档流），确认开发服务器可访问。
4. **量化自检（横向 + 纵向 + render，三项都要跑）**：
   - 横向/尺寸：`node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-geometry-check.cjs --url <页面URL> --page-dir d-d2c/temp/<page-dir> --component <id>` → 写 `components/<id>/geometry-diff.json`。
   - 纵向 top/height/bottom：`node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-vertical-check.cjs --url <页面URL> --page-dir d-d2c/temp/<page-dir> --component <id>` → 写 `components/<id>/vertical-diff.json`。`violationCount` 只计 **primary**（可定位根因）；`derived` 为级联派生，先修 primary 往往一并消失。
   - render 契约：`node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-render-check.cjs --url <页面URL> --page-dir d-d2c/temp/<page-dir> --component <id>` → 写 `components/<id>/render-diff.json`，检查可确定的字体、颜色、圆角、渐变、文本裁切和静态资源使用。
   - violations 引用**整页 mark**，对照 `component-meta.json` 的 `rootMarkMap` 映射回切片定位代码。⚠️ 纵向锚点（flex group / 组件根）须在生成时带 `data-d2c-mark`（见 `d-d2c-generate.md`）；缺标记会走文本回退并产 warning。
5. **过闸**：横向 violations、纵向 primary 与 render hard violations 均清零后执行 `node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-workflow-checkpoint.cjs --component-complete <id> --page-dir <page-dir>`（脚本会硬校验 `geometry-diff.json`、`vertical-diff.json`、`render-diff.json` 均存在）；确属不修的（逐条核对后）用 `--ack "<理由>"` 显式记录。⛔️ 未过闸禁止开始下一个组件，最终整页联合闸仍会重新检查。

全部组件过闸后才允许 `--complete 1`（checkpoint 脚本会硬校验），随后阶段 2~6（整页物料 → 评测 → 用户决策 → 摘要）与 `d-d2c-workflow-self-check.md` 现行流程完全一致。

## 4. 组件代码组织与拼装

- 每组件独立 `.vue`，放页面同级 `components/` 目录，命名语义化（见 `component-plan.json` 的 `vueFile`）。
- 页面 `.vue` 只做拼装：按 `Layer.md` 功能区域顺序在正常文档流中组合组件，区域间距用 `gap`/`margin`（遵守 `d-d2c-generate.md`「顶栏/底栏与页面级定位」）。
- 新组件回填 `d-d2c/components-mapping.md` 与 `d-d2c/sort-of-d2c.md`（同 `d-d2c-generate.md` 要求）。
- **拼装流自检**：所有组件拼装完成后，跑 `node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-vertical-check.cjs --url <页面URL> --page-dir d-d2c/temp/<page-dir> --page-flow`（只校验各组件根 top/height）→ 写 `r<N>/vertical-diff.json`，防住「组件内部都对、但拼装 gap/margin 错」。primary violation 须修复或核对。
