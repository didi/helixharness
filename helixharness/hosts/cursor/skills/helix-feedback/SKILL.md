---
name: helix-feedback
description: "HelixHarness 使用问题反馈通道。当用户在 /helix:* 命令与工作流中遇到问题并表现出困扰时主动使用：命令反复失败或报错无法推进、生成的文档/字段/结构与用户预期不符、流程在某一步卡住不动、用户对 HelixHarness 表达不满困惑或抱怨、放弃工作流改用手工方式，或明确要求提交反馈。此时向用户建议提交反馈，发送前必须经用户确认。不触发：用户业务代码的问题、与 HelixHarness 无关的报错、一般使用咨询、模型已自行修复的故障。"
disable-model-invocation: false
---

# Cursor method projection: feedback

入口类型：`method`。插件根固定为当前文件的 `../../../..`。完整读取 `hosts/cursor/skill-entry.md`，执行“公共绑定”后仅执行“方法入口”分支；不查询命令投影、不加载 commands 文件、不调用 Router。执行 runtime preflight 并以 `--platform cursor` 解析同一安装的 Host Context。完整读取 `reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 `resources.runtime`；需要子 agent 时再读取公共子代理协议及 `resources.subagent_protocol`。

完整读取并执行唯一 canonical 方法 `skills/feedback/SKILL.md` 的正文。canonical frontmatter 不属于 Cursor 元数据，不装载其中的 Claude hooks、工具声明或调用标记。正文相对资源从 `skills/feedback/` 解析；不得改以投影目录为基准。保留实际步骤、输出工件与全部门禁，不复制或改写业务语义，不扫描全局同名 Skill。
