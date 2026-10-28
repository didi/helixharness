---
name: systematic-debugging
description: 用户独立调用的调试工具。复现异常、缩小范围、验证假设并定位根因，按用户授权修复和验证，不由研发命令自动触发。
disable-model-invocation: true
---

# 系统化调试

用户明确要求使用本 Skill，且真实不一致已经出现时，独立开展调试：

- expected behavior != actual behavior
- test fails
- compile/runtime error appears
- observed behavior regresses or contradicts an agreed requirement

不要把这个 skill 用于模糊的设计讨论。那属于 planning，不属于 debugging。

## 目标

基于证据找到根因，再用回归检查确认修复真实有效。

## 工作流

1. 明确不一致是什么。
   写清楚：
   - expected behavior
   - actual behavior
   - where it was observed

2. 稳定复现。
   优先用一个确定性的命令、请求或测试来复现。
   如果复现不稳定，要明确记录。

3. 缩小范围。
   检查：
   - input data
   - call path
   - changed files
   - environment/config assumptions

4. 提出假设。
   假设必须具体且可证伪。
   好的例子：
   - "null value enters mapper X and breaks branch Y"
   不好的例子：
   - "something is wrong with the service"

5. 执行有区分度的检查。
   选择能排除某个假设的下一条命令或检查。

6. 确认根因。
   区分清楚：
   - symptom
   - trigger
   - root cause

7. 做最小且合理的修复。
   仅在用户已授权修复时执行；只要求诊断时，交付根因、证据和修复建议，不修改代码。
   除非根因要求，否则不要顺手做大范围重构。

8. 重新执行复现和回归检查。
   不重新验证，修复就不算完成。

## 独立使用边界

- 由用户独立调用，不作为研发命令或代码评审的内部步骤
- 无需先执行 Init、创建需求、开启统计或启动平台
- 不创建 feature、Spec/Tasks 或 eval-log，不启动或推进研发命令
- 交付诊断与验证结果，由用户决定后续处理；未执行修复或验证时如实标明

## 输出格式

用五行摘要输出调试结果：

- Expected:
- Actual:
- Reproduction:
- Root cause:
- Verified fix:

## 约束

- 在重新跑过失败场景前，不能说“已经修复”
- 在没找到根因前，不要做大范围改写
- 没有证据时，不要把环境问题和代码问题混为一谈
- 如果无法复现，要明确写出来
