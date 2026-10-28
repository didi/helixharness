# 并发编程规范

> 技术栈：Java + ThreadPoolExecutor + CompletableFuture + Spring @Async
> 更新时间：<!-- TODO: 填写日期 -->

## 线程池规范

- 所有线程池**必须**通过 `ThreadPoolExecutor` 显式创建，**禁止**使用 `Executors` 工厂方法（`newCachedThreadPool` / `newFixedThreadPool` / `newScheduledThreadPool` 等）
- **禁止**使用无界队列（`new LinkedBlockingQueue<>()` 无参构造），**必须**指定队列容量上限
- **禁止**在方法内创建局部线程池（每次调用新建，无法复用且无法监控），**必须**定义为 Spring Bean 或静态常量

```java
// 正确
@Bean
public ThreadPoolExecutor orderExecutor() {
    return new ThreadPoolExecutor(
        4,                                   // corePoolSize
        8,                                   // maximumPoolSize
        60L, TimeUnit.SECONDS,               // keepAliveTime
        new LinkedBlockingQueue<>(200),       // 有界队列
        new ThreadPoolExecutor.CallerRunsPolicy()  // 拒绝策略
    );
}

// 禁止
ExecutorService pool = Executors.newCachedThreadPool();  // 无界线程池
ExecutorService pool = Executors.newFixedThreadPool(10);  // 无界队列
```

- 线程池**必须**配置拒绝策略（`RejectedExecutionHandler`），推荐 `CallerRunsPolicy`（避免静默丢弃任务）
- 线程池参数（core/max/queue）**必须**根据业务场景评估，并在注释中说明推导依据

## 异步任务超时

- 所有异步调用**必须**配置超时时间：

```java
// 正确：配置超时
future.get(5, TimeUnit.SECONDS);

// 正确：带超时和兜底
CompletableFuture.supplyAsync(() -> doWork())
    .orTimeout(10, TimeUnit.SECONDS)
    .exceptionally(ex -> fallback());

// 禁止：无超时等待
future.get();
```

- `@Async` 方法**必须**在对应的线程池配置中设置超时（如 `spring.task.execution.pool.*`）

## 并发控制

- 存在并发写操作的场景**必须**做并发控制，防止数据不一致
- 推荐方案（按场景选择）：

| 场景 | 方案 | 说明 |
|------|------|------|
| 数据库单行更新 | 乐观锁（`version` 字段 + `WHERE version = ?`）| 适用大部分业务 |
| 短期互斥操作 | Redis 分布式锁 | TTL ≤ 30 秒，防死锁 |
| 防重复写入 | 数据库唯一索引 | 最简单可靠 |
| 复杂状态流转 | 状态机 + 乐观锁 | 配合 CAS 语义 |

- **禁止**仅依赖应用层 `synchronized` 做分布式并发控制（多实例无效）
- **禁止**在并发场景下执行「查询 → 内存判断 → 写入」不做原子性保护

```java
// 正确：乐观锁
UPDATE sc_order SET status = 'PAID', version = version + 1
WHERE order_id = #{orderId} AND version = #{currentVersion}

// 检查 affected rows = 0 → 并发冲突，重试或抛异常

// 禁止
Order order = mapper.selectById(orderId);  // 查
if (order.getStatus() == PENDING) {        // 判断
    order.setStatus(PAID);                 // 写（并发不安全）
    mapper.updateById(order);
}
```

## 禁止事项

- **禁止**使用无界队列线程池
- **禁止**在方法内创建局部线程池
- **禁止**异步调用不设超时时间
- **禁止**并发写场景不做任何并发控制
- **禁止**在并发场景下做非原子的「查→判→写」操作
