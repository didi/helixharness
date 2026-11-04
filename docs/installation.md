# Installation and updates / 安装与更新

[English guide](user-guide.md) · [中文指南](user-guide.zh-CN.md)

## Requirements

Python 3.10+, Git and a Codex or Claude Code installation supporting plugin marketplaces are required. Make sure the host is signed in or has its model configuration set up and is working normally. The platform is optional.

The examples use Bash/Zsh. `didi/helixharness` is the planned public repository; remote commands become usable after its publication. For testing before publication or modifying the plugin, use [developer installation](#developer-installation).

This edition uses only the HelixHarness identifiers: `helix@helixharness-marketplace`, `/helix:*`, `.helixharness/`, `helixharness.workspace.yaml`, and `HELIXHARNESS_*` environment variables. It does not recognize or migrate identifiers, configuration or runtime data from previous branding. Initialize a fresh application copy when validating this edition.

## Install from GitHub after publication

Choose your host and run its commands in a terminal. No manual source clone is needed:

```bash
# Codex
codex plugin marketplace add https://github.com/didi/helixharness.git
codex plugin add helix@helixharness-marketplace
```

```bash
# Claude Code
claude plugin marketplace add https://github.com/didi/helixharness.git
claude plugin install helix@helixharness-marketplace
```

The repository root contains `.claude-plugin/marketplace.json`, whose `./helixharness` source points to the plugin. Both hosts consume this compatible marketplace format. The platform is not installed by these commands.

Sources: [OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins#marketplace-metadata), [Claude Code marketplaces](https://code.claude.com/docs/en/plugin-marketplaces). Host commands and trust screens can vary with client version; use a version providing the listed commands.

## Developer installation

Use local source when developing the plugin or testing before publication. Download or clone the source, then run the following from the combined repository root containing both `helixharness/` and `helixharness-platform/`:

```bash
export HELIXHARNESS_SOURCE="$(pwd)"
# Choose one host:
codex plugin marketplace add "$HELIXHARNESS_SOURCE"
codex plugin add helix@helixharness-marketplace
```

For Claude Code, replace the last two lines with:

```bash
claude plugin marketplace add "$HELIXHARNESS_SOURCE"
claude plugin install helix@helixharness-marketplace
```

A standalone plugin copy can still use the absolute path to `helixharness/`; its nested marketplace points to `.`. Register only one source under `helixharness-marketplace` at a time. When switching sources, remove the old marketplace using the host's plugin management first.

Empty repositories are supported: `/helix:init` detects a greenfield project, skips code scans, creates the minimal harness/knowledge/spec structure with explicit TODOs, and then `/helix:propose` can define the first requirement.

## Verify installation

Open a new host session in the application repository you want to develop. Confirm the nine public commands: `init`, `propose`, `apply`, `archive`, `bug-fix`, `ut`, `update`, `status`, `config`.

Review and trust the plugin's hooks through the host's native interface. Confirm SessionStart identifies the host and installed plugin path. Command visibility alone does not prove hooks executed. Then run `/helix:init`; check generated project hooks and project documentation before creating a feature. Repeated init should preserve unrelated host settings. For single-repository Init, required project files include nonempty `docs/harness/index.md`, `docs/knowledge/index.md` and `docs/knowledge/project-context.md`; also check the other outputs required for the detected project type. A final completion message is not a substitute for those checks.

If an entrypoint reports `plugin_source_mismatch`, its executing copy differs from the source bound to the session. Check the active marketplace and installed plugin, keep the intended source selected, and start a new session. Use resources from that installation; do not mix another checkout or cached version into the workflow.

## Updates

Run `/helix:update` in the host conversation. The plugin checks the configured marketplace, shows an available update and uses the host adapter after authorization.

- **Git source:** the host refreshes the configured repository; use the same marketplace for later versions.
- **Local source:** update the source copy yourself first, then check for updates. The plugin does not pull an arbitrary local checkout automatically. If the copy was cloned, review its working tree and update it using your normal Git workflow.
- Start a new host session after updating; review changed hooks when the host requests it.


## Uninstall

Use the host's plugin management to uninstall `helix@helixharness-marketplace`. Generated project knowledge, requirements and business code are retained. Review project hook settings separately if you no longer want the host to invoke HelixHarness in that application repository.

## 中文说明

需要 Python 3.10+、Git，以及支持插件市场的 Codex 或 Claude Code。先确保宿主已完成登录或模型配置，能够正常使用。平台不是安装插件的前置条件。

正式发布后使用上面的 GitHub 安装命令，无需先手动克隆源码。发布前测试或修改插件时，使用[开发者安装（本地源码）](#developer-installation)：先下载或克隆源码，再从**包含两个子项目的根目录**添加本地市场。根市场指向 `./helixharness`，不会安装平台。独立插件副本仍可添加插件目录本身；同名市场只保留一个来源。

空仓库同样受支持：`/helix:init` 会识别 greenfield 项目，跳过代码扫描，生成带明确 TODO 的最小 harness/knowledge/spec 结构；随后可用 `/helix:propose` 定义第一个需求。

安装后新开会话，确认九个命令可见、插件 hooks 已审阅并实际执行，再在业务仓库运行 `/helix:init` 并检查项目 hooks。单仓 Init 还需检查非空的 `docs/harness/index.md`、`docs/knowledge/index.md`、`docs/knowledge/project-context.md` 及对应项目类型要求的其他产物，不能只看“已完成”回复。若出现 `plugin_source_mismatch`，先核对市场来源与当前安装插件，选定正确来源并新开会话，避免混用其他 checkout 或缓存版本。更新使用 `/helix:update`；本地来源需先自行更新源码，不能把版本识别成功当作已完成跨版本升级。
