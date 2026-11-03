---
name: ut
description: "单元测试生成与维护命令。基于知识库和活跃需求，逐层扫描代码并生成/维护单测，运行验证并检查逻辑缺陷，确保覆盖率达标。"
---

插件根为当前文件的 `../../../..`。完整读取 `hosts/qoder/skill-entry.md`，执行 runtime preflight
与来源校验，然后完整读取并执行唯一公共契约 `commands/ut.md`。

```bash
python3 {plugin_root}/scripts/host_runtime_cli.py resolve --platform qoder --expected-plugin-root {plugin_root}
```

按 `commands/ut.md` 的 Router 与门禁执行；需要阶段正文时读取 Router 返回的唯一 workflow，
不得改写路径、不叠加其他 scope、不递归调用公共入口。
