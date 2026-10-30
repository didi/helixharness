# HelixHarness 工作流架构

HelixHarness 是独立的 Lite 工作流插件。运行时只读取当前安装的插件和用户当前项目，
不依赖其他 HelixHarness checkout、历史版本或内部平台。

## 公共入口与唯一正文

九个公共命令是 `init`、`propose`、`apply`、`archive`、`ut`、`bug-fix`、`update`、`status`、`config`。
`commands/` 保存唯一公共入口；Propose、Apply、Archive 和 Bug Fix 通过 Router 加载当前正文。

| 范围 | 正文 | 职责 |
| --- | --- | --- |
| Single | [`workflows/single/`](../helixharness/workflows/single/) | 单仓需求、实现、评审、归档和修订 |
| Workspace | [`workflows/workspace/`](../helixharness/workflows/workspace/) | 多仓合同、依赖编排与根 Spec 归档 |
| Init / UT / Update | [`commands/`](../helixharness/commands/) | 初始化、单测维护和当前安装源更新 |

`propose --loop` 串联 Propose → Apply → Archive；两种执行范围共享相同业务合同。
Workspace 子任务从公共入口进入 Single 正文，根目录不写子仓业务工件或项目 linters/hooks。

`status` 只读检查已有需求工件并给出进度、问题和建议命令；`config` 只查看或修改用户配置。两者直接使用各自命令合同，不进入阶段 Router、不产生研发 Stage。

## 宿主边界

[`Host API`](../helixharness/reference/host-api.md) 定义 manifest 和适配器合同。
Claude Code 使用原生命令，Codex 使用 Skill 投影；宿主机制由 `hosts/{host-id}/` 提供。
两个原生插件清单显式选择各自的 hook 配置。宿主入口负责固定身份和事件转换，
共享生命周期逻辑不得根据平台名称、环境变量或缓存目录猜测宿主。

Codex Init 入口加载 `project_config`、`project_hooks`；UT 入口加载 `ut`。
单仓 Init 必须实际生成并校验当前 Host Kit 的项目 hooks，重复初始化保留用户配置并去重。
业务阶段显式记录 start/finish；会话 Stop 不是业务完成证据。

## 独立性与数据边界

- 只支持当前 Host API 和当前账本、轨迹 schema；不迁移旧版本数据，不接受已退役的工作流正文路径。
- 不支持的数据返回 unavailable 或明确阻断，原文件保持不变。
- 普通 Git `main` / `master` 分支识别属于用户仓库支持，不依赖历史插件主分支。
- 新宿主通过独立 Host Kit、分发配置、专项测试和真实客户端验收接入，不在共享核心增加平台分支。
- 安装可选的平台服务不是命令运行前提；关闭统计时研发流程可独立执行。

## 验证

统一回归入口为 `python3 scripts/run_tests.py`，另执行 Host Kit 和 Codex 投影校验。
原生客户端验收分别核查插件装载、启动身份、真实子任务、测试和覆盖率、
完整工件、归档状态与清理；单测通过、命令退出成功或模型回复完成均不能单独证明闭环通过。

## 未来架构

- [RFC 0001: TypeScript Core Runtime](rfcs/0001-typescript-core.md)：讨论把确定性核心从 Python 迁移到 TypeScript/JavaScript 的边界、阶段和验收标准。
