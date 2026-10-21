# 构建规范

## 环境变量

- 环境变量统一放在 `.env.*` 文件中
- 开发环境：`.env.development`
- 生产环境：`.env.production`
- 测试环境：`.env.test`
- **禁止在代码中硬编码环境相关配置**

## 代码分割策略

- 路由级懒加载：每个页面单独一个 chunk
- 第三方库：vendor chunk（React/Vue、组件库等）
- 公共组件：common chunk（跨页面复用的组件）
- 动态加载：大文件/按需功能使用 import() 动态加载

## 产物规范

- 输出目录：`dist/`
- 静态资源：`dist/assets/`
- 文件名格式：`[name].[hash:8].js`
- source map：生产环境不输出

## 构建性能

- 按需引入组件库，禁止全量引入
- 图片资源按需压缩，超过 500KB 考虑 CDN
- Tree Shaking：确保 sideEffects 配置正确
