# HelixHarness Workspace

## 目录导航

- `helixharness.workspace.yaml`：子仓登记与路径边界。
- `docs/knowledge/index.md`：跨仓知识入口；服务目录、关系和集成边界按需读取。
- `docs/specs/active/<feature>/spec.md`：Workspace 根唯一需求工件，记录概要设计、仓库影响范围与依赖。
- `docs/specs/completed/<feature>/spec.md`：所有子仓归档通过后，最后归档的根 Spec。

## 关键约束速览

Workspace 初始化只生成根目录协调产物，不自动初始化或改写子仓。
各子仓独立执行完整 Single Propose/Apply/Archive，分别维护 `spec.md`、`tasks.md`、`verify-report.md`。
根目录不复制子仓工件、不维护独立 Ledger，不生成 proposal、design、verification 或 repo-plan 文件。

<!-- helix:commands:start -->
## 常用命令

按当前宿主选择入口；同一 Workspace 可由多个宿主使用，保留两组指引。

| 命令 | Cursor | Claude Code / Codex |
| --- | --- | --- |
| 初始化根目录 | `/helix-init --workspace` | `/helix:init --workspace` |
| 建立需求 | `/helix-propose <feature> <requirement-or-path>` | `/helix:propose <feature> <requirement-or-path>` |
| 执行实现 | `/helix-apply <feature>` | `/helix:apply <feature>` |
| 归档 | `/helix-archive <feature>` | `/helix:archive <feature>` |

为 propose 增加 `--loop` 按顺序执行 Propose → Apply → Archive。Apply 与 Archive 按 Router 的
Workspace 范围调度子仓；其他命令的适用范围仍以各自 Router 和完整命令为准。
Workspace 根不支持 BugFix；需进入已初始化子仓，对未归档的单仓需求使用
Cursor `/helix-bug-fix <feature>` 或 Claude Code / Codex `/helix:bug-fix <feature>`。
仅入口写法不同；参数、路径、引号与命令适用范围保持一致。
<!-- helix:commands:end -->

## Agent 工作规则

所有子仓路径先通过 registry 解析；派发、等待与完成校验遵循 RepoJob 合同。
只在执行子仓任务时读取该仓宿主入口。共享协调文件保持单写者，跨仓授权分别确认精确范围。
子仓失败时保留已完成事实；所有子仓完成后才归档根 Spec。
