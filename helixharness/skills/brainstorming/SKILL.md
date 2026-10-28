---
name: brainstorming
description: 用户独立调用的方案探索工具。澄清问题与约束，比较可行方案并给出推荐方向，不由研发命令自动触发。
disable-model-invocation: true
---

# 方案头脑风暴

用户明确要求使用本 Skill 时，独立开展方案探索。无需先执行 Init、创建需求、开启统计或启动平台；不创建 feature、Spec/Tasks 或 eval-log，不启动研发命令。

适合触发的场景：

- requirements are still fuzzy
- more than one design is plausible
- the user asks for options, tradeoffs, or recommendations
- a risky change needs alternatives before execution

如果执行已经明确开始，就不要再用它，除非你确实需要重新打开方案空间。

## 目标

先扩展选项空间，再收敛到一个站得住脚的推荐路径。

## 工作流

1. 定义这次要做的决策。
   写清楚：
   - the problem to solve
   - hard constraints
   - what would make an option unacceptable

2. 生成 2-4 个真实可实施的选项。
   每个选项都必须能落地，不要只是口号。

3. 从实务维度比较方案。
   优先比较：
   - complexity
   - delivery speed
   - risk of regression
   - migration cost
   - operability
   - fit with existing architecture

4. 暴露未知项。
   明确指出哪些事情必须在执行前确认。

5. 给出一个推荐方案。
   解释为什么它是“当前最适合”的，而不是抽象上最优。

## 独立使用边界

- 由用户独立调用，不作为研发命令的内部步骤
- 输出要简洁，并且面向决策
- 如果代码库上下文重要，先读相关文件，再提方案
- 方向确认后交付讨论摘要，由用户决定是否将其作为后续需求的输入；本 Skill 不自动进入计划或实现流程

## 输出格式

- Problem:
- Constraints:
- Option A:
- Option B:
- Option C: (optional)
- Recommendation:
- Open questions:

## 约束

- 如果明显只有一条路，不要硬凑很多选项
- 不要默认推荐重写
- 不要用模糊语言掩盖关键 tradeoff
- 一旦方向清楚，就停止 brainstorm，进入下一步
