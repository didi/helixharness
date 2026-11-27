# Installation and updates / 安装与更新

[English guide](user-guide.md) · [中文指南](user-guide.zh-CN.md)

## Requirements

Python 3.10+, Git and a plugin-capable Codex, Claude Code, Cursor, OpenCode installation (macOS, 2.3.2; see the [support matrix](opencode-testing.md)) or Qoder Desktop / CLI are required. Make sure the host is signed in or has its model configuration set up and is working normally. The platform is optional. Cursor desktop support follows the release scope and limitations below; Qoder installs through a locally built plugin ZIP because the App has no marketplace CLI.

The shared runtime supports macOS, native Windows and Linux; see [platform support and verification](platform-support.md). Windows Codex uses the PowerShell entrypoint, while Claude Code hooks require Git for Windows Bash. Cursor native acceptance currently covers macOS; Windows Cursor is outside this release's verified scope. Qoder's Host Kit contract tests run in the CI matrix, but the Qoder App has no Windows acceptance yet and only the POSIX hook launcher exists.

Unless marked PowerShell, the examples use Bash/Zsh. `didi/helixharness` is the planned public repository; remote commands become usable after its publication. For testing before publication or modifying the plugin, use [developer installation](#developer-installation).

This edition uses only the HelixHarness identifiers: `helix@helixharness-marketplace`, logical `/helix:*` commands (Cursor: `/helix-{command}`), `.helixharness/`, `helixharness.workspace.yaml`, and `HELIXHARNESS_*` environment variables. It does not recognize or migrate identifiers, configuration or runtime data from previous branding. Initialize a fresh application copy when validating this edition.

## Install from GitHub after publication

For Codex and Claude Code, run the following terminal commands. For Cursor, use the [marketplace flow](#cursor). For Qoder, build a local ZIP and install it through the App — see [Qoder](#qoder). These marketplace routes do not require a manual source clone:

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

## OpenCode local-directory installation (verified with 2.3.2)

The plugin package lives in this repository's `helixharness/` subdirectory. In the tested OpenCode 2.3.2 client, native `plugin add` accepts npm/Git package specifications, but not an absolute local directory or a Git subdirectory suffix. Clone the repository after publication (replace the destination placeholder), then configure the plugin directory:

```bash
git clone https://github.com/didi/helixharness.git /absolute/path/to/helixharness-source
```

Merge this entry into the existing `plugins` array in project or global `opencode.json(c)`, preserving other settings and plugins:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/absolute/path/to/helixharness-source/helixharness"]
}
```

Use the directory containing the plugin's `package.json`, not the repository root or a single `.ts` file. This package has no runtime npm dependencies; an independent source copy loads without `node_modules` or `bun install`. Restart or reload OpenCode and open a new session after configuring it. These installation observations apply to 2.3.2; other client versions need their own verification.


**OpenCode Stable support (macOS, 2.3.2):** See the [support matrix](opencode-testing.md) for the current source candidate’s scope and boundaries. OpenCode 2.2.2 and 2.3.0 have automated compatibility coverage only.

The plugin registers native `helix:<command>` commands, `helix-<skill>` method skills and `helix-<agent>` roles. Commands, native `skill` tool results, Skill mentions and native Skill activation bind the requesting session's repository. Unbound Skill content is blocked when session lookup or directory validation fails. On 2026-11-20, OpenCode 2.3.2 with DeepSeek completed one synthetic single-repository Init → Propose → Apply → Archive lifecycle, with operator confirmations and a controlled contract amendment. On 2026-11-23, OpenCode 2.3.2 with DeepSeek also completed assisted synthetic two-repository Workspace and standalone UT runs; UT classification validation and protected atomic publication were subsequently fixed and passed a fresh real-model UT run without corrective operator prompts. [Targeted real-model tests on synthetic Workspaces](../helixharness/opencode/tests/README.md#recovery-and-auxiliary-workflow-acceptance-2026-11-23) also verified native Apply and partial Archive cancellation followed by fresh-session recovery, plus dependent-dispatch blocking after an injected native context-hook failure. Full production-project acceptance, unattended protocol compliance and full TUI/desktop UI acceptance remain outstanding; see [targeted terminal UI acceptance](../helixharness/opencode/tests/README.md#targeted-terminal-ui-acceptance-2026-11-23) for the tested scope. Workspace Apply uses `helix-worker` for `may_delegate` RepoJobs and requires `experimental.subagent_depth >= 2`; independent reviewers remain read-only leaf jobs. Insufficient depth blocks the RepoJob rather than bypassing review.

The adapter uses native tool/session hooks but provides no equivalent SessionStart/Stop/failure/session-end business guarantee. `scripts/task_plan.py` atomically updates the canonical `tasks.md`. Explicitly consented telemetry records stage facts and workflow traces only; native conversations, token usage and unobserved tool counts remain unavailable. `/helix:update` uses the existing confirmation protocol for clean source checkouts with an explicit Git upstream, pins the approved commit and only fast-forwards. Dirty, detached or package-managed copies are not automatically updated. Init merges `AGENTS.md` without `.opencode/` settings; plugin-level checks run existing project linters after successful native write/edit/patch. Shell changes still require explicit checks. The d-d2c Claude skill-scoped Stop/PreToolUse guards do not run; use its no-hook / `--bare` semantics while retaining user decisions, visual acceptance and completion checks.

See the [OpenCode installer](../helixharness/hosts/opencode/installer.md) for host-specific checks.

The repository root contains `.claude-plugin/marketplace.json`, whose `./helixharness` source points to the plugin. Codex and Claude Code consume this compatible marketplace format. OpenCode loads the configured local `helixharness/` package through its `package.json` export, `opencode/index.ts`. These installation routes do not install the platform.

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

For OpenCode local development, use the same [local-directory configuration](#opencode-local-directory-installation-verified-with-232) with the absolute plugin subdirectory of your source copy; no native `plugin add` step is used.

A standalone plugin copy can still use the absolute path to `helixharness/`; its nested marketplace points to `.`. Register only one source under `helixharness-marketplace` at a time. When switching sources, remove the old marketplace using the host's plugin management first.

## Verify installation

Open a new host session in the application repository you want to develop. Confirm the nine public commands: `init`, `propose`, `apply`, `archive`, `bug-fix`, `ut`, `update`, `status`, `config`.

On Claude Code, Codex and Cursor, review and trust the plugin's hooks through the host's native interface and confirm SessionStart identifies the host and installed plugin path. Command visibility alone does not prove hooks executed. OpenCode instead registers native commands, skills and agents; check a command expansion includes `canonical_host_id: opencode`, the intended `plugin_root`, and the current `repo_root` and session binding. It does not generate project hooks or `.opencode/` settings. Then run `/helix:init` (Cursor: `/helix-init`); check project documentation and, on hosts supporting them, generated project hooks before creating a feature. Repeated init should preserve unrelated host settings. For single-repository Init, required project files include nonempty `docs/harness/index.md`, `docs/knowledge/index.md` and `docs/knowledge/project-context.md`; also check the other outputs required for the detected project type. A final completion message is not a substitute for those checks.

If an entrypoint reports `plugin_source_mismatch`, its executing copy differs from the source bound to the session. Check the active marketplace and installed plugin, keep the intended source selected, and start a new session. Use resources from that installation; do not mix another checkout or cached version into the workflow.

### Start with an empty project

Run `/helix:init` (Cursor: `/helix-init`) in a Git repository root. For a new directory, run `git init` yourself first; an initial commit is not required. An empty or documentation-only repository skips code scans and gets a minimal project context, harness, specs directories, and host hooks where supported (OpenCode merges `AGENTS.md` only). Unconfirmed project details stay as TODOs. Use `/helix:propose` and `/helix:apply` (Cursor: `/helix-propose` and `/helix-apply`) to develop the first feature. Init and Apply do not create a commit.

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

<a id="qoder"></a>

## Qoder

Qoder Desktop and Qoder CLI only consume plugin ZIPs through the App install tool, so HelixHarness ships a packaging script instead of a marketplace entry. Clone the repository, then from the plugin directory build the ZIP:

```bash
cd helixharness
python3 scripts/package_qoder_plugin.py         # produces dist/helix-<version>.zip
```

The ZIP root is the plugin root and packs only the Qoder-facing allowlist: `.qoder-plugin/plugin.json`; `agents/`; `skills/`; the Qoder Host Kit under `hosts/qoder/` (host manifest, hooks, command projection, installer contract and runtime adapters) together with `hosts/shared/` and `hosts/host-manifest-v1.schema.json`; the host-neutral `commands/`, `reference/`, `scripts/`, `templates/` and `workflows/`; and the root license, notice, README and plugin-usage files. `hosts/qoder/hooks.json` is copied to `hooks/hooks.json` in the package. Codex, Claude Code, Cursor and openCode surfaces, `__pycache__` and local state are excluded; inside a Git checkout the build also refuses untracked files under allowlisted paths and symlinks that resolve outside the plugin root. Each `agents/*.md` gets a `name` frontmatter matching the file name, because Qoder registers subagents by `name`.

Install through one of:

- **Qoder App**: Extensions panel → Install local plugin → pick `dist/helix-<version>.zip`.
- **In-session tool**: ask the assistant to call `qoder-settings.install_extension` with `{source: "local", kind: "plugin", filePath: "/absolute/path/helix-<version>.zip"}`.

After installation, review and trust the plugin hooks in the App, then open a new session: plugin manifest, skills, agents and hooks load at session start, and a session already started continues using the previously bound plugin root. Qoder's `.qoder-plugin/plugin.json` `version` drives the cache directory `<QODER_CONFIG_DIR>/plugins/cache/local/helix/<version>/`, so each release bumps the version and the App replaces the older cache; coexisting versions inside the same session are not supported. Uninstall through Extensions → Installed → the plugin → Delete.

Because Qoder has no marketplace CLI equivalent, the plugin's `plugin_update` adapter is declared `unsupported`, and `/helix:update` returns a blocked status. Upgrades reinstall a new ZIP built from an updated source copy. The Qoder Host Kit declares a shape-based stale-binding policy so re-running `/helix:init` collapses the linter handlers from earlier version directories in `.qoder/settings.json` and reports residual drift as warnings (`--strict` promotes them to failures); user-owned hooks are never touched. See the [Qoder installer contract](../helixharness/hosts/qoder/installer.md) for the full packaging list, capability boundaries and acceptance checks.

Qoder Desktop 与 Qoder CLI 只能通过 App 安装工具消费插件 ZIP，因此 HelixHarness 提供本地打包脚本而不是市场条目。克隆仓库后在插件目录内运行：

```bash
cd helixharness
python3 scripts/package_qoder_plugin.py         # 产出 dist/helix-<version>.zip
```

ZIP 根即插件根，只按白名单收 Qoder 表面：`.qoder-plugin/plugin.json`；`agents/`；`skills/`；`hosts/qoder/` 下的 Qoder Host Kit（宿主清单、hooks、命令投影、安装合同与运行时适配文件）以及 `hosts/shared/` 与 `hosts/host-manifest-v1.schema.json`；与其它宿主共享的 `commands/`、`reference/`、`scripts/`、`templates/` 与 `workflows/`；以及根目录的 license、notice、README 与插件使用说明文件。`hosts/qoder/hooks.json` 打包时复制为 `hooks/hooks.json`。Codex、Claude Code、Cursor、openCode 的表面、`__pycache__` 与本地状态都在排除之列；在 git 仓库内构建时，白名单路径下未纳入版本控制的文件与解析到插件根之外的符号链接会直接报错。每个 `agents/*.md` 会被注入与文件名一致的 `name` frontmatter——Qoder 按 `name` 注册子代理角色。

安装方式二选一：

- **Qoder App**：扩展面板 → 安装本地插件 → 选择 `dist/helix-<version>.zip`。
- **会话内工具**：让助手调用 `qoder-settings.install_extension`，参数 `{source: "local", kind: "plugin", filePath: "/绝对路径/helix-<version>.zip"}`。

安装后先在 App 内审阅并信任插件 hooks，然后开新会话：插件清单、skills、agents 与 hook 都在会话启动时装载；已开启的会话仍沿用之前绑定的 `plugin_root`，不会随更新刷新。Qoder 的 `.qoder-plugin/plugin.json` `version` 决定缓存目录 `<QODER_CONFIG_DIR>/plugins/cache/local/helix/<version>/`，每次发布递增版本号，App 会替换旧版本缓存，同一会话内不保证多版本共存。卸载在 扩展 → 已安装 → 该插件 → … → 删除 完成。

Qoder 没有等价 marketplace CLI，插件的 `plugin_update` 声明为 `unsupported`，`/helix:update` 会返回阻断状态；升级须重新打包并安装新版 ZIP。Qoder Host Kit 声明了基于形状的陈旧绑定策略，重跑 `/helix:init` 时按 `--linter` 参数与脚本路径识别早先写入的检查命令，无论版本目录或路径写法怎么变都会自动收敛重复条目，`check` 默认把残留差异报为 warning，`--strict` 才判失败；用户自己的其他 Hook 不受影响。完整打包列表、能力边界与验收清单见 [Qoder 安装合同](../helixharness/hosts/qoder/installer.md)。

## Updates

On Claude Code and Codex, run `/helix:update` in the host conversation. The plugin checks the configured marketplace, shows an available update and uses the host adapter after authorization. Cursor uses `/helix-update` for registered Helix-managed local installations on macOS/Linux, following the same check, confirmation and one-time authorization protocol. Manual copies and Cursor marketplace installations use their original update mechanism. OpenCode's `/helix:update` supports clean source checkouts with an explicit Git upstream through the same confirmation protocol, pins the approved commit and only fast-forwards. Dirty, detached or package-managed copies require their original manual mechanism. Restart or reload OpenCode and open a new session after an update. Rollback remains an explicit source-revision selection, not an automatic reset; see the [OpenCode update and rollback steps](../helixharness/hosts/opencode/installer.md#更新回退与卸载). Qoder declares `plugin_update` as `unsupported` because the App has no marketplace CLI — upgrade by bumping `.qoder-plugin/plugin.json` `version`, rebuilding the ZIP with `scripts/package_qoder_plugin.py` and installing it through the App; the new version replaces the older cache and any session opened before the install continues using the previously bound plugin root.

- **Marketplace Git source (Codex/Claude Code):** the host refreshes the configured repository; use the same marketplace for later versions. OpenCode's clone-based directory configuration follows the local-source steps below.
- **Local source:** update the source copy yourself first, then restart or reload the host. The plugin does not pull an arbitrary local checkout automatically. If the copy was cloned, review its working tree and update it using your normal Git workflow.
- **Qoder ZIP:** rebuild and reinstall through the App; `/helix:update` returns a blocked status.
- Start a new host session after updating; review changed hooks when the host requests it.


## Uninstall

Use the host's plugin management to uninstall `helix@helixharness-marketplace`; for this repository's OpenCode directory configuration, remove only its entry from the `plugins` array and restart or reload OpenCode; for Qoder, uninstall through Extensions → Installed → the plugin → … → Delete. Generated project knowledge, requirements and business code are retained. Review project hook settings separately if you no longer want the host to invoke HelixHarness in that application repository.

## 中文说明

需要 Python 3.10+、Git，以及支持插件的 Codex、Claude Code、Cursor、OpenCode（macOS、2.3.2；见[支持矩阵](opencode-testing.md)）或 Qoder。Cursor 使用上面的 [Cursor 独立安装流程](#cursor-preview)，Qoder 使用上面的 [Qoder 本地 ZIP 安装流程](#qoder)。先确保宿主已完成登录或模型配置，能够正常使用。平台不是安装插件的前置条件。共享运行时的 Windows 支持不代表 Windows Cursor 已完成验收；Cursor 实机验收目前覆盖 macOS。Qoder 的 Host Kit 契约测试已进入 CI 矩阵，但 Qoder App 尚未完成 Windows 实机验收，且只提供 POSIX hook 启动器。

**OpenCode Stable 支持（macOS、2.3.2）：** 当前源码候选的支持范围与边界见[支持矩阵](opencode-testing.md)。OpenCode 2.2.2、2.3.0 仅有自动化兼容覆盖。

插件注册原生 `helix:<command>` 命令、`helix-<skill>` 方法 Skill 和 `helix-<agent>` 角色。命令、原生 `skill` 工具结果、Skill mention 与原生 Skill 激活均按请求会话绑定仓库目录；会话查询或目录校验失败时阻断未绑定的 Skill 正文。2026-11-20 已在 OpenCode 2.3.2 + DeepSeek 上完成一次合成单仓 Init → Propose → Apply → Archive 真实模型闭环，包含操作者确认和一次受控合同修订；2026-11-23 又完成了 OpenCode 2.3.2 + DeepSeek 的操作者辅助合成双仓 Workspace 与独立 UT 验证；后续已修复 UT 分类校验与原子发布保护，并通过无纠正提醒的真实模型复验。[合成 Workspace 的专项真实模型验证](../helixharness/opencode/tests/README.md#recovery-and-auxiliary-workflow-acceptance-2026-11-23)还覆盖了 Apply 与部分 Archive 原生取消后新会话恢复，以及注入原生 context hook 故障后阻断依赖派发。完整生产业务项目、无人干预时的协议遵守与完整 TUI/桌面 UI 验收仍未完成；已覆盖范围见[定向终端 UI 验收](../helixharness/opencode/tests/README.md#targeted-terminal-ui-acceptance-2026-11-23)。Workspace Apply 的 `may_delegate` RepoJob 使用 `helix-worker`，需要 `experimental.subagent_depth >= 2`；独立 reviewer 保持只读 leaf。深度不足时阻塞该 RepoJob，不得绕过评审。

OpenCode 适配使用原生工具和会话 Hook，但未提供等价的 SessionStart/Stop/失败/会话结束业务保证。任务跟踪通过 `scripts/task_plan.py` 原子更新同一份 `tasks.md`。统计在明确授权后记录阶段事实和工作流轨迹，不采集原始会话，token 和未观察到的工具次数保持未知。`/helix:update` 对有明确 Git upstream 的干净源码安装执行既有确认协议，锁定 commit 并仅快进；有本地修改、游离版本或包管理安装时不自动更新。Init 合并 `AGENTS.md`，不创建 `.opencode/` settings；插件在原生 write/edit/patch 成功后执行已有项目巡检，shell 修改仍需显式检查。d-d2c 的 Claude skill-scoped Stop/PreToolUse guard 不执行，采用无 hook / `--bare` 语义，仍须遵守用户决策、视觉验收和完成门禁。

正式发布后，Codex 与 Claude Code 使用上面的 GitHub 安装命令，Cursor 使用市场界面流程，这些市场安装方式无需先手动克隆源码。Qoder 通过 App 安装本地 ZIP：克隆本仓库，在 `helixharness/` 内运行 `python3 scripts/package_qoder_plugin.py` 生成 `dist/helix-<version>.zip`，再由 Qoder 扩展面板或 `qoder-settings.install_extension` 完成安装，之后在 App 内审阅并信任 hooks、新开一个会话；升级须重新打包并安装新版 ZIP，同一会话不会刷新已绑定的 `plugin_root`。OpenCode 的安装已在 2.3.2 验证：按[本地目录安装步骤](#opencode-local-directory-installation-verified-with-232)先克隆本仓库，再将 `helixharness/` 子目录的绝对路径加入项目或全局 `opencode.json(c)` 的 `plugins`；保留已有配置和其他插件，无需 `bun install`，随后重启或重新加载并新开会话。该版本原生 `plugin add` 不接受绝对本地目录或 Git 子目录后缀；其他版本须另行核验。发布前测试或修改插件时，使用[开发者安装（本地源码）](#developer-installation)：先下载或克隆源码；Codex/Claude Code 从**包含两个子项目的根目录**添加本地市场，OpenCode 继续使用上述本地目录配置。根市场指向 `./helixharness`，不会安装平台。独立插件副本仍可添加插件目录本身；同名市场只保留一个来源。

安装后新开会话，确认九个命令可见；Claude Code/Codex/Cursor 检查插件 hooks 已审阅并实际执行，Qoder 在扩展面板审阅插件 hooks，OpenCode 检查命令展开后的当前仓库/会话 Host Context 绑定，再在业务仓库运行 `/helix:init`（Cursor 为 `/helix-init`，Qoder 亦为 `/helix:init`，来源是 `hosts/qoder/skills/` 薄入口）并检查项目文档及宿主支持的项目 hooks（OpenCode 不生成项目 hooks）。单仓 Init 还需检查非空的 `docs/harness/index.md`、`docs/knowledge/index.md`、`docs/knowledge/project-context.md` 及对应项目类型要求的其他产物，不能只看“已完成”回复。若出现 `plugin_source_mismatch`，先核对市场来源与当前安装插件，选定正确来源并新开会话，避免混用其他 checkout 或缓存版本。Claude Code/Codex 更新使用 `/helix:update`；本地来源需先自行更新源码，不能把版本识别成功当作已完成跨版本升级。Cursor 在 macOS/Linux 上使用 `/helix-update` 升级已登记的 Helix 托管本地安装，复用检查、确认与一次性授权协议；手工副本和原生市场安装仍通过原安装方式更新。Qoder 的 `plugin_update` 声明为 `unsupported`，`/helix:update` 返回阻断，升级须重新打包并安装新版 ZIP；重跑 `/helix:init` 时 Qoder 的 Host Kit 会按 `--linter` 参数与脚本路径识别自家 handler，自动收敛旧版本目录与不同路径写法留下的重复条目。

OpenCode 的本仓库安装由用户自行更新源码并重启或重新加载；回退时在源码副本切换到此前批准的版本，不能用原生 `plugin update` 替代。卸载只移除 `plugins` 中本插件的目录条目，不删除业务仓库的知识、需求或代码。固定版本与回退示例见 [OpenCode 安装说明](../helixharness/hosts/opencode/installer.md#更新回退与卸载)。

**从空项目开始：**在 Git 仓库根目录运行 `/helix:init`（Cursor 为 `/helix-init`）。新目录请先自行执行 `git init`，不需要首次提交。空仓库或纯文档仓库跳过代码扫描，生成最小项目上下文、约束、需求目录与宿主支持的 hooks（OpenCode 只合并 `AGENTS.md`），未确认的信息标记 TODO。再用 `/helix:propose` 和 `/helix:apply`（Cursor 为 `/helix-propose` 和 `/helix-apply`）开发第一个需求。Init 和 Apply 不创建提交。

## Windows local source / Windows 本地源码

From the combined repository root in PowerShell:

```powershell
$env:HELIXHARNESS_SOURCE = (Get-Location).Path
codex plugin marketplace add "$env:HELIXHARNESS_SOURCE"
codex plugin add helix@helixharness-marketplace
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\helixharness\scripts\runtime_preflight.ps1 check
```

Windows 不需要 WSL。Codex 自动 Hook 使用 `commandWindows`；Claude Code 使用 Git for Windows 的 Bash，若无法自动发现，可设置 `$env:CLAUDE_CODE_GIT_BASH_PATH = 'C:\Program Files\Git\bin\bash.exe'` 后重启宿主。不要把 Windows 的 WSL 启动器当作 Git Bash。详细步骤与验证边界见[跨平台说明](platform-support.md)。
