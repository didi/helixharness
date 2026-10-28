---
description: d-d2c 组件模式流程覆盖层。用户使用 d-d2c --component，或任务目标是组件、弹层、卡片等组件级子树时读取；只定义流程差异，不改变通用还原逻辑。
alwaysApply: false
---

# d-d2c-component（组件模式）

> 💡 **存量项目？** 先阅读 [brownfield 接入指南](d-d2c-brownfield.md)。组件模式下建议配合 `--scope` + `--app-selector` 使用。

本文只描述组件模式相对默认整页流程的差异。除本文列出的覆盖项外，仍严格遵循：

- `d-d2c-workflow-self-check.md`
- `d-d2c-generate.md`
- `d-d2c-materials.md`
- `d-d2c-preview.md`
- `d-d2c-design-crop.md`
- `d-d2c-limited.md`
- `d-d2c-init.md`

不得因为组件模式修改 DSL/filter、布局/样式生成、geometry/vertical/render 判定算法、phase2 联合闸或 fidelity 评分口径。

## 1. 适用范围

适用于以下输入：

- `d-d2c --component <组件层 MasterGo 设计链接> + [Image #1]`
- 用户明确要求开发组件、弹层、卡片、独立模块；
- MasterGo 链接指向页面中的一个组件级子树，而不是完整业务页面。

若用户其实是在已有页面中新增/改动一块内容，并要求验证该块在页面中的最终位置，应优先走 `rules/d-d2c-update-page.md`；组件模式只验证组件自身。

## 2. 产物目录

组件模式的 `--page-dir` 固定使用：

```text
component/<name>
```

所有 DSL、设计图、自检物料和 workflow 状态落在：

```text
d-d2c/temp/component/<name>/
d-d2c/temp/component/<name>/r<N>/
```

`<name>` 使用组件名或稳定短横线 slug。该目录与整页 `d-d2c/temp/<page-dir>/` 隔离。

## 3. 路由与渲染靶子

- 不新增对外业务路由。
- 组件源码可落在目标仓库现有组件目录或页面局部组件目录，具体位置按 `sort-of-d2c.md` / `components-mapping.md` 和项目约定选择。
- 自检截图必须渲染目标组件本身。优先使用目标项目已有的组件预览入口；若没有可用入口，按 `d-d2c-preview.md` 使用 component mode 的 isolated Vue mount 临时渲染目标组件。
- preview harness 必须登记 `workflowMode=component`，产物生成后 cleanup，禁止把临时预览入口、mock、bootstrap bypass 残留在最终业务 diff。
- 禁止用静态 HTML、DOM replay、手写截图壳替代真实 Vue 构建链渲染。

`d2c-capture-page.cjs` 支持 `--capture-selector` 生成 region 物料；组件模式可用目标组件根选择器截图，并依赖 `capture-meta.json.regionBBoxCss` 做局部坐标换算。若预览入口本身只渲染目标组件，也可以继续使用 full-page 物料。

## 4. 设计图与 DSL

- 设计图 `image-source.png` 应对应组件自身导出的图，而不是包含宿主页其它模块的整页图。
- MasterGo 链接应指向组件层或组件子树；若只能拿到整页 DSL/整页截图，先明确组件范围后再准备组件级输入。
- 阶段 1 仍必须产出 `dsl_full.json`、`Layer.md`、`dom.md`、`styles.md`、`text-whitespace.json`、`dsl_layout_marked.json`、`dsl_visual.json` 等标准物料。
- `complexity-plan.json` 仅作为备查；`component/<name>` 是独立组件流程，不触发复杂整页拆解，不要求 `component-plan.json`。

## 5. 评测口径

- `Implementation source` 指向组件 `.vue` 源码。
- `Design screenshot` 使用组件级 `image-source.png`。
- `Rendered screenshot` / `Overlay screenshot` / `Geometry diff` / `Vertical diff` 使用 `d-d2c/temp/component/<name>/r<N>/` 下当前轮物料。
- phase2 `geometry + vertical + render` 联合前置闸仍生效：未过闸且未达固定 8 个有效轮次上限时，必须按 `D2C_AUTO_FIX_REQUIRED` 自动修复并重出下一轮物料，禁止提前进入 evaluator 或询问用户是否接受。

## 6. 接入已有页面

若组件最终需要插入某个已有页面：

- 组件自身可先按本文完成组件级闭环；
- 插入宿主页后，若用户需要验证页面中的最终位置和对其它模块的影响，必须再按 `d-d2c-update-page.md` 走已有页整页验收；
- 不得用组件级 isolated preview 的截图替代已有页最终整页验收。
