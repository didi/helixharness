"""Select a native Bash for shell fixtures, without launching Windows WSL."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def bash_environment(bash: str) -> dict[str, str]:
    environment = dict(os.environ)
    if sys.platform == "win32":
        root = Path(bash).parent.parent
        if root.name.lower() == "usr":
            root = root.parent
        # Non-login Bash does not load Git's profile. Make grep/awk/dirname
        # available without relying on user profiles or the runner's PATH.
        environment["PATH"] = os.pathsep.join((
            str(root / "usr/bin"), str(root / "bin"), environment.get("PATH", ""),
        ))
    return environment


def find_bash() -> str:
    candidates: list[Path] = []
    if sys.platform == "win32":
        git = shutil.which("git.exe")
        if git:
            for root in list(Path(git).parents)[:3]:
                candidates.extend((root / "bin/bash.exe", root / "usr/bin/bash.exe"))
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            directory = os.environ.get(variable)
            if directory:
                candidates.extend((Path(directory) / "Git/bin/bash.exe",
                                   Path(directory) / "Programs/Git/bin/bash.exe"))
    bash = shutil.which("bash")
    if bash:
        candidates.append(Path(bash))
    for candidate in dict.fromkeys(candidates):
        # System32/bash.exe is a WSL launcher, not a shell that understands
        # native Windows fixture paths. Never execute it, even for a probe.
        if sys.platform == "win32" and "system32" in {part.lower() for part in candidate.parts}:
            continue
        if not candidate.is_file():
            continue
        try:
            result = subprocess.run(
                [str(candidate), "--noprofile", "--norc", "-c", 'test -n "$BASH_VERSION"'],
                capture_output=True, timeout=5, check=False, env=bash_environment(str(candidate)),
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0:
            return str(candidate)
    raise RuntimeError("These shell tests require native Bash (Git for Windows on Windows); WSL is not supported")
