# First feature: filter a task list / 第一个需求：任务筛选

This example uses Python's standard library, synthetic task data and one small behavior change. Install the plugin first using the [installation guide](../docs/installation.md). A model service is needed to run the agent workflow; the baseline tests and acceptance script themselves do not call a model.

## 1. Create an application repository

From the combined HelixHarness source root, copy the starter to a separate temporary directory:

```bash
export HELIXHARNESS_EXAMPLE_SOURCE="$PWD/examples"
TASK_FILTER_DEMO="$(mktemp -d)/task-filter-demo"
mkdir -p "$TASK_FILTER_DEMO"
cp -R "$HELIXHARNESS_EXAMPLE_SOURCE/starter/." "$TASK_FILTER_DEMO/"
cd "$TASK_FILTER_DEMO"
git init -b main
# Configure your own Git identity if you have not already done so.
git add tasks.py test_tasks.py requirements/
git commit -m "Add task-list starter"
python3 -m unittest -v
```

The baseline has two passing tests. It returns all titles but does not yet accept a status filter. Do not run the development workflow inside the HelixHarness plugin source.

## 2. Run HelixHarness

Open Codex, Claude Code or OpenCode in the `$TASK_FILTER_DEMO` directory (run `pwd` in that terminal to see its path) and enter:

```text
/helix:init
/helix:propose task-filter ./requirements/task-filter.md --loop
```

Allow the host's project-hook setup when prompted. For this example, choose telemetry **off** when asked. Review the proposed requirement boundaries and answer any clarification in the conversation.

Expected workflow:

| Stage | What to inspect |
| --- | --- |
| Propose | `docs/specs/active/task-filter/spec.md` and `tasks.md`: the six supplied acceptance criteria |
| Apply | A backward-compatible filter, behavior tests, code review and `verify-report.md` with actual evidence |
| Archive | The completed feature under `docs/specs/completed/task-filter/`; source and tests remain reviewable |

To run each stage manually, omit `--loop`, then enter `/helix:apply task-filter` and `/helix:archive task-filter` after completing the preceding stage.

## 3. Independently verify the result

In the same terminal:

```bash
cd "$TASK_FILTER_DEMO"
python3 -m unittest -v
python3 "$HELIXHARNESS_EXAMPLE_SOURCE/verify_feature.py" "$TASK_FILTER_DEMO"
git diff
git log -5 --oneline
```

The acceptance script checks filtering, order, backward compatibility, empty/no-match behavior, invalid status and input preservation. **It intentionally fails on the starter.** `git diff` may be empty if the workflow committed the result; inspect the relevant commits too.

Check that the completed feature includes Spec, Tasks and verification evidence. A final agent message or passing acceptance script alone does not establish that review and Archive completed.

This is a reproducible walkthrough, not a pre-recorded model run. No generated Spec or review approval is fabricated in this example.

## 中文操作说明

按第一步复制起始项目到独立临时目录并建立 Git 基线，两个基础测试应通过。在该业务目录打开宿主，依次执行 Init 和上面的 Propose 命令；示例可关闭统计，不需要平台地址。

检查需求是否保留六条验收标准，Apply 是否实现筛选并补充测试、评审，Archive 是否生成完整的 completed 工件。最后运行基础测试和独立验收脚本。起始代码运行独立验收会失败，这是因为筛选需求尚未实现；不要把教程中的预期工件当作已经发生的模型执行记录。
