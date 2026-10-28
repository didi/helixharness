# 前端开发约束总览

> 本文档定义本项目的前端开发规范与约束。

## 约束分类

### 不变量约束 (Invariants)

| 文件/目录 | 定位 | 加载时机 |
|------|------|------|
| [组件规约](invariants/component.md) | 组件开发规范、Props 命名、复用原则 | 涉及组件时 |
| [状态管理规约](invariants/state.md) | 全局状态与局部状态边界、Store 设计原则 | 涉及状态管理时 |
| [API 调用规约](invariants/api.md) | 错误处理、鉴权、缓存、重试策略 | 涉及 API 调用时 |

### 架构约束 (Architecture)

| 文件/目录 | 定位 | 加载时机 |
|------|------|------|
| [分层架构约束](architecture/layering.md) | 前端四层架构规范、分层原则、跨层依赖规则 | 涉及目录或模块边界时 |

### 基础设施规范 (Infrastructure)

| 文件/目录 | 定位 | 加载时机 |
|------|------|------|
| [构建规范](infrastructure/build.md) | 产物路径、代码分割、环境变量配置 | 涉及构建与发布配置时 |
| [类型系统规范](infrastructure/types.md) | TypeScript 使用规范、any 限制、泛型约定 | 涉及类型定义时 |
| [代码质量规范](infrastructure/lint.md) | ESLint、Prettier、提交规范 | 涉及代码修改时 |

## 核心原则

1. **类型安全优先**：优先使用 TypeScript 严格模式，避免 `any` 类型
2. **组件纯粹性**：组件只负责渲染，业务逻辑抽离到 Hooks/Utils
3. **状态最小化**：能不放在全局的状态绝不放在全局
4. **API 统一封装**：所有 API 调用统一封装，不分散在组件中
