"""Track isolated verifier process groups for cleanup by the owning Worker pool."""

from __future__ import annotations

import os
import signal
from pathlib import Path

PROCESS_REGISTRY = "HELIXHARNESS_PLATFORM_WORKER_PROCESS_REGISTRY"


def kill_group(pid: int) -> None:
    """Stop a process group, including descendants left after its leader exits."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def register_group(pid: int) -> Path | None:
    """Register a verifier in the private directory inherited from its supervisor."""
    directory = os.environ.get(PROCESS_REGISTRY)
    if not directory:
        return None
    registration = Path(directory) / str(pid)
    registration.touch()
    return registration


def reap_registered_groups(directory: Path) -> None:
    """Clean verifier groups even if their Worker exited without running finally."""
    for registration in directory.iterdir():
        kill_group(int(registration.name))
        registration.unlink(missing_ok=True)
