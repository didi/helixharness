---
description: d-d2c 宿主布局策略与叠图坐标系约束
alwaysApply: true
---

# d-d2c-design-crop（宿主布局与叠图裁切）

## 先判宿主布局，后决定是否裁切

`filter-dsl.cjs` 必须先用原始 DSL 生成 `host-layout.json` / `layout-h5.json`：

- `immersive-overlay`：原生状态栏/导航控件覆盖在全屏 H5 上。控件不生成，但顶部坐标空间、背景和业务原始 Y 全部保留；`layoutShiftY=0`、`designCropTopRaster=0`。
- `stacked-host`：WebView/H5 画布从原生宿主底部开始。按 `hostChromeBottomY` 平移和裁切；禁止按 `firstBusinessY` 裁切，否则会删除宿主与首业务模块之间的真实业务留白。
- `none`：未检测到原生宿主壳层，不平移、不裁切。

判定来自 DSL 几何/层级，不依赖 executor 是否支持图片。可用 `filter-dsl.cjs --host-layout immersive|stacked|auto` 显式覆盖；默认 `auto`。

## 两套坐标，禁止混用

| 名称 | 含义 | 用于 |
|---|---|---|
| `hostChromeBottomY` | 原始 MasterGo 画板中原生宿主区底边 | 判定 stacked 布局边界，不等于首业务 Y |
| `firstBusinessY` / `businessTopGap` | 首业务 Y / 首业务 Y 与宿主底边的差 | 必须保留的业务顶部留白 |
| `layoutShiftY` / `designCropTopDsl` | 实际施加到 H5/设计侧的画板坐标平移量 | immersive 为 0；stacked 为 hostChromeBottomY |
| `designCropTopRaster` | **`image-source.png` 栅格**顶部裁切像素 | `d2c-design-opacity.cjs`、`d2c-diff-overlay.cjs` 的 `--design-crop-top` |
| `contentOffsetY` | `layoutShiftY` 的兼容别名（deprecated） | 旧消费者兼容，新增逻辑禁止赋予其它语义 |

导出原图 `image-source.png` 的尺寸**经常**与 DSL 画板不一致（例：479×1024 vs 750×1602）。
**⛔ 禁止**把 DSL 画板坐标的 `layoutShiftY` 直接传给 `--design-crop-top`；也禁止在 `immersive-overlay` 下传任何非零裁切值。

## 换算公式

```text
designCropTopRaster = round(layoutShiftY × imageSource.height / designFrame.height)
```

- `layoutShiftY`、`designFrame`：来自 `layout-h5.json`（filter-dsl 阶段写入）
- `imageSource`：当前 `image-source.png` 实际宽高

## 执行方式（推荐）

**默认：不传 `--design-crop-top`**，脚本读取同目录 `layout-h5.json` 自动解析栅格裁切。

```bash
node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-design-opacity.cjs \
  --design d-d2c/temp/<page-dir>/image-source.png \
  --actual "$actual_path" \
  --name <page-id> --out d-d2c/temp/<page-dir>/r<N>/

node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-diff-overlay.cjs \
  --design d-d2c/temp/<page-dir>/image-source.png \
  --actual "$actual_path" \
  --name <page-id> --out d-d2c/temp/<page-dir>/r<N>/
```

仅查询裁切值：

```bash
node $HELIXHARNESS_PLUGIN/skills/d-d2c/scripts/d2c-resolve-design-crop.cjs \
  --design d-d2c/temp/<page-dir>/image-source.png --print value
```

若手动传入值与 `designCropTopRaster` 不一致，或沉浸式页面传入非零值，脚本会 **stderr 报错**。

## 宿主控件与顶部空间不是一回事

- 两种策略都禁止在 H5 补画状态栏、返回键、原生标题、胶囊等宿主控件。
- `immersive-overlay` 保留原始画布；actual 顶部应是 H5 背景/留白，业务节点继续位于设计原始 Y。设计稿不裁顶，`ignoredDesignRegions` 只把原生控件区域排除在视觉混合/评分之外。
- `stacked-host` 才裁设计稿，并只裁到 `hostChromeBottomY`；业务自身的 `businessTopGap` 必须保留。
- 禁止平移截图凑对齐。坐标策略由 DSL 产物决定，geometry/vertical 继续检查真实 DOM。

## 评测说明

调用 `d2c-fidelity-evaluator` 时须写明：

- `hostLayoutPolicy` / `hostLayoutConfidence` / evidence
- `hostChromeBottomY` / `firstBusinessY` / `businessTopGap`
- `layoutShiftY` / `designCropTopDsl`（DSL）
- `designCropTopRaster`（实际叠图裁切 px）
- `ignoredDesignRegions`（仅沉浸式）

宿主控件不参与扣分，但沉浸式顶部空间和首业务 Y 是页面布局的一部分，必须参与 geometry/vertical 评分。

若叠图顶部位移且 `design-crop-top` 误用 DSL 值，先记 **物料对齐问题（material）**，勿归因为 H5 布局实现。
