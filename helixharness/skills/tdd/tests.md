# 好测试与坏测试

## 好测试

**集成风格**：通过真实接口测试，不 Mock 内部组成部分。

```typescript
// 好：验证可观察行为
test("有效购物车可以完成结算", async () => {
  const cart = createCart();
  cart.add(product);
  const result = await checkout(cart, paymentMethod);
  expect(result.status).toBe("confirmed");
});
```

特征：

- 验证用户或调用方关心的行为
- 只使用公共 API
- 能够经受内部重构
- 描述做什么，而不是怎么做
- 每个测试只承载一个逻辑断言

## 坏测试

**实现细节测试**：与内部结构耦合。

```typescript
// 坏：验证实现细节
test("结算会调用 paymentService.process", async () => {
  const mockPayment = jest.mock(paymentService);
  await checkout(cart, payment);
  expect(mockPayment.process).toHaveBeenCalledWith(cart.total);
});
```

危险信号：

- Mock 内部协作者
- 测试私有方法
- 断言调用次数或顺序
- 行为没有变化，仅重构就导致测试失败
- 测试名称描述怎么做，而不是做什么
- 绕过公共接口从外部途径验证

```typescript
// 坏：绕过接口验证
test("createUser 会写入数据库", async () => {
  await createUser({ name: "Alice" });
  const row = await db.query("SELECT * FROM users WHERE name = ?", ["Alice"]);
  expect(row).toBeDefined();
});

// 好：通过接口验证
test("createUser 创建的用户可以被查询", async () => {
  const user = await createUser({ name: "Alice" });
  const retrieved = await getUser(user.id);
  expect(retrieved.name).toBe("Alice");
});
```

**同义反复测试**：期望值重复实现逻辑，因此测试天然不会失败。

```typescript
// 坏：按代码使用的方式重新计算期望值
test("calculateTotal 汇总商品金额", () => {
  const items = [{ price: 10 }, { price: 5 }];
  const expected = items.reduce((sum, i) => sum + i.price, 0);
  expect(calculateTotal(items)).toBe(expected);
});

// 好：期望值是独立的已知字面量
test("calculateTotal 汇总商品金额", () => {
  expect(calculateTotal([{ price: 10 }, { price: 5 }])).toBe(15);
});
```
