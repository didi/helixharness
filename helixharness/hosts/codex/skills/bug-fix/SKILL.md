---
name: bug-fix
description: "修订未归档需求并复用原 Apply 基线。"
triggers:
  - /helix:bug-fix
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/codex/skill-entry.md`，执行 runtime preflight，然后执行一次：

```bash
python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --command bug-fix --resource subagent_protocol --resource telemetry_protocol
```

按协议完整消费 packet，以唯一 `commands/bug-fix.md` 为命令契约。
