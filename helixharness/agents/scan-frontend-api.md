---
description: "前端 API 调用扫描 Agent。仅扫描 API 封装层（请求拦截、端点清单），输出到 docs/knowledge/api-endpoints.md。"
---

## 任务：生成前端 API 调用清单

你是一名前端 API 专家。只扫描 **API 封装层** 和 **HTTP 端点引用**，不关心组件、状态、路由。


## 输出位置

- `docs/knowledge/api-endpoints.md`

## 扫描策略

### 0. 预检：定位实际 API 目录

**不要在枚举路径里猜**——先用内容特征反推实际位置：

```bash
# 方法 1：从 HTTP 客户端调用反推封装层位置
grep -rh "import.*from.*['\"].*\(request\|axios\|http\|fetch\)" \
  {src,client,frontend,web,apps} 2>/dev/null | sort | uniq -c | sort -rn | head -10

# 方法 2：直接找可能的请求封装目录
API_DIRS=$(find {src,client,frontend,web,apps} -type d \
  \( -name "api" -o -name "services" -o -name "requests" -o -name "http" \) 2>/dev/null | head -5)

# 方法 3：找请求实例文件（axios.create / fetch wrapper）
grep -rl "axios.create\|new.*Request\|createHttpClient" \
  {src,client,frontend,web} --include="*.ts" --include="*.js" 2>/dev/null | head -5
```

**有结果** → 用实际路径；**无结果** → fallback 到下方枚举路径。

### 1. 定位 API 封装层

查找请求封装文件：
- `src/api/` / `src/services/` / `src/requests/`
- `src/utils/request.{ts,js}` / `src/common/request.{ts,js}`
- `client/common/request.{ts,js}`
- 任何 export 了 `request` / `axios` / `fetch` wrapper 的文件

### 2. 解析封装基类

- 基础实例：axios / fetch / ky / superagent
- **请求拦截器**：token 注入、签名、通用 header
- **响应拦截器**：业务码处理、错误统一提示、token 失效跳转登录
- **错误处理策略**：统一错误码映射、全局 toast
- **超时配置**
- **取消请求**：AbortController / CancelToken

### 3. 提取 API 端点清单

遍历所有源文件（`.vue` / `.ts` / `.tsx` / `.js`）中的 HTTP 调用：

- 直接 axios 调用：`axios.get/post/put/delete`
- request 实例调用：`request.get('/user')`
- 函数封装：`apiGetUser()` `fetchUser()`

对每个调用提取：
- **HTTP 方法**：GET / POST / PUT / DELETE
- **路径**：`/api/user/login` / `/v2/order/list`
- **调用方**：所在文件 + 所在函数
- **参数类型**：从 TypeScript 类型签名提取
- **响应类型**：从泛型参数提取

### 4. 按模块分组

按 API 端点前缀分组：
- `/api/user/*` → 用户模块
- `/api/order/*` → 订单模块

## 输出格式

```markdown
# 前端 API 调用清单

> 生成时间：YYYY-MM-DD
> 封装基础：{axios / fetch / ...}
> 端点总数：{N}

## 封装层

| 文件 | 职责 |
|------|------|
| src/utils/request.ts | axios 实例 + 拦截器 |
| src/api/user.ts | 用户模块 API |
| src/api/order.ts | 订单模块 API |

## 拦截器

### 请求拦截器
- 注入 `Authorization: Bearer ${token}`
- 注入 `X-Request-Id: uuid()`

### 响应拦截器
- 业务码 0 成功，其他抛出业务异常
- 401 跳转登录

## 端点清单

### 用户模块 `/api/user/*`

| 方法 | 路径 | 调用方 | 参数 | 响应 |
|------|------|--------|------|------|
| POST | /api/user/login | Login.vue#handleLogin | `{username, password}` | `{token, userInfo}` |
| GET | /api/user/info | useUserStore#fetchUser | - | `IUserInfo` |
| ... | ... | ... | ... | ... |

### 订单模块 `/api/order/*`
...
```

## 注意事项

- 无法确定的字段标注 `<!-- TODO: 待确认 -->`
- 非前端项目直接退出，不生成文件
- 端点清单超过 80 条时折叠为概要 + 详细表格（按模块）
- **重点关注高频调用端点**（被多处引用的）
- 不要展开 mock 数据处理（除非 mock 是核心模式）

## 交付与写域

完整遵循 `reference/scan-agent-protocol.md`，只写当前 job 授权的产物；共享 knowledge 根索引由 bootstrap 串行维护。
