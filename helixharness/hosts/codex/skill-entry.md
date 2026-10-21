# Codex Skill Entry Protocol

Codex 薄入口的 plugin root 固定为当前 `hosts/codex/skills/{name}/SKILL.md` 向上四级。
在任何 Python 调用前完整读取 `reference/runtime-preflight.md`，执行：

```text
sh {plugin_root}/scripts/runtime_preflight.sh check
```

- `ok=true` 时，本轮只执行一次 `scripts/codex_entry_load.py` **prepare**。prepare 回执不包含正文，只返回
  最多两个逻辑 packet 的 page 计数、资源 hash、15 分钟 TTL 与随机 `invocation_key`；prepare 回执和任一
  page 回执都必须小于 16 KiB。把 key 记录在 invocation 内存态 `HELIXHARNESS_CODEX_ENTRY_LOAD`，同一业务
  invocation 禁止重复 prepare。
- 严格按 `packets[].round`、每包从 `page_index=0` 到 `page_count-1` 顺序执行
  `python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --read-page --invocation-key {key} --packet-round {round} --page-index {index}`。
  page read 是同一已验证快照的有界 continuation，不计作资源发现轮次；允许在 TTL 内幂等重读同一页，
  禁止跳页、增加第三个逻辑 packet、按回执 path 直接读源文件或重新扫描 cache。
- 每次宿主工具调用只读取一页，完整检查该页回执后再读下一页。不得用 shell 循环、批量脚本或
  同一工具调用合并多页输出；单页有界不代表合并输出仍能避免宿主截断。输出被截断时在 TTL 内
  单独重读该页，完成消费前不得调用 finish。
- 每个资源必须按 `resource_page_index` 无损拼接，并以 prepare 的 `source_sha256` 复核；全部资源消费后立即执行
  `python3 {plugin_root}/scripts/codex_entry_load.py --plugin-root {plugin_root} --finish --invocation-key {key}`。
  finish 必须使用独立工具调用；成功后快照会关闭，不能再补读遗漏或被截断的页。
  缺页、过期、hash 不符或 finish 失败时关闭失败，不得用第二次 prepare 恢复当前 invocation。
- Python 不可用时，严格按 runtime preflight 的用户选择、安装和 warning 语义处理。用户选择不安装并
  允许业务继续时，把薄入口给出的同一组 `--command/--resource` 参数原样交给
  `sh {plugin_root}/scripts/codex_entry_load.sh`。shell prepare/read-page/finish 使用与上方相同参数和顺序；
  只有 prepare 成功才消费其持久化安全快照。shell loader 必须验证唯一 projection、
  adapter allowlist、UTF-8、单资源与 packet 大小并返回 source/page hash。不得自行拼包、搜索 cache、
  增加第三包、降级业务门禁或暴露 `command not found`。
- Git 不可用、资源缺失或 packet 越界时关闭失败，不得猜测替代路径；仓库其他内容变化不影响当前已冻结快照。

薄入口只选择加载机制；`commands/*.md` 是唯一公共入口；其 Router 选择的 `workflows/single/*.md` 或 `workflows/workspace/*.md` 是对应阶段正文，canonical 方法 Skill 仍位于
`skills/{name}/SKILL.md`。
