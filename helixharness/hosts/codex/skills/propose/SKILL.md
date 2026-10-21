---
name: propose
description: "收敛需求并生成 Spec/Tasks；--loop 自动串联至 Archive。"
triggers:
  - /helix:propose
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/codex/skill-entry.md`，执行 runtime preflight，然后执行一次：

```bash
python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --command propose --resource subagent_protocol --resource telemetry_protocol
```

按协议完整消费 packet，以唯一 `commands/propose.md` 为命令契约。
