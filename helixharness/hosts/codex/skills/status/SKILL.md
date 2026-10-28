---
name: status
description: "只读查看需求阶段、任务进度、阻塞原因和下一步命令。"
triggers:
  - /helix:status
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/codex/skill-entry.md`，执行 runtime preflight，然后执行一次：

```bash
python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --command status
```

按协议完整消费 packet，以唯一 `commands/status.md` 为命令契约。
