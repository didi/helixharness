---
feature: {feature-name}
execution_scope: workspace
status: planning
testing_seam_confirmed: true
---

# {feature-name} — Workspace 跨仓概要设计

> 本文件是 Workspace 与各 Repo Single 之间唯一的全局 contract，也是
> Coordinator 的静态调度输入。它不记录 task 状态、Git baseline、Review 结果或运行日志。

## 背景、目标与范围

- 当前问题：{用户影响与需要改变的行为}
- 业务目标：{多个仓共同交付的最终能力}
- 交付范围：{纳入本次共同修改的系统与链路}
- 非目标：{明确不处理的内容}

## 总体架构

```mermaid
flowchart LR
    CALLER[调用方] -->|API / RPC| OWNER[契约 Owner]
    OWNER -->|MQ / Event| CONSUMER[下游 Consumer]
    CONSUMER --> STORE[(数据或外部依赖)]
```

- 主调用方向：{说明}
- Module 职责：{说明}
- 外部依赖：{说明；不涉及则写“不涉及”}

## 仓库职责与依赖

> `Repo` 必须是 `helixharness.workspace.yaml` 中的 key；`Mode` 只允许 `write|read`；
> `Depends On` 只表达静态实现依赖，不记录阶段或状态。

| Repo | Mode | Responsibility | Depends On | Contract Refs |
|---|---|---|---|---|
| {repo-a} | write | {本仓对最终能力负责什么} | — | C-01 |
| {repo-b} | write | {本仓对最终能力负责什么} | {repo-a} | C-01 |

## 跨仓接口与数据契约

### C-01：{Contract 名称}

- Owner：{repo/module}
- Consumer：{repo/module}
- 交互类型：{HTTP / RPC / MQ / DB / shared schema}
- 请求、响应或事件 Schema：{字段、类型、必填性、版本}
- 成功语义：{状态码或成功事件}
- 错误语义：{可重试、不可重试、错误码}
- Timeout / Retry：{超时与重试约定}
- 幂等策略：{幂等键与去重行为}
- 兼容策略：{向前/向后兼容与默认值}
- 降级行为：{依赖不可用时的行为}

## 端到端业务流程

```mermaid
sequenceDiagram
    participant U as 用户或上游
    participant A as {repo-a}
    participant B as {repo-b}
    U->>A: 发起请求
    A->>B: 按 C-01 交互
    B-->>A: 返回结果
    A-->>U: 返回最终行为
```

- 主成功链路：{说明}
- 关键失败链路：{说明}
- 重试或补偿链路：{说明；不涉及则写“不涉及”}

## 状态、数据归属与一致性

- 权威数据 Owner：{repo/module}
- 全局状态及合法流转：{说明}
- 一致性模型：{强一致 / 最终一致及原因}
- 补偿与幂等边界：{说明}

## 兼容、发布与回滚

| Owner / Producer | Consumer | 是否兼容 | 条件 |
|---|---|---|---|
| {new-owner} | {old-consumer} | {是/否} | {条件} |

- 发布顺序：{按 repo 列出}
- Feature Flag / 灰度：{说明}
- 部分发布失败：{说明}
- 回滚顺序与数据处理：{说明}

## 稳定性、安全与可观测性

- 业务关联 ID / Trace：{跨仓统一字段}
- 指标与告警：{关键指标、阈值、Owner}
- 限流、熔断和容量：{说明}
- 鉴权上下文：{如何传递和校验}
- 敏感数据：{日志、消息和存储约束}

## 跨仓验收标准

| AC | Scenario | Involved Repos | Expected Evidence |
|---|---|---|---|
| AC-01 | {端到端场景} | {repo-a}, {repo-b} | {各仓测试/Review 与集成事实} |

## Repo Handoff

### {repo-a}

- 必须实现：{本仓承担的 Contract 与 AC}
- 必须验证：{本仓需要证明的行为}
- 不得重新决定：{全局 contract 与不变量}
- 允许本仓决定：{内部 implementation、文件、类和测试 seam}

### {repo-b}

- 必须实现：{本仓承担的 Contract 与 AC}
- 必须验证：{本仓需要证明的行为}
- 不得重新决定：{全局 contract 与不变量}
- 允许本仓决定：{内部 implementation、文件、类和测试 seam}

## 已确认决策与非目标

- 已确认决策：{D-01 ...；所有 blocking 决策必须在 status: ready 前关闭}
- 非阻塞假设：{证据与影响范围；无则写“无”}
- 非目标：{明确不处理的内容}
