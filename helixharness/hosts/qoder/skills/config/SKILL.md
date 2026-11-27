---
name: config
description: "查看或修改 HelixHarness 用户配置：统计授权、作用范围、统计与反馈上报地址。"
---

> Qoder 薄入口：只绑定 Host Kit 并加载唯一公共契约，不承载业务步骤。

1. 确定 plugin root：优先使用 SessionStart 注入的 HelixHarness Host Context 里的 `plugin_root`；没有注入时取当前文件向上两级目录。不得搜索其他版本的插件缓存。
2. 完整读取并执行 `{plugin_root}/hosts/qoder/skill-entry.md`，命令名固定为 `config`。
3. 完整读取 `{plugin_root}/commands/config.md`，把下方用户输入逐字作为该契约的 `$ARGUMENTS` 执行：不重排参数、不补全需求名、不从历史会话推断。

## User Input

```text
$ARGUMENTS
```
