# Qoder Skill Entry Protocol

Qoder 把插件内 `skills/{name}/SKILL.md` 暴露为 `/name`（限定名 `/helix:{name}`），
而 `commands/*.md` 不会被暴露为可调用入口。因此每个公共命令由一个薄 Skill 入口承载，
正文仍是唯一公共契约 `commands/{command}.md`。

## 插件根换算

薄 Skill 位于 `hosts/qoder/skills/{name}/SKILL.md`，插件根固定为当前文件向上四级：

```text
hosts/qoder/skills/{name}/SKILL.md  →  ../../../..  →  插件根
```

该换算由 Host Kit 固定声明，不搜索插件缓存、不枚举安装目录。

## 执行顺序

1. 在任何 Python 调用前完整读取 `reference/runtime-preflight.md` 并执行：

   ```text
   sh {plugin_root}/scripts/runtime_preflight.sh check
   ```

2. 执行一次来源校验，只消费返回的 `resolved_resources`：

   ```text
   python3 {plugin_root}/scripts/host_runtime_cli.py resolve --platform qoder --expected-plugin-root {plugin_root}
   ```

3. 完整读取并执行唯一公共契约 `commands/{command}.md`；需要阶段正文时按其中 Router
   返回值读取 `workflows/single/*.md` 或 `workflows/workspace/*.md`。

## 边界

- 不得用 `find` / `rg` / `ls` 搜索插件缓存来定位资源；绑定路径缺失即
  `BLOCKED:helixharness_host_contract_invalid`。
- 不得读取 Claude Code 或 Codex 的入口文件与 hooks。
- 同一 invocation 内已读取且未变化的资源必须复用。
