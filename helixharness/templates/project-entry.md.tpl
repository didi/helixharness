# HelixHarness 导航

## 目录导航

- `docs/harness/index.md`：项目约束入口。
- `docs/knowledge/index.md`：当前知识入口，按“文件/目录、定位、加载时机”选择相关叶子。
- `docs/specs/active/<feature>/`：当前需求；Propose 生成 `spec.md`、`tasks.md`，Apply 生成 `verify-report.md`。
- `docs/specs/completed/<feature>/`：通过 Archive 门禁后归档的三份工件。

## 关键约束速览

{{PROJECT_CONTEXT}}

## 常用命令

`/helix:init`、`/helix:propose`、`/helix:apply`、`/helix:archive`、
`/helix:ut`、`/helix:bug-fix`、`/helix:update`。
`propose --loop` 按顺序执行 Propose → Apply → Archive。

## Agent 工作规则

读取当前宿主原生入口与项目索引，按任务范围加载约束和知识。完整命令以插件 `commands/` 为准。
保留用户已有内容；实现、验证和归档必须有实际工件证据。Apply 内部执行 Standards 与 Spec 双轴评审。
归档前展示知识修改建议并按用户确认的精确范围更新；提交和推送遵循当前命令的授权规则。
