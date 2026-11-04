# Pi 子代理适配

本宿主 `subagents=unsupported`。公共 Subagent 协议中的派发、容量、等待与终态校验仍然生效，
但只允许 Dispatch Contract 明确声明的 `unavailable_fallback`：父流程按同一 `prompt_source`
串行 inline 执行每个逻辑 job，并记录 `WARN:pi_subagent_unavailable`。

```text
apply_shared_write_mode: parent_serial_write
ut_experience_write_mode: parent_finalize
result_encoding: inline_section
```

- 不得虚构子代理工具，不得把串行 inline 结果写成宿主原生 job 终态。
- 逻辑 job 数量、依赖顺序、`read_scope`、`write_scope`、产物与验收语义不因 inline 而改变；
  并行组按依赖顺序串行执行，输出仍按逻辑 job 分别给出。
- 共享索引、决策、计划、checkpoint、timing 与最终报告保持单写者语义：inline 时父流程即唯一写入者。
- 安装 pi 子代理扩展后可评估把 `subagents` 提升为 `native`；在此之前不得按宿主名称猜测能力。
