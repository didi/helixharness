# 组件开发规约

## 组件命名

- 使用 PascalCase 命名组件：`UserProfileCard`、`OrderList`
- 组件文件名与组件名一致：`UserProfileCard.tsx`
- Props 类型命名：`ComponentNameProps`

## Props 设计原则

1. **Props 数量控制**：单个组件 Props 不超过 7 个，超过考虑拆分
2. **避免布尔陷阱**：避免 `isEditMode` 这种改变组件行为的布尔 Props
3. **回调命名**：`on` + 动词过去式：`onItemClick`、`onFormSubmit`

## 组件分层

| 层级 | 职责 | 允许依赖 |
|------|------|---------|
| 页面组件 (Pages) | 路由级页面、业务逻辑编排 | 业务组件、Hooks、Services |
| 业务组件 (Business) | 特定业务场景的可复用组件 | 通用组件、Hooks |
| 通用组件 (Common) | 项目内通用 UI 组件 | 无业务依赖 |
| 基础组件 (Base) | Design System 原子组件 | AntD / Element 等 UI 库 |

## 复用原则

- 重复出现 3 次以上才考虑抽离为通用组件
- 组件复用优先考虑组合而非继承
- 通用组件不允许包含任何业务逻辑
