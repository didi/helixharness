---
name: helix-systematic-debugging
description: "用户独立调用的调试工具。复现异常、缩小范围、验证假设并定位根因，按用户授权修复和验证，不由研发命令自动触发。"
disable-model-invocation: true
---

# Cursor method projection: systematic-debugging

入口类型：`method`。插件根固定为当前文件的 `../../../..`。完整读取 `hosts/cursor/skill-entry.md`，执行“公共绑定”后仅执行“方法入口”分支；不查询命令投影、不加载 commands 文件、不调用 Router。执行 runtime preflight 并以 `--platform cursor` 解析同一安装的 Host Context。完整读取 `reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 `resources.runtime`；需要子 agent 时再读取公共子代理协议及 `resources.subagent_protocol`。

完整读取并执行唯一 canonical 方法 `skills/systematic-debugging/SKILL.md` 的正文。canonical frontmatter 不属于 Cursor 元数据，不装载其中的 Claude hooks、工具声明或调用标记。正文相对资源从 `skills/systematic-debugging/` 解析；不得改以投影目录为基准。保留实际步骤、输出工件与全部门禁，不复制或改写业务语义，不扫描全局同名 Skill。
