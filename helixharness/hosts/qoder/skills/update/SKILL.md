---
name: update
description: "检查并升级 HelixHarness plugin。通过 Host Kit 识别安装版本、展示更新内容，并在用户授权后执行当前宿主的更新适配器。"
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/qoder/skill-entry.md`，执行 runtime preflight
与来源校验，然后完整读取并执行唯一公共契约 `commands/update.md`。

```bash
python3 {plugin_root}/scripts/host_runtime_cli.py resolve --platform qoder --expected-plugin-root {plugin_root}
```

按 `commands/update.md` 的 Router 与门禁执行；需要阶段正文时读取 Router 返回的唯一 workflow，
不得改写路径、不叠加其他 scope、不递归调用公共入口。
