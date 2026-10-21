---
description: d-d2c create-page/component 专用 isolated Vue preview 规则；update-page 禁止读取或调用
alwaysApply: false
---

# d-d2c preview（仅 create-page / component）

本规则只属于 `--create-page` 与 `--component`。`--update-page` 没有 isolated
preview 能力，真实路由不可达时按 `d-d2c-update-page.md` 进入 browser handoff。

## 1. 使用条件

- 优先使用项目真实路由或项目已有组件预览入口。
- create-page/component 的目标 Vue 因业务 bootstrap/router guard 无法稳定落地时，
  才允许创建 isolated Vue mount。
- 禁止静态 HTML、DOM replay、手写截图壳或只加载 CDN Vue 的模板预览。
- preview 只能绕过业务 bootstrap，目标 `.vue` 仍必须经项目自身 Vue/webpack
  构建链编译和运行。

## 2. 创建入口

调用时必须显式传当前 workflow mode；脚本只接受 `create-page` 或 `component`：

```bash
node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-isolated-vue-preview.cjs \
  --mode <create-page|component> \
  --page-dir d-d2c/temp/<page-dir> \
  --target <target-page-or-component.vue> \
  --slug <page-dir> \
  --base-url <dev-server-base-url> \
  --target-url <full-url>
```

脚本会登记 `preview-harness.json.workflowMode`，创建真实 webpack entry 与 HTML
入口，并输出 `previewUrl`。若 `requiresDevServerRestart=true`，由当前执行环境
受控重启原项目 dev server；不得另起替代服务。

### 2.1 Nuxt 等封装型构建链

项目用 Nuxt 把 webpack 配置封在框架内部、无法就地打补丁时，脚本自动改走
**独立构建链**（不改 `nuxt.config.js`，`buildConfigKind=nuxt`）：

- 生成 `client/d2c-preview/<slug>-webpack.config.cjs`，输出到
  `static/d2c-preview-runtime/<slug>/`，并给出 `buildCommand`；
- `optimization.splitChunks` / `runtimeChunk` **必须关闭**：入口代码集中在单文件，
  否则入口执行断言可能落在未被 HTML 加载的子 chunk 上，无法归因；
- 可选 loader（ts / stylus / url / file 等）缺失时跳过对应规则，不会让整份配置加载失败；
- 流程为 `requiresPreviewBuild=true`：先跑 `buildCommand`，
  再把 runtime 目录按 `--base-url` 起服务，最后用 `previewUrl` 截图。

```bash
node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-build-isolated-preview.cjs \
  --config client/d2c-preview/<slug>-webpack.config.cjs
```

### 2.2 入口执行断言

隔离入口在 `$mount` 成功后写 `window.__D2C_PREVIEW_ENTRY_EXECUTED__ = true`
（mount 抛错则写 `__D2C_PREVIEW_ENTRY_ERROR__`）。成功 preview 物料必须带该证据：
checkpoint 对 `previewKind=isolated-vue-mount` 报专用错误码
`D2C_PREVIEW_ENTRY_NOT_EXECUTED`，并归因到四种成因之一（mount 抛错 /
入口 chunk 未注入 / 静态快照 / 入口已加载但未执行）。
注意：模板只登记构建期事实 `data-d2c-template-preview-*`，运行时状态
（`data-d2c-preview-mode` / `-slug`）只由运行时写入，模板里的同名属性不能当作「预览已生效」。

若需要手工登记额外 preview-only 文件，初始化 harness 同样必须带 mode：

```bash
node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-preview-harness.cjs \
  --page-dir d-d2c/temp/<page-dir> \
  --init --mode <create-page|component> \
  --preview-kind <isolated-vue-mount|preview-bootstrap-bypass> \
  --preview-url <preview-url> --target-url <full-url>
```

## 3. 物料与硬门

截图使用项目 dev server 下的 preview URL，并明确记录来源：

```bash
node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-capture-page.cjs \
  --workflow-mode <create-page|component> \
  --url <preview-url> \
  --target-url <full-url> \
  --access-mode vue-route-preview \
  --meta-out d-d2c/temp/<page-dir>/r<N>/capture-meta.json \
  --design d-d2c/temp/<page-dir>/image-source.png \
  --label <page-id> --out d-d2c/temp/<page-dir>/r<N>/ --dpr 2
```

geometry、vertical、render 必须复用同一 preview URL、viewport、scope 与轮次。
`capture-meta.json` 必须证明目标 Vue DOM 已挂载；preview 自身重定向、runtime
error、空 DOM 或整页 text-missing 均为 material invalid，不得进入 fidelity。

## 4. 生命周期

联合闸仍需自动修复时保持 harness active 并复用同一入口。只有联合闸放行或
达到最大有效轮次后才清理：

```bash
node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-preview-harness.cjs \
  --page-dir d-d2c/temp/<page-dir> --cleanup
```

cleanup 后重新执行 phase-2 checkpoint。最终业务 diff 不得残留 preview entry、
webpack patch、bootstrap bypass、fixture 或 mock。
