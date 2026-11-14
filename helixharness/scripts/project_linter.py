#!/usr/bin/env python3
"""Run a project linter with bounded output and no shell pipeline."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def git_bash() -> str | None:
    """Select Git for Windows' POSIX shell, never the Windows/WSL launcher."""
    if os.name != "nt":
        return shutil.which("bash")
    candidates = []
    explicit = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if explicit:
        candidates.append(Path(explicit))
    git = shutil.which("git")
    if git:
        for parent in Path(git).resolve().parents:
            candidates.extend((
                parent / "bin/bash.exe",
                parent / "usr/bin/bash.exe",
                parent / "usr/bin/sh.exe",
            ))
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        directory = os.environ.get(name)
        if directory:
            candidates.extend((
                Path(directory) / "Git/bin/bash.exe",
                Path(directory) / "Git/usr/bin/sh.exe",
                Path(directory) / "Programs/Git/bin/bash.exe",
                Path(directory) / "Programs/Git/usr/bin/sh.exe",
            ))
    return next((str(path) for path in candidates if path.is_file()), None)


def run_linter(repo: Path, name: str) -> int:
    repo = repo.resolve()
    folder = repo / "docs/harness/linters"
    python_script = folder / f"{name}.py"
    shell_script = folder / f"{name}.sh"
    if python_script.is_file():
        command = [sys.executable, "-X", "utf8", str(python_script)]
    elif shell_script.is_file():
        bash = git_bash()
        if not bash:
            print("HelixHarness: this legacy .sh linter requires Git Bash; regenerate it as Python.")
            return 1
        command = [bash, shell_script.as_posix()]
    else:
        print(f"HelixHarness: missing project linter {name}; rerun /helix:init.")
        return 1
    # A spool avoids unbounded memory use, and preserves the linter's exit code
    # (the old `| head` pipeline reported head's success instead).
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(command, cwd=repo, stdout=output, stderr=subprocess.STDOUT, check=False)
        output.seek(0)
        for _ in range(50):
            line = output.readline(4096)
            if not line:
                break
            print(line.decode("utf-8", errors="replace").rstrip())
    return result.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--linter", choices=("invariant-check", "spec-sync-check"), required=True)
    args = parser.parse_args()
    raise SystemExit(run_linter(args.repo_root, args.linter))
