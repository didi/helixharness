"""Run independent acceptance against a completed example checkout.

Usage: python3 verify_feature.py /absolute/path/to/task-filter-demo
This intentionally fails on the unmodified starter.
"""

import copy
import importlib.util
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python3 verify_feature.py /path/to/task-filter-demo")
    path = Path(sys.argv[1]).resolve() / "tasks.py"
    spec = importlib.util.spec_from_file_location("example_tasks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tasks = [
        {"title": "Draft", "done": False},
        {"title": "Review", "done": True},
        {"title": "Ship", "done": False},
    ]
    before = copy.deepcopy(tasks)
    for status, expected in (("all", ["Draft", "Review", "Ship"]),
                             ("open", ["Draft", "Ship"]), ("done", ["Review"])):
        actual = module.list_tasks(tasks, status=status)
        if actual != expected:
            raise AssertionError(f"{status}: expected {expected}, got {actual}")
        if module.list_tasks([], status=status) != []:
            raise AssertionError("empty input must return []")
    if module.list_tasks(tasks) != ["Draft", "Review", "Ship"] or tasks != before:
        raise AssertionError("default behavior or input preservation regressed")
    if module.list_tasks([tasks[1]], status="open") != []:
        raise AssertionError("no matches must return []")
    for invalid in ("unknown", "", "ALL"):
        for items in (tasks, []):
            try:
                module.list_tasks(items, status=invalid)
            except ValueError:
                pass
            else:
                raise AssertionError("invalid status must raise ValueError")
    print("PASS: filtering, ordering, compatibility, empty results, validation and input preservation")


if __name__ == "__main__":
    main()
