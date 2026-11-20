---
name: helix-d2c-fidelity-evaluator
description: "Evaluate design-to-code fidelity for a generated page by comparing design inputs and implementation outputs. Use when the user provides or references a design screenshot, design DSL, source code, rendered page screenshot, and optionally a transparent design-over-render overlay image, and wants a restoration/fidelity percentage plus an actionable report that can drive the next D2C optimization pass."
disable-model-invocation: false
---

# Cursor method projection: d2c-fidelity-evaluator

入口类型：`method`。插件根固定为当前文件的 `../../../..`。完整读取 `hosts/cursor/skill-entry.md`，执行“公共绑定”后仅执行“方法入口”分支；不查询命令投影、不加载 commands 文件、不调用 Router。执行 runtime preflight 并以 `--platform cursor` 解析同一安装的 Host Context。完整读取 `reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 `resources.runtime`；需要子 agent 时再读取公共子代理协议及 `resources.subagent_protocol`。

完整读取并执行唯一 canonical 方法 `skills/d2c-fidelity-evaluator/SKILL.md` 的正文。canonical frontmatter 不属于 Cursor 元数据，不装载其中的 Claude hooks、工具声明或调用标记。正文相对资源从 `skills/d2c-fidelity-evaluator/` 解析；不得改以投影目录为基准。保留实际步骤、输出工件与全部门禁，不复制或改写业务语义，不扫描全局同名 Skill。
