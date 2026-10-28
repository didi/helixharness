---
name: init
description: "HelixHarness 初始化入口。默认初始化单服务仓库；传入 --workspace 时初始化多仓 workspace，并生成 workspace registry 与服务关系基线。"
triggers:
  - /helix:init
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/codex/skill-entry.md`，执行 runtime preflight，然后执行一次：

```bash
python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --command init --resource subagent_protocol --resource telemetry_protocol --resource project_config --resource project_hooks
```

按协议完整消费 packet，以唯一 `commands/init.md` 为命令契约。
