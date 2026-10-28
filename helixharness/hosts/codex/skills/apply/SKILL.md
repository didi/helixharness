---
name: apply
description: "执行任务、验证并完成双轴代码评审。"
triggers:
  - /helix:apply
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/codex/skill-entry.md`，执行 runtime preflight，然后执行一次：

```bash
python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --command apply --resource subagent_protocol --resource telemetry_protocol
```

按协议完整消费 packet，以唯一 `commands/apply.md` 为命令契约。
