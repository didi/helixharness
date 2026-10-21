---
description: "检查并升级 HelixHarness plugin。通过 Host Kit 识别安装版本、展示更新内容，并在用户授权后执行当前宿主的更新适配器。"
---

> HelixHarness 公共命令唯一契约：本文件同时承载 update 的完整业务步骤和原生命令分发表面。所有宿主都必须通过当前 `resources.command_projection` 加载本文件，并使用预绑定 Host Context；完整读取 `reference/workflow-projection-protocol.md`、`reference/host-runtime-protocol.md`、当前 `resources.runtime` 和 Host Kit 声明的 version/update adapter，不得再读取其他 update command/workflow 补充业务语义。

> Runtime 硬门禁：在任何 `python3` 调用前，必须读取并执行 `reference/runtime-preflight.md`。Python 不可用时输出受控 warning，不能直接调用版本脚本或暴露 `command not found`。

> 参数宽容规则：解析 `$ARGUMENTS` 时先按 shell token 分词；每个 token 仅去除尾部常见中英文句读（`, . ; : ! ? ，。；：！？、` 及右侧闭合括号）后，再对 flag 做精确匹配。数值、枚举和路径参数使用同一规范化结果；不得使用任意子串匹配。

## User Input

```text
$ARGUMENTS
```

## 参数说明

| 参数 | 说明 | 示例 |
|------|------|------|
| `--check` | 只检查版本并展示更新内容，不执行升级 | `/helix:update --check` |
| `--all` | 更新适配器支持多安装范围时，升级它返回的全部已安装范围 | `/helix:update --all` |
| `--strict` | 无法识别版本或正式来源时按阻断处理 | `/helix:update --strict` |

## 执行协议

读取此 plugin 捆绑的 `reference/version-update-protocol.md`，严格按该协议执行。所有正式来源识别、已安装范围、缓存位置、版本权威源和原生安装命令均由当前 Host Kit 的 `version`、`update` 适配器决定；核心命令不得按宿主 ID 分支，也不得自行推断这些值。

> 升级授权硬门禁：禁止 Agent 直接执行任何宿主的 plugin 安装或更新命令。`prepare` 返回正式的 `prompt.title/message/allowed_actions`，交互框必须原样使用；用户选择通过 `decide --action` 处理。真正升级只能由 `scripts/version_update.py install` 调用当前 Host Kit 的更新适配器执行。推荐项、强制策略、缓存恢复或 Agent 自主判断都不能替代弹框确认。无交互环境输出 `BLOCKED:helixharness_update_confirmation_required`，不得升级。

## 执行流程

### Step 1：调用统一版本入口

```text
python3 {plugin_root}/scripts/version_update.py \
  --platform {canonical_host_id} --repo-root {repo_root} check --force-refresh
```

`canonical_host_id` 必须取自预绑定 Host Context，禁止传 `darwin/linux/windows` 等操作系统名。

- 脚本必须从 Host Registry 解析当前宿主，动态加载清单声明的适配器。
- `unsupported_host + continue_silently` 时静默结束，不创建缓存、授权或提示。
- `next_action=continue_with_warning` 时原样输出脚本 `warning`；`--strict` 额外将该状态作为阻断。
- 不得绕过结构化结果，手工补造当前版本、最新版本、安装范围或升级路径。

### Step 2：展示确定性检查结果

只使用 `check` 输出的结构化字段展示：

- 当前版本和最新版本；
- 当前安装范围；
- 是否推荐或强制升级；
- 当前版本到目标版本之间的完整 release notes；
- 跨版本路径是否包含 `update_type=required`。

如果已是最新版本，输出：

```text
HelixHarness 已是最新版本，无需升级。
```

### Step 3：执行统一授权状态机

如果传入 `--check`：

- 只展示版本状态和更新内容；
- 不创建升级 authorization，不执行 `decide/install`；
- 允许版本入口按当前适配器声明的位置刷新检查缓存；
- 用户主动执行本命令或 `--check` 时仍展示更新状态，不受跳过缓存抑制。

如果需要升级且未传 `--check`：

1. 调用 `prepare`，`--scope` 只能使用 `check` 返回且当前更新适配器声明支持的范围；`--all` 只能选择适配器返回的全部已安装范围。
2. 交互框原样使用 `prompt.title/message/allowed_actions`。
3. 用户明确选择后调用 `decide --challenge-id {id} --action {upgrade_now|skip_update} --user-confirmed`。
4. 只有 `upgrade_now` 决策成功后，才能调用 `install --challenge-id {id}`；`install` 是执行宿主原生更新动作的唯一入口。

`skip_update` 必须由 `decide` 保存推荐版本跳过状态，禁止直接改缓存。`prepare`、`decide` 或
`install` 任一步失败都不得声称已升级；required 更新按 `BLOCKED:helixharness_update_required` 收口，
recommended 更新报告真实失败并停止本次升级。缓存不可写时只原样展示脚本 warning，不得改写到
项目仓库。

推荐升级询问模板：

```text
检测到 HelixHarness 有推荐更新：

- 当前版本：{current_version}
- 最新版本：{latest_version}
- 更新摘要：{summary}

是否现在升级？升级后需要重启当前编程平台或新开会话，本次流程会先暂停。

1. 现在升级
2. 跳过本次更新（出现新版本前不再提示）
```

如果升级路径包含强制策略：

- 不提供继续使用旧版本选项；
- 强制策略只能阻止继续使用旧版本，不能替代用户授权升级；
- 用户选择升级时，使用 `prepare → decide(upgrade_now) → install` 一次性授权链路；
- 用户选择跳过时，`decide(skip_update)` 取消 authorization，输出 `BLOCKED:helixharness_update_required` 并停止；
- 无交互环境输出 `BLOCKED:helixharness_update_confirmation_required` 并停止。

强制升级询问模板：

```text
HelixHarness 当前版本必须升级后才能继续：

- 当前版本：{current_version}
- 最新版本：{latest_version}
- 强制原因：{required_reason}

是否立即升级？选择跳过后，本次 HelixHarness 流程将停止。

1. 立即升级
2. 跳过本次升级
```

### Step 4：升级后提示

升级成功后输出：

```text
HelixHarness 已更新。请重启当前编程平台或新开会话，让新的 /helix:* 命令和捆绑资源完全生效。
```

宿主资源声明：`resources.runtime`、`resources.subagent_protocol`、`resources.telemetry_protocol`。
