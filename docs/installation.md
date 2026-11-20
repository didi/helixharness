# Installation and updates / 安装与更新

[English guide](user-guide.md) · [中文指南](user-guide.zh-CN.md)

## Requirements

Python 3.10+, Git and a plugin-capable Codex, Claude Code or Cursor installation are required. Make sure the host is signed in or has its model configuration set up and is working normally. The platform is optional. Cursor desktop support follows the release scope and limitations below.

The shared runtime supports macOS, native Windows and Linux; see [platform support and verification](platform-support.md). Windows Codex uses the PowerShell entrypoint, while Claude Code hooks require Git for Windows Bash. Cursor native acceptance currently covers macOS; Windows Cursor is outside this release's verified scope.

Unless marked PowerShell, the examples use Bash/Zsh. `didi/helixharness` is the planned public repository; remote commands become usable after its publication. For testing before publication or modifying the plugin, use [developer installation](#developer-installation).

This edition uses only the HelixHarness identifiers: `helix@helixharness-marketplace`, logical `/helix:*` commands (Cursor: `/helix-{command}`), `.helixharness/`, `helixharness.workspace.yaml`, and `HELIXHARNESS_*` environment variables. It does not recognize or migrate identifiers, configuration or runtime data from previous branding. Initialize a fresh application copy when validating this edition.

## Install from GitHub after publication

For Codex and Claude Code, run the following terminal commands. For Cursor, use the [marketplace flow](#cursor). These marketplace routes do not require a manual source clone:

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

## Verify installation

Open a new host session in the application repository you want to develop. Confirm the nine public commands: `init`, `propose`, `apply`, `archive`, `bug-fix`, `ut`, `update`, `status`, `config`.

Review and trust the plugin's hooks through the host's native interface. Confirm SessionStart identifies the host and installed plugin path. Command visibility alone does not prove hooks executed. Then run `/helix:init` (Cursor: `/helix-init`); check generated project hooks and project documentation before creating a feature. Repeated init should preserve unrelated host settings. For single-repository Init, required project files include nonempty `docs/harness/index.md`, `docs/knowledge/index.md` and `docs/knowledge/project-context.md`; also check the other outputs required for the detected project type. A final completion message is not a substitute for those checks.

If an entrypoint reports `plugin_source_mismatch`, its executing copy differs from the source bound to the session. Check the active marketplace and installed plugin, keep the intended source selected, and start a new session. Use resources from that installation; do not mix another checkout or cached version into the workflow.

### Start with an empty project

Run `/helix:init` (Cursor: `/helix-init`) in a Git repository root. For a new directory, run `git init` yourself first; an initial commit is not required. An empty or documentation-only repository skips code scans and gets a minimal project context, harness, specs directories, and host hooks. Unconfirmed project details stay as TODOs. Use `/helix:propose` and `/helix:apply` (Cursor: `/helix-propose` and `/helix-apply`) to develop the first feature. Init and Apply do not create a commit.

<a id="cursor-preview"></a>

## Cursor

Cursor desktop support uses `/helix-xxx` Skills and requires Python 3.10+, following the shared runtime contract. The default distribution route is a Cursor marketplace. After the release repository is available, a team administrator imports it through **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo**. Users then open **Customize → helix → Install** and select project or user scope. Follow Cursor's repository-provider and account-permission prompts. Helix has the required marketplace manifests; a public Cursor Marketplace listing requires submission and review. See [Cursor's marketplace documentation](https://cursor.com/docs/plugins).

Cursor 桌面支持使用 `/helix-xxx` Skill，按共享运行时合同要求 Python 3.10+，默认通过 Cursor 市场分发。发行仓库可访问后，团队管理员通过 **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo** 导入；用户在 **Customize → helix → Install** 选择项目或用户范围。仓库和账号权限按 Cursor 提示配置。Helix 已具备市场清单，公共市场上架需单独提交审核。

Reload the window, start a new Agent and select `/helix-status` from `/helix-` completion. Verify the selected plugin root and runtime preflight, then use `/helix-init` in the application repository. The nine command names and arguments follow the shared workflow contracts. Existing native acceptance covers a fixed remote source; the general marketplace UI flow remains an accepted verification gap for this release.

安装后重载窗口，新建 Agent，从 `/helix-` 补全选择 `/helix-status`，核对插件来源及运行预检，再在业务仓库运行 `/helix-init`。九个命令的名称与参数沿用公共流程合同。已有原生实机证据覆盖固定远程来源；普通市场 UI 完整流程保留为本次接受的验证缺口。

The [Cursor installer contract](../helixharness/hosts/cursor/installer.md) documents advanced fixed-SHA project configuration, optional Helix-managed Local installation and recovery. Fixed-SHA configuration lets Cursor obtain the package without a source checkout. Registered managed-local installations on macOS/Linux support `/helix-update`; native marketplace installations use Cursor's update controls. A manually copied Local package has no managed registration.

[Cursor 安装合同](../helixharness/hosts/cursor/installer.md)提供高级固定 SHA 项目配置、可选的 Helix 托管 Local 安装及恢复步骤。固定 SHA 由 Cursor 获取插件，无需先下载源码。macOS/Linux 的已登记托管 Local 支持 `/helix-update`；原生市场安装通过 Cursor 管理更新，手工复制副本没有托管登记。

Keep a single intended Helix source active in Cursor. A Skill from an imported copy can conflict with a Hook from another installation. Resolve `plugin_source_mismatch` by checking the actual Skill and plugin root. Matching names or versions alone do not identify the selected source. Imported Claude hooks may also duplicate checks. Preserve other hosts' installations and user settings while resolving these conflicts.

Cursor 中应选择明确的 Helix 来源。导入副本的 Skill 与另一安装的 Hook 可能冲突，遇到 `plugin_source_mismatch` 时核对实际 Skill 与插件根路径。同名或同版本不能证明来源一致；导入 Claude hooks 也可能重复检查。处理冲突时保留其他宿主安装及用户设置。

Statistics require explicit consent and current-session identity. Missing identity skips statistics while workflows continue. Token attribution can be partial or unavailable. Consent revocation cleans the current repository; window-close and later-startup cleanup are best effort. CLI, Cloud and full D2C are outside this release's complete acceptance scope. See [support and verification boundaries](cursor-testing.md).

统计需要明确授权和本次会话身份，缺身份时跳过统计并继续业务，Token 归属可为 partial/unavailable。关闭授权清理当前仓库，关窗及后续启动清理尽力执行。CLI、Cloud、完整 D2C 不在本期完整验收范围，详见[支持与验证边界](cursor-testing.md)。

## Updates

On Claude Code and Codex, run `/helix:update` in the host conversation. The plugin checks the configured marketplace, shows an available update and uses the host adapter after authorization. Cursor uses `/helix-update` for registered Helix-managed local installations on macOS/Linux, following the same check, confirmation and one-time authorization protocol. Manual copies and Cursor marketplace installations use their original update mechanism.

- **Git source:** the host refreshes the configured repository; use the same marketplace for later versions.
- **Local source:** update the source copy yourself first, then check for updates. The plugin does not pull an arbitrary local checkout automatically. If the copy was cloned, review its working tree and update it using your normal Git workflow.
- Start a new host session after updating; review changed hooks when the host requests it.


## Uninstall

Use the host's plugin management to uninstall `helix@helixharness-marketplace`. Generated project knowledge, requirements and business code are retained. Review project hook settings separately if you no longer want the host to invoke HelixHarness in that application repository.

## 中文说明

需要 Python 3.10+、Git，以及支持插件的 Codex、Claude Code 或 Cursor。Cursor 使用上面的 [Cursor 独立安装流程](#cursor-preview)。先确保宿主已完成登录或模型配置，能够正常使用。平台不是安装插件的前置条件。共享运行时的 Windows 支持不代表 Windows Cursor 已完成验收；Cursor 实机验收目前覆盖 macOS。

正式发布后，Codex 与 Claude Code 使用上面的 GitHub 安装命令，Cursor 使用市场界面流程；这些市场安装方式无需先手动克隆源码。发布前测试或修改插件时，使用[开发者安装（本地源码）](#developer-installation)：先下载或克隆源码，再从**包含两个子项目的根目录**添加本地市场。根市场指向 `./helixharness`，不会安装平台。独立插件副本仍可添加插件目录本身；同名市场只保留一个来源。

安装后新开会话，确认九个命令可见、插件 hooks 已审阅并实际执行，再在业务仓库运行 `/helix:init`（Cursor 为 `/helix-init`）并检查项目 hooks。单仓 Init 还需检查非空的 `docs/harness/index.md`、`docs/knowledge/index.md`、`docs/knowledge/project-context.md` 及对应项目类型要求的其他产物，不能只看“已完成”回复。若出现 `plugin_source_mismatch`，先核对市场来源与当前安装插件，选定正确来源并新开会话，避免混用其他 checkout 或缓存版本。Claude Code/Codex 更新使用 `/helix:update`；本地来源需先自行更新源码，不能把版本识别成功当作已完成跨版本升级。Cursor 在 macOS/Linux 上使用 `/helix-update` 升级已登记的 Helix 托管本地安装，复用检查、确认与一次性授权协议；手工副本和原生市场安装仍通过原安装方式更新。

**从空项目开始：**在 Git 仓库根目录运行 `/helix:init`（Cursor 为 `/helix-init`）。新目录请先自行执行 `git init`，不需要首次提交。空仓库或纯文档仓库跳过代码扫描，生成最小项目上下文、约束、需求目录与宿主 hooks，未确认的信息标记 TODO。再用 `/helix:propose` 和 `/helix:apply`（Cursor 为 `/helix-propose` 和 `/helix-apply`）开发第一个需求。Init 和 Apply 不创建提交。

## Windows local source / Windows 本地源码

From the combined repository root in PowerShell:

```powershell
$env:HELIXHARNESS_SOURCE = (Get-Location).Path
codex plugin marketplace add "$env:HELIXHARNESS_SOURCE"
codex plugin add helix@helixharness-marketplace
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\helixharness\scripts\runtime_preflight.ps1 check
```

Windows 不需要 WSL。Codex 自动 Hook 使用 `commandWindows`；Claude Code 使用 Git for Windows 的 Bash，若无法自动发现，可设置 `$env:CLAUDE_CODE_GIT_BASH_PATH = 'C:\Program Files\Git\bin\bash.exe'` 后重启宿主。不要把 Windows 的 WSL 启动器当作 Git Bash。详细步骤与验证边界见[跨平台说明](platform-support.md)。
