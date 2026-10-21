# Contributing / 参与贡献

Bug reports, documentation improvements, tests and focused pull requests are welcome.

## Discuss and propose

Use [GitHub Issues](https://github.com/didi/helixharness/issues) for reproducible bugs and feature proposals. You do not need to deploy helixharness-platform to report an issue. Security issues follow [SECURITY.md](SECURITY.md).

Before a substantial behavior change, describe the problem, acceptance criteria and compatibility impact in an Issue. Work on a branch in your fork and open a PR against the repository's default branch. Keep each PR focused and include the verification commands and results. Maintainers review before merging; no CLA or DCO bot is configured in this source tree.

## Development environment

Use Python 3.10+ and Git. Follow the component's `AGENTS.md`.

```bash
# Repository root
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e './helixharness-platform[server,dev]'

# Plugin behavior and host contracts
(cd helixharness && python scripts/run_tests.py)

# Platform behavior and module boundaries
(cd helixharness-platform && python -m pytest)
(cd helixharness-platform && python -m ruff check web_ui eval bench evolution tests)
```

Tests must use synthetic data and mock paid model calls, external workers and network dependencies. Live agent acceptance requires your own model configuration and a disposable application repository. Do not put runtime output or credentials in a PR.

## Source boundaries

- `helixharness/commands/`: nine public entrypoints: seven development commands plus `status` and `config`.
- `helixharness/workflows/single/` and `workflows/workspace/`: workflow bodies.
- `helixharness/skills/`, `agents/`, `hosts/`, `scripts/`: reusable methods, delegated roles, host adapters, deterministic runtime and tests.
- `helixharness-platform/web_ui/`: composition and UI; `eval/`, `bench/`, `evolution/` remain independent modules.
- Root `.claude-plugin/marketplace.json`: combined-repository distribution, with source `./helixharness`.

Keep generated host projections aligned when changing plugin commands or skills. Plugin tests belong in `helixharness/scripts/tests/`. For platform UI changes, validate the real Demo page as well as relevant Web tests.

Plugin development references: [Host API](helixharness/reference/host-api.md), [scan protocol](helixharness/reference/scan-agent-protocol.md), [script exit codes](helixharness/reference/script-exit-codes.md), [working directory rules](helixharness/reference/skill-runtime-working-directory.md), [frontend loading](helixharness/reference/frontend-loading.md), and [loading protocol](helixharness/reference/loading-protocol.md).

To rebuild the platform stylesheet, use Node.js and run from `helixharness-platform/`:

```bash
npx --yes tailwindcss@3.4.17 -c tailwind.config.js -i web_ui/assets/tailwind.css -o web_ui/static/css/tailwind.css --minify
```

## PR checklist

- Explain the user-visible problem and resulting behavior.
- List relevant tests; distinguish local tests, Mock execution and real model verification.
- Update both language entry pages and the relevant guide when behavior changes.
- Keep unrelated files and generated runtime evidence out of the diff.
- Preserve third-party licenses and notices.

Unless explicitly stated otherwise, contributions intentionally submitted for inclusion are provided under [Apache-2.0](LICENSE). Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## 中文说明

欢迎通过 GitHub Issue 提交问题、通过 PR 改进代码和文档；无需先部署平台。较大行为变更先说明问题、验收标准和兼容性影响。PR 面向仓库默认分支，保持单一目的，列出实际执行的验证命令和结果，并由维护者评审。

使用 Python 3.10+，按上面的命令准备环境并运行相关组件测试。工作流正文放在 `workflows/`，不要复制回 `commands/`；平台三个后端保持模块边界。示例与测试使用合成数据，不提交凭据、私有对话或运行产物。贡献按 Apache-2.0 提交，并遵守社区行为准则。
