# 安装与更新

完整步骤见[安装指南](../../../docs/installation.md)。当前单仓源码使用插件目录的绝对路径；
公开发布后也可使用 `https://github.com/didi/helixharness.git`，记为 `HELIXHARNESS_SOURCE`。
OpenCode V2 使用插件包根目录（本仓库的 `helixharness/`）中的 `package.json` 和
`opencode/index.ts`。

## OpenCode 预览支持

当前为 Preview：已提供九个命令、方法 Skill、原生子代理及命令/Skill 的 Host Context 绑定，
完整 Init → Propose → Apply → Archive 工作流尚未完成真实客户端验收。

- 无原生 SessionStart/Stop/失败/会话结束 hooks；无原生计划工具，计划写入当前 Feature `tasks.md`。
- 统计为 `unsupported`（脚本返回 `unsupported_host`）；插件内更新为 `unsupported`
  （`/helix:update` 返回 `continue_silently`），请通过 OpenCode 插件管理手动更新。
- 项目 hooks 不支持；Init 只合并 `AGENTS.md`，不创建 `.opencode/` settings。
- d-d2c 的 Claude skill-scoped Stop/PreToolUse guard 不执行，采用无 hook / `--bare` 语义；
  仍须完成 Skill 正文中的用户决策、视觉验收和完成门禁。
- Workspace Apply 需要 `experimental.subagent_depth >= 2`，详见下方多仓配置。

## 从 Git 安装

```bash
opencode plugin add 'github:didi/helixharness::path:helixharness'
```

Git 安装支持分支、Tag 和完整提交；需要固定版本时使用完整提交或 npm 版本。安装后 OpenCode
加载 `package.json` 的 `exports["."]`；插件只使用 Bun 内置模块和 OpenCode 提供的插件上下文，
没有运行时 npm 依赖，因此本地源码副本也无需先执行 `bun install`。开发时运行
`bun install` 只是为了 `bun test` 和 `tsc` 的类型检查。

## 本地源码

本地插件目录必须配置为目录，不能指向单个 `.ts` 文件。将绝对路径加入项目或全局
`opencode.json(c)`：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/absolute/path/to/helixharness"]
}
```

本地源码修改后按 OpenCode 要求重启服务或重新加载插件。

## 多仓 Workspace

OpenCode 默认子代理嵌套深度为一。Workspace Apply 的 RepoJob 使用
`delegation_policy=may_delegate` 并使用 `helix-worker`，需要允许 child 继续派发 Single Apply 的只读评审角色。在该项目
或全局 `opencode.json(c)` 中将 `experimental.subagent_depth` 设置为至少 `2`：

```jsonc
{
  "experimental": {
    "subagent_depth": 2
  }
}
```

不设置时 Single 仓流程仍可运行，但 Workspace Apply 必须如实报告深度不足并停止该 RepoJob，不能
降级内联、由 Implementer 自审或伪造完成。原生 `subagent` 的执行和等待参数以当前暴露的
schema 为准；等待真实结果或原生后台完成通知，`sessionID` 不是完成证据。

## 验收

新开 OpenCode 会话并确认：

- 九个公开命令可见：`helix:init`、`helix:propose`、`helix:apply`、`helix:archive`、
  `helix:bug-fix`、`helix:ut`、`helix:update`、`helix:status`、`helix:config`；
- 方法 Skill 以 `helix-<name>` 出现在原生 skill 列表；
- 子任务角色以 `helix-<name>` 出现在 subagent 列表；
- 直接执行 `/helix:status` 时正文包含 `canonical_host_id: opencode`、当前 `plugin_root` 和
  `repo_root`。

仅命令可见不能证明 Host Context 注入成功；必须检查一次命令展开后的正文。

开发者可运行[宿主集成测试](../../opencode/tests/README.md)：使用真实 OpenCode 服务和本地脚本
模型验证目录绑定、失败正文替换与子任务执行边界，不需要真实模型账号。该结果不替代完整需求的
真实模型验收。

## 更新与卸载

- Git 来源：使用 `opencode plugin update`，或重新执行 `opencode plugin add` 并指定目标版本。
- 本地来源：先自行更新源码，再重启或重新加载 OpenCode。
- 卸载：使用 `opencode plugin remove`，或在 `plugins` 数组中移除该目录。插件卸载不删除业务
  仓库中的 `docs/harness/`、`docs/knowledge/`、`docs/specs/` 或业务代码。

`/helix:update` 在 OpenCode 上返回 `continue_silently`，因为当前 `plugin_update=unsupported`；
这不影响手动更新。OpenCode 不会创建 `.opencode/` 项目设置或项目 hooks；Init 只合并
`AGENTS.md`。
