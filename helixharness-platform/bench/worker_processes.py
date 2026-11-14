"""Track isolated verifier process groups for cleanup by the owning Worker pool."""

from __future__ import annotations

import os
from pathlib import Path

from bench.process_runtime import kill_group  # re-export for existing callers

PROCESS_REGISTRY = "HELIXHARNESS_PLATFORM_WORKER_PROCESS_REGISTRY"


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
