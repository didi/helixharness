#!/usr/bin/env python3
"""One-shot Cursor shutdown cleanup; never a collector, sender or scheduler."""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import telemetry_consent_state as consent  # noqa: E402
import telemetry_ledger  # noqa: E402


@contextmanager
def cleanup_lease(repo: Path):
    """Deduplicate only Cursor cleanup, with crash-safe OS lock release."""
    path = telemetry_ledger._managed(repo, Path(".helixharness/telemetry-v2/cursor-cleanup.lock"))
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
        else:
            import fcntl
        try:
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def cleanup_repository(repo: Path) -> bool:
    """Hold current disabled consent throughout deletion; re-enabling waits."""
    private = telemetry_ledger._managed(repo, Path(".helixharness/telemetry-v2"))
    if not private.is_dir():
        return False
    with cleanup_lease(repo) as acquired:
        if not acquired:
            return False
        with consent.consent_snapshot(repo, "cursor", deadline=time.monotonic() + 1.5) as state:
            if state.get("effective_mode") != "disabled":
                return False
            import metrics
            metrics.purge_private_states(repo, "cursor", strict_scope=True)
            return True


def main(workspaces: list[str] | None = None) -> int:
    deadline = time.monotonic() + 15.0
    seen: set[Path] = set()
    for workspace in workspaces if workspaces is not None else sys.argv[1:]:
        if time.monotonic() >= deadline:
            break
        try:
            if not Path(workspace).is_absolute():
                continue
            repo = consent._repo_root(workspace, deadline=deadline)
            if repo in seen:
                continue
            seen.add(repo)
            cleanup_repository(repo)
        except (OSError, ValueError):
            # No persistent job and no authorization guess on a failed attempt.
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
