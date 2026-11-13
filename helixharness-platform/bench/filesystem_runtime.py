"""Cleanup disposable evaluation trees containing read-only Git objects."""

import shutil
from pathlib import Path


def remove_tree(path: Path, *, ignore_errors: bool = False) -> None:
    """Retry a read-only regular file without following symlinks or hiding errors."""
    def retry(function, filename, error):
        target = Path(filename)
        if isinstance(error[1], PermissionError) and target.is_file() and not target.is_symlink():
            target.chmod(target.stat().st_mode | 0o200)
            function(filename)
        else:
            raise error[1]

    try:
        shutil.rmtree(path, onerror=retry)
    except OSError:
        if not ignore_errors:
            raise
