---
name: ut
description: "单元测试生成与维护命令。基于知识库和活跃需求，逐层扫描代码并生成/维护单测，运行验证并检查逻辑缺陷，确保覆盖率达标。"
triggers:
  - /helix:ut
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/codex/skill-entry.md`，执行 runtime preflight，然后执行一次：

```bash
python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --command ut --resource subagent_protocol --resource telemetry_protocol --resource ut
```

按协议完整消费 packet，以唯一 `commands/ut.md` 为命令契约。
