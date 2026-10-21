# 前端知识库加载协议

## 触发条件

当项目类型为 `frontend`、`fullstack`、`fullstack-node`、`fullstack-separated` 或 `monorepo`
且当前扫描计划包含前端时，加载前端知识库。

## 加载顺序

1. 首先读取 `docs/knowledge/index.md`
2. 根据当前任务类型按需加载具体文档：
   - 组件开发相关任务 → 加载 `components.md` + `style.md`
   - 状态变更相关任务 → 加载 `state.md`
   - API 对接相关任务 → 加载 `api-endpoints.md`
   - 路由变更相关任务 → 加载 `routes.md`
   - 构建配置变更 → 加载 `build.md`
   - 项目结构调整 → 加载 `structure.md`

## 与后端知识的关系

- 前端知识独立于后端知识存储和加载
- 全栈项目会同时加载前后端知识
- 前后端知识通过 `docs/knowledge/index.md` 统一索引
