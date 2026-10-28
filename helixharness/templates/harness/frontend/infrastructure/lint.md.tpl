# 代码质量规范

## ESLint 规则

### 必须开启
- `no-console`：生产环境禁止 console.log
- `no-unused-vars`：禁止未使用变量
- `exhaustive-deps`：React Hooks 依赖数组完整性
- `no-implicit-any`：禁止隐式 any

### 推荐配置
- `eslint:recommended`
- `@typescript-eslint/recommended`
- `react/recommended` / `vue/recommended`

## Prettier 格式化

- 统一使用项目配置的 `.prettierrc`
- 提交前必须格式化：`prettier --write`
- **不允许在代码中关闭 prettier 格式化**

## Git 提交规范

使用 Conventional Commits 规范：

```
<type>(<scope>): <subject>

<body>

<footer>
```

Type 类型：
- `feat`：新功能
- `fix`：修复 bug
- `docs`：文档更新
- `style`：格式调整
- `refactor`：重构
- `perf`：性能优化
- `test`：测试相关
- `chore`：构建/工具/依赖更新
