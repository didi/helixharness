"""Resolve the command execution scope without running lifecycle business logic."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class WorkflowTargetError(ValueError):
    """Raised when the execution scope is unsafe or unsupported."""


def resolve_workflow_target(
    repo_root: str | Path,
    command: str,
    declared_scope: str | None = None,
) -> dict[str, Any]:
    """Return the canonical workflow body and its Single or Workspace scope."""
    if command not in {"propose", "apply", "archive", "bug-fix"}:
        raise WorkflowTargetError("workflow command is unsupported")
    root = Path(repo_root).resolve()
    marker = root / "helixharness.workspace.yaml"
    marker_present = os.path.lexists(marker)
    if marker_present:
        if marker.is_symlink() or not marker.is_file():
            raise WorkflowTargetError("helixharness.workspace.yaml is unsafe")
    if declared_scope not in {None, "single", "workspace"}:
        raise WorkflowTargetError("execution_scope is invalid")
    scope = declared_scope or ("workspace" if marker_present else "single")
    if scope == "workspace" and not marker_present:
        raise WorkflowTargetError("workspace execution_scope requires helixharness.workspace.yaml")
    if scope == "single" and marker_present:
        raise WorkflowTargetError("single execution_scope conflicts with workspace root")
    if scope == "workspace" and command not in {"propose", "apply", "archive"}:
        raise WorkflowTargetError(
            "Workspace supports only propose, apply, and archive"
        )
    return {
        "execution_scope": scope,
        "workflow": f"workflows/{scope}/{command}.md",
    }
