# HelixHarness 使用统计协议

本文件定义当前工作流的统计授权与职责边界；异步统计完整约定见 `reference/eval-supplement-protocol.md`。公共账本唯一写入 Schema 为 `helixharness-eval-ledger/1.0`，授权缓存的 `1.0` 是独立格式，不是账本版本。账本必须同时满足 `schema=helixharness-eval-ledger/1.0`、`schema_version=1.0` 和完整字段约束；不能仅凭数字 `1.0` 将旧 Legacy 日志认作当前账本。此前的 `2.0` 账本保持原文件，当前版本不自动迁移。

## 1. 目标与职责边界

- 阶段通过 `scripts/metrics.py` 写入调用方已知的固定事实；不读取会话、不调用模型，业务不等待采集或网络。
- 异步 Runtime 经授权窗口、DLP 和 Archive 封口约束生成 Supplement。helixharness-platform 接收并关联统计；需先启动平台并配置地址。
- 授权入口为 `scripts/telemetry_consent.py`；recorder/Runtime 通过 `scripts/telemetry_consent_state.py` 读取同一授权缓存及持有 lease。
- 授权查询异常、未授权或宿主不支持时跳过统计，仍按真实业务门禁执行。不得估算缺失数据、编造 entry 或自动提交推送。

## 2. 数据统计开关与提醒缓存

Usage telemetry 只有 `enabled` 和 `disabled` 两档，默认 `disabled`。

| 模式 | 行为 |
|---|---|
| `enabled` | 允许需求级命令写 `docs/eval/{feature}/eval-log.json`，并提醒用户随业务代码提交。 |
| `disabled` | 跳过阶段 start/finish 与采集，不创建或更新 `docs/eval/**`，业务流程继续。 |

当前已支持宿主的缓存路径：

```text
Claude Code: ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/helixharness/telemetry-consent-cache.json
Codex: ${CODEX_HOME:-$HOME/.codex}/helixharness/telemetry-consent-cache.json
```

用户主动查询和修改配置使用 `/helix:config`，通过 `scripts/user_config.py` 复用本协议 API；默认查看不触发 CLI resolve 的清理副作用，显式修改仍由既有授权 API 管理 scope、epoch 和清理。

缓存必须由 plugin 捆绑的 `scripts/telemetry_consent.py` 读写，命令或 Agent 不得自行构造、修改 JSON，也不得自行猜测项目指纹。调用方式：

```text
python3 {plugin_root}/scripts/telemetry_consent.py --repo-root {repo_root} --platform {claude-code|codex} resolve
python3 {plugin_root}/scripts/telemetry_consent.py --repo-root {repo_root} --platform {claude-code|codex} set --scope {project|user} --mode disabled
python3 {plugin_root}/scripts/telemetry_consent.py --repo-root {repo_root} --platform {claude-code|codex} set --scope {project|user} --mode enabled --source {interactive-user|explicit-command} --user-confirmed
python3 {plugin_root}/scripts/telemetry_consent.py --repo-root {repo_root} --platform {claude-code|codex} choose --action {allowed_action.id} --user-confirmed
```

授权与异步地址分别判断：`telemetry_consent.py resolve` 正常按授权缓存和提醒周期返回 `prompt_due` 与 `allowed_actions`，没有地址或地址无效也不抑制开启/续期问询。地址诊断与 Runtime 共用 `telemetry_async.endpoint_status`：优先读取 `${HELIXHARNESS_TELEMETRY_HOME:-~/.helixharness/telemetry}/config.json` 的非空 `endpoint`，否则使用 `scripts/reporting_config.py` 的 `DEFAULT_REPORT_ENDPOINT` 发布默认值（当前为空）；地址环境变量不再参与解析，只校验配置，不探测服务在线状态。未配置时附带 `reason=endpoint_unconfigured`；地址或文件无效时附带 `reason=endpoint_invalid` 和 `WARN:telemetry_endpoint_invalid`。这些字段仅说明异步通路不可用，不能作为跳过授权交互的条件。`resolve` 不改写授权缓存和提醒时间，用户确认后由 `choose` 正常更新。 `endpoint_unconfigured` 仅供内部诊断，正常授权交互、选择结果和阶段摘要不显示地址缺失或未异步上报提示，也不主动提醒配置；仅在用户主动查询或排障时解释。地址无效的 warning 保留。

用户选择 enabled 后，同步账本正常记录，平台可本地读取或经 Git 同步获取；没有有效地址时只跳过异步采集和投递。有有效地址且授权 enabled 时，Runtime 才能采集及投递。未授权或 disabled 时，同步账本不新增记录，异步不采集、不投递；历史账本和平台数据不自动删除。配置地址不代表授权，账本 `completed` 不代表异步送达。

`resolve` 输出 `effective_mode`、`effective_scope`、`state`、`prompt_due`、`prompt_kind`、有序 `allowed_actions[]`、`last_prompted_at`、`next_prompt_after` 和匿名 `project_fingerprint`。每个 action 包含固定的 `id/label/description/scope/mode/recommended`；交互工具必须原样使用 label 和 description。脚本失败时安全降级为 `disabled`，输出 `WARN:telemetry_consent_cache_unavailable`，不得手工覆盖损坏缓存。

POSIX 上 `resolve`/snapshot 对既有 lock 使用共享只读 lease；全新或 cache 存在但 lock 缺失时，改在宿主配置目录的稳定既有父目录取得共享 lease，不为一次读取创建全局文件。首个 `set/choose` 在同一目录 lease 上排他串行，创建 lock 后再原子更新 cache；调用方的当前 repo 隐私 barrier 始终包含在该 lease 内，不能与首次开启交叉清理新 epoch。

对未注册宿主，`resolve` 不做缓存 I/O，固定返回 `effective_scope=unsupported_host`、`state=unsupported-host`、`reason=unsupported_host`、`prompt_due=false` 和空 `allowed_actions`。调用方不得展示 consent 提示，也不得调用 `set/choose`。

精确缓存 schema：

```json
{
  "schema_version": "1.0",
  "updated_at": "2026-07-14T03:24:48Z",
  "user": {
    "mode": "enabled",
    "consent_source": "interactive-user",
    "consent_epoch": "cep_0123456789abcdef01234567",
    "enabled_at": "2026-07-14T03:24:48Z",
    "last_prompted_at": "2026-07-14T03:24:48Z",
    "next_prompt_after": "2027-01-10T03:24:48Z"
  },
  "projects": {
    "prj_0123456789abcdef": {
      "mode": "disabled",
      "disabled_at": "2026-07-14T03:24:48Z",
      "last_prompted_at": "2026-07-14T03:24:48Z",
      "next_prompt_after": "2026-08-13T03:24:48Z"
    }
  }
}
```

未配置 user scope 时 `user={}`，不得写 `mode=unset|unknown`。每个 scope 的 `mode` 只允许 `enabled|disabled`；开启记录还必须包含由脚本写入的 `consent_source=interactive-user|explicit-command` 和随机 `consent_epoch`。缺失这些当前必需字段或手写的 `enabled` 记录无效，不做迁移。关闭再开启会生成新的 consent epoch，任何 entry 窗口不得跨越关闭区间。缓存顶层只允许 `schema_version/updated_at/user/projects`，不得保存安装 ID、项目 label、原始 remote URL、绝对 repo 路径，也不得重复保存 scope 内的 `last_prompted_at` / `next_prompt_after`。

项目指纹固定为：

```text
prj_ + sha256("helixharness-project-v1\0" + normalized_identity)[:16]
```

- 有 `origin` 时，`normalized_identity` 使用规范化的 `remote:{lowercase_host}/{repo_path}`：去除协议、用户名/密码、query、fragment、首尾 `/` 和结尾 `.git`，SSH/HTTPS 的同一仓库得到相同指纹。
- 无 `origin` 时，在内存中使用 `local:{absolute_repo_root}` 计算哈希；绝对路径不写入缓存。
- 指纹算法版本通过 `helixharness-project-v1` 固定，不得借用 session ID 的匿名化规则临时推断。

示例：`git@Git.Example.com:Payments/Loan.git` 与 `https://user:secret@git.example.com:443/Payments/Loan.git?ref=main#readme` 都规范化为 `remote:git.example.com/Payments/Loan`，并得到同一个 `prj_728f28769d11c234`。若没有 origin，本地路径只参与内存哈希，cache 仍只保存 `prj_*`。

关闭后 30 天再次提醒；开启后 180 天确认是否继续保持当前范围开启。提醒时间只由脚本计算。

解析优先级：

1. `user.mode=disabled`
2. 当前项目 `mode=enabled|disabled`
3. `user.mode=enabled`
4. 无配置：默认 `disabled`

人维度关闭时，项目开启不得覆盖。项目级开启请求必须输出：

```text
WARN:telemetry_user_scope_disabled
```


### 入口与交互

- 单仓 Init 在展示开启选项前展示下方固定说明；Workspace Init 不处理子仓授权，不创建根 Ledger。
- Propose 的用户直接入口、`propose --loop` 实际进入 Propose，或 Bug Fix 因契约修订进入 Propose 时，只有 `prompt_due=true` 才展示说明及有序 `allowed_actions[]`。有效缓存直接复用。
- Apply/Archive 只读取 resolver 返回的 `effective_mode`，不因无缓存或提醒到期补问、不调用 choose。Loop/Bug Fix 从 Apply/Archive 续跑也不补问；已有有效 enabled 继续有效，disabled 继续关闭。
- 其他自动 Agent 调用不代替用户确认，不弹授权/安装交互；未注册宿主不询问。
- 统计授权单选使用宿主支持的结构化单选能力；只有宿主不支持时才用编号文本。逐项保留 resolver 的 label、description 和顺序，调用 `choose --action {id} --user-confirmed`，不得猜测 scope/mode。推荐或默认选项不是授权。此要求不扩展到业务需求交互。
- Python 不可用时遵循 `reference/runtime-preflight.md`，暂不展示统计问询，也不手写缓存。恢复运行依赖并复检后重新调用 resolve，按返回的授权状态和提醒周期决定是否询问，地址诊断不阻止问询；安装 Python 不代表授权统计。
- Init 只处理授权，不创建统计 Entry，不调用统计跟踪准备。后续 recorder 在 enabled 时自行准备 Ledger 的 Git 跟踪；这不包含 commit 或 push。

### 固定数据说明

支持宿主上，展示统计开启或提醒选项前必须逐字展示以下说明。`commands/init.md` 保留同步副本，由契约测试校验一致；自动调用和未注册宿主不展示。

<!-- helixharness-eval-data-notice-v1:start -->
```text
数据统计说明：默认关闭。开启后，HelixHarness 在 docs/eval/{feature}/eval-log.json 中记录当前 Git user.name（可用时）、工作流阶段、时间、状态、工件、质量和代码范围等固定事实；该账本可随业务代码提交，不保存原始会话 ID、对话、Token 或工具原始输入输出。
阶段记录本身不读取对话或调用模型。阶段结束后，异步 Runtime 按已授权的精确会话窗口采集 Token、工具及子任务统计和经过筛选脱敏的用户输入、助手回复；不上传 Thinking、原始源码、完整 Diff、附件或原始日志。Archive 冻结采集边界，后台仅处理冻结范围和待投数据，不同步调用会话 classifier。
无论是否配置异步地址，都按授权缓存和提醒周期正常询问统计授权。开启后同步记录本地账本，由平台本地读取或 Git 同步获取；异步采集和投递另需先启动 helixharness-platform，并使用发布脚本 reporting_config.py 中的有效默认地址，或统计私有目录 config.json 的 endpoint 覆盖地址。未配置或地址无效时只跳过异步采集和投递；未授权或关闭时不新增同步账本，也不异步采集或投递。统计和网络异常不阻塞研发。授权允许本机私有采集及向配置地址上报，不等于授权 Git commit 或 push；提交和推送仍由当前命令单独取得授权。关闭后停止新增采集，并由后续命令、Hook 或 Worker 清理未发送私有证据；已提交的历史账本不改写。
```
<!-- helixharness-eval-data-notice-v1:end -->

## 3. 阶段生命周期

遵循 `reference/telemetry-command-protocol.md`。只有 start 返回 started 时保存真实 entry_id；skipped 或统计异常不要求 entry_id，不阻断业务。等待用户回答保持同一阶段，不 finish 或重复 start。归档采集边界、投递和独立提交授权见 `reference/eval-supplement-protocol.md`。
