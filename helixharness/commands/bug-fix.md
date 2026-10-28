---
description: "修订未归档需求并复用原 Apply 基线。"
---

# /helix:bug-fix

> HelixHarness 公共命令唯一契约：本文件只负责公共入口、确定性路由和路由结果消费。业务正文由 Router 返回的唯一 `workflow` 文件承载，不在这里复制单仓或 Workspace 步骤。

> Runtime 硬门禁：在任何 Python 调用、需求工件读取或路由判断前，完整读取并执行 `reference/runtime-preflight.md`。Python 不可用时按其中的恢复规则处理；安装并复检成功后才从下方 Router 继续，暂不安装或失败则输出 `BLOCKED:helixharness_python_runtime_unavailable`。

## User Input

```text
$ARGUMENTS
```

## 1. 只执行一次确定性路由

把 `$ARGUMENTS` 原样按当前 shell 的单参数规则安全引用，然后执行：

```bash
python3 "{plugin_root}/scripts/workflow_router.py" --repo-root "{repo_root}" route --command bug-fix --arguments={SHELL_QUOTED_ARGUMENTS} --arguments-mode auto
```

`{plugin_root}` 必须来自当前 plugin command resource，`{repo_root}` 是当前仓库根。不得使用 `eval` 或拼接未经引用的用户文本。除上述 Runtime preflight 外，不得在路由前自行读取 Spec/Tasks、扫描 active/completed 目录或猜测阶段。

## 2. 只按 `action` 消费 JSON

退出码 0 或 2 且 stdout 是有效 JSON 时都按 `action` 处理；只有进程异常、无有效 JSON 或未知 action 才输出 `BLOCKED:workflow_router_failed`，不得退回模型推断。

- `load_workflow` 或 `repair_feature`：完整读取 `workflow` 指向的唯一 `workflows/single/*.md` 或 `workflows/workspace/*.md` 正文并执行，传递 `feature`、`execution_scope`、`workflow_arguments` 与调用 trigger。不得改写路径、同时加载另一 scope、重新判断流程或递归调用公共入口。
- `recommend_command`：原样展示 recommendation 并停止，不读取工作流正文。
- `select_feature`：只展示 candidates 让用户选择；回答后保留原始参数，追加 `--feature "{用户确认的 feature}"` 重跑。不得按顺序、修改时间或语义相似度代选。
- `blocked` 且 `reason=feature_required`：只请求明确的 feature 名；回答后保留原始参数，追加 `--feature "{用户确认的 feature}"` 重跑，不把缺少名称当成不可恢复错误。
- 其他 `blocked`：输出 `BLOCKED:{reason}` 后停止，不读取工作流正文，不迁移或覆盖冲突工件。

公共命令不写业务工件；只有所选工作流可按自身合同写入。宿主资源声明：`resources.runtime`、`resources.subagent_protocol`、`resources.telemetry_protocol`。

## 用法

`/helix:bug-fix <name> [description-or-path]`。有问题来源时开始或识别修订；只传名称时先进入正文执行 `repair-preflight`，有未完成修订则复用其身份续跑，无可恢复状态时请求问题来源。只修订 active 单仓需求，不支持 completed 或 Workspace；`repair_feature` 只加载 `workflows/single/bug-fix.md`。
