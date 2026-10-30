# HelixHarness Host API 1.1

HelixHarness 当前发布 Claude Code、Codex 与 OpenCode 三个 Host Kit。公共业务契约与宿主运行机制保持解耦：
`commands/`、`workflows/`、共享 `skills/`、`agents/` 和 `templates/` 定义业务结果，`hosts/{host-id}/`
只定义如何在某个宿主中安装、发现和执行。

仓库根 `commands/` 是所有宿主共享的公共入口契约，Router 返回的 `workflows/` 文件承载唯一阶段正文。每个公开命令必须带
“HelixHarness 公共命令唯一契约”标记，工具拼写按 `reference/tool-semantics.md` 解释。Host Kit
可以改变物理执行方式，但不得复制或改写流程顺序、产物、门禁和完成条件。

## 当前支持边界

当前 Registry 必须只发现：

- `claude-code`：原生命令、Hook 生命周期、原生 Agent、Claude marketplace；
- `codex`：Skill 命令投影、独立启动/结束 Hook 与命令驱动阶段生命周期、Codex subagent、Codex marketplace；
- `opencode`：原生命令/Skill/Agent 注册、插件内 Host Context 注入、原生 subagent、OpenCode plugin 分发。

三者使用 Host API 1.1。Claude Code 与 Codex 的支持等级为 Stable；OpenCode 为 Preview，明确声明
`telemetry` 与 `plugin_update` 为 `unsupported`，不提供其他平台的 manifest、安装器、分发清单、
运行时适配器或入口文件。未知宿主必须关闭失败，不能回退到 Claude Code 或借用其他 Host Kit 资源。

## 共享核心与 Host Kit

共享核心负责：

- 工作流顺序、Feature 状态和检查点；
- spec/tasks/verify-report 等产物契约；
- Apply 测试与双轴评审、Archive 工件与范围门禁；
- 逻辑任务、写入所有权、终态和受控降级；
- 与宿主无关的工具语义、遥测业务字段和版本策略。

Host Kit 负责：

- marketplace 分发与安装/升级手册；
- 用户命令或 Skill 到公共 command contract 的投影；
- Skill 分发表面的 frontmatter、发现描述和 user/model 调用策略翻译；
- 逻辑工具到宿主原生工具的映射；
- 子代理派发、等待、结果编码和资源回收；
- workflow transition、autopilot、UT、delivery 与 lifecycle；
- 项目入口、settings/hook 合并；
- 会话数据采集、consent 路径和原生更新命令。

## Manifest 契约

`hosts/{host-id}/host.json` 只允许以下顶层字段：

- `host_id`：规范宿主 ID，必须与目录名一致；
- `host_api_version`：当前正式 Host Kit 使用 `1.1`；
- `aliases`：可选输入别名，必须全局唯一；
- `capabilities`：能力等级；
- `adapters`：`resource:` 或 `python:` 引用；
- `support`：支持等级、展示名、E2E 证据和限制。

机器 schema 是 `hosts/host-manifest-v1.schema.json`，Python Registry 是最终运行时门禁。Host API
1.1 不包含 Profile、Invocation 或通用 Distribution **运行时**契约；Codex 构建期可以把 canonical
Skill 已声明的调用意图确定性投影为自己的原生 metadata，但该 metadata 不得进入 Host Context、
改变公共业务语义或被其他宿主读取。若未来确有多个宿主共享同一种新运行时机制，应先用新的向后
兼容 API 版本建模，再接入平台，不能恢复平台名分支。

## 标准资源适配器

Host API 1.1 的业务 Host Kit 必须提供：

- `distribution`：该宿主的原生 marketplace/plugin 清单；
- `installer`：安装、升级、卸载与重载 smoke 手册；
- `command_projection`：全部公开入口到入口资源和唯一公共 contract 的映射；
- `runtime`：逻辑动作到原生工具的映射；
- `subagent_protocol`：派发、等待、终态、结果和资源回收；
- `workflow_transition`：命令间跳转的物理方式；
- `project_config`：项目入口、模板和 settings 路径；
- `project_hooks`：settings 目标、owner、合并策略、去重键和 payload；
- `ut`：单测工作流物理入口；
- `lifecycle`：启动、阶段完成和失败生命周期；
- `telemetry_protocol`：`hook|command` 遥测生命周期。

`project_config` 的 `settings_file=null` 表示 init 不创建宿主设置文件。公共 init 不得自行选择另
一个宿主的路径。`command_projection` 必须完整覆盖全部 workflow，contract 必须是带唯一契约标记
的 `commands/{command}.md`。`project_hooks` 的目标必须与 `project_config.settings_file`
一致。

可选 Python 能力与 adapter 成对启用：

- `telemetry=native|emulated`：必须提供 `telemetry` 与 `version`；
- `plugin_update=native|emulated`：必须提供 `update` 与 `version`；
- `detector`：仅供开发诊断和 Host Context CLI 自动识别使用。

## 能力等级

- `native`：宿主直接提供该行为；
- `emulated`：Host Kit 提供有边界的等价实现；
- `unsupported`：只能使用公共契约明确允许的降级；没有安全降级时关闭失败。

未声明能力一律视为 `unsupported`。能力启用时必须同时提供对应 adapter，不能用说明文档伪装可
执行实现。

## Registry 与绑定

Registry 扫描 `hosts/*/host.json`，不在核心代码中维护平台 ID 分支。它负责开发诊断、结构校验、
支持矩阵、遥测/更新 adapter 构建和发布检查；正常业务命令由已安装插件的分发表面预绑定 Host
Context，不在每条命令中启动 Python 重新识别。

宿主 ID、别名和资源路径必须全局有效；未知 ID、冲突检测、越界资源、缺失 adapter 或不支持的 API
版本都必须关闭失败。已安装宿主可通过 `scripts/host_runtime_cli.py list` 查询。

## 未来新增宿主

架构保留清单驱动扩展点，但新增宿主必须作为一次独立架构变更重新引入，不得只复制旧平台文件。
最小步骤是：

1. 评估新宿主能否满足 Host API 1.1；不能满足时先设计新的通用 API 版本；
2. 新增 `hosts/{host-id}/host.json` 和全部必需资源；
3. 新增宿主原生分发入口、安装/重载流程和 Host Context 绑定；
4. 为 telemetry/update 提供独立 adapter，或真实声明 `unsupported`；
5. 增加 command projection、项目配置、生命周期、安装和真实数据源专项测试；
6. 更新用户文档、支持矩阵和版本元数据；
7. 在目标客户端完成安装、新会话发现、核心工作流和发布 E2E。

通常不应修改 `commands/`、`workflows/`、共享 `skills/`、`metrics.py`、`version_update.py` 或 Registry
的控制流。如果必须加入 `if <platform>`，说明 Host API 没有表达真实差异，应先修正抽象边界。

## 插件 Hook 分发

Claude Code 与 Codex 的原生 plugin manifest 必须显式声明 `hosts/{host-id}/hooks.json`。
不得发布根目录默认 `hooks/hooks.json`，以免客户端自动装载另一个宿主的入口。
OpenCode 不发布 hook manifest，而是在 `opencode/index.ts` 的 `setup` 中注册 surface，并按
`hosts/opencode/lifecycle.md` 声明 `session_start=emulated` 和 `session_events=unsupported`。
Hook 入口位于对应 Host Kit，绑定自身安装目录的规范 manifest，转换宿主事件与环境持久化。
共享 `scripts/lifecycle_service.py` 只消费显式 HostManifest 和规范事件，不猜平台。
项目级检查 hooks 独立使用 `project_hooks` 合同；单仓 Init 的实际 settings 缺失或重复必须阻断。
遥测 adapter 统一通过该 Registry 的 manifest Python 引用装载，不维护第二份平台注册表。
