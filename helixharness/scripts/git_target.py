#!/usr/bin/env python3
"""Small deterministic Git target and in-progress-operation helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def resolve_target_branch(repo: Path) -> tuple[str | None, str | None]:
    remote_head = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if remote_head.startswith("origin/"):
        name = remote_head.removeprefix("origin/")
        commit = git(repo, "rev-parse", "--verify", f"refs/remotes/{remote_head}")
        if commit:
            return name, commit
    fallbacks: list[tuple[str, str]] = []
    for name in ("master", "main"):
        commit = (
            git(repo, "rev-parse", "--verify", f"refs/remotes/origin/{name}")
            or git(repo, "rev-parse", "--verify", f"refs/heads/{name}")
        )
        if commit:
            fallbacks.append((name, commit))
    if len(fallbacks) == 1:
        return fallbacks[0]
    return None, None


def operation_state(repo: Path) -> dict[str, object]:
    git_dir_raw = git(repo, "rev-parse", "--git-dir")
    git_dir = Path(git_dir_raw) if git_dir_raw else None
    if git_dir is not None and not git_dir.is_absolute():
        git_dir = repo / git_dir
    markers = {
        "merge": "MERGE_HEAD",
        "rebase": "rebase-merge",
        "rebase_apply": "rebase-apply",
        "cherry_pick": "CHERRY_PICK_HEAD",
        "revert": "REVERT_HEAD",
    }
    operations = sorted(
        name for name, marker in markers.items() if git_dir is not None and (git_dir / marker).exists()
    )
    if git_dir is None:
        operations.append("git_state_unavailable")
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "-z", "--diff-filter=U"],
            check=False,
            capture_output=True,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is None or result.returncode != 0:
        operations.append("git_index_state_unavailable")
        unmerged: list[str] = []
    else:
        unmerged = [
            item.decode("utf-8", errors="backslashreplace")
            for item in result.stdout.split(b"\0")
            if item
        ]
    return {
        "operations": sorted(set(operations)),
        "unmerged_paths": sorted(unmerged),
    }
