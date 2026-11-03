---
name: init
description: "HelixHarness 初始化入口。默认初始化单服务仓库；传入 --workspace 时初始化多仓 workspace，并生成 workspace registry 与服务关系基线。"
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/qoder/skill-entry.md`，执行 runtime preflight
与来源校验，然后完整读取并执行唯一公共契约 `commands/init.md`。

```bash
python3 {plugin_root}/scripts/host_runtime_cli.py resolve --platform qoder --expected-plugin-root {plugin_root}
```

按 `commands/init.md` 的 Router 与门禁执行；需要阶段正文时读取 Router 返回的唯一 workflow，
不得改写路径、不叠加其他 scope、不递归调用公共入口。
