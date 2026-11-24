---
name: ut
description: "单元测试生成与维护命令。基于知识库和活跃需求，逐层扫描代码并生成/维护单测，运行验证并检查逻辑缺陷，确保覆盖率达标。"
---

> Qoder 薄入口：只绑定 Host Kit 并加载唯一公共契约，不承载业务步骤。

1. 确定 plugin root：优先使用 SessionStart 注入的 HelixHarness Host Context 里的 `plugin_root`；没有注入时取当前文件向上两级目录。不得搜索其他版本的插件缓存。
2. 完整读取并执行 `{plugin_root}/hosts/qoder/skill-entry.md`，命令名固定为 `ut`。
3. 完整读取 `{plugin_root}/commands/ut.md`，把下方用户输入逐字作为该契约的 `$ARGUMENTS` 执行：不重排参数、不补全需求名、不从历史会话推断。

## User Input

```text
$ARGUMENTS
```
