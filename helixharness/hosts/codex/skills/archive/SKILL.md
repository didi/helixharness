---
name: archive
description: "校验完成证据，更新知识并归档需求。"
triggers:
  - /helix:archive
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/codex/skill-entry.md`，执行 runtime preflight，然后执行一次：

```bash
python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --command archive --resource subagent_protocol --resource telemetry_protocol
```

按协议完整消费 packet，以唯一 `commands/archive.md` 为命令契约。
