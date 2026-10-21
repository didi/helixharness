# HelixHarness Version Update Protocol


版本策略只接受稳定发布版本，并按 major、minor、patch 数值比较；不接收内部 SNAPSHOT 或预发布版本。
本协议定义 HelixHarness plugin 的版本识别、更新内容展示、确认升级与强制升级规则。

## 目标

- 识别当前用户实际安装的 HelixHarness 版本、运行平台和安装维度。
- 刷新 marketplace 后识别可更新版本。
- 展示当前版本到目标版本之间的更新内容。
- 按版本策略决定是否允许继续使用旧版本。
- 使用当前平台的 plugin 原生命令完成升级，不手工复制 plugin 文件。
- 所有实际升级必须经过用户交互确认和一次性授权；强制升级只决定是否允许继续旧版本，不能代替用户授权安装。

## 宿主支持边界

update 与 eval 分别使用动态 Host Registry，并保持独立 capability、cache 和生命周期。update 是否
可用只看当前 Host Kit 的 `plugin_update=native|emulated|unsupported`；公共协议不维护宿主白名单。
当前内置 Claude Code/Codex adapter 都提供正式更新能力。任何未注册宿主执行 update `check` 时固定返回
`state=unsupported, reason=unsupported_host, next_action=continue_silently`，不读取或写入
版本缓存、不提示更新、不创建 authorization，也不执行任何 plugin 命令。

公共版本策略、缓存、授权、安装 journal 和并发控制不包含宿主分支；宿主差异只存在于
Host Kit 的 update adapter。未来新增宿主必须先实现正式安装识别、marketplace 定位、原生安装
步骤和验证门禁，再声明 `plugin_update=native|emulated`；不得增加共享平台分支。

## 数据来源

### 当前版本

当前版本只能通过统一脚本识别：

```text
python3 {plugin_root}/scripts/version_update.py \
  --platform {canonical_host_id} --repo-root {repo_root} check
```

脚本内部匹配规则：

1. 只匹配完整 ID `helix@helixharness-marketplace` 且 enabled；`helix-local`、其他 marketplace、手工目录全部忽略。
2. Claude Code project 安装仅在 `projectPath` 覆盖当前 repo 时生效；多个匹配时选路径最深者，否则选择唯一 user scope。Codex 只接受唯一 enabled/installed 的正式 plugin，scope 固定为 `default`。
3. Claude Code 当前执行脚本的 plugin root 必须等于 `installPath`，或等于唯一已注册 `source=directory` marketplace 的 manifest 中 `helix.source` 声明的本地路径；该相对路径不得越出 marketplace root。Codex 允许 plugin root 等于 `source.path`，或等于 `${CODEX_HOME:-$HOME/.codex}/plugins/cache/helixharness-marketplace/helix/{selected.version}` 这一精确的宿主管理版本缓存路径。两端版本都必须等于脚本所在的当前宿主 version 权威源（Claude Code 为 `.claude-plugin/marketplace.json` 的 entry version，Codex 为 `.codex-plugin/plugin.json` 的 version）；否则报告 `WARN:helixharness_version_unknown`，不得猜测或跨来源比较。

### 可用版本

当前宿主 adapter 刷新并列出正式 marketplace：

```bash
# Claude Code
claude plugin marketplace update helixharness-marketplace
claude plugin marketplace list --json

# Codex
# 先读取来源；本地来源不执行仅支持 Git 来源的 upgrade。
codex plugin marketplace list --json
# 仅 Git 来源执行：
codex plugin marketplace upgrade helixharness-marketplace --json
codex plugin marketplace list --json
```

脚本从 marketplace list 中找到唯一 `name == "helixharness-marketplace"`，Claude 读取 `installLocation`、Codex 读取 `root`，记为 `{marketplace_root}`。然后读取根 manifest：

- `{marketplace_root}/.claude-plugin/marketplace.json`

从 `plugins[name=helix].source` 动态解析 `{formal_plugin_root}`；source 必须位于 marketplace root 内，再读取：

- `{formal_plugin_root}/reference/version-policy.json`
- `{formal_plugin_root}/{当前宿主 manifest}/plugin.json`

`version-policy.json.latest_version` 是自动版本决策的唯一 latest 来源，并且必须与当前宿主的 version 权威源（Claude Code 的 marketplace entry version、Codex 的 `.codex-plugin/plugin.json` version）一致。任一文件缺失或版本不一致时，本次 policy 不可用；禁止回退到 CHANGELOG、目录名或其他安装源。

### 更新内容

只读取 `reference/version-policy.json.releases[]`，筛选 `current_version < release.version <= latest_version`。这个筛选结果称为“升级路径”。CHANGELOG 只能在结构化 policy 已成功确认 current/latest 后作为人工补充说明，不能生成或改变任何版本结论。

## 版本比较

按 semver 比较 `major.minor.patch`。如果版本中带 `v` 前缀，比较前先去掉。缺失 patch 时按 `0` 补齐。

## 策略

读取 marketplace 或版本策略缓存中的 `reference/version-policy.json`：

- `current_version == latest_version`：无需升级。
- `current_version < latest_version` 且升级路径里存在 `update_type == "required"` 的 release：强制升级。
- `current_version < force_update_below`：强制升级。
- `current_version < minimum_supported_version`：强制升级。
- `current_version < latest_version` 且升级路径里没有 required release：推荐升级。
- 版本无法识别：只提示风险，不强制阻断；除非当前命令明确要求 `--strict`。

### Release 更新类型

每个 release 可以声明：

```json
{
  "version": "1.0.0",
  "update_type": "recommended",
  "title": "版本检查与升级确认"
}
```

`update_type` 取值：

- `required`：只要用户当前版本低于这个 release，并且目标最新版本包含这个 release，就必须升级。
- `recommended`：展示更新内容并建议升级，但用户可以暂不升级。

`update_type` 是唯一更新类型字段。缺失、未知字段或非法 release 会使整份 policy 失效，不允许静默忽略某个 release。

### 跨多版本判断

跨多个版本时，不是只看最新版本是否 required，而是看从当前版本到最新版本的升级路径里是否出现 required release。

示例：

| 当前版本 | 可用版本路径 | 判断 | 结果 |
|----------|--------------|------|------|
| 1.0.0 | 2.0.0 recommended → 3.0.0 required | 路径包含 required | 必须升级到最新版本 |
| 2.0.0 | 3.0.0 required | 路径包含 required | 必须升级到最新版本 |
| 1.0.0 | 2.0.0 recommended | 路径不含 required | 提示可升级，用户可暂不升级 |
| 3.0.0 | 4.0.0 recommended | 路径不含 required | 提示可升级，用户可暂不升级 |

强制升级时，更新内容必须同时展示所有跨过的版本，并标出是哪一个 required release 触发了强制升级。

### 强制升级

强制升级时必须：

1. 展示当前版本、目标版本和强制原因。
2. 展示版本区间更新内容，并标出触发强制升级的 release。
3. 不给“继续使用旧版本”的选项。
4. 如果用户同意，通过 `scripts/version_update.py` 的一次性 `prepare → decide(upgrade_now) → install` 链路执行更新。
5. 如果用户拒绝或升级失败，终止当前 HelixHarness 业务流程。

即使是 required 更新也必须弹出确认框。无交互环境不得自动升级，输出 `BLOCKED:helixharness_update_confirmation_required`。

强制升级完成后按当前 `resources.installer` 的 reload 要求提示：Claude Code 可重启或新开会话；
Codex Desktop 必须完全退出并重新启动，仅新建 task 不足以刷新进程级 plugin catalog。重载前不得
继续本次 HelixHarness 业务流程。

### 推荐升级

推荐升级时必须：

1. 展示当前版本、目标版本和更新内容。
2. 询问用户是否现在升级。
3. 用户选择升级时，通过一次性授权链路执行更新。
4. 用户选择跳过当前版本时，通过确定性脚本保存带 `source=interactive-user` 的 skip 记录，本次继续业务流程；在出现更高版本前，主流程自动检查不再提示该版本。

### 推荐升级跳过缓存

主流程命令遇到 recommended 更新时，必须允许用户按版本跳过，避免跨会话反复打断。

规则：

1. 主流程自动检查调用统一 `version_update.py check`；禁止绕过正式来源检查直接读取缓存。
2. 若 check 返回 `skip_effective=true` 且升级路径不含 required release，静默继续业务流程：不弹交互问题，也不输出 `WARN:helixharness_update_available`。
3. 若没有跳过当前 `latest_version`，必须用交互式问题询问用户现在升级还是跳过当前版本。
4. 用户选择升级时，执行 `/helix:update` 等价的一次性授权流程。升级完成后终止当前业务流程并按 `resources.installer` 提醒重载宿主。
5. 用户选择跳过时，调用 `version_update.py skip --target-version {latest_version} --source interactive-user --user-confirmed`，本次业务流程继续。
6. 当远端 `latest_version` 高于 `skip.version` 时，refresh 清除旧跳过记录，再对新版本交互询问。
7. `required` 更新不受跳过缓存影响；即使相同版本曾作为 recommended 被跳过，只要当前升级路径出现 required release，仍必须阻断直到升级。
8. 用户主动执行 `/helix:update` 或 `/helix:update --check` 时，仍展示当前更新状态；跳过缓存只抑制主流程自动提示，不阻止主动检查或升级。

推荐升级的首次交互提示模板：

```text
检测到 HelixHarness 有推荐更新。

- 当前版本：{current_version}
- 最新版本：{latest_version}
- 更新摘要：{top_release_title_or_summary}

是否现在升级？升级后需要按当前 Host Kit installer 重载宿主，本次 HelixHarness 流程会先暂停。

选项：
1. 现在升级
2. 跳过本次更新（出现新版本前不再提示）
```

## 升级命令

下列平台原生命令只能由 `scripts/version_update.py install` 在消费有效的一次性 authorization 后执行。Commands 和 Agents 禁止直接调用。

Claude Code 根据实际安装 scope 执行：

```bash
claude plugin update helix@helixharness-marketplace --scope user
claude plugin update helix@helixharness-marketplace --scope project
```

如果 user 和 project scope 都启用了 HelixHarness：

- 当前项目命令优先更新 project scope。
- 同时列出 user scope 仍可能影响其他项目。
- 用户传入 `--all` 时，逐个更新所有启用 scope。

## 版本策略缓存

为了让主流程命令能发现“远端发布了强制升级版本”，又避免每次都被网络拖慢，HelixHarness 使用本机缓存保存最近一次远端版本策略检查结果。

缓存路径按当前平台解析：

```text
Claude Code: ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/helixharness/version-check-cache.json
Codex: ${CODEX_HOME:-$HOME/.codex}/helixharness/version-check-cache.json
```

缓存只能由 plugin 捆绑的 `scripts/version_update.py` 读写，禁止命令或 Agent 自行构造、修改 JSON。调用方式：

```text
python3 {plugin_root}/scripts/version_update.py --platform claude-code --repo-root {repo_root} check
python3 {plugin_root}/scripts/version_update.py --platform claude-code resolve
python3 {plugin_root}/scripts/version_update.py --platform claude-code refresh --policy-file {path} --current-version {version} --scope {scope}
python3 {plugin_root}/scripts/version_update.py --platform claude-code skip --target-version {version} --source interactive-user --user-confirmed
```

精确缓存结构：

```json
{
  "schema_version": "1.0",
  "platform": "claude-code",
  "plugin_id": "helix@helixharness-marketplace",
  "checked_at": "2026-07-04T04:00:00Z",
  "marketplace": "helixharness-marketplace",
  "current_version": "0.9.0",
  "current_scope": "project",
  "current_source": "helixharness-marketplace",
  "latest_version": "1.0.0",
  "minimum_supported_version": "0.9.0",
  "force_update_below": "0.9.0",
  "update_status": "recommended",
  "required_reason": null,
  "skip": {
    "version": "1.0.0",
    "skipped_at": "2026-07-04T04:05:00Z",
    "source": "interactive-user"
  },
  "releases": []
}
```

缓存只接受精确 schema 1.0：缺少 required 字段、包含未知字段、重复 JSON key、字段非法或其他 schema version 都直接拒绝。不接收已废弃字段，也不提供旧 schema 迁移。`platform/plugin_id/current_version/current_scope/current_source` 任一项与当前正式安装不同都立即触发刷新，禁止继续使用旧缓存。只有 `skip.version == latest_version`、状态为 recommended 且 `source=interactive-user` 时 skip 才生效。required 更新永远忽略 skip。

`/helix:update` 每次成功读取 marketplace policy 后必须通过脚本刷新缓存。刷新时仅保留仍等于 `latest_version` 的有效 skip；发现更高版本或 required 更新时清除旧 skip。

当前宿主配置目录不可写时，Codex `check` 可使用 `$TMPDIR/helixharness/codex-version-check/`
下按 `CODEX_THREAD_ID + plugin_root` 单向 hash 隔离的 invocation/thread 临时缓存。临时正缓存仍使用上述
精确 schema，并通过正式安装的平台、plugin ID、版本、scope 与 source 复核；网络失败负缓存只保存 host、version、
稳定失败类别和 15 分钟过期时间，不保存原始异常、repo path、用户输入、源码、授权或 skip 决策。
正式 `/helix:update --check --force-refresh` 绕过负缓存。临时缓存不可用于 `skip/prepare/decide/install`，
也不得写进项目仓库；持久目录不可写且需要保存用户 skip 时仍输出
`WARN:version_check_cache_unwritable`，明确该决定不能跨会话生效。

## 主流程快速版本检查模式

独立 init 与 HelixHarness Propose 进入业务前执行快速版本检查；propose --loop 或 bug-fix 实际进入 Propose 时复用同一入口。Apply 和 Archive 不重复版本检查。

1. 上述纳入检查的每次命令和阶段续跑调用 `scripts/version_update.py --platform {platform} --repo-root {repo_root} check`。脚本内部完成正式安装识别、缓存匹配、必要的 marketplace 刷新、动态 source 定位、policy 校验和最终 resolve；命令或 Agent 不得拆开重做。
2. 未注册宿主固定返回 `unsupported_host + continue_silently`；调用方立即继续业务，不得提示版本、访问宿主 home 或尝试其他来源。
3. Claude Code / Codex `check` 都只允许 `helix@helixharness-marketplace` 与同一 `helixharness-marketplace` policy 比较。Claude Code 当前执行路径必须匹配所选 `installPath`；Codex 当前执行路径必须匹配所选 `source.path` 或其精确版本化宿主管理 cache。只存在 local/其他来源、当前执行路径不满足对应宿主规则或版本不一致时返回 `WARN:helixharness_version_unknown`，不得继续寻找替代来源。
4. 缓存缺失、非法、超过 24 小时，或 platform/current version/scope/source 与正式安装不同，脚本刷新 `helixharness-marketplace`。刷新只更新 marketplace snapshot 和版本缓存，不执行 plugin 安装。Codex 从正式 marketplace 的 Git checkout 读取 origin 与当前分支（detached checkout 保持固定提交），在临时目录获取策略快照，读取后删除；不得在 check 中调用会替换已安装缓存的原生 `marketplace upgrade`。
5. policy 路径必须通过 marketplace manifest 的 `plugins[].source` 动态解析；`version-policy.json` 是唯一决策来源。policy 缺失、结构非法或与 manifest 版本不一致时不得读取 CHANGELOG 猜 latest。
6. 若远端刷新失败但存在与当前正式安装完全匹配的持久或 Codex 临时正缓存，使用缓存判断；否则写入
   当前安装绑定的 15 分钟临时负缓存并返回 `continue_with_warning + WARN:helixharness_version_check_unavailable`。
   同一负缓存有效期内后续命令不得重复访问 marketplace；`--force-refresh` 除外。
7. 脚本的 `next_action` 是唯一输出决策依据：
   - `continue_silently`：当前版本为 `latest/current-newer`，或 recommended 更新已有有效 `skip`；静默继续，不弹框、不输出 WARN。
   - `prompt_update`：存在未跳过的 recommended 更新；弹框询问升级或跳过。
   - `block_for_update`：升级路径包含 required release；输出 `BLOCKED:helixharness_update_required`，终止业务流程并提示执行 `/helix:update`。
   - `continue_with_warning`：只原样输出脚本返回的 `warning` 后继续；不得补充 current/latest 推断。
8. 用户选择跳过 recommended 更新后，`version_update.py skip` 返回 `continue_silently`；当前命令与后续命令都静默继续，不输出“已跳过”类 WARN。交互框本身已经明确表达用户选择，无需再制造警告。
9. 纳入检查的入口每次执行 check；Apply 与 Archive 不复述或继承前序版本提示。
10. `update_status=latest|current-newer`、`reason=unsupported_host` 或 `check` 返回有效 `skip_effective=true` 时只能 `continue_silently`。只有当前这次宿主 `check` 确实无法完成正式安装识别、策略检查或缓存写入，且没有可用缓存时，才可原样输出脚本返回的对应 WARN；不得根据历史消息、阶段文本或 Agent 推断生成 WARN。

主动查看完整更新内容或执行升级时使用 `/helix:update`。

## 一次性升级授权

真正执行升级必须按以下顺序：

1. 按正式格式调用 `python3 {plugin_root}/scripts/version_update.py --platform {当前平台} prepare --scope {Claude: user|project|all；Codex: default}`，生成 10 分钟有效的 pending `challenge_id`，并返回正式 `prompt.title/message/allowed_actions`。
2. 交互框原样使用 prompt，不得自行改写、缩减或重排按钮。
3. 用户选择后按正式格式调用 `python3 {plugin_root}/scripts/version_update.py --platform {当前平台} decide --challenge-id {id} --action {upgrade_now|skip_update} --user-confirmed`。`upgrade_now` 将 challenge 标为 authorized；`skip_update` 取消 challenge，recommended 保存 skip，required 返回阻断结果。
4. 仅 `upgrade_now` 可以继续调用 `python3 {plugin_root}/scripts/version_update.py --platform {当前平台} install --challenge-id {id}`。install 校验 platform、scope、目标版本、有效期、实时正式安装和 authorized 状态；随后写入 `installing` journal，逐步执行 adapter 命令。Claude 执行 `plugin update`；Codex 在授权后执行 `plugin marketplace upgrade`，再执行幂等 `plugin add`；每个完成步骤立即原子持久化，全部成功后才消费 authorization，中途失败保留 journal，重试只执行未完成步骤。

禁止跳过任一步骤；禁止 Agent 直接执行平台 plugin 安装命令。pending、过期、目标版本变化、无交互环境或用户取消时均不得安装。`--check` 不得创建 authorization。所有宿主 CLI 都有有界输出和超时；普通状态锁等待上限 2 秒，marketplace 获取的每条 Git/宿主命令上限 20 秒，单个安装步骤上限 60 秒。
