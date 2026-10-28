"""Resolve the pinned Codex runtime used by evolution subprocesses."""

from __future__ import annotations

import os
from pathlib import Path


def _bundled_codex_path() -> Path:
    from codex_cli_bin import bundled_codex_path

    return Path(bundled_codex_path()).resolve()


def resolve_codex_executable() -> Path:
    """Return an executable Codex binary without assuming a desktop OS layout."""

    path = _bundled_codex_path()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("bundled Codex runtime is unavailable or not executable")
    return path


def codex_runtime_available() -> bool:
    """Report whether Candidate and semantic analysis can actually start."""

    try:
        resolve_codex_executable()
    except (ImportError, OSError, RuntimeError):
        return False
    return True
