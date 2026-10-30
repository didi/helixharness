---
description: "HelixHarness 初始化入口。默认初始化单服务仓库；传入 --workspace 时初始化多仓 workspace，并生成 workspace registry 与服务关系基线。"
---

> HelixHarness 公共命令唯一契约：本文件同时承载 init 的完整业务步骤和原生命令分发表面。所有宿主都必须通过当前 `resources.command_projection` 加载本文件，并使用预绑定 Host Context；完整读取 `reference/workflow-projection-protocol.md`、`reference/host-runtime-protocol.md`、`reference/tool-semantics.md`、当前 `resources.runtime`、`resources.subagent_protocol`、`resources.project_config`、`resources.project_hooks` 和 `resources.telemetry_protocol`，不得再读取其他 init command/workflow 补充业务语义。

> Init Agent DAG 不变量：本文件中的 Agent tool 拼写按 `reference/tool-semantics.md` 执行。后端 A 组三路并行，B 组保持 `scan-service → scan-boundary → scan-service-meta` 依赖顺序；前端保持六路并行。当前 Host Kit 只能映射派发、等待和物理写入者，不得改变 Agent 数量、依赖、产物或 BLOCKED 传播。


> Telemetry Host Kit 门禁：读取 `reference/telemetry-command-protocol.md` 和 `resources.telemetry_protocol`，只按 capability、生命周期模式和逻辑动作执行；不得根据宿主名称选择宿主私有缓存、stage 或 finish 流程。

> Runtime 硬门禁：在版本检查、consent 或任何 `python3` 调用前，必须读取并执行 `reference/runtime-preflight.md`。Python 不可用时暂不展示统计问询；依赖恢复后由 resolver 解析授权缓存和提醒周期，异步地址不阻止问询，不得因安装 Python 自动开启统计。

> 参数宽容规则：解析 `$ARGUMENTS` 时先按 shell token 分词；每个 token 仅去除尾部常见中英文句读（`, . ; : ! ? ，。；：！？、` 及右侧闭合括号）后，再对 flag 做精确匹配。feature、数值、枚举和路径参数使用同一规范化结果；不得使用任意子串匹配，例如 `--workspace-later` 不能视为 `--workspace`。

## Host Kit 项目配置

Host Context 解析成功后，必须读取 `resources.project_config` 指向的 JSON，并把其中字段作为
本轮 init 的唯一宿主项目配置依据：

- `entry_file`：仓库或 workspace 根入口文件；已有内容只能合并，不能覆盖；
- `entry_template`：生成入口文件时使用的模板；
- `workspace_entry_template`：生成 workspace 根入口文件时使用的模板；
- `settings_file`：需要合并的宿主项目设置；为 `null` 时不得为该宿主虚构设置文件。

实际产物、bootstrap message 和 workspace 门禁必须使用 Host Kit 声明值。
新宿主不得通过修改本命令增加入口文件分支。

## Init Agent/Task Dispatch Contract

### 结构化回执与一次格式恢复

```text
result_reencode_budget: 1
```

该预算仅适用于当前 Host Kit 的 `result_encoding=helixharness_structured_text`；原生 Agent result
保持原样。派发每个 Init job 时，父流程必须传入同一插件内
`hosts/shared/subagent-result-encoding.md` 的绝对路径、准确 job_id 和该文件的 canonical 终态模板，
要求 child 完整读取后按模板返回；不得只要求“报告成功”。A 组还必须按下文返回 evidence index。

先确认原生任务成功终止，并独立校验实际产物和写域；仅当原始 final 已含可验证的业务事实、
错误只在编码/schema 时，允许按编码协议向**同一 Agent** 发起一次无工具、无读写的格式纠正。
原生失败/取消、产物失败或缺失业务事实不适用。保留原始与纠正后回执及真实错误，最多两次校验；
仍不合格时按原门禁停止，不新建 Agent、不重跑扫描、不猜测成功字段。

下列 job 定义 Init 的宿主无关扫描与生成流程。每次派发前必须使用入口已加载的公共子代理
协议与 `resources.subagent_protocol`；扫描 job 还须完整读取 `reference/scan-agent-protocol.md`，并将该路径与 Host Context 一起传给子代理；message 必须带仓库根、
项目类型、`job_id`、完整 `prompt_source`、依赖、读写边界、预期产物和验证。只有
`resources.subagent_protocol` 明确返回子代理能力不可用时，才允许父流程读取同一 prompt 内联一次；
容量暂满、等待中、失败或取消都不得触发内联降级。

派发前先从当前 `resources.subagent_protocol` 确定原生工具及其类型参数；`prompt_source` 是角色正文路径，
不是原生 Agent 类型，只有 Host Kit 可将逻辑 job 映射为当前已注册类型。每个 Init job 固定为
`delegation_policy=leaf`，派发 message 开头必须说明：
“你就是本 job 的执行者，直接读取并执行 prompt_source，不再派发同名或其他子任务。”
父流程为每个 `job_id` 保留一个本轮原生 handle；已有任务仍在运行时只等待，不因尚未生成文件、
输出暂缺或等待超时而重复派发。发现子任务继续派生、同 job 重复运行或并发任务写域重叠时，本轮按
`BLOCKED: agent_dispatch_contract_violation` 收口，不能仅以最终文件存在判定通过。

每个 job 的 `read_scope` 必须在派发 message 中解析为明确路径清单：

- `init_workspace_bootstrap`：workspace registry、各 repo 的入口/README/构建元数据与已有
  `docs/knowledge/service-meta.yaml`，不得读取 repo 业务实现正文；
- `init_scan_tech|init_scan_db|init_scan_api`：当前仓库中各 prompt 声明的构建配置、schema/migration、
  接口/IDL 与相关业务符号；三者不得借读其他扫描 job 的未完成产物；
- `init_scan_service|init_scan_boundary|init_scan_service_meta`：已验证的 A 组产物、前序 B 组产物，
  以及 prompt 为补齐证据明确列出的当前仓库源文件；
- `init_scan_frontend_*`：当前仓库的前端 manifest、配置与各 prompt 声明的源码类别；fullstack merge
  时 `init_scan_frontend_meta` 还可读取已验证的 `architecture.md`；
- `init_service_bootstrap|init_frontend_bootstrap`：已验证扫描产物、外部 knowledge（若显式传入）、
  当前 Host Kit 项目配置和需要增量合并的既有 HelixHarness 入口/索引/设置文件。

任何 read_scope 不得使用未解析的仓库外 glob；动态 workspace repo 必须先解析为规范路径。
每个 job 同时只读当前已验证 plugin root 中所需的 prompt、协议与模板。明确传入
`ownership_root`，所有本仓相对路径都从该目录解析；读取、搜索、目录遍历及符号链接遵守
`reference/subagent-runtime-protocol.md` 的读取边界。单仓 bootstrap 不能读取同级已初始化仓库
作为范例，也不能因缺少模板或扫描事实而搜索父目录。缺口必须返回父流程处理。

| job_id | prompt_source | depends_on | write_scope | forbidden_scope | expected_artifacts / validation |
|---|---|---|---|---|---|
| `init_workspace_bootstrap` | `agents/init-workspace-bootstrap.md` | W0 repo registry 已确定 | workspace 根入口文件（由 `resources.project_config.workspace_entry_template` 生成）、`helixharness.workspace.yaml`、workspace `docs/knowledge/**`、`docs/specs/active/`、`docs/specs/completed/` | 所有 repo-local 路径 | W1.5 的 8 项 workspace 产物全部存在并通过结构校验 |
| `init_scan_tech` | `agents/scan-tech.md` | 项目类型为 backend/unknown，或全栈 scan plan 含后端扫描，且未传 `--knowledge` | `docs/knowledge/architecture.md` | `docs/knowledge/index.md`、init state、timings | 文件存在且非空 |
| `init_scan_db` | `agents/scan-db.md` | 同上 | `docs/knowledge/database/**` | `docs/knowledge/index.md`、init state、timings | `database/index.md`、`database/er.md` 存在且非空 |
| `init_scan_api` | `agents/scan-api.md` | 同上 | `docs/knowledge/api/**` | `docs/knowledge/index.md`、init state、timings | `api/index.md` 存在且非空 |
| `init_scan_service` | `agents/scan-service.md` | A 组三项全部通过 | `docs/knowledge/domain-model.md` | 其他扫描产物、共享索引、init state、timings | 文件存在且非空 |
| `init_scan_boundary` | `agents/scan-boundary.md` | `init_scan_service` | `docs/knowledge/service-boundary.md` | 其他扫描产物、共享索引、init state、timings | 文件存在且非空 |
| `init_scan_service_meta` | `agents/scan-service-meta.md` | `init_scan_service`、`init_scan_boundary` | `docs/knowledge/service-meta.yaml` | 其他扫描产物、共享索引、init state、timings | 文件存在、非空且 YAML 可解析 |
| `init_scan_frontend_meta` | `agents/scan-frontend-meta.md` | 项目类型包含 frontend；全栈全量扫描时额外依赖 `init_scan_tech` 并增量合并 architecture | `docs/knowledge/architecture.md`、`docs/knowledge/build.md`、`docs/knowledge/structure.md`、`docs/knowledge/style.md` | 共享索引、init state、timings | 四个文件存在且非空 |
| `init_scan_frontend_api` | `agents/scan-frontend-api.md` | 同上 | `docs/knowledge/api-endpoints.md` | 共享索引、init state、timings | 文件存在且非空 |
| `init_scan_frontend_routes` | `agents/scan-frontend-routes.md` | 同上 | `docs/knowledge/routes.md` | 共享索引、init state、timings | 文件存在且非空 |
| `init_scan_frontend_state` | `agents/scan-frontend-state.md` | 同上 | `docs/knowledge/state.md` | 共享索引、init state、timings | 文件存在且非空 |
| `init_scan_frontend_components` | `agents/scan-frontend-components.md` | 同上 | `docs/knowledge/components.md` | 共享索引、init state、timings | 文件存在且非空 |
| `init_scan_frontend_tracking` | `agents/scan-frontend-tracking.md` | 同上 | `docs/knowledge/tracking.md`（检测到埋点时） | 共享索引、init state、timings | 文件存在且非空，或 final 明确“未检测到埋点” |
| `init_frontend_bootstrap` | `agents/init-frontend-bootstrap.md` | 六个前端扫描 job 全部通过；全栈时还依赖 `init_service_bootstrap` | 下方初始化目标产物和当前宿主项目配置允许的入口/设置 | 已验证扫描产物之外的业务文件 | 初始化目标产物逐项通过 |
| `init_service_bootstrap` | `agents/init-service-bootstrap.md` | 后端扫描 DAG 全部通过，或外部 knowledge 导入完成；全栈时先于 frontend bootstrap | 下方初始化目标产物和当前宿主项目配置允许的入口/设置 | 已验证扫描产物之外的业务文件 | 初始化目标产物逐项通过 |

A 组 `init_scan_tech/init_scan_db/init_scan_api` 必须并行；B 组必须严格按
`init_scan_service → init_scan_boundary → init_scan_service_meta` 串行。六个前端扫描 job 必须保持
六路并行语义；运行时容量不足时只延后物理准入，不得减少 job。全栈全量扫描时，任何动态写域
重叠都必须先串行化；尤其两个会更新 `architecture.md` 的 job，后执行者只能合并补充，不能覆盖
已验证内容。共享 `docs/knowledge/index.md`、init state、timings 和最终设置合并始终保持单写者。

A 组三个 final 的 evidence index 业务事实每个 job 最多 30 项；`supports` 只允许
`architecture|database|api`，`source` 必须定位到仓库相对路径和行号、配置键或符号。
`native_agent_result` 使用宿主原生 Agent result，不新增文本结构要求，父流程只消费原生结果中
真实存在且可验证的 evidence；`helixharness_structured_text` 必须按当前 `resources.subagent_protocol`
的结果编码返回 evidence index，缺失即结果校验失败。
父流程校验、去重后把可靠 evidence 与产物路径交给 `init_scan_service`；没有可靠 evidence 时只消费
已验证产物，不得推断凑数，B 组仍只针对缺口或冲突补查。

## 执行编排（前置：项目类型预检测）

### Step 0: 项目类型快速检测

先执行空项目预检：

```text
python3 {plugin_root}/scripts/init_empty_check.py --repo-root {repo_root}
```

- `empty=true`：这是 greenfield 仓库。写入
  `docs/.helixharness-init-state.json`，至少包含
  `projectType=empty`、`detectionLevel=fast`、`confidence=1.0`、
  `techStack.hasBackend=false`、`techStack.hasFrontend=false`、`scanPlan=[]`、
  `userConfirmed=false`，然后直接进入 Step 2 的 greenfield bootstrap；
  不得执行 Step 1 的任何扫描 agent。
- `empty=false`：继续读取此 plugin 捆绑的 reference 文档
  `reference/project-type-detection.md`，执行其中的完整检测脚本，生成
  `docs/.helixharness-init-state.json` 状态文件，供 Step 1 按项目类型分支编排。

空项目只表示“还没有代码或构建元数据”，不表示 `unknown`。不得把空项目送进后端扫描兜底。

---

## User Input

```text
$ARGUMENTS
```

## 版本快速检查

进入业务流程前，读取此 plugin 捆绑的 `reference/version-update-protocol.md`，按“主流程快速版本检查模式”执行：

- 每次命令执行和阶段续跑都调用 `python3 {plugin_root}/scripts/version_update.py --platform {canonical_host_id} --repo-root {repo_root} check`；`canonical_host_id` 必须取自预绑定 Host Context，禁止传操作系统名；不得继承任何前序版本结论
- `check` 只认 `helix@helixharness-marketplace`，自动识别正式安装、处理缓存、刷新同一 marketplace 并动态解析其 plugin source；禁止手工调用 plugin list、读取 CHANGELOG 或猜测版本
- 严格按脚本最终 `next_action` 执行：`continue_silently` 静默；`prompt_update` 弹框；`block_for_update` 阻断；`continue_with_warning` 只原样输出脚本 `warning` 后继续
- `update_status=latest|current-newer` 或有效 `skip_effective=true` 时必须静默继续；用户刚选择跳过后也不输出 WARN
- 快速检查绝不自动安装；选择升级时进入 `/helix:update` 的弹框确认与一次性授权流程

## Step 0.5：数据统计 consent（必须在 runtime preflight 后、任何业务 bootstrap 前完成）

单仓服务/前端初始化按 `reference/eval-protocol.md` 的“数据统计开关与提醒缓存”解析当前平台缓存，并得到 `effective_telemetry_mode=enabled|disabled`。`--workspace` 只建立协调层、不创建 repo-local `docs/eval/**`，因此跳过本次 consent 交互，等待各 repo 单独 init 时处理。

在已支持宿主上，只要本步骤需要展示 consent/开启选项，必须先逐字呈现 `reference/eval-protocol.md` 中标识为 `helixharness-eval-data-notice-v1` 的唯一数据说明；本命令内保留如下受静态校验约束的同步副本，不得改写：

<!-- helixharness-eval-data-notice-v1:start -->
```text
数据统计说明：默认关闭。开启后，HelixHarness 在 docs/eval/{feature}/eval-log.json 中记录当前 Git user.name（可用时）、工作流阶段、时间、状态、工件、质量和代码范围等固定事实；该账本可随业务代码提交，不保存原始会话 ID、对话、Token 或工具原始输入输出。
阶段记录本身不读取对话或调用模型。阶段结束后，异步 Runtime 按已授权的精确会话窗口采集 Token、工具及子任务统计和经过筛选脱敏的用户输入、助手回复；不上传 Thinking、原始源码、完整 Diff、附件或原始日志。Archive 冻结采集边界，后台仅处理冻结范围和待投数据，不同步调用会话 classifier。
无论是否配置异步地址，都按授权缓存和提醒周期正常询问统计授权。开启后同步记录本地账本，由平台本地读取或 Git 同步获取；异步采集和投递另需先启动 helixharness-platform，并使用发布脚本 reporting_config.py 中的有效默认地址，或统计私有目录 config.json 的 endpoint 覆盖地址。未配置或地址无效时只跳过异步采集和投递；未授权或关闭时不新增同步账本，也不异步采集或投递。统计和网络异常不阻塞研发。授权允许本机私有采集及向配置地址上报，不等于授权 Git commit 或 push；提交和推送仍由当前命令单独取得授权。关闭后停止新增采集，并由后续命令、Hook 或 Worker 清理未发送私有证据；已提交的历史账本不改写。
```
<!-- helixharness-eval-data-notice-v1:end -->

0. Python 不可用时不调用 resolver、不展示统计说明或开启选项，也不读写 consent 缓存；遵循 `reference/runtime-preflight.md` 恢复运行依赖。恢复并复检成功后从下述 resolve 重新开始。未恢复时，本次不启用统计，不影响无需 Python 的初始化步骤。Git 缺失优先 blocker。
1. 调用 `python3 "{plugin_root}/scripts/telemetry_consent.py" --repo-root "{repo_root}" --platform "{canonical_host_id}" resolve`；
   - 若 `reason=endpoint_unconfigured`，静默继续，不展示“地址未配置”“未异步上报”等状态提示，也不主动要求配置；不能跳过下述统计问询。`endpoint_invalid` 仍展示返回的 warning。按 `prompt_due` 和 `allowed_actions` 处理授权，用户确认后正常调用 choose。
2. 若 `prompt_due=true`，在创建 `docs/eval/**`、修改 `.gitignore` 或调用 bootstrap 前，先展示上述固定数据说明，再提示用户。Consent 子问题必须逐项使用 resolver 的有序 `allowed_actions[]`，保持 label 原文和顺序；可以与其他初始化问题合并展示，但不得压缩为通用“开启/关闭”或丢失 project/user scope。
3. 若当前有效状态为 `enabled` 且 `prompt_due=true`，询问：保持当前统计范围开启 / 关闭当前项目统计 / 关闭我本人统计。
4. 收到用户选择后调用 `choose --action {allowed_action.id} --user-confirmed`，由脚本完成 scope/mode 映射；推荐项、默认项和 Agent 自主判断都不构成确认。保持关闭后 30 天、保持开启后 180 天内不再提示。禁止直接写缓存 JSON。
5. `choose` 后保存脚本返回的真实 `effective_mode` 并展示 notice/warning。Init 不创建统计 Entry 或追加 eval 跟踪规则；后续需求阶段由 recorder 按有效授权自行准备 Ledger 的 Git 跟踪。
6. 后续 service/frontend bootstrap 都必须接收 `effective_telemetry_mode`，不得自行按默认 disabled 重新判断或再次询问。
7. `disabled` 时，本次 init 从开始到结束都不得新建 `docs/eval/**` 或追加 eval `.gitignore` 例外；历史文件不删除。

## 参数说明

| 参数 | 说明 | 示例 |
|------|------|------|
| `--knowledge <path>` | 导入外部知识库文档 | `/helix:init --knowledge ./knowledge-seed/` |
| `--workspace` | 初始化多 git 仓库 workspace | `/helix:init --workspace` |
| `--repos <paths...>` | workspace 初始化时显式指定 repo 路径 | `/helix:init --workspace --repos ../order-api ../coupon-api ./web-console` |
| `--refresh` | workspace 已初始化时强制重新扫描并刷新 registry 与服务关系基线（默认已 init 则幂等跳过） | `/helix:init --workspace --refresh` |

## 入口判断

解析 `$ARGUMENTS`：

- 含 `--workspace` → 执行 **Workspace 初始化**
- 不含 `--workspace` → 执行既有 **服务仓库初始化**

`/helix:init --workspace` 是 workspace 模式的唯一初始化入口；不要引入新的 `/helix:workspace init` 命令。

### Workspace init 幂等

`--workspace` 模式下，进入 W0 之前先在当前工作目录根检查 `helixharness.workspace.yaml` 是否存在：

- **存在且未带 `--refresh`** → 视为已初始化，输出回执（如 `workspace 已初始化，跳过 init；如需刷新 registry 与服务关系基线请加 --refresh`）后立即结束，不执行 W0/W1/W2，不刷新任何产物。
- **不存在，或带 `--refresh`** → 正常执行 W0→W1（含 W1.5 完成验收）→W2。

幂等判定**只看 `helixharness.workspace.yaml` 单个文件**；不在此重复检查 service-catalog/graph 等基线（实际执行 init 时由 W1.5 在 W1 之后兜底验收 8 项产物）。`--refresh` 是刷新 workspace registry 与服务关系基线的唯一入口；默认（无 `--refresh`）幂等跳过。

## Workspace 初始化

Workspace 初始化只在当前工作区根目录执行，目标是建立多仓协调层，不替代各 repo 的单仓 HelixHarness 初始化。

### Step W0：发现 repo registry

按以下顺序发现 repo：

1. 若传入 `--repos <paths...>`，只扫描这些路径。
2. 否则扫描当前目录下一层包含 `.git` 的目录。
3. 对每个 repo 读取：
   - repo key：默认使用目录名，若冲突则停止并要求用户确认命名
   - `path`
   - `git remote -v`
   - 当前分支与默认分支
   - 是否存在 `docs/knowledge/service-meta.yaml`
   - 是否存在当前 Host Kit 的 `entry_file`
   - README、package name、pom artifact、go module、部署配置中的服务名线索

生成或刷新：

```text
helixharness.workspace.yaml
```

该文件只作为 repo registry，不写完整系统知识。完整服务关系写入 `docs/knowledge/`。

### Step W1：workspace bootstrap

调用 Agent tool，完整读取 `agents/init-workspace-bootstrap.md`，传入 workspace 根目录、原始参数、
明确 repo 列表和每个 repo 的初始化状态；任何 repo-local 写入都视为失败。

Workspace 初始化目标产物：

- Host Kit 的 `entry_file`
- `helixharness.workspace.yaml`
- `docs/knowledge/service-catalog.yaml`
- `docs/knowledge/service-graph.md`
- `docs/knowledge/integration-boundaries.md`
- `docs/knowledge/index.md`
- `docs/specs/active/`
- `docs/specs/completed/`

#### Step W1.1：repo-local 初始化状态处理

如果 workspace 中的各 repo 已经存在以下任一 HelixHarness 产物：

- 当前 Host Kit 的 `entry_file`
- `docs/harness/`
- `docs/knowledge/`
- `docs/knowledge/service-meta.yaml`
- `docs/specs/`
- `docs/hooks/`

`/helix:init --workspace` 只能读取这些 repo-local 产物作为输入，用于生成 workspace registry、service catalog 和 service graph。不得重新生成、覆盖、刷新或迁移任何 repo-local HelixHarness 初始化内容。

如果某个 repo 尚未初始化，在 `helixharness.workspace.yaml` 中列表记录。初始化判定必须同时满足：

- 当前 Host Kit 的 `{repo}/{entry_file}` 存在且非空；
- `{repo}/docs/harness/index.md` 与 `{repo}/docs/knowledge/index.md` 存在且非空；
- `{repo}/docs/specs/active/` 与 `{repo}/docs/specs/completed/` 是真实目录。

`docs/knowledge/service-meta.yaml` 只作为服务元数据增强项：存在时记录相对路径，不存在时写
`service_meta:null`。纯前端 repo 不生成 service-meta 是合法状态，不能因此把已完成 init 的前端仓标为
`initialized:false`。遍历完所有 repo 后：

- 全部已 init → 直接继续。
- 存在未 init 的 repo → 列出每个 repo 缺失的入口/index/specs 事实，向用户确认："以下 repo 尚未完成当前宿主的单仓 init：{列表}。是否现在执行单仓 `/helix:init`？（init 会扫描代码、生成 `docs/harness`、`docs/knowledge` 与入口，供 workspace 级命令使用）"
  - 用户确认 → 逐个（串行）进入对应 repo 目录执行 `/helix:init`（走单仓 scan + bootstrap agent，**不走 workspace init 逻辑**）；为每次单仓 Init 重新绑定 `ownership_root` 与本仓各 job 的 `read_scope`，不得继承 workspace bootstrap 的全仓读取清单；完成后回写 `helixharness.workspace.yaml` 中该 repo 的 `initialized: true` 与 `service_meta`，再继续。
  - 用户拒绝或未确认 → 保持 `initialized: false` 标记，继续（后续 `propose` 的 W1-5 仍会被拦截、再次询问确认）。

不得在 workspace init 中静默跳过未 init repo 或自动进入 repo 执行单仓 init。

#### Step W1.5：workspace root 产物门禁

workspace bootstrap 完成后，顶层命令必须在**当前工作目录根**逐项检查以下产物是否真实存在：

```text
./{entry_file}（来自 Host Kit）
./helixharness.workspace.yaml
./docs/knowledge/service-catalog.yaml
./docs/knowledge/service-graph.md
./docs/knowledge/integration-boundaries.md
./docs/knowledge/index.md
./docs/specs/active/
./docs/specs/completed/
```

若任一缺失，立即报告：

```text
BLOCKED: workspace_init_outputs_missing
```

并列出缺失路径。不得把子 repo 下的 `.helixharness/`、`docs/` 或 telemetry 文件视为 workspace root 产物。

`/helix:init --workspace` 的成功判定只看 workspace root 上述产物；每个服务 repo 下的 `.helixharness/telemetry.json`、`.helixharness/upload-state.json` 不属于 workspace 初始化目标。

### Step W2：服务关系基线

`/helix:init --workspace` 必须在 init 阶段建立 baseline service graph，但不要求一次性完美。

信息来源包括：

- 各 repo 的 `docs/knowledge/service-meta.yaml`
- OpenAPI / Swagger / RPC IDL
- Feign / HTTP client / Dubbo / gRPC 调用声明
- MQ topic producer / consumer
- 数据库表 ownership
- 部署配置、服务名、环境变量
- README / 架构文档

不确定关系不能强行写死，必须进入 `uncertain_edges`，包含：

```yaml
confidence: low|medium|high
needs_confirmation: true
evidence: "{可追溯证据}"
```

若某个 repo 尚未执行单仓 `/helix:init`，只在 `helixharness.workspace.yaml` 中标记：

```yaml
helix:
  initialized: false
  service_meta: null
```

不要在 workspace init 中自动进入该 repo 完整执行单仓初始化；如需初始化单个 repo，用户可在 repo 内执行 `/helix:init`。

Workspace 初始化完成后立即输出报告并停止；不得继续执行下方“服务仓库初始化”流程。

## 执行编排

### 服务仓库初始化

此模式下，**knowledge 扫描必须由顶层命令显式编排并实际触发 Agent tool/子代理调用**，不能只把“应当扫描”的要求下沉给 bootstrap agent 后就继续执行。

#### Step 1：knowledge 扫描编排

读取 Step 0 生成的项目类型检测结果，按类型执行不同扫描 pipeline：

```bash
STATE_FILE="docs/.helixharness-init-state.json"
PROJECT_TYPE=$(grep -o '"projectType":[^,]*' "$STATE_FILE" 2>/dev/null | cut -d'"' -f4 || echo "unknown")
```

**分支 0: 空项目 / Greenfield — 不执行扫描**

```bash
if [ "$PROJECT_TYPE" = "empty" ]; then
  # Step 0 已写入 scanPlan=[]；这里不得调用任何 scan agent。
  # 直接进入 Step 2 的 greenfield bootstrap，由 bootstrap 与用户确认
  # 项目目标、技术栈和运行方式，并生成最小 knowledge/harness。
  :
fi
```

**分支 1: 后端 / 未知项目 — 执行原后端完整扫描流程**

```bash
if [ "$PROJECT_TYPE" = "backend" ] || [ "$PROJECT_TYPE" = "unknown" ]; then
  # ===== 原有后端扫描流程，100% 不变 =====
  - 若 `$ARGUMENTS` 含 `--knowledge`：
    - 不调用 `scan-tech` / `scan-db` / `scan-api` / `scan-service` / `scan-boundary` / `scan-service-meta`
    - 由后续 bootstrap agent 读取外部知识源并生成 `docs/knowledge/` 入口索引

  - 若 `$ARGUMENTS` 不含 `--knowledge`：
    - 必须先并行实际调用以下三个 agent：
      - `agents/scan-tech.md`
      - `agents/scan-db.md`
      - `agents/scan-api.md`
    - 必须等待上述三个 agent 全部完成后，再实际调用 `agents/scan-service.md`。
    - 必须等待 `scan-service` 完成后，再实际调用 `agents/scan-boundary.md`。
    - 必须等待 `scan-service` 与 `scan-boundary` 完成后，再实际调用 `agents/scan-service-meta.md`。
    - 每个 Agent 完成后检查其目标产物；任一 Agent 未实际触发成功或产物缺失时立即报告 BLOCKED。

   **通用约束**：
   - 若任一扫描 agent 未实际触发成功，立即停止并报告 BLOCKED，不得跳过后续依赖步骤
  # ===== 原有后端扫描流程结束 =================
fi
```

**分支 2: 纯前端项目 — 执行前端简化扫描流程**

```bash
if [ "$PROJECT_TYPE" = "frontend" ]; then
  echo "📱 检测为前端项目，执行前端优化扫描流程"

  # 跳过: scan-db, scan-api, scan-service, scan-boundary, scan-service-meta

  # 六路并行：
  # scan-frontend-meta：元信息扫描（合并 tech + build + structure + style，串行 4 子任务）
  # scan-frontend-api：API 调用清单
  # scan-frontend-routes：路由配置
  # scan-frontend-state：状态管理
  # scan-frontend-components：组件清单
  # scan-frontend-tracking：埋点检测（无埋点则直接退出，<2s）

  # 顶层命令显式触发 6 个 Agent tool，并保持六路并行
  # 这些 agent prompt 位于此 plugin 的 agents/ 目录下

  # 通过公共 Read/Edit/Write 工具语义读取并更新 STATE_FILE；其他宿主按 resources.runtime 映射。
  # 文件缺失或不是 JSON 对象时立即 BLOCKED:init_state_missing，不得跳过状态写入。
  # 只把 scanPlan 设为六路前端任务数组，保留其余字段；写后重新 Read，确认 JSON 可解析且值一致。

  # ⏱ 计时：Agent tool 返回的 duration_ms 为系统级 wall-clock（非 agent 自报），
  # 直接以 {agent: seconds} 格式写入 stateFile.scanTimings 字段
  # agent 不再写入 scan-timing 标签——所有计时由 init 编排层统一采集
fi
```

**前端扫描编排**：保持六路 Agent tool 并行。每个 Agent 只能写自身 prompt 声明的产物；父流程等待
全部 Agent 结束后统一写 timings，并逐项校验 `architecture.md`、`build.md`、`structure.md`、
`style.md`、`api-endpoints.md`、`routes.md`、`state.md` 和 `components.md`。tracking 未检测到埋点时
允许不生成 `tracking.md`，但 Agent 必须明确报告结果。

**分支 3: 全栈 / Monorepo 项目 — 执行 Step 0 已确认的完整全栈 scan plan**

```bash
if [ "$PROJECT_TYPE" = "fullstack-node" ] || [ "$PROJECT_TYPE" = "fullstack-separated" ] || [ "$PROJECT_TYPE" = "monorepo" ]; then
  # Step 0 的 project-type-detection 已完成唯一一次用户确认：
  # 仅前端/仅后端会把 PROJECT_TYPE 归一化为 frontend/backend；仍保留全栈标签只表示用户选择了全量扫描。
  # 此处不得再次询问扫描模式，也不得改写已确认的 scanPlan。

  # 后端 A 组三路并行，随后按 scan-service → scan-boundary → scan-service-meta 推进。
  # frontend-api/routes/state/components/tracking 可与无写域冲突的后端 job 并行。
  # init_scan_frontend_meta 额外依赖 init_scan_tech：scan-tech 先写 architecture.md，
  # frontend-meta 随后只增量合并前端架构并写 build/structure/style，不得覆盖后端事实。
  # 父流程等待 scanPlan 中全部 job 真实完成并逐项校验，再进入串行 fullstack bootstrap。
fi
```

#### Step 2：bootstrap 生成

完成 knowledge 导入或扫描后，按项目类型选择 bootstrap agent：

**空项目（`$PROJECT_TYPE` = `empty` / greenfield）**：

使用原有 Agent tool 调用语义执行 `agents/init-service-bootstrap.md`，传入：

- `bootstrap_mode=greenfield`
- `project-mode=greenfield`
- 仓库根目录、原始参数 `$ARGUMENTS`、当前平台、`effective_telemetry_mode`
- 明确说明：本轮没有 scan 产物，不得等待或引用 `scan-*` 文件

bootstrap 必须先与用户确认项目目标、技术栈、运行方式和测试入口，再基于模板生成最小
`docs/harness/`、`docs/knowledge/`、`docs/specs/` 和 Host Kit 入口。无法确认的内容写
`<!-- TODO: 待确认 -->`，不得补造架构、数据库或 API 事实。完成后仍按 Step 3 逐项校验产物。

**纯前端项目（`$PROJECT_TYPE` = `frontend`）**：
- 读取 agent prompt：`agents/init-frontend-bootstrap.md`
- 调用 `init_scan_frontend_meta` 时传入 `architecture_mode=frontend-create`；无既有 `architecture.md` 时必须创建纯前端架构文档。
- 传递上下文：仓库根目录、原始参数 `$ARGUMENTS`、knowledge 扫描结果、当前平台、`effective_telemetry_mode`
- 前端扫描任务全部完成并通过产物校验后，使用原有 Agent tool 调用语义执行该 bootstrap prompt。

**后端项目（`$PROJECT_TYPE` = `backend` / `unknown`）**：

使用原有 Agent tool 调用语义执行 `agents/init-service-bootstrap.md`。bootstrap Agent 按
`resources.project_hooks` 声明的 owner、merge strategy 和去重键增量合并当前
`resources.project_config.settings_file`；该字段为 `null` 时跳过设置写入。完成后必须逐项校验初始化目标产物，否则报告 BLOCKED。

**完整全栈 bootstrap（`$PROJECT_TYPE` = `fullstack-node` / `fullstack-separated` / `monorepo`，且用户选择完整全栈扫描）**：

1. 先执行 `agents/init-service-bootstrap.md`，传入 `bootstrap_mode=fullstack-base`，消费后端扫描 DAG，生成公共目录、后端 knowledge/harness、Host Kit 入口和可选设置。
2. `init_scan_frontend_meta` 传入 `architecture_mode=fullstack-merge`；它必须等待 `init_scan_tech` 并只更新前端架构区块。后端 bootstrap 真实完成并通过产物校验后，再执行 `agents/init-frontend-bootstrap.md`，传入 `bootstrap_mode=fullstack-merge`，消费六路前端扫描结果；只增量补充前端 knowledge/harness/linters 和同一个 Host Kit 入口区块，不覆盖 service bootstrap 已生成内容，也不因公共目录已存在再次询问是否更新。
3. 两个 bootstrap 绝对串行；最终同时校验后端必需产物、前端必需产物、唯一根索引、Host Kit 的 `entry_file` 和可选 settings 去重结果。任一侧失败都不能把全栈 init 声称为完成。

初始化目标产物：

- `docs/harness/`
- `docs/knowledge/`
- `docs/knowledge/service-meta.yaml`
- `docs/specs/`
- `docs/hooks/`
- `docs/eval/`（仅统计开启时）
- `.gitignore` 中的 HelixHarness eval 提交例外块（仅统计开启时）
- Host Kit 的 `entry_file`
- Host Kit 的 `settings_file`（非 null 时必须存在，包含 `project_hooks.payload` 且无重复）

#### Step 3：统一初始化产物校验

单仓（service/frontend/fullstack）的父流程不能只检查目录存在。除上方目标清单外，必须确认
`docs/harness/index.md`、`docs/knowledge/index.md`、`docs/knowledge/project-context.md`
均为非空文件；`project-context.md` 内容按当前项目类型对应的 bootstrap 模板逐项核对，
未知项明确标注待确认，不得补造事实或向前端套用服务端字段。知识根索引必须直接链接该文件，供 Propose/Apply 加载。
缺失时把准确路径回传给原 bootstrap agent 补齐，由父流程重新核验；未补齐前报告
`BLOCKED: init_required_artifact_missing`，不得凭 bootstrap 的完成回执跳过检查。

bootstrap 达到真实终态后，父流程必须逐项检查上述初始化目标产物清单，并确认入口文件、知识索引、
目录和可选 settings 合并结果符合当前模式。任一必需产物缺失或为空时沿用既有产物门禁收口；不得以
额外 Python validator 替代逐项检查。

项目 hooks 是完成门禁的一部分，不能以 bootstrap 的 `parent-owned` 或合并请求代替实际文件：

- `resources.project_hooks.owner=parent` 时，父流程在 bootstrap 返回后执行
  `python3 {plugin_root}/scripts/project_hooks_cli.py --repo-root {repo_root} --platform {canonical_host_id} merge`。
- `owner=bootstrap` 时由 bootstrap 按原有宿主写入权限合并；父流程不得绕过宿主配置权限。
- 两种 owner 均由父流程执行同一路径的 `check`，核对实际 settings 的必需 hook、去重与 JSON 结构；
  `settings_file=null` 时工具返回明确 skipped。任何缺失、冲突或检查失败均报告
  `BLOCKED: project_hooks_incomplete`，保留其他已生成工件，不得报告 Init 完成。

### Usage Telemetry

`/helix:init` 只建立 HelixHarness 工作区基础目录和配置，不写入 usage telemetry。平台侧采用情况由已提交的需求级 eval-log 聚合判断。

Consent、repo 命名和扫描模式选择只服务初始化，不属于任何 feature；不得为了记录这些选择创建 eval entry、调用模型或把事件延迟混入后续 feature。

数据统计开关必须使用 Step 0.5 已解析并传入 bootstrap 的 `effective_telemetry_mode`；此处不得在产物生成后才首次询问，也不得让 bootstrap 自行回退到默认 disabled。后续通过 `scripts/telemetry_consent.py resolve/choose` 管理项目统计授权。

完成门禁：
- 若统计保持开启，`/helix:init` 结束前必须确认 `docs/eval/` 目录存在
- 若统计保持开启，`/helix:init` 结束前必须确认 `.gitignore` 允许提交 `docs/eval/**`；若仍被忽略，输出 `WARN:usage_telemetry_gitignored`
- 若用户关闭统计，不创建新的 eval-log，不追加 eval `.gitignore` 例外块；已存在的历史 `docs/eval/**` 不删除

## 约束

- **命令文件只负责编排** — 详细生成流程统一下沉到 bootstrap agents
- **生成的 `docs/knowledge/` 必须支持后续命令按需加载** — 生成唯一根索引 `docs/knowledge/index.md`，直接索引所有叶子文件（含子目录下的），后续命令按 `reference/loading-protocol.md` 协议按需加载
- **不捏造信息** — 无法确认的内容必须标 `<!-- TODO: 待确认 -->`

宿主资源声明：`resources.runtime`、`resources.subagent_protocol`、`resources.telemetry_protocol`、`resources.project_config`、`resources.project_hooks`。
