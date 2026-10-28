---
name: config
description: "查看或修改 HelixHarness 用户配置：统计授权、作用范围、统计与反馈上报地址。"
triggers:
  - /helix:config
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/codex/skill-entry.md`，执行 runtime preflight，然后执行一次：

```bash
python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --command config
```

按协议完整消费 packet，以唯一 `commands/config.md` 为命令契约。
