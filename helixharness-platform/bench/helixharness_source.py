"""Resolve a plugin from a Git commit, independently of the working tree layout."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_MANIFEST = ".claude-plugin/plugin.json"
_COMMANDS = ("commands/propose.md", "commands/apply.md", "commands/archive.md")
_FILE_MODES = {"100644", "100755"}


class HelixHarnessSourceError(ValueError):
    """The selected revision or plugin path cannot identify one valid plugin."""


def _git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=False, capture_output=True,
    )
    if result.returncode:
        raise HelixHarnessSourceError("HelixHarness Git repository or revision is unavailable")
    return result.stdout


def git_repository_root(repository: Path) -> Path:
    """Accept a checkout or an existing directory inside it, including worktrees."""
    path = repository.expanduser().resolve()
    return Path(_git(path, "rev-parse", "--show-toplevel").decode().strip())


def validate_plugin_path(value: str) -> str:
    """Require a canonical Git-relative directory; '.' selects the repository root."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise HelixHarnessSourceError("Plugin path must be a non-empty Git-relative directory")
    path = PurePosixPath(value)
    if (
        path.is_absolute() or ".." in path.parts or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or path.as_posix() != value
    ):
        raise HelixHarnessSourceError("Plugin path must be a canonical Git-relative directory")
    return value


@dataclass(frozen=True)
class FrozenPluginSource:
    """One validated directory and its immutable export identity."""

    path: str
    tree: str


def frozen_plugin_source(
    repository: Path, commit: str, *, plugin_path: str | None = None,
) -> FrozenPluginSource:
    """Find exactly one Helix plugin in this revision, or validate an explicit path.

    Only Git objects are inspected. Uncommitted files, symlinks and submodules
    cannot supply the plugin identity or its required command entrypoints.
    """
    root = git_repository_root(repository)
    revision = _git(root, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}").decode().strip()
    selected = validate_plugin_path(plugin_path) if plugin_path is not None else None
    entries: dict[str, tuple[str, str]] = {}
    for record in _git(root, "ls-tree", "-r", "-z", "--full-tree", revision).split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        if kind == "blob":
            entries[name.decode("utf-8", errors="surrogateescape")] = (mode, oid)
    candidates = [selected] if selected is not None else sorted({
        str(PurePosixPath(name).parent.parent)
        for name in entries
        if name == _MANIFEST or name.endswith("/" + _MANIFEST)
    })
    valid = []
    for candidate in candidates:
        prefix = "" if candidate == "." else candidate + "/"
        if any(entries.get(prefix + name, ("", ""))[0] not in _FILE_MODES
               for name in (_MANIFEST, *_COMMANDS)):
            continue
        raw = _git(root, "cat-file", "blob", entries[prefix + _MANIFEST][1])
        try:
            manifest = json.loads(raw)
        except (ValueError, UnicodeError):
            continue
        if isinstance(manifest, dict) and manifest.get("name") == "helix":
            valid.append(candidate)
    if not valid:
        if selected is not None:
            raise HelixHarnessSourceError(
                f"Selected plugin path {selected!r} must contain the helix manifest and propose/apply/archive commands"
            )
        raise HelixHarnessSourceError(
            "Selected commit is missing a valid HelixHarness plugin manifest and propose/apply/archive commands"
        )
    if len(valid) != 1:
        raise HelixHarnessSourceError(
            "Multiple HelixHarness plugins found: " + ", ".join(valid)
            + "; set HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_PLUGIN_PATH to one Git-relative directory"
        )
    path = valid[0]
    tree = revision if path == "." else _git(root, "rev-parse", f"{revision}:{path}").decode().strip()
    return FrozenPluginSource(path=path, tree=tree)


def frozen_plugin_tree(repository: Path, commit: str, *, plugin_path: str | None = None) -> str:
    """Return the export identity for an automatically or explicitly selected plugin."""
    return frozen_plugin_source(repository, commit, plugin_path=plugin_path).tree
