---
description: "前端路由扫描 Agent。仅扫描路由配置（vue-router / react-router / next.js pages），输出到 docs/knowledge/routes.md。"
---

## 任务：生成前端路由配置清单

你是一名前端路由专家。只扫描**路由配置文件**，不关心组件、状态、API。

## 输出位置

- `docs/knowledge/routes.md`

## 扫描策略

### 0. 预检：定位路由文件

**不要在枚举路径里猜**——用代码特征反推实际位置：

```bash
# 方法 1：搜 VueRouter / createRouter / createBrowserRouter 的实例化位置
grep -rl "VueRouter\|createRouter\|createBrowserRouter\|new Router" \
  {src,client,frontend,web,app} --include="*.js" --include="*.ts" --include="*.tsx" 2>/dev/null | head -5

# 方法 2：找 routes 配置文件（常为数组导出）
grep -rl "export default \[" \
  {src,client,frontend,web,app}/router {src,client,frontend,web,app}/routes 2>/dev/null | head -5

# 方法 3：Next.js / Nuxt 文件系统路由
[ -d pages ] && echo "Next.js/Nuxt pages/: $(find pages -name '*.tsx' -o -name '*.vue' | wc -l) 个页面"
[ -d app ] && echo "Next.js app/: $(find app -name 'page.tsx' | wc -l) 个页面"
```

**有结果** → 直接读取定位到的文件；**无结果** → fallback 到下方枚举路径。

### 1. 定位路由配置

根据项目框架查找路由文件：

| 框架 | 路由文件位置 |
|------|------------|
| Vue 2/3 (vue-router) | `src/router/index.{js,ts}` 或 `src/router/*.ts` |
| React (react-router) | `src/router/index.{js,ts}` 或 `src/routes/index.{js,ts}` |
| Next.js | `pages/*.tsx` + `app/*.tsx`（基于文件系统的路由） |
| Nuxt | `pages/*.vue`（基于文件系统的路由） |
| Angular | `src/app/app-routing.module.ts` |

### 2. 解析路由表

对每条路由提取：

- **路径**：`/user/:id` / `/dashboard` / `/404`
- **对应页面组件**：`import('@/views/User.vue')` 或 `component: UserPage`
- **名称**：`name: 'UserList'`
- **嵌套路由**：`children: [...]`
- **路由参数**：`:id` / `:slug`
- **权限配置**：`meta: { requireAuth: true, roles: ['admin'] }`
- **懒加载**：`component: () => import(...)`
- **重定向**：`redirect: '/home'`

### 3. 提取路由元信息

- 总路由数
- 嵌套层级最深
- 是否有动态路由 / 通配符路由（`*` / `/*` / `:pathMatch(.*)*`）
- 全局路由守卫（`beforeEach` / `beforeResolve`）
- 路由懒加载覆盖率（%）

## 输出格式

```markdown
# 前端路由配置

> 生成时间：YYYY-MM-DD
> 路由文件：{文件路径}
> 路由总数：{N}

## 路由表

| 路径 | 名称 | 组件 | 权限 | 懒加载 |
|------|------|------|------|--------|
| / | Home | Home.vue | - | ✅ |
| /user/:id | UserDetail | User.vue | requireAuth | ✅ |
| ... | ... | ... | ... | ... |

## 嵌套路由

```
/admin
  ├── /admin/users
  └── /admin/settings
```

## 路由守卫

- `beforeEach`：{用途说明}
- `beforeResolve`：{用途说明}

## 权限配置模式

- 鉴权方式：{基于角色 / 基于 token / 自定义}
- 公开路由：`/login`, `/register`
- 私有路由：其他所有
```

## 注意事项

- 无法确定的字段标注 `<!-- TODO: 待确认 -->`
- 非前端项目直接退出，不生成文件
- 路由表超过 50 条时分页或折叠（保留前 30 + 概要）
- 不要展开解析页面组件内容（那是 components agent 的事）

## 交付与写域

完整遵循 `reference/scan-agent-protocol.md`，只写当前 job 授权的产物；共享 knowledge 根索引由 bootstrap 串行维护。
