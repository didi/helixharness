# Filter tasks by completion status / 按完成状态筛选任务

The current `list_tasks(tasks)` returns all task titles. Add an optional `status="all"` argument.

Acceptance criteria:

1. `all` returns every title in its original order. Calling with no status remains compatible.
2. `open` returns only tasks whose `done` value is `False`.
3. `done` returns only tasks whose `done` value is `True`.
4. An empty list or a filter with no matches returns `[]`.
5. Any other status raises `ValueError`, including when the input list is empty.
6. Do not mutate input tasks. Keep the implementation dependency-free and add behavior tests.

Input tasks contain a string `title` and a boolean `done`. Do not add persistence, a Web UI, authentication or deployment.

中文：为现有任务列表增加 `all`、`open`、`done` 三种状态筛选，默认行为保持兼容，保持顺序、不修改输入，无匹配返回空列表，非法状态抛出 `ValueError`。只处理这个明确需求，使用标准库并补充测试。
