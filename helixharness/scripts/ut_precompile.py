#!/usr/bin/env python3
"""Compile current JVM main sources before UT workers consume class outputs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from cli_stdio import configure_utf8_stdio


def _output_path(root: Path, value: Path) -> Path:
    candidate = value if value.is_absolute() else root / value
    if ".." in candidate.parts:
        raise ValueError("Class directory cannot traverse parents")
    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Class directory cannot traverse a symbolic link")
    return candidate


def precompile(repo: Path, *, class_directories: list[Path] | None = None) -> dict[str, Any]:
    """Run the project's build tool every time; old directories cannot grant readiness."""
    result: dict[str, Any] = {
        "status": "blocked", "ready": False, "tests_run": False,
        "command": [], "exit_code": None, "stdout": "", "stderr": "",
        "class_directories": [],
    }
    try:
        root = repo.expanduser().resolve(strict=True)
    except OSError as exc:
        return {**result, "reason": "project_root_invalid", "stderr": str(exc)}
    if not root.is_dir():
        return {**result, "reason": "project_root_invalid"}

    gradle = any((root / name).is_file() for name in (
        "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    ))
    maven = (root / "pom.xml").is_file()
    if gradle == maven:
        return {**result, "reason": "build_tool_ambiguous" if gradle else "build_tool_missing"}
    tool = "gradle" if gradle else "maven"
    requested = class_directories or (
        [Path(f"build/classes/{language}/main") for language in ("java", "kotlin", "groovy")]
        if gradle else [Path("target/classes")]
    )
    result["class_directory_scope"] = "explicit" if class_directories else "standard_root_only"
    try:
        outputs = [_output_path(root, Path(value)) for value in requested]
    except ValueError as exc:
        return {**result, "build_tool": tool, "reason": "class_directory_invalid", "stderr": str(exc)}
    wrapper_name = (
        "gradlew.bat" if os.name == "nt" else "gradlew"
    ) if gradle else ("mvnw.cmd" if os.name == "nt" else "mvnw")
    wrapper = root / wrapper_name
    executable = str(wrapper) if wrapper.is_file() else shutil.which("gradle" if gradle else "mvn")
    if executable is None:
        return {**result, "build_tool": tool, "reason": "build_executable_missing"}
    executable = str(Path(executable).absolute())
    arguments = (
        ["classes", "--no-daemon", "--no-parallel", "--no-watch-fs", "-q"]
        if gradle else ["compile", "--batch-mode", "-q"]
    )
    command = [executable, *arguments]
    result.update({"build_tool": tool, "command": command})
    try:
        completed = subprocess.run(
            command, cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
    except OSError as exc:
        return {**result, "reason": "precompile_start_failed", "stderr": str(exc)}
    result.update({
        "exit_code": completed.returncode,
        "stdout": completed.stdout, "stderr": completed.stderr,
    })
    if completed.returncode != 0:
        return {**result, "reason": "precompile_failed"}

    try:
        outputs = [_output_path(root, value) for value in outputs]
    except ValueError as exc:
        return {**result, "reason": "class_directory_invalid", "stderr": str(exc)}
    directories = sorted({str(value) for value in outputs if value.is_dir()})
    if not directories or (class_directories and any(not value.is_dir() for value in outputs)):
        return {**result, "reason": "class_output_missing"}
    result["class_directories"] = directories
    return {**result, "status": "compile_ready", "ready": True}


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--class-directory", type=Path, action="append",
                        help="Repeat for each active module output verified from current build configuration")
    args = parser.parse_args(argv)
    result = precompile(args.repo_root, class_directories=args.class_directory)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ready"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
