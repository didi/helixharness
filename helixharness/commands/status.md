---
description: "只读查看需求阶段、任务进度、阻塞原因和下一步命令。"
---

> HelixHarness 公共命令唯一契约：本文件承载 status 的全部业务步骤。所有宿主通过当前 `resources.command_projection` 加载本文件，使用预绑定 Host Context；完整读取 `reference/workflow-projection-protocol.md`、`reference/host-runtime-protocol.md` 和当前 `resources.runtime`。

> 在任何 Python 调用前执行 `reference/runtime-preflight.md`。本命令不触发版本刷新、统计询问、Stage start/finish、任务执行、索引修复或自动迁移。

## User Input

```text
$ARGUMENTS
```

## 参数

| 用法 | 行为 |
| --- | --- |
| `/helix:status` | 列出当前仓库所有 active 需求，不要求用户先选一个 |
| `/helix:status <feature>` | 查看指定需求；active 不存在时查 completed |
| `/helix:status --completed` | 列出已归档目录中的需求并核验已有证据 |
| `/helix:status <feature> --completed` | 只查指定已归档需求 |

按 shell token 精确解析可选 feature 和 `--completed`，未知参数报错，不猜测需求名或读取其他项目。配置查询引导到 `/helix:config`。

## 执行

```bash
python3 "{plugin_root}/scripts/project_status.py" --repo-root "{repo_root}"
```

按实际参数追加 `--feature "{feature}"`、`--completed`，安全传参；只运行当前插件捆绑脚本。默认列出全部 active，不启动 Router 或其他命令。

用结构化输出展示简洁表格：需求、执行范围、记录阶段、已完成/总任务数、问题、建议命令。Workspace 展示各参与仓库的进度与依赖，不以根 Spec 状态替代子仓事实。

- `empty=true`：说明查询范围内没有需求，不擅自 Init 或新建需求。
- `tasks=null`：展示“尚无任务数据/不适用”，不能当作 0/0 完成。
- `issues` 是实际检查发现的问题；`blocked_tasks` 只展示工件记录的任务与依赖，不编造阻塞原因。
- `next_command` 是建议，不执行。存在问题时先说明需要处理的问题，不能把建议称为已通过所有执行门禁。
- `archive_check=passed` 表示已有归档工件门禁通过；只有 completed 目录且检查通过才显示“已归档”。`failed` 展示原始原因和对应工件位置，不自动改状态或退回 Apply。
- 不重新运行测试或模型评审，不将 task completed、目录存在或摘要状态等同于测试通过。报告注明“依据已有工件检查，未重跑测试和评审”。

只读取规定的项目工件和门禁所需本地状态；不读取统计会话、网络服务或真实数据库，不修复、不写业务文件、不 commit/push。所有其他操作等待用户另行请求。

宿主资源声明：`resources.runtime`。
