# Case 可移植性与回归规则

本规则用于避免 Case 把某次历史 Solution 的实现形态误当成业务契约。静态命中只表示风险；只有冻结执行报告或重放失败才能确认缺陷。

## 生成前候选门禁

- 用 `git diff --numstat <base>..<solution>` 核验真实代码改动。生产代码改动为 0 行，或仅修改 `docs/specs`、归档资料、报告和测试数据时，跳过该候选。
- Base/Solution 必须可达且区间有明确业务行为变化；shallow、submodule、LFS 或缺失 commit 时标记 `NEEDS_REVIEW`。
- 需求证据必须能回答正常路径、空值/缺失、异常、兼容性、幂等/重复调用及不应改变的既有行为；没有证据时不得臆造答案。
- 不得把 Candidate 工作区无法枚举的“历史 N 项测试”、测试结果或报告哈希写成强制验收条件。只有测试身份、命令和输入在 Base 可见的冻结证据中都可复现时，才能要求复跑；Solution-only 测试只能作为隐藏溯源或确定性校准证据。
- Initial Request 可以记录 Base/Solution、F2P/P2P 或隐藏 JUnit 的校准口径，但必须同时明确：这些由评测平台在工作流后独立执行，不会暴露给 Candidate，缺少它们不是 Review finding、Verify blocker 或 Archive blocker。

## 澄清策略

`clarification-policy.json.facts` 中每项必须是具体、可执行且可追溯的业务事实，例如排序优先级、空值处理、异常策略、兼容范围或字段映射。事实来源应能定位到冻结需求或 commit 内容。

以下内容不是业务事实，不应写入 policy：

- 是否 Review、归档、commit、push 或继续下一阶段；
- 使用哪个工作流命令、是否采用 workflow 模式；
- “按需求实现”“保持兼容”“行为已冻结”等无边界答案的空壳表述。

这些流程选择由 Scenario V2 Run Execution Strategy 提供确定性默认规则。若真正缺少业务信息，应在生成阶段标记 `NEEDS_REVIEW`，而不是让正式 Run 临时询问用户。

## 隐藏测试设计

- 断言外部可观察结果、公共协议、持久化/调用效果或明确冻结的 API；不要反射未冻结的私有字段名。
- 除非需求明确限定，不得要求固定的构造器、字段注入、注解、mock 框架版本或唯一 accessor 名。
- 依赖注入测试按可赋值类型定位依赖，并兼容合法的构造器注入与字段注入。
- 若业务契约没有限定名称，可接受常见且语义等价的 accessor；不得只接受历史 Solution 恰好使用的名字。
- 手工 `javac` stub 必须覆盖 Base 与所有合法候选所需的框架表面。优先使用仓库自身构建系统和依赖，避免脆弱 stub。
- Case-owned fixture/patch 可以修复隐藏 harness 的依赖或 Mockito 兼容问题，但不得修改被评测业务仓库，也不得削弱业务断言。工作流开始前必需的构建隔离必须放在 `workspace_fixture_patch`，只触及声明的构建/工具配置；`hidden_test_patch` 仅保留工作流后注入的隐藏断言。

## F2P/P2P 隔离

F2P 与 P2P 应有独立入口、源码集合和构建结果。P2P 不得编译或加载只服务于 F2P 的测试源码、stub 或候选形态假设。否则一个合法的替代实现可能在 P2P 前就被无关编译错误拦截，造成 Base 或 Candidate 假低分。

## 三方重放矩阵

| 实现 | F2P | P2P | 目的 |
|---|---|---|---|
| pristine Base | 失败 | 通过 | 证明需求能区分且既有行为未被破坏 |
| historical Solution | 通过 | 通过 | 证明冻结历史解可满足 Case |
| valid alternate | 通过 | 通过 | 证明测试没有绑定唯一实现形态 |

替代实现应主动改变至少一种非业务结构，例如注入方式、私有字段名、构造器形态或等价 accessor。若没有足够证据定义“合法替代”，保留 `NEEDS_REVIEW`。

## 校准与正式闭环

- `PUBLISHED + CALIBRATED` 仅证明当前 `source_hash` 的确定性 Base/Solution 校准通过。
- 完整工作流可闭环必须由同一 `source_hash` 的正式 Run 证明；历史 Source 的 Run 不能替代当前证据。
- `NEEDS_USER_INPUT` 优先检查 clarification policy 是否缺少具体业务边界；流程性询问应由 Run 默认策略解决。
- `TURN_STATUS_MISSING`、`REVIEW_REQUIRED`、Verify 循环耗尽或 Candidate integrity 失败属于正式 Run 证据，不能靠直接改 DB 或伪造状态消除。
- Source 更新后重新 Preflight、Guard、发布和确定性校准，并重新生成 Candidate integrity bundle；保留旧 Run 和旧快照。

## 批量审计结论

- `CONFIRMED_DEFECT`：当前 Source 的冻结报告或三方重放已复现错误；可以修 Case。
- `STATIC_RISK`：命中私有实现耦合、共享构建或空壳事实等启发式，但尚无失败证据；需要审查，不得批量盲改。
- `PORTABLE`：静态门禁无阻断且三方重放通过；仍不等同于正式工作流闭环。
