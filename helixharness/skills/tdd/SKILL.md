---
user-invocable: false
name: tdd
description: 为 HelixHarness feature 或 Bug Fix 的可观察行为改动执行测试驱动开发；在 Spec 已确认的公共测试 seam 上按红灯 → 绿灯的垂直切片实现，并生成可长期保留、对重构稳定的测试。
---

# HelixHarness 测试驱动开发

TDD 是红灯 → 绿灯循环。本 Skill 规定如何让这个循环产出值得长期保留的测试：什么是好测试、测试应写在哪个边界、应避免哪些反模式，以及循环必须遵守什么规则。每轮循环开始前和执行中都遵循全部规则，不要等结束后再补做。

## HelixHarness 集成

从当前 `spec.md`、`tasks.md` 或已复现的 Bug 场景中选择一个行为切片。测试 seam 必须已经在 Spec 的 Testing Decisions 中确认；seam 缺失或公共接口仍有争议时，返回 HelixHarness Propose 确认，不要在实现阶段自行创建测试入口。

复用 HelixHarness Apply 已通过 `reference/loading-protocol.md` 加载的项目入口、`docs/harness/` 约束、`docs/knowledge/` 知识、Spec 和当前 Task。只有任务边界变化时才按该协议重新评估索引叶子。测试名称和接口词汇使用这些已加载事实以及当前代码、配置和测试中的项目语言。

## 什么是好测试

通过公共接口验证行为，不验证实现细节。代码可以完全重写，但测试不应因此改变。好测试读起来像一条规格说明，例如“有效购物车可以完成结算”明确描述了系统能力；它不依赖内部结构，因此能够经受重构。

具体示例见 [tests.md](tests.md)，Mock 指引见 [mocking.md](mocking.md)。

## Seam：测试写在哪里

**Seam** 是被测试的公共边界：通过这个接口观察行为，而不进入实现内部。只在 seam 上测试，不测试内部结构。

**只测试预先确认的 seam。** 在 HelixHarness 中，Spec 的 Testing Decisions 就是这份约定。已确认的 seam 不重复询问，未确认的 seam 不写测试。接口形态本身仍有争议时返回 HelixHarness Propose，不要在 Apply 中自行选择或创建接口。

## 反模式

- **与实现耦合**：Mock 内部协作者、测试私有方法，或绕过公共接口从侧面验证，例如直接查询数据库。识别信号是：行为没有变化，仅内部重构就导致测试失败。
- **同义反复**：断言用与实现相同的方式重新计算期望值，例如 `expect(add(a, b)).toBe(a + b)`、按实现逻辑手工生成快照，或断言常量等于自身。这类测试天然不会与实现产生分歧。期望值必须来自独立事实，例如已知字面量、推导完成的样例或 Spec。
- **水平切片**：先写完全部测试，再集中实现。批量测试验证的是想象中的行为，容易只测试结构，并在理解实现前过早锁定测试形态。改用垂直切片：一个测试 → 一个实现 → 再进入下一轮；让每个测试都成为根据上一轮结果继续前进的 tracer bullet。

## 循环规则

- **先红后绿**：先写失败测试，再只写足够让它通过的代码。不要预判后续测试或添加推测性功能。
- **一次一个切片**：每轮只处理一个 seam、一个测试和一个最小实现。
- **重构不属于本循环**：重构放在 HelixHarness Apply 的 Code Review 阶段，不放进红灯 → 绿灯实现循环。任何 Review 修复完成后，都要重跑覆盖该行为的测试。

每个实际执行的 RED-GREEN 切片保留以下最小事实，供 Apply 整体 APPROVE 后一次性汇总到最终 `verify-report.md`：

- Task ID 与关联 AC ID；
- test file 与 failing test name；
- failing output summary；
- passing output summary。

摘要只保留能证明先红后绿的关键信息，不复制完整测试输出。不得在 TDD 循环中创建或更新 `verify-report.md`；Apply 在最终测试和双轴 Review 均通过后统一写入正式报告。文档、格式或无法表达为行为测试的配置变更只记录对应的确定性检查，不伪造 RED/GREEN 证据。
