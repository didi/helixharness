"""Scrubbed subprocess environments that still boot Python on every platform.

Host scenario fixtures deliberately clear the ambient environment so a developer's
tooling cannot make a case pass. Two facts about Windows break a naive scrub:
a child `python.exe` aborts in `_Py_HashRandomization_Init` unless `SystemRoot` is
present, and `os.defpath` has no Git install directory, so `git` and `sh` — which
the fixtures themselves execute — cannot be resolved.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Required by the Windows C runtime to load a freshly started interpreter.
BOOT_VARS = ("SystemRoot", "COMSPEC", "PATHEXT")


def boot_vars() -> dict[str, str]:
    """Environment entries a Windows child interpreter needs to start at all."""
    return {key: os.environ[key] for key in BOOT_VARS if key in os.environ}


def toolchain_dirs() -> list[str]:
    """Directories holding the `git`/`sh` binaries the fixtures invoke.

    POSIX resolves both through `os.defpath`; on Windows they live inside the Git
    installation, so the layout is discovered from the ambient `git` instead of a
    hardcoded path. The Windows `sh` stub in `WindowsApps` starts WSL and is never
    usable here, so only Git-provided directories are returned.
    """
    if os.name != "nt":
        return []
    git = shutil.which("git")
    if not git:
        return []
    directories, current = [], Path(git).resolve().parent
    for _ in range(4):
        for candidate in (current, current / "bin", current / "usr" / "bin"):
            if (candidate / "git.exe").is_file() or (candidate / "sh.exe").is_file():
                if str(candidate) not in directories:
                    directories.append(str(candidate))
        if not current.parent or current.parent == current:
            break
        current = current.parent
    return directories


def search_path() -> str:
    """Minimal PATH: the test interpreter, the fixture binaries, the platform defaults."""
    parts = [str(Path(sys.executable).parent), *toolchain_dirs(), os.defpath]
    seen, ordered = set(), []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            ordered.append(part)
    return os.pathsep.join(ordered)
