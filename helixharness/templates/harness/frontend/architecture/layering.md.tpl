# 前端分层架构约束

## 四层架构

```
┌─────────────────────────────────────┐
│         Pages / Views               │  路由层：页面组装、生命周期
├─────────────────────────────────────┤
│         Components                  │  组件层：UI 渲染、交互
├─────────────────────────────────────┤
│         Hooks / Composables         │  逻辑层：业务逻辑封装、状态复用
├─────────────────────────────────────┤
│         Services / APIs             │  服务层：API 调用、数据转换
└─────────────────────────────────────┘
```

## 依赖规则（只能下层依赖上层）

✅ Pages → Components → Hooks → Services
✅ Pages → Hooks → Services
✅ Components → Services（简单场景）
❌ Services 不能调用 Hooks
❌ Components 不能直接调用 API（通过 Services）
❌ 循环依赖

## 各层职责

### Pages / Views 层
- 路由级容器组件
- 组装子组件和业务逻辑
- 管理页面级生命周期
- **不包含具体 UI 渲染逻辑**

### Components 层
- 纯 UI 渲染
- Props 驱动，尽量无副作用
- 可复用的 UI 单元
- **不包含具体业务逻辑**

### Hooks / Composables 层
- 封装可复用的业务逻辑
- 状态管理和操作
- 订阅和生命周期管理
- **不直接操作 DOM**

### Services / APIs 层
- API 请求封装
- 数据格式转换
- 错误处理
- **不包含 UI 相关逻辑**
