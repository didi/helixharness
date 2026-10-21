# 类型系统规范

## TypeScript 严格模式

**必须开启**以下严格模式配置：
```json
{
  "strict": true,
  "noImplicitAny": true,
  "strictNullChecks": true,
  "strictFunctionTypes": true
}
```

## any 使用规范

✅ **允许使用 any 的场景**：
- 真正未知类型的数据（第三方 SDK 返回值）
- 临时兼容旧代码（必须加 TODO 注释）

❌ **禁止使用 any 的场景**：
- 新写代码不允许 any
- Props 定义不允许 any
- API 返回类型不允许 any

**替代方案**：
- 使用 `unknown` 替代 `any`，然后做类型收窄
- 使用泛型约束
- 使用类型守卫

## 类型定义规范

1. **接口命名**：PascalCase，业务相关首字母大写，不使用 I 前缀
2. **类型别名**：Type 后缀可选，根据场景
3. **枚举命名**：PascalCase，值大写
4. **泛型参数**：有意义的名字，不只是 T/K/V

## 类型导出

- 类型与实现放同一文件
- 公共类型抽离到 `types/` 目录
- 跨模块共享的类型统一导出
