# AGENTS.md

> 本文件适用于整个 `helixharness-platform/`。修改代码前先确认模块边界与验证方式。

## Scope

- 本项目位于一个更大的 Git worktree 中；除非任务明确包含兄弟项目，只修改本目录。
- 保留无关用户改动。评审 diff 时使用项目相对路径。
- 不读取、输出或提交凭据、`.env`、未脱敏对话和代码内容。

## Architecture

```text
web_ui/  FastAPI composition root, routes, templates
  ├── eval/       Git eval-log sync, consumer quality Gate, actual-usage dashboard
  ├── bench/      Case, immutable Run, execution, Judge, score, trusted report
  └── evolution/  capability catalog, hypothesis, cycle, Candidate, promotion bundle
```

核心原则：`web_ui → eval / bench / evolution`。

- 三个后端模块彼此禁止 import，也不能跨模块查询 SQLite。
- `web_ui` 是唯一组合层，可将一个模块的显式 DTO 传给另一个模块的应用服务。
- 模块的 CLI、配置、文档、测试和运行目录必须留在模块内。
- `tests/test_module_boundaries.py` 校验依赖和目录边界。

### eval

- `usage_loader.py`：真实使用数据读取、聚合和展示 HTML。
- `usage_sync.py`：Git 数据源、同步计划与同步状态。
- `telemetry_quality.py`：独立消费端 Gate；`eval-log.json` 是事实源，SQLite 只是可重建投影。
- `usage_ai.py`：标注后的辅助洞察，只用于看板展示，不能成为自动自进化证据。

### bench

- 独占 Case 准入、不可变 Run、Worker、确定性检查、语义 Judge、评分轴、硬门禁和 verdict。
- `runtime_secrets.py` 统一加载评测执行与 Judge provider 配置。
- `bench/.cache/`、`docs/`、`tests/` 属于 Bench。

### evolution

- `catalog/capabilities.yaml` 是 Git 版本化、人工拥有的能力目录。
- `discovery.py` 可调用大模型生成语义机会，但 REPAIR/GROWTH 的最终分类由契约规则决定。
- `service.py` 默认 48 小时一轮；无新增合格证据 `NOOP`；每轮最多自动启动一个修复实验。
- 增长型必须人工确认能力目录并人工选择。没有 Published + Calibrated Case 不得启动实验。
- `candidate.py` 在隔离 worktree 中生成单假设 Candidate，冻结为 commit/ref，并硬拦截受保护路径。
- evolution 不评分、不复制 Bench gate，也不自动发布。

### web_ui

- `main.py`、`deps.py` 是单服务 composition root。
- `routes/{eval,bench,evolution}.py` 是三个后端的 HTTP adapters。
- 模板位于 `templates/{eval,bench,evolution}/`；保持现有 Tailwind 与声明式 HTMX 风格。

## Environment and commands

```bash
python -m pip install -e ".[server,dev]"
python -m pytest
python -m ruff check web_ui eval bench evolution tests
HELIXHARNESS_PLATFORM_USAGE_DATA_SOURCE=demo python -m web_ui.main --port 7860
mkdocs serve -f evolution/mkdocs.yml
```

外部同步、模型调用、正式 Benchmark Run 和部署可能消耗私有资源或付费额度；只有任务明确需要时才执行。单元测试必须 mock 网络、模型与外部 Worker。

## Coding and verification

- 支持 Python 3.10+；公共或不明显行为使用类型标注与简洁 docstring。
- Ruff 规则为 `E/F/I/W`，行长 120，忽略 `E501`。
- 避免在 import 时加载会破坏基础安装的可选依赖。
- 修改 `eval/usage_loader.py` 后运行 `eval/tests/test_usage_loader.py`。
- 修改 Web 路由或模板后运行 `web_ui/tests`。
- 修改 Candidate、调度或 Gate 后运行 `evolution/tests` 与 `eval/tests/test_telemetry_quality.py`。
- 完成前运行最相关测试和 Ruff；范围较大时运行完整 pytest。区分新增失败和已有基线失败。

## Data and Git

- 不提交 `bench/.cache/`、`eval/.cache/`、`evolution/.cache/`、模块日志、虚拟环境或生成站点。
- Candidate worktree 和模型输出是运行产物，不进入平台源码提交。
- 保持 Case/Run/eval-log 历史不可变；投影可重建不代表原始事实可覆盖。
- 在当前功能分支开发；不要 push、部署或创建 PR，除非用户另行授权。
- Commit message 使用 `type(scope): 描述`。

完成时报告修改范围、最新验证命令、未执行的外部验证和仍需人工配置的能力目录/Case。
