# 什么时候使用 Mock

只在**系统边界**使用 Mock：

- 外部 API，例如支付、邮件等
- 数据库，部分场景可用；优先使用测试数据库
- 时间或随机数
- 文件系统，部分场景可用

不要 Mock：

- 自己的类或模块
- 内部协作者
- 任何由当前项目控制的实现

## 设计便于 Mock 的边界

在系统边界设计容易 Mock 的接口。

**1. 使用依赖注入**

从外部传入依赖，不要在函数内部创建：

```typescript
// 容易 Mock
function processPayment(order, paymentClient) {
  return paymentClient.charge(order.total);
}

// 难以 Mock
function processPayment(order) {
  const client = new StripeClient(process.env.STRIPE_KEY);
  return client.charge(order.total);
}
```

**2. 优先使用 SDK 风格接口，不使用通用 fetcher**

为每个外部操作定义具体方法，不要让一个通用方法承载需要条件分支的逻辑：

```typescript
// 好：每个方法都可以独立 Mock
const api = {
  getUser: (id) => fetch(`/users/${id}`),
  getOrders: (userId) => fetch(`/users/${userId}/orders`),
  createOrder: (data) => fetch('/orders', { method: 'POST', body: data }),
};

// 坏：Mock 内部需要条件分支
const api = {
  fetch: (endpoint, options) => fetch(endpoint, options),
};
```

SDK 风格的好处：

- 每个 Mock 只返回一种明确的数据形态
- 测试准备不需要条件逻辑
- 容易看出测试使用了哪些外部操作
- 每个操作都能获得独立的类型约束
