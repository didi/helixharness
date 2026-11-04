#!/usr/bin/env python3
"""Detect whether a repository is still a greenfield/empty project.

``init`` is a scanner first. A repository with no build metadata and no source
files has nothing to scan, so the caller should take the greenfield bootstrap
path instead of dispatching scan agents that cannot produce artifacts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable


BUILD_FILES = frozenset(
    {
        "package.json",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "go.mod",
        "Cargo.toml",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "setup.cfg",
        "Pipfile",
        "poetry.lock",
        "composer.json",
        "Gemfile",
        "mix.exs",
        "deno.json",
        "deno.jsonc",
        "bun.lock",
        "bun.lockb",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Makefile",
        "CMakeLists.txt",
        "meson.build",
    }
)

SOURCE_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".js",
        ".mjs",
        ".cjs",
        ".ts",
        ".mts",
        ".cts",
        ".tsx",
        ".jsx",
        ".vue",
        ".svelte",
        ".go",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".rs",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".swift",
        ".m",
        ".mm",
        ".dart",
        ".ex",
        ".exs",
        ".clj",
        ".cljs",
        ".fs",
        ".fsx",
    }
)

IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".github",
        ".gitlab",
        ".helixharness",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "dist",
        "build",
        "target",
        "out",
        "coverage",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".cache",
        "__pycache__",
        "vendor",
        "third_party",
        "third-party",
        "docs",
        "doc",
        "documentation",
        "requirements",
        "tmp",
        "temp",
    }
)


def _relative_files(root: Path) -> Iterable[Path]:
    for current, directories, files in os.walk(root):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in IGNORED_DIRECTORIES
        )
        current_path = Path(current)
        for filename in sorted(files):
            yield (current_path / filename).relative_to(root)


def inspect_repository(root: Path) -> dict[str, object]:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("repository directory does not exist")
    signals: list[str] = []
    for relative in _relative_files(root):
        name = relative.name
        if name in BUILD_FILES or relative.suffix.lower() in SOURCE_SUFFIXES:
            signals.append(relative.as_posix())
            if len(signals) >= 20:
                break
    return {
        "empty": not signals,
        "signals": signals,
        "reason": "no_build_or_source_files" if not signals else "project_signals_found",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = inspect_repository(args.repo_root)
    except (OSError, ValueError) as exc:
        print(json.dumps({"empty": None, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
