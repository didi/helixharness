#!/usr/bin/env python3
"""Inspect or atomically update the canonical Feature task table, without a second plan store."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from atomic_files import atomic_write_text
from workflow import (WorkflowError, TASK_STATUSES, _repair_lock, feature_dir,
                      frontmatter, markdown_cells, task_rows, validate_task_graph)


def _cell_spans(line: str) -> list[tuple[int, int]]:
    """Locate raw cells using the canonical parser's escaped-pipe semantics."""
    end = len(line.rstrip("\r\n"))
    spans = []
    start = index = 0
    while index < end:
        if line[index] == "\\" and index + 1 < end and line[index + 1] == "|":
            index += 2
            continue
        if line[index] == "|":
            spans.append((start, index))
            start = index + 1
        index += 1
    spans.append((start, end))
    if spans and not line[slice(*spans[0])].strip():
        spans.pop(0)
    if spans and not line[slice(*spans[-1])].strip():
        spans.pop()
    return spans


def _load(repo: Path, feature: str):
    root = repo.resolve(strict=True)
    target = feature_dir(root, feature) / "tasks.md"
    if any(parent.is_symlink() for parent in (target, *target.parents) if parent != root and root in parent.parents):
        raise WorkflowError("tasks.md path is unsafe")
    if not target.resolve().is_relative_to(root) or not target.is_file() or target.stat().st_size > 8 * 1024 * 1024:
        raise WorkflowError("tasks.md is missing or unsafe")
    raw = target.read_bytes()
    text = raw.decode("utf-8")
    values, _, _ = frontmatter(text, "tasks.md")
    if values.get("feature") != feature:
        raise WorkflowError("tasks.md feature identity mismatch")
    rows = task_rows(text)
    try:
        next_task, active = validate_task_graph(rows)
    except WorkflowError as exc:
        if str(exc) != "tasks.md has unfinished work but no executable task":
            raise
        next_task, active = None, []
    return target, text, values, rows, {"feature": feature, "status": values.get("status"),
        "tasks": rows, "next_task": next_task, "in_progress_tasks": active,
        "receipt_sha256": hashlib.sha256(raw).hexdigest()}


def inspect_plan(repo: Path, feature: str) -> dict:
    return _load(repo, feature)[-1]


def update_task(repo: Path, feature: str, task: str, status: str, expected_receipt: str) -> dict:
    # Reuse the workflow mutation lock so repair and plan updates cannot race.
    _load(repo, feature)
    with _repair_lock(repo.resolve(), feature):
        target, text, values, rows, result = _load(repo, feature)
        if result["receipt_sha256"] != expected_receipt:
            raise WorkflowError("tasks.md changed; inspect the plan again")
        if values.get("status") not in {"ready", "applying"}:
            raise WorkflowError("plan is not open for progress updates")
        if status not in TASK_STATUSES:
            raise WorkflowError("invalid task status")
        by_id = {row["id"]: row for row in rows}
        if task not in by_id:
            raise WorkflowError("unknown task")
        row = by_id[task]
        if status in {"in_progress", "completed"} and any(by_id[key]["status"] != "completed" for key in row["blocked_by"]):
            raise WorkflowError("task dependencies are incomplete")
        if status != "completed" and any(task in item["blocked_by"] and item["status"] in {"in_progress", "completed"} for item in rows):
            raise WorkflowError("cannot reopen a dependency of started work")
        lines = text.splitlines(keepends=True)
        columns = None
        changed = 0
        for index, line in enumerate(lines):
            cells = markdown_cells(line)
            normalized = [cell.strip().lower() for cell in cells]
            if "id" in normalized and "status" in normalized:
                columns = {cell: position for position, cell in enumerate(normalized)}
            elif columns and len(cells) > max(columns.values()) and cells[columns["id"]].strip() == task:
                # Preserve all unrelated prose, columns, whitespace and line endings.
                spans = _cell_spans(line)
                if len(spans) != len(cells):
                    raise WorkflowError("task cell boundaries are ambiguous")
                start, end = spans[columns["status"]]
                replacement = re.sub(r"\S+", status, line[start:end], count=1)
                lines[index] = line[:start] + replacement + line[end:]
                changed += 1
        if changed != 1:
            raise WorkflowError("task row is ambiguous")
        updated = "".join(lines)
        expected = [{**item, "status": status} if item["id"] == task else item for item in rows]
        if task_rows(updated) != expected:
            raise WorkflowError("plan update changed an unrelated task field")
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected_receipt:
            raise WorkflowError("tasks.md changed before publication")
        atomic_write_text(target, updated, mode=target.stat().st_mode & 0o777)
        return inspect_plan(repo, feature)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--feature", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    update = sub.add_parser("update")
    update.add_argument("--task", required=True)
    update.add_argument("--status", choices=sorted(TASK_STATUSES), required=True)
    update.add_argument("--expected-receipt", required=True)
    args = parser.parse_args()
    try:
        result = (inspect_plan(args.repo_root, args.feature) if args.command == "show" else
                  update_task(args.repo_root, args.feature, args.task, args.status, args.expected_receipt))
    except (WorkflowError, OSError, UnicodeError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
