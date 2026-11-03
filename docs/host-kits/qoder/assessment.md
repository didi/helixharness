# Qoder Host Kit 能力摸底

> 本文是 `support_qoder` 分支的第一份产出：**只定级，不接代码**。
> 所有结论来自本机实测，不是凭文档推测。复现命令见每节末尾。

## 0. 版本与结论速览

| 项 | 值 |
|---|---|
| 被测产品 | Qoder CN（中文版），CLI `qoderclicn` |
| CLI 版本 | `1.1.75-hotfix.1`（session 记录中的 `version` 字段） |
| 应用 | `/Applications/Qoder CN.app`，用户配置 `~/.qoder-cn` |
| 结论 | **可以接入，且成本显著低于 Codex**。Qoder 与 Claude Code 在插件、命令、子代理、hooks、会话格式上高度同构 |
| 风险点 | 集中在 `unit_test_workflow` 的权限托管、项目级 settings 路径、telemetry 会话格式差异 |
| 建议首批能力 | 16 项中 **12 项 native / 1 项 emulated / 3 项 unsupported** |

> ⚠️ 修正：前几轮对话中我口述为「17 项 capabilities」，实际两个现有 Host Kit 的**并集是 16 项**。以本文为准。

## 1. 关键发现：Qoder 的插件模型是 Claude Code 的超集

`~/.qoder-cn/plugins/cache/local/ecc/2.2.3/.qoder-plugin/plugin.json`（第三方真实插件）：

```json
{
  "name": "ecc",
  "displayName": "ECC",
  "version": "2.2.3",
  "keywords": ["qoder", "agents", "skills", "tdd", "code-review"],
  "skills": "./skills/",
  "commands": "./commands/",
  "mcpServers": "./.mcp.json",
  "hooks": "./hooks/qoder-hooks.json"
}
```

对比 HelixHarness 自己的 `helixharness/.claude-plugin/plugin.json`：

```json
{ "name": "helix", "version": "1.0.0", "hooks": "./hosts/claude-code/hooks.json" }
```

**差异只有两处**：Qoder 用 `.qoder-plugin/` 目录名，并且要求把 `commands` / `skills` **显式声明**成路径（Claude Code 靠目录约定隐式发现）。这意味着 HelixHarness 的 `commands/*.md`、`skills/*/SKILL.md`、`agents/*.md` **不需要改格式**。

复现：

```bash
cat ~/.qoder-cn/plugins/cache/local/ecc/2.2.3/.qoder-plugin/plugin.json
cat ~/.qoder-cn/plugins/cache/local/ecc/2.2.3/hooks/qoder-hooks.json
```

## 2. 十六项能力定级

| # | capability | 定级 | 证据 | 需要的 adapter |
|---|---|---|---|---|
| 1 | `command_surface` | **native**（实测修正） | **命令面由 skill 承载**：`/echo` 这类由 `skills/{name}/SKILL.md` 提供的入口可被直接调用并执行。`commands/*.md` 在 `plugins validate` 中被计入，但**实测未暴露为可调用入口**（`/hvprobe:echo` 与第三方 `/ecc:aside` 均失败）。因此应采用 **Codex 同款薄 Skill 投影**，而非 Claude Code 的隐式命令发现 | `command_projection.json` + 宿主薄 Skill |
| 2 | `skill_discovery` | **native** | `qoderclicn skills list` 正常列出插件与用户技能；`SKILL.md` frontmatter 为 `name` + `description`，与 canonical 一致 | — |
| 3 | `session_start` | **native** | `hooks.json` 的 `SessionStart`；插件内 hooks 支持 `matcher` / `async` / `timeout` / `statusMessage` | `lifecycle.md` |
| 4 | `session_end` | **native** | 同上，`SessionEnd` 事件存在 | `lifecycle.md` |
| 5 | `session_events` | **native** | 18 个 hook 事件：`PreToolUse` `PostToolUse` `PostToolUseFailure` `UserPromptSubmit` `SessionStart` `SessionEnd` `Stop` `SubagentStart` `SubagentStop` `PreCompact` `PostCompact` `CwdChanged` `InstructionsLoaded` `FileChanged` `PermissionRequest` `PermissionDenied` `WorktreeCreate` `WorktreeRemove` | `hooks.json` |
| 6 | `stop_hook` | **unsupported** | 宿主**有** `Stop` 事件，但共享生命周期明确「不注册全局 Stop 或 Failure 业务钩子」，阶段终态由命令显式记账。与 `claude-code` 保持同一取值 | — |
| 7 | `failure_hook` | **unsupported** | 同上（`PostToolUseFailure` 存在但按合同不使用） | — |
| 8 | `command_lifecycle` | **emulated** | 阶段 start / trace / finish 由 `scripts/metrics.py` 在命令内显式调用，不依赖宿主生命周期；与 `claude-code` 一致 | — |
| 9 | `workflow_transition` | **native** | 斜杠命令可原生调用 | `workflow-transition.md` |
| 10 | `subagents` | **native** | `qoderclicn agents list` → **79 个**活动 agent，插件作用域命名 `ecc:a11y-architect · Sonnet`；`agents/*.md` frontmatter 为 `name` / `description` / `tools` / `model` / `color`，与 Claude Code 完全一致；`Agent` 工具、`subagent_type`、`run_in_background` 均在运行时存在 | `subagent.md` |
| 11 | `unit_test_workflow` | **native** | 后台 Agent（`run_in_background`）+ `TaskCreate/TaskGet/TaskUpdate/TaskList` 任务跟踪齐备 | `ut.md` |
| 12 | `task_tracking` | **native** | 原生 `TaskCreate/TaskGet/TaskUpdate/TaskList/UpdateGoal`；`QODER_FEATURE_TASKS=0` 时回退 `TodoWrite` | — |
| 13 | `project_hooks` | **native** | `--setting-sources user,project,local` + 项目配置目录 `.qoder`；`settings.local.json` 字面量存在 | `project-hooks.json`、`project.json` |
| 14 | `telemetry` | **unsupported**（v1） | 会话存储 `~/.qoder-cn/projects/{escaped-cwd}/{sessionId}.jsonl`，记录结构与 Claude Code 高度相似（`type`/`uuid`/`timestamp`/`message`/`parentUuid`/`isSidechain`/`cwd`/`sessionId`/`gitBranch`），但含 Qoder 专有类型 `workspace-directories`、`runtime-config`。可写 `telemetry_hosts/qoder.py` 升级为 emulated，但**不阻塞业务** | 首版省略；升级时补 |
| 15 | `plugin_install` | **native** | `qoderclicn plugins install`（支持 marketplace、远程 zip、本地目录）；`qoderclicn plugins marketplace` 管理市场 | `installer.md`、`.qoder-plugin/plugin.json` |
| 16 | `plugin_update` | **native** | `qoderclicn plugins update`、`qoderclicn update`、`qoderclicn rollback` | `update_hosts/qoder.py` |

### 关于 `telemetry: unsupported` 的实际影响

按现有合同，未注册统计 Adapter 的宿主**两条通路都不产生数据**：`scripts/metrics.py` 在
`get_host_adapter(platform) is None` 时直接 `_skip("unsupported_host")`，因此既**不写**
`docs/eval/{feature}/eval-log.json` 同步账本，也不生成异步 Supplement。

也就是说 Qoder 目前对 `helixharness-platform` **完全不可见**：没有阶段进度、Token 用量、
耗时、返工等任何数据。业务功能与质量门禁不受影响，这是有意的降级边界。
升级路径明确：在 `scripts/telemetry_hosts/` 实现并注册一个 `HostAdapter`，不需要改动业务正文。

## 3. 与现有两个 Host Kit 的差异清单

| 维度 | Claude Code | Codex | Qoder | 对 Host Kit 的影响 |
|---|---|---|---|---|
| 分发清单 | `.claude-plugin/plugin.json` | `.codex-plugin/plugin.json` | **`.qoder-plugin/plugin.json`** | 新增三字段 `commands` / `skills` / `mcpServers` |
| 插件根变量 | `${CLAUDE_PLUGIN_ROOT}` | `${PLUGIN_ROOT}` | **`${QODER_PLUGIN_ROOT}`** | hooks 与脚本路径改写 |
| 命令入口 | `commands/` 隐式发现 | 薄 Skill 投影 | **显式 `commands` 字段** | 比 Codex **简单得多**，无需 `codex_entry_load.py` 那套零发现加载 |
| 项目入口文件 | `CLAUDE.md` | `AGENTS.md` | **`AGENTS.md`** | 复用 Codex 的模板选择 |
| 项目配置目录 | `.claude/` | `.codex/` | **`.qoder/`** | `project.json` 改写 |
| 用户配置根 | `~/.claude` | `~/.codex` | **`~/.qoder-cn`（CN）/ `~/.qoder`（国际版）** | version/update adapter 需处理双根 |
| hooks 命令写法 | `{"command": "sh ..."}` | 同 | **`{"command": "node", "args": [...]}` 或 `{"command": "sh ..."}`** | 两种都支持，可沿用 sh 形式 |
| 子代理定义 | `agents/*.md` + 专用 `subagent_type` | 通用类型 + prompt_source | **`agents/*.md`**，与 Claude 一致 | `subagent.md` 可大量复用 Claude 版 |
| 会话格式 | `~/.claude/projects/*/*.jsonl` | rollout | **`~/.qoder-cn/projects/*/*.jsonl`**，结构相近 | telemetry 后续可低成本升级 |
| settings 兼容 | — | — | **官方做 Claude → Qoder 兼容转换**（`permissions.defaultMode` → `general.defaultPermissionMode`，`disableBypassPermissionsMode` → `security.disableYoloMode`） | 现有 `.claude/settings.json` 写法基本可平移 |
| hooks 迁移 | — | — | **`qoderclicn hooks migrate --from-claude`** | 官方提供迁移命令，可参考其映射规则 |

### 结论：`hosts/qoder/` 的起点应该是复制 `hosts/claude-code/`，不是 `hosts/codex/`

Codex 的复杂度来自「没有原生命令面」，必须自建薄 Skill + 零发现加载 + 分页协议。Qoder **有原生命令面**，这部分完全不需要。

## 4. 仍需 E2E 才能确认的三件事

按 `reference/host-api.md` 的要求，这三项不能靠推断定级，必须实测后再定：

1. **项目级 hooks 的精确落盘路径**：`--setting-sources project` 对应 `.qoder/settings.json` 还是 `.qoder/settings.local.json`？影响 `project.json.settings_file` 与 `project-hooks.json`。
2. **`unit_test_workflow` 的权限托管**：`/helix:ut` 需要「准备临时权限 → 并发 worker → 精确回滚」。Qoder 的 `permissions.allow/ask/deny` 结构是否支持按条目精确回滚（`claude-code/ut.md` 依赖 `claude_project_settings_exact_revert`）。
3. ~~**插件作用域命令的调用写法**~~ → **已实测确认**：见 §7，命令面由 skill 承载，入口写 `/echo` 这类平铺名；子代理用 `hvprobe:scout` 作用域名，`scout` 平铺名也可用。

## 5. 工作量估算

| 阶段 | 内容 | 量级 |
|---|---|---|
| **P0** | `hosts/qoder/` 目录骨架：`host.json`（已出草案）、`command-projection.json`、`project.json`、`project-hooks.json`、`runtime.md`、`subagent.md`；薄 Skill 入口复用 `hosts/codex/skills/` 写法 | 1 天（大量复用 claude-code + codex 薄 Skill） |
| **P0** | `.qoder-plugin/plugin.json` + `scripts/update_hosts/qoder.py` | 0.5 天 |
| **P1** | `hooks.json` + `hook_entry.py` + `lifecycle.md` + `installer.md` | 1 天 |
| **P1** | `ut.md`（权限托管，最不确定） | 1–2 天 |
| **P2** | `telemetry_hosts/qoder.py`（可延后，先 unsupported） | 1 天 |
| **P2** | `scripts/tests/` 专项测试 + 文档中英同步 | 1 天 |
| **P3** | 真实客户端 E2E（安装 → 新会话发现 → 四阶段闭环 → 归档） | 1 天 |

**总计约 6–8 人日**，明显低于 Codex 适配（因 Codex 需要自建命令面与零发现加载）。

## 6. 建议的落地顺序

```
① ✅ 已完成（2026-11-02）：command_surface × subagents 垂直验证全部通过
   命令面走 skill 承载，子代理支持作用域命名与并行派发

② 补齐 host.json 全部 16 项 + 目录骨架（薄 Skill 入口复用 codex 写法）

③ 跑通 propose → apply → archive 单仓闭环

④ 补 ut 的权限托管验证 / telemetry / 文档
```

## 7. 垂直验证结果（已实测，2026-11-02）

探针插件：`/tmp/hv-probe`，结构 = `.qoder-plugin/plugin.json` + `commands/echo.md` + `agents/scout.md` + `skills/{probe,echo}/SKILL.md` + `hooks/hooks.json`。

`qoderclicn plugins validate` 输出：

```
Components loaded successfully:
  - skills (2)
  - agents (1)
  - commands (1)
  - hooks (2)
Plugin "hvprobe" is valid and ready to install.
```

| 测试 | 结论 | 实测证据 |
|---|---|---|
| F0 插件可加载 | ✅ | `plugins validate` 通过，四类组件全部识别 |
| F1 命令面（`commands/*.md`） | ❌ **不可用** | `/hvprobe:echo` → `Error: Activate skill hvprobe:echo?`；第三方 `/ecc:aside` → "not available in the current skill listing" |
| F2 命令面（skill 承载） | ✅ | 新增 `skills/echo/SKILL.md` 后调用 `/echo` 返回 `PROBE_COMMAND_OK` |
| F3 子代理派发 | ✅ | 派发 `scout` 返回 `PROBE_SUBAGENT_OK cwd=/tmp/hv-probe` |
| F4 插件作用域命名 | ✅ | 派发 `hvprobe:scout` 同样成功 |
| F5 并行派发 | ✅ | 单消息 3 个 Agent 调用，3 条结果全部返回 |
| F6 插件 hooks | ✅ | `hook.log` 记录到 `PROBE_SESSIONSTART_OK`（5 次）与 `PROBE_PRETOOL_OK`，证明 `SessionStart` 与 `PreToolUse(Bash)` matcher 均生效 |
| F7 内置通用子代理 | ✅ | 会话内可见 `Explore`、`Plan`、`general-purpose` |
| F8 插件子代理清单 | ✅ | `ecc:a11y-architect` 等 12+ 个第三方插件 agent 全部可派发 |

可调用命令实测清单（未装探针插件的会话）：

```
/loop /init /better-harness /update-codemaps /update-docs /hookify /security-scan /workflows
```

这些都是 **skill 派生**的平铺名，没有 `plugin:command` 形式。

### 对 Host Kit 设计的三个直接结论

1. **`command_projection.json` 必须采用薄 Skill 投影**，与 `hosts/codex/` 同构；但**不需要** Codex 那套零发现加载/分页协议 —— 薄 Skill 里直接 `read_file` 读取 `commands/{command}.md` 即可，Qoder 没有「资源发现受限」的问题。
   - 好消息：`hosts/codex/skills/{name}/SKILL.md` 的写法可直接复用，改一下 `plugin_root` 推导层级即可。
2. **子代理链路完全可用**，`init` 的并行扫描 DAG、`code-review` 的双轴隔离 Review 都能成立。`general-purpose` 内置类型存在，满足 `subagent.md` 现有要求。
3. **hooks 可用且写法兼容**：`{"command": "sh", "args": [...]}` 形式生效，`matcher` 正则生效。`project-hooks.json` 的 payload 可沿用 Claude 版。

### 剩下的唯一实质风险

`unit_test_workflow` 的**权限精确回滚**（`/helix:ut` 需要准备临时 permissions 再逐条撤销）仍未验证。其余 11 项 native 已有直接证据。

## 附：本次摸排用到的命令

```bash
# 插件与市场
qoderclicn plugins list
qoderclicn plugins --help
cat ~/.qoder-cn/plugins/installed_plugins.json
cat ~/.qoder-cn/plugins/known_marketplaces.json

# 子代理与技能
qoderclicn agents list
qoderclicn skills list
head -10 <plugin>/agents/<name>.md

# hooks 迁移能力
qoderclicn hooks migrate --help    # --from-claude

# 运行时能力（app.asar 字符串）
strings -a "/Applications/Qoder CN.app/Contents/Resources/app.asar" | grep -oE 'PreToolUse|PostToolUse|SubagentStop|...'

# 会话格式
head -2 ~/.qoder-cn/projects/*/*.jsonl
```

## 8. B：骨架落地结果（2026-11-03）

垂直验证通过后已完成 Host Kit 骨架，可被 Registry 识别。

### 新增文件

```text
helixharness/
├── .qoder-plugin/
│   ├── plugin.json                 # 分发清单（skills / hooks 显式声明）
│   └── marketplace.json            # 版本权威源（供 update adapter 读取）
├── hosts/qoder/
│   ├── host.json                   # 16 项能力 + 14 个 adapter
│   ├── command-projection.json     # surface=skill，9 条入口 → commands/{cmd}.md
│   ├── project.json                # AGENTS.md + .qoder/settings.json
│   ├── project-hooks.json          # owner=parent, json_append_dedup
│   ├── hooks.json                  # SessionStart / SessionEnd
│   ├── hook_entry.py               # 会话事件适配
│   ├── runtime.md                  # 11 个逻辑动作映射
│   ├── subagent.md                 # 子代理派发与 Init 角色映射
│   ├── workflow-transition.md      # inline 模式
│   ├── lifecycle.md  ut.md  installer.md  skill-entry.md
│   └── skills/{9 个命令}/SKILL.md   # 薄 Skill 入口
└── scripts/update_hosts/qoder.py   # version / update / detector
```

### 验收结果

| 检查 | 结果 |
|---|---|
| `host_runtime_cli.py list` | `["claude-code", "codex", "qoder"]` |
| `host_runtime_cli.py resolve --platform qoder` | 返回完整 Host Context，14 个 adapter 全部解析成功 |
| `qoderclicn plugins validate <plugin>` | **valid and ready to install**；skills 9 / agents 16 / commands 9 / hooks 2 |
| update adapter 冒烟 | `plugin_version`=1.0.0；`state_home`=`~/.qoder-cn`；detector 不被 Claude 环境变量误判 |
| 插件测试套件 | **364 passed**（一个依赖 `ps` 的用例在沙箱下无法运行，完整权限下通过；已在干净基线确认为环境因素） |

### 一处必要的共享改动

`qoderclicn plugins validate` 揭示 Qoder 会**自动发现插件根 `agents/` 目录，并要求 frontmatter 含 `name`**。
项目的 16 个 `agents/*.md` 原本只有 `description`，因此为每个文件补上 `name: <文件名>`。
该字段对 Claude Code / Codex 均无副作用，且与 `subagent.md` 中 `helix:scan-tech`
等角色映射一致。

同时更新了两处既有测试：`test_plugin_hooks.py` 增加 qoder 分支并支持 hook 的 argv 形式；
`test_public_workflow_router.py` 的宿主集合断言加入 `qoder`。

### 已知未完成项

1. 真实客户端 E2E 尚未执行，`support.level` 保持 `preview`、`evidence` 为 `null`。
2. `.qoder/settings.json` 作为项目级 hooks 目标路径待证实。
3. `/helix:ut` 的权限精确回滚未验证（`ut.md` 中已标注 `_pending_verification` 并要求无法确认时改走宿主审批）。
4. `telemetry` 为 `unsupported`：既不写 eval-log 同步账本，也不做异步上报，平台看不到 Qoder 用量；业务不受影响。

## 9. C：真实客户端 E2E 结果（2026-11-03）

在 Qoder CN（`qoderclicn` 1.1.81）中以本地插件目录加载，跑通单仓完整闭环。
业务仓为 `examples/starter` 的副本，需求为 `task-filter`。

| 步骤 | 结果 | 关键事实 |
|---|---|---|
| 插件加载 | ✅ | `plugins validate`：skills 9 / agents 16 / commands 9 / hooks 2 |
| Host Context | ✅ | `resolve --platform qoder` 解析 14 个 adapter |
| 命令面 | ✅ | `/helix:status` 经薄 Skill 加载 `commands/status.md` 并执行脚本 |
| **init** | ✅ | `projectType=unknown`；6 个扫描 Agent 全部产出；`scanStatus` / `bootstrapStatus` 均 completed；生成 21 个 docs 文件 + `AGENTS.md` + `.qoder/settings.json` |
| 项目级 hooks | ✅ | `project_hooks_cli.py --platform qoder check` → `{"status":"pass","settings_file":".qoder/settings.json"}` |
| **propose** | ✅ | `spec.md` / `tasks.md` 生成，`status=ready`，`validate-tasks` 通过 |
| **apply** | ✅ | 10 个测试通过；**双轴 Review 成立**（Standards APPROVE + 1 ADVISORY、Spec APPROVE）；`verify-report.md` 生成 |
| 独立验收 | ✅ | `examples/verify_feature.py` 输出 `PASS`（该脚本在起始代码上故意失败） |
| **archive** | ✅ | `active → completed` 三工件齐备；Apply Scope 提交 `29e9537` 仅含 `tasks.py` 与 `test_tasks.py`；未推送 |

### 由 E2E 消除的不确定性

- **项目级 hooks 路径已证实**：`.qoder/settings.json`，且能通过确定性 CLI 校验。
- **`subagents: native` 得到实证**：init 的 6 路扫描并行、apply 的双轴隔离 Review 均真实发生。
- **telemetry 降级符合预期**：consent resolver 返回 `unsupported_host`，
  `prompt_due=false`，未写 eval-log，业务门禁不受影响。

### 仍保持 preview 的原因

E2E 走的是 `--plugin-dir` 本地加载，**未覆盖 marketplace 安装与 `plugins update` 流程**。
因此 `support.level` 保持 `preview`，仅把 `support.evidence` 指向
`evidence/host-e2e/qoder/e2e-2026-11-03.json`。

### 安装与升级链路验证（2026-11-03）

用仓库根作为本地 marketplace 源完整跑了一遍发布路径，结束后已还原环境。

| 步骤 | 命令 | 结果 |
|---|---|---|
| 注册市场 | `qoderclicn plugins marketplace add <repo-root>` | 成功，`source={"source":"directory","path":...}` |
| 安装 | `qoderclicn plugins install helix@helixharness-marketplace` | 安装到 `cache/helixharness-marketplace/helix/1.0.0`，`scope=user`、enabled |
| 无 `--plugin-dir` 调用 | `/helix:status` | 正常执行，读取业务仓既有工件 |
| 版本检查 | `version_update.py --platform qoder check --force-refresh` | `update_status=latest`、`next_action=continue_silently` |
| 检测升级 | 版本抬至 1.1.0 并刷新市场 | `update_status=recommended`、`next_action=prompt_update` |
| 执行升级 | `qoderclicn plugins update helix@helixharness-marketplace --scope user` | 升级到 1.1.0，复检为 latest |
| 清理还原 | uninstall + marketplace remove + 快照还原 | 两个状态文件与测试前 **byte-identical** |

### 由此修复的共享核心缺陷

`version_update.py:locate_formal_policy()` 原本把 marketplace 清单路径硬编码为
`.claude-plugin/marketplace.json`：

```python
marketplace = _read_json(root / ".claude-plugin" / "marketplace.json")   # 旧
marketplace = _read_json(root / adapter.marketplace_manifest_relative)  # 新
```

这导致 Qoder 读取错误的清单（版本恒为 1.0.0，升级永远检测不到），并且违反
`host-api.md`「不得按宿主 ID 分支」的约束。现已改为由 Host Kit 声明：

- `UpdateHostAdapter.marketplace_manifest_relative` 默认 `.claude-plugin/marketplace.json`（Claude Code / Codex 不变）
- `QoderUpdateAdapter` 覆写为 `.qoder-plugin/marketplace.json`

这是 E2E 才能暴露的问题：单测与静态检查都不会碰到它。

### 权限机制补充验证（2026-11-03）

`/helix:ut` 依赖的「权限准备 → 使用 → 精确回滚」链路已按机制验证：

| 实验 | 结果 |
|---|---|
| `.qoder/settings.json` 写入 `permissions.deny = ["Bash(echo denied-probe*)"]` | 同一命令被拒绝，错误原文 `Command denied by projectSettings rule: ...` |
| 移除该条目并还原文件 | 同一命令恢复执行 |
| 还原前后 `cmp` | **byte-identical** |

由此确认：`.qoder/settings.json` 是项目级设置文件，`permissions.allow/ask/deny` 会被读取，
且可以精确回滚。`hosts/qoder/ut.md` 已移除 `_pending_verification` 标记。

### 已完成（本轮）

- ✅ `/helix:ut` 权限机制验证（见上表；完整 UT 端到端仍需真实构建工程）
- ✅ 中英入口文档与支持矩阵同步：`README.md`、`README.zh-CN.md`、`docs/installation.md`、
  `docs/user-guide.md`、`docs/user-guide.zh-CN.md`、`docs/workflow-architecture.md`、
  `helixharness/README.md`、`helixharness/PLUGIN_USAGE.md`、`reference/host-api.md`
- ✅ `agents/*.md` 的 `name` 字段固化进 `reference/scan-agent-protocol.md`，并新增回归测试
  `AgentFrontmatterTests` 防止被误删

### 后续待办

1. 在真实 marketplace 源上验证 `plugins marketplace add` / `install` / `update`，
   通过后可把 `support.level` 从 `preview` 提升为 `stable`。
2. 补齐 `telemetry_hosts/qoder.py` 后把 `telemetry` 从 unsupported 升级为 emulated。
3. 在真实构建工程（Java/Maven 或 Gradle）上跑通完整 `/helix:ut`。
