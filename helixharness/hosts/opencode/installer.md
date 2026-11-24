# 安装与更新

完整步骤见[安装指南](../../../docs/installation.md)。当前单仓源码使用插件目录的绝对路径；
公开发布后也可使用 `https://github.com/didi/helixharness.git`，记为 `HELIXHARNESS_SOURCE`。
OpenCode V2 使用插件包根目录（本仓库的 `helixharness/`）中的 `package.json` 和
`opencode/index.ts`。

## OpenCode Stable 支持范围

当前源码候选在 **macOS、OpenCode 2.3.2** 的[支持矩阵](../../../docs/opencode-testing.md)范围内为 Stable。
OpenCode 2.2.2、2.3.0 仅有自动化原生服务兼容覆盖，不外推为全部版本或平台均已验收。

已提供九个命令、方法 Skill 和原生子代理。命令、原生 `skill` 工具结果、
Skill mention 与原生 Skill 激活均按请求会话绑定仓库目录；查询或目录校验失败时阻断
未绑定的 Skill 正文。2026-11-20 已在
OpenCode 2.3.2 + DeepSeek 上完成一次合成单仓 Init → Propose → Apply → Archive 真实模型闭环，
包含操作者确认和一次受控合同修订；2026-11-23 又完成了 OpenCode 2.3.2 + DeepSeek 的操作者辅助合成双仓 Workspace 与独立 UT 验证；后续已修复 UT 分类校验与原子发布保护，并通过无纠正提醒的真实模型复验。[合成 Workspace 的专项真实模型验证](../../opencode/tests/README.md#recovery-and-auxiliary-workflow-acceptance-2026-11-23)还覆盖了 Apply 与部分 Archive 原生取消后新会话恢复，以及注入原生 context hook 故障后阻断依赖派发。完整生产业务项目、无人干预时的协议遵守与完整 TUI/桌面 UI 验收仍未完成；已覆盖范围见[定向终端 UI 验收](../../opencode/tests/README.md#targeted-terminal-ui-acceptance-2026-11-23)。

2026-11-23 已修复并回归 Skill mention 绑定和资源符号链接边界：在读取文件或枚举目录前，
同时校验字面路径与真实路径；拒绝插件外部、悬空和环状链接，允许插件内部链接。
原生宿主回归覆盖 OpenCode 2.2.2、2.3.0、2.3.2；使用本地脚本模型检查实际请求，不代替
完整业务场景的真实模型验收，也不扩展上述 Stable 支持范围。

- 已使用原生工具/会话 Hook，但未提供等价的 SessionStart/Stop/失败/会话结束业务保证。
- 任务进度由 `scripts/task_plan.py` 操作同一份 `tasks.md`；统计在明确授权后仅记录阶段事实和轨迹。
- `/helix:update` 对有明确 upstream 的干净 Git 源码安装执行确认后、锁定 commit 的快进更新；
  本地改动、游离版本和包管理安装不自动处理，更新后仍须重启或重新加载 OpenCode。
- Init 不创建 `.opencode/` settings；插件在原生编辑成功后运行已有项目巡检。见[能力适配](capabilities.md)。
- d-d2c 的 Claude skill-scoped Stop/PreToolUse guard 不执行；通过 `resources.d2c_gate` 提供
  owner 绑定的显式证据检查。每个请求须重新初始化并认领 checkpoint；它不等价于原生 Stop Hook，
  不认证浏览器来源，也不替代用户决策或完整视觉验收，详见[运行时协议](runtime.md)。
- Workspace Apply 需要 `experimental.subagent_depth >= 2`，详见下方多仓配置。

## 克隆并配置本地目录（OpenCode 2.3.2 已验证）

本仓库的插件包位于 `helixharness/` 子目录。实际验证的 OpenCode 2.3.2 原生 `plugin add`
接受 npm/Git 包 spec，不接受绝对本地目录，也不解析 Git 子目录后缀；不能用该命令直接安装
本仓库布局。其他客户端版本需要另行核验。

公开发布后，先克隆源码；将以下目的目录占位值替换成你选择的路径：

```bash
git clone https://github.com/didi/helixharness.git /absolute/path/to/helixharness-source
```

将插件子目录的绝对路径合并到项目或全局 `opencode.json(c)` 的 `plugins` 数组，保留其他配置
和插件条目：

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["/absolute/path/to/helixharness-source/helixharness"]
}
```

路径须指向含 `package.json` 的插件目录，不能指向仓库根或单个 `.ts` 文件。独立插件副本也可
配置其包根目录。OpenCode 加载 `package.json` 的 `exports["."]`；插件没有运行时 npm 依赖，
已验证独立副本在无 `node_modules`、未执行 `bun install` 时可加载。开发依赖仅供
`bun test` 和 `tsc` 类型检查使用。配置或源码改变后，重启服务或重新加载插件并新开会话。

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

## 入口预检边界

变更型命令会观察允许名单中的原生工具登记，已知缺少 read/shell 时在 prompt 准入前阻断；
不会自动修改全局配置或批准权限。登记不证明执行权限，嵌套深度当前仍报告 unknown；
Workspace Apply 的 depth ≥ 2 要求和真实派发门禁继续有效。Status/Config 不依赖登记查询。

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

## 更新、回退与卸载

本仓库在 OpenCode 2.3.2 使用上述本地目录配置，更新和回退都由用户操作源码副本：

1. 先检查工作树，保留自己的修改，并记录当前完整 commit 供回退；通过正常 Git 流程取得并核对
   要使用的批准版本。以下占位值必须替换为已核对的完整 commit，不表示已发布的 Tag：

   ```bash
   git -C /absolute/path/to/helixharness-source checkout --detach "<approved-full-commit>"
   ```

2. 重启或重新加载 OpenCode，新开会话并核对实际插件目录与入口。回退时由用户在该源码副本
   执行同样的 `git checkout --detach`，将占位值替换为先前记录且已批准的完整 commit，随后再次
   重载和核对。不要覆盖未保存的本地修改。
3. 卸载时只在项目或全局 `plugins` 数组中移除该插件目录，再重启或重新加载。保留其他插件、
   业务仓库中的 `docs/harness/`、`docs/knowledge/`、`docs/specs/` 和业务代码。

原生 `opencode plugin add` / `update` / `remove` 是 npm/Git 包管理入口；Git 形式要求仓库根
本身就是可直接导出的独立插件包，不适用于本仓库的子目录配置。它们不能替代上述源码更新、
回退或配置移除。此区别已在 2.3.2 核验，不推定其他版本行为。

源码 checkout 可使用 `/helix:update --check` 检查，再按 `/helix:update` 的既有确认流程安装；
检查不切换 HEAD，安装只允许已批准 commit 的快进，不 stash/reset/commit/push。不是干净且有
upstream 的分支时，保留手动更新方式，不把未知版本当作最新。Init 仍只合并 `AGENTS.md`，
不创建 `.opencode/` 项目设置；项目巡检由插件事件触发。
