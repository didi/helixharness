# macOS and Windows / macOS 与 Windows

HelixHarness targets Python 3.10+ on macOS, native Windows and Linux. Install Git and a plugin-capable host. Windows does not require WSL. Use a normal user account and a local filesystem supporting atomic file replacement and hard links (NTFS on Windows).

HelixHarness 面向 macOS、原生 Windows 与 Linux，要求 Python 3.10+、Git 和支持插件的宿主。Windows 无需 WSL；使用普通用户和支持原子文件替换、硬链接的本地文件系统（Windows 使用 NTFS）。

| Component / 组件 | macOS / Linux | Windows |
| --- | --- | --- |
| Codex plugin / 插件 | POSIX preflight and hooks | PowerShell preflight; Codex `commandWindows` hooks |
| Claude Code plugin / 插件 | Bash hooks | Git for Windows Bash; set `CLAUDE_CODE_GIT_BASH_PATH` if auto-discovery fails |
| Project linters / 项目检查 | Python scripts | Same Python scripts; legacy `.sh` scripts require Git Bash |
| Platform Web, Demo, eval / 平台 | Python service | Same Python service, PowerShell environment syntax |
| Scheduled usage sync / 定时同步 | crontab | Task Scheduler, current user logged in; cron matching in Python |
| Bench workers / 评测 Worker | Process groups | Windows Job Objects; no `killpg` dependency |
| Candidate / 候选生成 | Ordinary user: Codex sandbox | Ordinary user: Codex sandbox |
| Optional root user isolation / 可选 root 隔离 | Linux `setpriv` only | Leave `HELIXHARNESS_PLATFORM_EVOLUTION_EXECUTION_USER` unset; do not configure Linux user isolation |
| D2C browser helpers / 浏览器工具 | Node and Chrome/Edge | Node and Chrome/Edge; native executable discovery |

The host controls hook trust and execution. Codex must support `commandWindows`; Claude Code must support Bash hooks (`shell: "bash"`). Review changed hooks after updating and start a new session. Merely seeing commands in the menu does not verify hooks.

宿主负责 Hook 信任与执行。Codex 需要支持 `commandWindows`，Claude Code 需要支持 Bash Hook。升级后审阅变更的 Hook 并新开会话；菜单中可见命令不等于 Hook 已执行。已有业务项目重新运行 `/helix:init`，由 `project_hooks_cli.py merge` 更新本插件的旧检查命令，保留用户的其他 Hook。生成的项目 Hook 绑定当前解释器和插件路径；移动 Python 或更新插件安装路径后重新 Init。

## Preflight / 预检

Run from the source plugin directory (`helixharness/`). For an installed plugin, use the exact plugin root bound by the current host.

macOS / Linux / Git Bash:

```sh
sh scripts/runtime_preflight.sh check
sh scripts/runtime_preflight.sh exec scripts/host_runtime_cli.py resolve --platform codex
```

Windows PowerShell 5.1 or 7:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\runtime_preflight.ps1 check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\runtime_preflight.ps1 exec .\scripts\host_runtime_cli.py resolve --platform codex
```

The Windows launcher probes `python.exe`, `python3.exe`, then `py.exe -3`, skips WindowsApps aliases, and actually checks the version. Use its `exec` action or the returned executable/arguments for subsequent Python calls. POSIX launchers also accept `python` and `py -3`; they never install dependencies during a check or hook.

Windows 预检会实际探测解释器，而不是只检查命令名是否存在。文档中的 `python3` 示例在 Windows 上应使用已选中的解释器或 `.ps1 exec` 执行；不要修改 WindowsApps 别名来绕过检查。环境变量使用 `$env:NAME = 'value'`，带空格的路径加引号。依赖安装仍需用户明确同意。

## Verification / 验证

From the plugin directory:

```text
python scripts/run_tests.py
python scripts/codex_skill_projection.py --check
```

From the platform directory:

```text
python -m pip install -e ".[server,dev]"
python -m pytest -q
python -m ruff check web_ui eval bench evolution tests
```

The CI matrix runs these on macOS, Windows and Linux with Python 3.10 and 3.12. Native Windows tests cover PowerShell argv/stdin/UTF-8 and exit codes, lifecycle startup, snapshot read/finish, concurrent initialization, and Job Object descendant cleanup. POSIX signal assertions are run only on POSIX; they are not evidence of Windows behavior.

CI 配置提供三平台回归门禁；只有对应任务实际通过才算该平台验证通过。本地 macOS 的通过结果不能代替 Windows 实机结果。宿主自动触发 Hook、真实模型评测、用户项目构建工具和浏览器操作仍需在目标环境完成验收；测试不会调用付费模型或发布、部署服务。

Issue coverage: #18 (snapshot bytes and read-only cleanup), #19 (exclusive publication of a complete owner marker), #20 (PowerShell bootstrap, interpreter selection, lifecycle hooks and portable project linters). PR #21 addresses the first of these; this change does not merge or close the upstream PR/issues.

Windows 定时同步在页面中安装后由任务计划程序每分钟检查已保存的 cron，只有到期且启用时才同步。支持五字段 cron 和 `@hourly` / `@daily` 等时间别名，不支持 `@reboot`；用户退出登录时不运行，也不补跑关机期间的每个时间点。安装结果通过任务 XML 回读确认；失败时不把配置标记为已启用。
