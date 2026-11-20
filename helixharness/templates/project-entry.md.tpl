# HelixHarness 导航

## 目录导航

- `docs/harness/index.md`：项目约束入口。
- `docs/knowledge/index.md`：当前知识入口，按“文件/目录、定位、加载时机”选择相关叶子。
- `docs/specs/active/<feature>/`：当前需求；Propose 生成 `spec.md`、`tasks.md`，Apply 生成 `verify-report.md`。
- `docs/specs/completed/<feature>/`：通过 Archive 门禁后归档的三份工件。

## 关键约束速览

{{PROJECT_CONTEXT}}

<!-- helix:commands:start -->
## 常用命令

按当前宿主选择入口；同一项目可由多个宿主使用，保留两组指引。

| 命令 | Cursor | Claude Code / Codex |
| --- | --- | --- |
| 初始化 | `/helix-init` | `/helix:init` |
| 需求规划 | `/helix-propose` | `/helix:propose` |
| 执行实现 | `/helix-apply` | `/helix:apply` |
| 归档 | `/helix-archive` | `/helix:archive` |
| 单元测试 | `/helix-ut` | `/helix:ut` |
| 修复未归档需求 | `/helix-bug-fix` | `/helix:bug-fix` |
| 更新插件 | `/helix-update` | `/helix:update` |
| 查看状态 | `/helix-status` | `/helix:status` |
| 用户配置 | `/helix-config` | `/helix:config` |

Cursor 使用 `/helix-propose <feature> <requirement-or-path> --loop`；Claude Code / Codex 使用
`/helix:propose <feature> <requirement-or-path> --loop`，按顺序执行 Propose → Apply → Archive。
仅入口写法不同；参数、路径、引号与命令适用范围保持一致。`bug-fix` 仅用于未归档的单仓需求。
<!-- helix:commands:end -->

## Agent 工作规则

读取当前宿主原生入口与项目索引，按任务范围加载约束和知识。完整命令以插件 `commands/` 为准。
保留用户已有内容；实现、验证和归档必须有实际工件证据。Apply 内部执行 Standards 与 Spec 双轴评审。
归档前展示知识修改建议并按用户确认的精确范围更新；提交和推送遵循当前命令的授权规则。
