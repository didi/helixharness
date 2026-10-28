# HelixHarness Runtime Preflight

## 目标与边界

HelixHarness 的版本检查、Usage Telemetry、Hook 生命周期和部分辅助能力依赖
Python。任何命令在第一次调用 `python3` 前都必须执行本协议，不能把 shell 的
`command not found` 暴露给用户。

- 最低 Python 版本：3.9。
- 命令名必须是 `python3`，并且可从当前进程的 `PATH` 找到。
- Git 是 HelixHarness repo 工作流的必需依赖，也在同一次 preflight 中检查。
- Python 不可用时无法解析授权缓存和提醒周期，因此 Init 暂不展示统计说明或开启选项，不读取或手写授权缓存。恢复 Python 后重新调用 resolver，按授权缓存和提醒周期决定是否问询，不因缺少异步地址跳过。普通生命周期命令只修复运行依赖，不新增 consent 选择。
- 用户选择保持关闭时，不因 Python 缺失自动修改系统；不依赖 Python 的业务部分
  可以在明确警告后继续。

## 唯一检查入口

命令入口、阶段续跑和 telemetry fallback 在任何 Python 脚本前执行：

```text
sh {plugin_root}/scripts/runtime_preflight.sh check
```

只使用脚本返回的结构化字段：

- `ok=true`：Python 3.9+ 与 Git 均可用，继续版本检查和后续流程。
- Git 检查优先；`git.status=missing`：输出
  `BLOCKED:helixharness_git_runtime_unavailable` 并停止，不得展示 telemetry 选择、继续
  repo 定位或进行业务写入。
- Git 可用且 `python.status=missing|unsupported_version|unusable`：输出
  `WARN:helixharness_python_runtime_unavailable`，不得直接调用 Python 脚本，但仍须按
  下节处理运行依赖恢复，不展示统计开启选项。

## 运行依赖可用后的来源校验

Python/Git 检查通过后，在版本检查和业务读取/写入前执行：

```text
python3 {plugin_root}/scripts/host_runtime_cli.py resolve --platform {canonical_host_id} --expected-plugin-root {session_bound_plugin_root}
```

两个根目录均来自当前 Host Context，不能通过搜索另一安装补齐。只有 `status=resolved` 且
返回的 `plugin_root` 与 SessionStart/当前入口绑定一致才继续；所有插件资源从 `resolved_resources`
和该根目录解析。`plugin_source_mismatch` 按 `BLOCKED:helixharness_host_contract_invalid` 停止，
不尝试其他版本。缺少宿主 SessionStart 的环境使用当前实际命令入口的根目录，不猜测用户缓存。

## Init 在 Python 不可用时的处理

不借缺少 Python 触发统计问询，不因为统计而自动安装 Python。无需 Python 的初始化步骤可以在明确警告后继续，本次不启用统计，不改写已有授权缓存或历史账本。

若当前初始化步骤确实依赖 Python，可单独询问安装运行依赖或停止该步骤。经用户明确确认后执行 `sh {plugin_root}/scripts/runtime_preflight.sh install-python --user-confirmed`，随后重新 `check`。安装器仅使用受支持的系统包管理器；失败时展示受控错误，不绕过确认或继续依赖 Python 的步骤。

恢复成功后回到 Init 的 `telemetry_consent.py resolve`，按 `prompt_due` 和 `allowed_actions` 处理授权；异步地址诊断不阻止授权问询。安装同意不能代替统计授权；不得直接调用 `set --mode enabled`。自动 Agent/Loop 不弹系统安装确认。

## Python 必需工作流命令的恢复

`/helix:propose`、`/helix:apply`、`/helix:archive` 与
`/helix:propose --loop` 与 `/helix:bug-fix` 的确定性 Router、HelixHarness 校验器和 recorder 都需要 Python。
这些公共命令必须在任何 Python 调用和业务读取前执行本协议；Python 不可用时不能
先暴露 `command not found`，也不能绕过 Router 猜测 workflow。

用户直接执行上述命令时，固定提供：

1. `runtime_install_and_continue` — `安装 Python 3.9+ 并继续当前命令`
2. `runtime_stop_without_install` — `暂不安装，停止当前命令`

第一项明确构成本次系统安装确认。执行 `install-python --user-confirmed` 后必须立即
重新 `check`；只有 `python.status=ready` 才从当前公共命令的 Router 步骤继续。该恢复
动作只恢复运行依赖，不读取、开启、关闭或改写 telemetry consent；Python 恢复后由
原工作流按既有 resolver/recorder 规则处理当前 consent。因此无论统计原本 enabled
还是 disabled，都不会因缺少 Python 静默改变授权。

选择第二项、安装失败或复检仍不可用时，输出
`BLOCKED:helixharness_python_runtime_unavailable` 并停止当前命令。自动 Agent/Loop 不弹
系统安装确认；Python 不可用时使用同一 blocker，等待用户直接执行命令后确认。这个
blocker 来自当前命令的 Router/校验器确实依赖 Python，不把 telemetry 本身误写成业务
失败。

## 其他外部依赖

- `claude` / `codex`：由当前宿主提供；版本更新 adapter 已把缺失或执行失败转换为
  受控 warning，不在 preflight 中重复安装。
- Gradle / Maven / npm 等：属于目标项目的构建能力，由 Apply/UT 的项目门禁
  检查，不能在 plugin 安装时全局强装。
- 系统包管理器：只在用户明确确认安装 Python 后探测；缺失时返回
  `python_package_manager_unavailable`，不回退到下载并执行远端脚本。
