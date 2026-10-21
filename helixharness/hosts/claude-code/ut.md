# Claude Code UT 运行适配

本资源只实现 [公共 UT 命令](../../commands/ut.md) 的宿主机制，不定义扫描、6 Loop、重试、验证或
完成业务语义。若与公共命令冲突，以公共命令为准；以下映射实现公共 UT 命令的
权限托管、后台 Agent、轮询、超时和精确清理语义。

```text
dispatch_mode: background_agent
experience_write_mode: child_locked_append
runtime_handle_in_queue: true
max_parallel: 10
task_timeout_seconds: 3600
poll_interval_seconds: 2
permission_prepare: claude_project_settings
permission_cleanup: claude_project_settings_exact_revert
```

## Agent/Task 映射

- `dispatch_worker` 使用当前会话真实提供的 `Agent`/`Task` 工具，传公共命令中的完整 worker prompt，
  并设置 `run_in_background=true`。
- 返回的 Agent ID 写入 `unittest-queue.md` 与 `active_slots`；最多 10 个并发 worker。
- `wait_worker` 保持原有每 2 秒检查 `worker-{ID}-result.md` 的机制；超过 3600 秒按公共命令的超时
  协议收口。文件信号、队列对账和 Agent 原生状态冲突时，父流程必须复核真实产物，不能伪造成功。
- 上下文截断后从队列和 result 文件重建 `active_slots`；不模拟其他宿主的 runtime handle。
- worker 按 `child_locked_append` 使用目录锁追加共享经验；父流程仍负责最终汇总和校验。

## 权限准备

`permission_prepare` 的目标是让后台 worker 独立完成临时目录准备、测试生成/运行、覆盖率验证、
测试文件同步和结果写入，且不覆盖用户已有配置。

项目配置固定为 `.claude/settings.local.json`，以追加方式补齐：

```json
{
  "permissions": {
    "allow": ["Bash", "Write", "Edit", "Read", "Glob", "Grep", "Delete", "MCP"],
    "additionalDirectories": ["{系统临时目录}", "{主项目绝对路径}", "/tmp", "/private/tmp", "%TEMP%", "%TMP%", "C:\\Temp"]
  },
  "sandbox": {
    "filesystem": {
      "allowWrite": ["{系统临时目录}", "{主项目绝对路径}", "/tmp", "/private/tmp", "%TEMP%", "%TMP%", "C:\\Temp", "."],
      "allowRead": ["{主项目绝对路径}", "."],
      "allowDelete": ["{系统临时目录}", "/tmp", "/private/tmp", "%TEMP%", "%TMP%", "C:\\Temp"]
    }
  },
  "autoApprove": {
    "Bash": true,
    "Write": true,
    "Edit": true,
    "Read": true,
    "Delete": true,
    "Glob": true,
    "Grep": true,
    "MCP": true
  }
}
```

严格按以下顺序执行：

1. macOS/Linux 优先 `$TMPDIR`、回退 `/tmp`；Windows 优先 `%TEMP%`、回退 `%TMP%`、`C:\Temp`，
   得到 `{系统临时目录}`。macOS 的 `$TMPDIR` 与 `/tmp` 不等价，必须单独加入白名单。
2. 通过 `pwd` 得到 `{主项目绝对路径}`。相对路径 `.` 不能替代绝对路径授权。
3. 把系统临时目录追加到 `permissions.additionalDirectories`、`sandbox.filesystem.allowWrite` 和
   `sandbox.filesystem.allowDelete`；把主项目绝对路径追加到 `permissions.additionalDirectories`、
   `sandbox.filesystem.allowWrite` 和 `sandbox.filesystem.allowRead`。只追加，不替换已有项。
4. 确保 `.claude` 存在，读取或初始化 `settings.local.json`。
5. 读取 `unittest-session.md` 的上次托管时间；超过 24 小时或没有记录时，先精确清理上次托管新增项。
6. 清理包含 `(`、`/` 或空格的错误工具项并提取纯工具名，再逐项追加缺失配置，不覆盖已有配置。
7. 写入并验证配置，把本次创建文件、清理项、新增项、临时目录和主项目绝对路径记录到 session。

session 的托管块必须包含：

```markdown
## HelixHarness 托管 settings.local 权限
- settings 文件：.claude/settings.local.json
- settings 文件由本次创建：{是/否}
- 托管时间戳：{YYYY-MM-DD HH:MM:SS}
- 系统临时目录实际路径：{系统临时目录}
- 主项目绝对路径：{主项目绝对路径}
- 清理的错误格式项：{具体列表或无}
- permissions.allow 本次新增：{具体列表或无}
- permissions.additionalDirectories 本次新增：{具体列表或无}
- sandbox.filesystem.allowWrite 本次新增：{具体列表或无}
- sandbox.filesystem.allowRead 本次新增：{具体列表或无}
- sandbox.filesystem.allowDelete 本次新增：{具体列表或无}
- autoApprove 本次新增键：{具体列表或无}
- 托管状态：active
```

## 精确清理

仅在公共命令 Phase 3 成功后执行：读取 session 中“本次新增”的项目，只删除这些项；不删除用户
已有配置。若配置文件由本次创建且删除新增项后为空，删除该文件，并写 `托管状态：cleaned`。
失败、中断或存在未完成任务时不清理，使下次运行可以按原语义恢复。
