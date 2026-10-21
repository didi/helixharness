#!/usr/bin/env python3
"""Small same-directory atomic file writer used by workflow state updates."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class AtomicWriteError(OSError):
    """Raised when an atomic write target is not safe or cannot be replaced."""


def atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    """Replace a regular target without following a predictable temp symlink."""
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise AtomicWriteError("atomic write parent is unsafe")
    if os.path.lexists(path) and path.is_symlink():
        raise AtomicWriteError("atomic write target is unsafe")

    descriptor = -1
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise AtomicWriteError(str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def atomic_create_text(path: Path, text: str, *, mode: int = 0o644) -> bool:
    """Publish a complete regular file only when the target does not exist."""
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir():
        raise AtomicWriteError("atomic create parent is unsafe")

    descriptor = -1
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        os.chmod(temporary, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    except OSError as exc:
        raise AtomicWriteError(str(exc)) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
