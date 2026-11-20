# Cursor installation / Cursor 安装

Cursor desktop support is stable within the documented capability limits. Use a Cursor marketplace as the normal distribution channel. A published, accessible repository is required. Helix includes the marketplace and plugin manifests, while public Cursor Marketplace listing requires a separate submission and review.

Cursor 桌面接入按已声明的能力边界正式支持，常规分发采用 Cursor 插件市场。安装来源需是已发布且可访问的仓库。Helix 已包含市场和插件清单，Cursor 公共市场上架还需单独提交和审核。

Requirements are Python 3.10+, Git and a working Cursor desktop installation. The launcher resolves a supported interpreter without changing PATH. Native acceptance covers macOS; managed Local installation/update uses macOS/Linux primitives. Windows Cursor is outside this release's verified scope.

需要 Python 3.10+、Git 和可正常使用的 Cursor 桌面客户端。启动器在不修改 PATH 的情况下解析受支持的解释器。原生验收覆盖 macOS，托管 Local 安装/更新使用 macOS/Linux 能力。Windows Cursor 不在本期已验证范围。

## Marketplace installation / 市场安装

For a team marketplace, an administrator imports the release repository under **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo**, then reviews and makes `helix` available. Users open **Customize**, select `helix`, choose **Install** and a project or user scope. Repository-provider access and account permissions follow Cursor's prompts. After installation, reload the window, open a new Agent and select `/helix-status` from `/helix-` completion.

团队市场由管理员在 **Dashboard → Plugins & MCPs → Add Marketplace → Import from Repo** 导入发行仓库，审核并提供 `helix`。用户进入 **Customize**，选择 `helix`，点击 **Install** 并选择项目或用户范围。按 Cursor 提示完成仓库访问与账号权限配置。安装后重载窗口，新建 Agent，从 `/helix-` 补全选择 `/helix-status`。

The combined repository's `.cursor-plugin/marketplace.json` points to `./helixharness`. The standalone plugin's manifest points to `.`. Marketplace availability and updates are managed by Cursor. This release's native smoke used the fixed-source configuration below; the general marketplace UI flow has not been fully accepted for this candidate. That verification gap is accepted within the current release scope. See [Cursor's marketplace documentation](https://cursor.com/docs/plugins).

组合仓库的 `.cursor-plugin/marketplace.json` 指向 `./helixharness`，独立插件仓指向 `.`。市场可见性和更新由 Cursor 管理。本次原生实机样例使用下方固定来源配置；当前候选的普通市场 UI 完整流程保留为已接受的验证缺口。参考 [Cursor 市场说明](https://cursor.com/docs/plugins)。

## Advanced pinned project source / 高级固定来源配置

For reproducible project installation, pin a remote repository and full commit in the project's `.cursor/settings.json`. Keep the plugin key `helixharness-marketplace/helix` and `/helix-xxx` commands. This was tested in Cursor 3.30.14 Agents Window on macOS; it is not an official marketplace listing or acceptance of every Cursor version.

需要固定版本的项目可在 `.cursor/settings.json` 固定远程仓库与完整提交，保留 `helixharness-marketplace/helix` 和 `/helix-xxx`。已实测 macOS Cursor 3.30.14 Agents Window；不代表官方市场上架或所有版本均已验收。

Merge only the Helix entry into existing settings (do not replace other settings). Replace URL and SHA with a reviewed, reachable release; use `.` for a standalone plugin repository and `./helixharness` for the combined repository:

仅合并 Helix 配置，保留其他设置。地址和 SHA 必须换成已审核且可取得的发行提交；独立插件仓用 `.`，组合仓用 `./helixharness`：

```json
{
  "plugins": {
    "helixharness-marketplace/helix": {
      "enabled": true,
      "gitUrl": "https://example.invalid/owner/helixharness.git",
      "gitRef": "REPLACE_WITH_FULL_40_CHARACTER_COMMIT_SHA",
      "gitPath": "./helixharness"
    }
  }
}
```

No source download is required for this manual configuration route: Cursor obtains the pinned package. If a reviewed plugin copy is already available, the optional maintenance tool provides `plan`, `configure`, `check`, and `restore`:

人工配置方式不要求先下载源码，Cursor 会获取固定的插件包。已有审核过的插件副本时，可使用维护工具：

First run `sh "{plugin_root}/scripts/runtime_preflight.sh" check` and require `ok=true` in its JSON response. The following `exec` calls use the same interpreter resolution; do not switch back to an unverified Python command after preflight.

先运行 `sh "{plugin_root}/scripts/runtime_preflight.sh" check`，确认 JSON 中 `ok=true`。下方 `exec` 使用同一解释器解析规则，预检后不改回未验证的 Python 命令。

```text
sh "{plugin_root}/scripts/runtime_preflight.sh" exec "{plugin_root}/scripts/cursor_native_install.py" plan --project-root "{project}" --source "{Git_URL}" --commit "{SHA}"
sh "{plugin_root}/scripts/runtime_preflight.sh" exec "{plugin_root}/scripts/cursor_native_install.py" configure --project-root "{project}" --source "{Git_URL}" --commit "{SHA}" --user-confirmed
sh "{plugin_root}/scripts/runtime_preflight.sh" exec "{plugin_root}/scripts/cursor_native_install.py" check --project-root "{project}" --source "{Git_URL}" --commit "{SHA}" --plugin-root "{actual_Cursor_cache_root}"
sh "{plugin_root}/scripts/runtime_preflight.sh" exec "{plugin_root}/scripts/cursor_native_install.py" restore --project-root "{project}" --user-confirmed
```

Quote actual paths/arguments in your shell. `configure` reports configuration only; after Developer: Reload Window, start a new Agent, select `/helix-status`, and verify the actual Skill/root and Hook binding. A matching cache alone cannot prove current-session selection. The tool accepts strict JSON; it refuses JSONC/duplicate keys without replacing them. Replacement needs `--replace`; restore a previous tool-owned transaction before choosing a new pin. Restore preserves unrelated settings and refuses an externally changed Helix entry.

实际执行需引用路径和参数。配置成功不等于宿主加载成功：重载窗口、新建 Agent、从补全选 `/helix-status`，核验实际 Skill/root 与 Hook 绑定。工具仅接受严格 JSON，JSONC/重复键受控拒绝。不同绑定需 `--replace`；已有本工具事务先 restore 再换 pin。恢复保留其他设置，Helix 键被外部改动时停止。

Before switching, move only an explicitly owned Cursor Local Helix copy to a backup outside `plugins/local`, and disable a conflicting account-native Helix through Cursor UI. Keep Claude/Codex installations and the third-party import switch unchanged. The tool diagnoses Local conflicts but never deletes caches, moves other installations, or edits private Cursor databases. If loading fails, Imported/old cache may remain usable; that is not proof the new candidate loaded.

切换前仅将已确认归属的 Cursor Local Helix 可恢复备份到扫描目录外，通过 Cursor UI 停用冲突的账号原生 Helix。不改 Claude/Codex 或全局导入开关。工具只诊断冲突，不自动清缓存或操作其他安装。失败回退到 Imported/旧缓存不能算新版本安装成功。

Native automatic upgrade through `/helix-update` is unsupported in this release. `--check` shows current installation and a nonblocking warning; it does not assert the latest release. Replace the reviewed project pin explicitly (or use the owning marketplace's supported controls), reload and reverify. A pinned SHA does not follow branch updates. Existing registered managed-local installations retain their separate automatic-update/recovery contract.

本期原生渠道不支持 `/helix-update` 自动升级。`--check` 只显示当前安装和非阻塞提示，不宣称已是最新。升级需明确更换已审核 pin（或使用所属市场的管理入口），重载并复验；固定 SHA 不自动跟随分支。已有 managed-local 的升级/恢复能力保留。

Statistics retain `partial`/`unavailable` when attribution is incomplete. The native test covered isolated real Hook events, a loopback ACK and repeat-collection deduplication, not full token precision, default-background delivery or production-server ingestion. `sessionEnd` remains best effort; revoke consent for explicit scoped cleanup. CLI/Cloud and full D2C acceptance are outside this native installation smoke test.

统计保留 `partial/unavailable`；当前原生验证是隔离环境真实 Hook、本机接收和重复采集去重，不代表全量 token、默认后台投递或生产入库验收。`sessionEnd` 仍尽力执行，关闭授权触发明确清理；CLI/Cloud、完整 D2C 不由本次安装 smoke 替代。

## Optional Local installation / 可选本地安装

Cursor 插件清单为 `.cursor-plugin/plugin.json`，只装载当前宿主的薄 Skills 与 hooks。
根 marketplace 指向 `./helixharness`；独立插件副本的 marketplace 指向 `.`。

### Helix 管理的本地安装

`plugin_update=emulated` 使用 Helix 自己管理的 local 安装，不是 Cursor 原生 marketplace 更新 API。
初装前展示 Git 来源、所跟随的 branch/tag/commit 和目标 `~/.cursor/plugins/local/helix`，取得一次真实用户确认，
执行 runtime preflight 后运行安装器（此维护 CLI 不增加公开业务命令）：

```text
sh "{plugin_root}/scripts/runtime_preflight.sh" exec "{plugin_root}/scripts/cursor_managed_install.py" install --source "{已确认的Git来源}" --ref "{已确认的ref}" --user-confirmed
```

来源只接受无内嵌凭据的 HTTPS/SSH Git URL 或用户明确选定的绝对本地 Git 路径。拒绝 URL password、
query 和 fragment；本地来源仅发布所选 Git commit，未提交文件不会安装。安装器从 Git archive 选择分发目录，
排除 `.git`、`.env*`、用户配置、运行输出和缓存，拒绝链接与越界路径；不递归复制工作目录。

目标必须不存在。安装器不接管已有手工副本、Cursor marketplace cache 或其它宿主管理的安装。
已有安装需用户先通过所属管理方式移除，再明确选择初装；不能为通过登记检查而手写收据。
初装生成独立登记 `~/.cursor/helixharness/managed-install.json`，以及安装内的 `.helixharness-managed.json` 收据。
登记固定安装 ID、Git 来源/ref、插件子路径和安装根；收据记录版本、commit、分发树摘要与登记摘要。
初装在移动目录前，将已核验候选持久保存到 `~/.cursor/helixharness/initial-{install-id}/plugin`，
并写入 `managed-install.pending.json`。权限失败、进程中断或登记发布后的同步失败会保留可恢复状态。
修复环境问题并取得用户确认后，运行 `cursor_managed_install.py recover-initial --user-confirmed`，
或以相同来源和 ref 重试初装。两者都续办原日志冻结的 commit，不重新获取 branch 上的新版本。
恢复只接受原日志绑定的安装 ID、来源、收据和文件树；未知目录、被替换的登记或越界暂存路径均拒绝。
目录和登记均以独占重命名发布，不覆盖并发出现的陌生内容；不能手工补造登记或覆盖现有插件。
成功后清理本次暂存目录和 pending 日志。日志发布前的普通失败会清理暂存目录；在该窗口强制终止
进程可能留下未被日志引用的暂存目录，安装器不会猜测其归属并自动删除。

当前受管理更新支持 macOS/Linux 的原子目录交换。安装与更新均使用真实目录，不使用 symlink；
不支持原子交换的环境受控失败，不退化为原地逐文件覆盖。测试可用 `HELIXHARNESS_CURSOR_HOME` 指向
隔离目录；这不改变真实 Cursor 的插件发现设置，不能仅凭测试目录存在宣称 IDE 已加载。

### 本地 IDE 验收与手工副本

将**完整插件目录**复制到 `~/.cursor/plugins/local/helix`，其中必须包含 `.cursor-plugin/plugin.json`、
`hosts/`、`commands/`、`workflows/`、`skills/` 和 `scripts/`。然后重载窗口，在 Customize 检查组件。
不只复制薄 Skill；它们依赖同一安装内的 canonical 文件。
此手工复制方式可用于本地安装及验收，但没有 Helix 管理登记，因此版本更新入口会明确报告来源不可识别；
需要自动升级时使用上一节的受管理初装。

本地导入受 Cursor 管理策略约束；企业管理员可以禁用。已安装同名 marketplace 插件优先于本地副本。
Cursor 不加载指向该 local 目录外部的 symlink，不把创建外部软链当作安装成功。
本说明不代表插件已经提交或获准在官方 marketplace 发布。

确认九个入口：`helix-init`、`helix-propose`、`helix-apply`、`helix-archive`、`helix-bug-fix`、
`helix-ut`、`helix-update`、`helix-status`、`helix-config`。通过 `/` 搜索相应 Skill；
canonical `/helix:*` 由 runtime 映射，参数按 Skill 入口合同绑定；本期不提供 `/helix:xxx` 冒号别名。

新开会话确认 Hook 注入 `host_id=cursor` 与当前安装路径；仅菜单可见不能证明 hooks 已执行。
用合成仓库验证 Init、参数保真、Router、独立子 agent、工作流门禁与重复 Init 的幂等性。

## 本地 CLI 与能力边界

官方 CLI 可用 `agent --plugin-dir "{绝对插件目录}"` 临时装载，多个目录可重复该参数。
这只用于独立验收，不等同于 IDE 安装或稳定支持。测试需隔离用户配置、Git hooks、凭据与真实对话，
不得为了 headless 测试自动设置 `--force`、`--yolo` 或扩大审批策略。

`agent update` 和 Cursor `/update` 更新 Agent 本体，不能用来升级 HelixHarness。
Cursor 官方 marketplace/dashboard 的 Refresh 与 Auto Refresh 由其管理，不属于本适配器。
卸载通过 Cursor 的插件管理，只移除选定安装；不删除业务仓库工件。

## 受管理更新与恢复

`helix-update` 复用公共 `check → prepare → decide → install`：scope 为 `local`。检查在临时目录读取 Git
快照和版本策略，不改变安装；授权前再次校验独立登记与当前安装树。确认框明确展示来源、安装位置、commit
与摘要。一次性授权绑定这些事实，安装时不接受相同版本号下的不同文件树，也不接受被替换的登记或来源。
branch 在确认后推进时仍只安装已批准的精确 commit；该 commit 不可获取则失败，不能偷换成最新 branch。

确认后先准备、核验完整候选目录，再原子交换；安装后重新核验版本、commit、目录摘要、Host Kit 与九个入口。
验证失败交换恢复旧目录。交换后进程中断可用同一未过期授权重试，已完成的精确目标不重复部署。
后续检查即使刷新当前版本、发现更高版本或因离线失败，仍可依据独立事务凭据恢复原批准目标。
`helix-update --check` 只展示待恢复事务；正常更新入口优先完成该事务，更高版本需另行确认。
托管目录内仅允许已登记分发文件及 Python 生成的 `__pycache__/*.pyc`。发现额外用户文件、配置、
运行输出或其它未托管路径时拒绝升级，不读取这些文件内容，也不将它们悄悄从当前安装移入备份；
由用户将需保留的内容移到插件安装外后，再重新检查和授权。
成功后一次性授权被消耗，旧目录保留在 `~/.cursor/helixharness/rollback/{challenge-id}`；不自动删除备份。
任何阶段失败都保留真实错误，不能声称更新成功。重试仍须使用公共安装状态机，不直接调用内部部署函数。

完成后执行 Developer: Reload Window 或重启 Cursor，并新开 Agent 会话；再次确认加载路径、版本与组件。
有同名 marketplace 安装时它优先于 local 副本，因此磁盘部署成功不等同于当前会话已切换。

参考文档：[插件安装](https://cursor.com/docs/plugins)、
[插件格式](https://cursor.com/docs/reference/plugins)、
[CLI 参数](https://cursor.com/docs/cli/reference/parameters)。桌面 IDE 按上述范围支持；本地 CLI 待独立验收，Cloud Agents 不在本期范围。
