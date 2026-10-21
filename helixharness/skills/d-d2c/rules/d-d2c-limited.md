---
description: d-d2c 实现边界规范（宿主 UI、资源实现优先级与限制规则）
alwaysApply: true
---

# d-d2c-limited（d-d2c 实现边界）

## 尺寸与适配
- 750px 2倍设计稿：直接写 px，**禁止 ÷2**。
- rem 规则：100px = 1rem，由 postcss 自动转换。
- 禁止整体缩放字号、间距对齐视觉。
- 组件高度默认禁止内容自适应撑高，保证与设计稿一致；仅当文本内容较长、需要多行展示时，允许垂直方向自适应高度，其余尺寸仍严格遵循设计稿。

### `px` 只能写在 `<style>` 块里（关键 / 易踩坑）
- `postcss-pxtorem` **只处理 `.vue` 的 `<style>` 块**；**模板内联 `:style` / JS 拼出来的字符串 `px` 不会被转换**，会以字面值生效。
- 后果：宽 ≤ 375 的视口（Chrome 手机模式 / 真机）下，`<style>` 里的 `710px` 被缩成 `355px`，但内联里的 `622px` 仍是 `622px`，元素飘出容器、相互覆盖（典型表现：右上"筛选/搜索"盖住第 4 个 tab、绝对定位元素错位）。
- **禁止**：`:style="{ left: x + 'px', top: '24px', width: '72px' }"` 这类拼接。
- **正确做法**（任选其一）：
  1. 把定位/尺寸全部写到 scoped `<style>`，模板只切 class（首选）；
  2. 必须用计算值时，**转 rem**：`:style="{ left: (x / 100) + 'rem', top: '0.24rem' }"`；
  3. 或用 CSS 变量：`<style>` 里 `left: var(--x)`，模板设 `:style="{ '--x': (x/100) + 'rem' }"`。
- 自检：组件改完后 `grep -nE ":style.*'px'|\\+ 'px'" pages/`，命中即修。

### 单行文案勿换行

固定宽 + `border` 的按钮/标签：`nowrap` + 验算 `width - padding - border ≥ TEXT width`（见 `d-d2c-generate.md`「单行文本」）。

### 含连续空白的文案禁止 nowrap（关键 / 易踩坑）

- `white-space: nowrap` 会把文案里的**连续空格折叠成一个**，文本渲染宽度小于 DSL bbox，右端逐字累积横向漂移（叠图表现为文字拖尾/双轮廓）。
- 命中 `text-whitespace.json`（`hasWhitespaceRisk=true`）的节点：改用 `white-space: pre`（同样不换行且保留空白），或连续空格转 `&nbsp;`（见 `d-d2c-generate.md`「文本空白与水平锁宽」）。
- 自检：`grep -nE 'white-space:? ?nowrap|nowrap' pages/<页面>` 命中的元素，逐个核对其文案在 `text-whitespace.json` 中是否有空白风险，命中即改 `pre`。
