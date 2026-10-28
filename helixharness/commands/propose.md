---
description: "收敛需求并生成 Spec/Tasks；--loop 自动串联至 Archive。"
---

# /helix:propose

> HelixHarness 公共命令唯一契约：本文件只负责公共入口、确定性路由和路由结果消费。业务正文由 Router 返回的唯一 `workflow` 文件承载，不在这里复制单仓或 Workspace 步骤。

> Runtime 硬门禁：在任何 Python 调用、需求工件读取或路由判断前，完整读取并执行 `reference/runtime-preflight.md`。Python 不可用时按其中的恢复规则处理；安装并复检成功后才从下方 Router 继续，暂不安装或失败则输出 `BLOCKED:helixharness_python_runtime_unavailable`。

## User Input

```text
$ARGUMENTS
```

## 1. 只执行一次确定性路由

把 `$ARGUMENTS` 原样按当前 shell 的单参数规则安全引用，然后执行：

```bash
python3 "{plugin_root}/scripts/workflow_router.py" --repo-root "{repo_root}" route --command propose --arguments={SHELL_QUOTED_ARGUMENTS} --arguments-mode auto
```

`{plugin_root}` 必须来自当前 plugin command resource，`{repo_root}` 是当前仓库根。不得使用 `eval` 或拼接未经引用的用户文本。除上述 Runtime preflight 外，不得在路由前自行读取 Spec/Tasks、扫描 active/completed 目录或猜测阶段。

`auto` 模式沿用原版名称识别：首个 ASCII 名称 token、引号包裹的名称、`feature=...` 或 `需求名：...` 明确指定 feature；没有明确名称的自然语言只作为需求。补名后必须保留需求正文，名称与已绑定上下文冲突时停止。无参数时由 Router 返回唯一已有需求或候选，不自行扫描。

业务正文只接收 Router 返回的 shell-safe `workflow_arguments`，不重新解析原始 `$ARGUMENTS`。`--` 后全部视为字面需求；`--loop`、`--revise` 由 Router 消费并剥离。旧版本迁移及需求地址参数不属于当前合同。

## 2. 只按 `action` 消费 JSON

退出码 0 或 2 且 stdout 是有效 JSON 时都按 `action` 处理；只有进程异常、无有效 JSON 或未知 action 才输出 `BLOCKED:workflow_router_failed`，不得退回模型推断。

- `load_workflow` 或 `repair_feature`：完整读取 `workflow` 指向的唯一 `workflows/single/*.md` 或 `workflows/workspace/*.md` 正文并执行，传递 `feature`、`execution_scope`、`workflow_arguments`、可选的 `revision_requested` / `workspace_revision` 与调用 trigger。不得改写路径、同时加载另一 scope、重新判断流程或递归调用公共入口。
- `recommend_command`：原样展示 recommendation 并停止，不读取工作流正文。
- `select_feature`：只展示 candidates 让用户选择；回答后保留原始参数，追加 `--feature "{用户确认的 feature}"` 重跑。不得按顺序、修改时间或语义相似度代选。
- `blocked` 且 `reason=feature_required`：只请求明确的 feature 名；回答后保留原始参数，追加 `--feature "{用户确认的 feature}"` 重跑，不把缺少名称当成不可恢复错误。
- 其他 `blocked`：输出 `BLOCKED:{reason}` 后停止，不读取工作流正文，不迁移或覆盖冲突工件。

公共命令不写业务工件；只有所选工作流可按自身合同写入。宿主资源声明：`resources.runtime`、`resources.subagent_protocol`、`resources.telemetry_protocol`。

## 用法

```text
/helix:propose <name> <requirement-or-path> [--loop]
/helix:propose <name> --loop
/helix:propose <name> <revised-requirement-or-path> --revise [--loop]
```

需求来源可为用户文本、文件或可访问 URL。现有 planning 需求可以不重复提供来源。Workspace 由根 `helixharness.workspace.yaml` 确定，不能从需求文字猜仓库。

`--revise` 只用于明确修订未归档的 Workspace 需求，必须提供新的需求来源；它重新进入
Workspace Propose 的决策树，不能直接视为新合同已确认，也不重开归档中的需求。
已完成子仓的旧任务和原始 Apply 基线通过受控 Repair 机制保留。普通重复 Propose
不隐式修订合同；Single 需求的修订仍使用 `/helix:bug-fix`。

## --loop

`loop_requested=true` 时按 Router 返回的 stage 与唯一 workflow 执行 Propose、Apply 或 Archive；以 `trigger=loop` 进入正文，不嵌套调用公共 Apply/Archive。每阶段成功后重新调用本入口的 Router，参数为 `<name> --loop`，继续第一个未完成阶段。新需求从 Propose 开始，已 ready 从 Apply 恢复，有效 implemented 证据从 Archive 恢复。

循环只编排这三个阶段。需求取舍与真实失败仍按阶段协议处理；不得跳过 TDD、双轴 Review、证据门禁或用户确有必要作出的决定。需求与测试 seam 已确认后，正常执行任务与归档无需再询问是否继续。失败或 blocked 时停止，保留可恢复状态。

Archive 的提交／推送选择必须按对应 Archive 正文保留；`NOTICE:lite_telemetry_delivery_required` 按正文仅在最终交付选择前原样展示一次，选择并执行后报告真实状态，不在循环完成摘要中重复提醒。`--loop` 不构成提交或推送授权；已有明确且覆盖当前精确范围的用户授权按正文复用，不重复询问。需要用户作出的 Archive 提交选择必须等待真实回答，发出问题不等于选择完成；不得把未回答当作“暂不提交”，也不得用完成摘要替代待回答的问题。具体提问与等待方式使用当前 Host Kit 的 `ask_user` 映射。

每个实际阶段写独立 Stage Entry，不创建 wrapper entry。阶段工件与证据决定续跑，禁止仅凭对话宣称完成。Archive 成功且 active 已移到 completed 后，不再次用 active 路由探测；只有对应正文要求的最终提交／推送选择已获回答并按选择处理，或按正文无需该选择时，才结束循环。业务归档完成但选择待回答时，保持“归档完成，提交／推送待选择”，不重复 move、start 或 finish，不重开已冻结的 Stage Entry。没有 --loop 时执行本次 Propose 后提示 Apply，不自动实现。循环不包含发布或部署。
