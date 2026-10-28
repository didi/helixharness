# 存量项目接入指南

已有项目的 d2c 流程与"从零生成新页面"不同。本文档覆盖存量项目接入的差异点和最佳实践。

## 快速开始（3 步）

1. **限定扫描范围**：初始化时加 `--scope client/views/<your-module>`（目录路径），只扫描你改动的目录
2. **使用组件模式**：`d-d2c --component <link>`，联合闸只检查你的组件
3. **指定挂载点**：Vue 2 项目 el 替换挂载点后加 `--app-selector "#rootApp"`

## 可以跳过的步骤

| 步骤 | 整页模式 | 组件模式 | 说明 |
|------|---------|---------|------|
| sort-of-d2c.md 全量扫描 | ✅ | ❌ 用 --scope | 只扫改动的目录 |
| fidelity evaluator | ✅ | ❌ 自动跳过 | 组件模式不跑整页评测 |
| data-d2c-mark | ✅ | ⚠️ 回退匹配只确认文本存在 | 存量代码没有 mark 属性 |

## 常见问题

### SPA 没挂载（截图是空白/登录页）
- 检查 `--app-selector`：Vue 2 的 el 会替换 #app，试试 `--app-selector "#rootApp"`
- 检查鉴权：需要登录的页面用 `--session-state` 注入 cookie
- 检查 dev server：确保 `npm run dev` 正常运行

### 组件已挂载但无业务数据（骨架屏/空状态/默认文案）
- **这是最常见但最容易被忽视的情况**：DOM 存在但内容是空的，被误认为"渲染正常"
- 判据：目标组件 DOM 选择器命中但 `v-if` 内层不渲染、列表 0 条、文案显示空字符串
- **解法：按 [SKILL.md#注入 Mock 数据](../SKILL.md#-agent-主动检测--自动-mock-截图后必须执行) 的自动检测流程**，分析组件数据依赖 → 生成 `--mock-script` → 重新截图
- 常见需要 mock 的典型场景：人物/账户信息卡片（依赖 API 返回的 `userInfo`）、业务列表（依赖后端分页数据）、状态驱动视图（依赖接口返回的状态枚举）

### geometry check 0 匹配
- 存量代码没有 `data-d2c-mark` 属性 → 使用 `--fallback-strategy conservative`（**仅确认文本存在**，命中记为 `low-confidence-match`，不做几何判定；默认 `off` 时缺失节点会直接报 `text-missing`）
- 页面被登录页重定向 → 使用 `--session-state` 注入登录态
- URL 不正确 → 确认 `--url` 指向正确的本地地址

### 联合闸卡住不放行
- 使用 `--component` 模式，联合闸只检查组件作用域
- 非代码问题（DSL 本身的问题）标记为 source-material，不占轮次
