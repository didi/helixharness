---
name: update
description: "检查并升级 HelixHarness plugin。通过 Host Kit 识别安装版本、展示更新内容，并在用户授权后执行当前宿主的更新适配器。"
triggers:
  - /helix:update
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/codex/skill-entry.md`，执行 runtime preflight，然后执行一次：

```bash
python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --command update --resource subagent_protocol --resource telemetry_protocol
```

按协议完整消费 packet，以唯一 `commands/update.md` 为命令契约。
