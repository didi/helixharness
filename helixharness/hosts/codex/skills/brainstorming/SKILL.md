---
name: brainstorming
description: "用户独立调用的方案探索工具。澄清问题与约束，比较可行方案并给出推荐方向，不由研发命令自动触发。"
---

# Codex method projection: brainstorming

插件根固定为当前文件的 `../../../..`。任何 Python 调用前，必须先完整读取 `reference/runtime-preflight.md` 并执行当前操作系统的预检入口；原生 Windows 固定使用 `scripts/runtime_preflight.ps1` 的 `check` / `exec`，不得直接调用 `python3`、`python` 或 WindowsApps 别名。然后完整读取 `hosts/codex/host.json`、`reference/host-runtime-protocol.md`、`reference/tool-semantics.md` 和 manifest 绑定的 `resources.runtime`。canonical 正文首次需要 Agent/Task 时，再完整读取 `reference/subagent-runtime-protocol.md` 与 `resources.subagent_protocol`，按 Codex 适配器注入 leaf/afk 边界。然后完整读取并执行唯一 canonical 方法 Skill `skills/brainstorming/SKILL.md`；不得复制、改写或补充其业务语义，也不得扫描 plugin cache。canonical frontmatter 不是 Codex 运行时元数据；Codex 只消费其正文，宿主能力按 Host Context 映射。
