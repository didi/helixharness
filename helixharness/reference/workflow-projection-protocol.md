# Workflow Projection Protocol 1.0

本协议定义用户入口如何执行 projection 声明的宿主无关业务契约。每个 contract 固定为带
“HelixHarness 公共命令唯一契约”标记的 `commands/{command}.md`；公共入口只负责路由和编排，所选 `workflows/single/*.md` 或 `workflows/workspace/*.md` 是唯一业务正文；不得在两层重复业务步骤。Init / UT / Update / Status / Config 无阶段正文，其完整步骤保留在公共 command contract。

## 入口绑定

1. 插件分发表面或启动适配器必须在入口执行前绑定唯一 Host Context。
2. 完整读取 `reference/host-runtime-protocol.md`、`reference/tool-semantics.md`、当前 manifest 的
   `resources.runtime` 和 `resources.command_projection`。
3. `resources.command_projection` 必须声明当前公开入口及其唯一 command contract；未声明的入口关闭失败。
4. Skill 与原生命令入口先执行 projection 声明的 `commands/{command}.md`；Claude Code 允许入口与
   contract 是同一个 command 文件，Codex 等 Skill 表面使用宿主薄入口加载该 contract。需要阶段正文时，完整读取 Router 返回的唯一 workflow，保留身份、scope 和 trigger，不叠加另一套正文。Host Kit
   资源只能映射执行机制，不得成为第二份业务事实源。
5. 仅当 command contract 声明 Agent/Task 逻辑 job 时，才在首次派发前读取
   `reference/subagent-runtime-protocol.md` 与当前 `resources.subagent_protocol`；无子任务命令不得
   为了通用初始化额外加载这两个资源。

## 零发现加载

- 入口中的资源引用以当前 command/Skill 的实际路径和 projection 声明为解析基准。标准
  `commands/{entry}.md` 的插件根固定为 `..`；`workflows/{scope}/{stage}.md` 的插件根固定为 `../..`；根方法 Skill `skills/{name}/SKILL.md` 的插件根固定为
  `../..`；宿主薄入口使用对应 Host Kit 明确声明的固定换算。不得相对于项目工作区猜测，也不得把
  一个宿主的入口布局套到另一个宿主。
- 根 `skills/{name}/SKILL.md` 只保存可复用方法或编排能力，不得与公开
  `commands/{command}.md` 同名。宿主需要 Skill 命令表面时，必须通过自己的 command projection
  暴露宿主薄入口，不能在根 `skills/` 恢复第二个公共命令定义。
- 方法 Skill 的正文仍以根 `skills/{name}/SKILL.md` 为唯一业务事实源；宿主薄入口拥有该分发表面的
  生效 frontmatter。投影生成器必须把 canonical 的调用意图翻译为宿主原生元数据，并剥离另一个
  宿主的 hooks、tool allowlist、路径变量等分发字段；不得直接复制后让非目标宿主误执行。
- Codex 方法与命令入口的 `agents/openai.yaml` 必须由
  `scripts/codex_skill_projection.py` 确定性生成。canonical 的 `disable-model-invocation: true` 映射为
  `allow_implicit_invocation: false`；未声明时显式映射为 `true`，保证 command/Autopilot 内部调度
  不被误关。false 只禁止无提示自动选择，command contract 声明的 `invoke_skill(name)` 仍由 Codex
  runtime 显式映射为 `$helix:{name}`。显示名称和短描述只用于发现，不得加入第二份业务步骤。
- Codex 方法薄入口必须先绑定 `hosts/codex/host.json`、公共 Host Runtime/Tool Semantics 与
  `resources.runtime`，然后才执行 canonical 正文；正文首次派发 Agent/Task 时再按需读取公共协议和
  `resources.subagent_protocol`。直接调用方法 Skill 与从公共 command 调用时使用同一能力映射。
- Host Context、projection 和 manifest 已给出的资源路径必须直接使用；不得枚举或搜索 plugin 根目录、
  安装目录或 cache 来重新发现同名资源，也不得用 `find/rg/ls/wc/stat` 试探已绑定资源的位置或长度。
- 固定且互不依赖的资源可在宿主支持时合并为一个有界读取批次；生命周期协议要求独占工具轮次的动作
  仍保持原有串行边界，不能为了批量加载改变命令步骤。
- 同一命令内已完整读取且未被本轮修改的资源必须复用，不得重复读取。绑定路径缺失或指向失效资源时
  输出 `BLOCKED: helixharness_host_contract_invalid`，不得扫描 plugin cache 寻找替代版本。

## 执行

- command contract 决定入口路由和循环编排；所选 workflow 决定阶段的逻辑 job、工件、质量门禁、**逻辑写入所有权**、原子性和完成条件。
  `resources.subagent_protocol` 选择实际执行写操作的**物理写入者**；该选择不得改变最终工件、
  单写者不变量、证据门禁或 parent 的终态校验责任。
- `resources.runtime` 映射逻辑工具；涉及子任务、转场、项目配置、UT、delivery、telemetry、update
  或 lifecycle 时，只读取当前 manifest 对应的 capability/resource。
- adapter 只能改变执行方式。出现冲突时，业务结果服从 command contract；宿主原生权限和安全限制保持有效。
- review、verify、test、UT 等质量门禁不得跳过；adapter 无法等价执行时必须关闭失败。
- `unsupported` 必须使用 contract 声明的降级或关闭失败，不能读取其他 Host Kit 的资源补齐。

## 完成

完成前验证逻辑 job 终态、write scope、要求的工件和真实命令证据。入口 projection、contract 和
adapter 任一缺失或不一致时输出 `BLOCKED: helixharness_host_contract_invalid`，不得猜测继续。
