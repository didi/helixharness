"""Resolve a synchronized repository's ledger directory for all eval consumers."""

from __future__ import annotations

import json
import os
from pathlib import Path


def confined_ledger_directory(root: str | Path, relative: str | Path) -> Path:
    """Reject traversal and symlinks that leave the selected repository."""
    root = Path(root).expanduser().resolve()
    relative = Path(relative)
    target = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(root):
        raise ValueError("ledger directory escapes the repository")
    return target


def ledger_files(root: str | Path, directory: Path, *, recursive: bool = False) -> list[Path]:
    """Check every discovered ledger before any consumer reads its contents."""
    root = Path(root).expanduser().resolve()
    paths = sorted(directory.rglob("eval-log.json") if recursive else directory.glob("*/eval-log.json"))
    if any(not path.resolve().is_relative_to(root) for path in paths):
        raise ValueError("ledger file escapes the repository")
    return [path for path in paths if path.is_file()]


def project_ledger_directory(root: str | Path) -> Path:
    """Use the last synchronized layout, with docs/eval as the local default."""
    root = Path(root).expanduser().resolve()
    cache = Path(os.environ.get("HELIXHARNESS_PLATFORM_USAGE_SYNC_CACHE", str(Path(__file__).resolve().parent / ".cache")))
    try:
        state = json.loads((cache / "sync_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    repos = state.get("repos", {}) if isinstance(state, dict) else {}
    for entry in repos.values() if isinstance(repos, dict) else ():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        if Path(entry["path"]).resolve() != root:
            continue
        return confined_ledger_directory(root, str(entry.get("eval_dir") or "docs/eval"))
    return confined_ledger_directory(root, "docs/eval")


def project_ledger_files(root: str | Path, *, recursive: bool = False) -> list[Path]:
    """Fail closed for invalid repository layouts, including symlink loops."""
    try:
        return ledger_files(root, project_ledger_directory(root), recursive=recursive)
    except (OSError, RuntimeError, ValueError):
        return []
