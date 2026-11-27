#!/usr/bin/env python3
"""Commit Init checkpoints after the parent has verified the actual artifacts.

The verification flags are the parent's declarations, not independent proof of
semantic validity. This writer does not scan source, merge hooks, validate
knowledge content, or replace the Init command's item-by-item gates.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any


BACKEND_SCANS = ["scan-tech", "scan-db", "scan-api", "scan-service",
                 "scan-boundary", "scan-service-meta"]
FRONTEND_SCANS = ["scan-frontend-meta", "scan-frontend-api", "scan-frontend-routes",
                  "scan-frontend-state", "scan-frontend-components", "scan-frontend-tracking"]
SCAN_PLANS = {"service": BACKEND_SCANS, "frontend": FRONTEND_SCANS,
              "fullstack": BACKEND_SCANS + FRONTEND_SCANS}
PROJECT_TYPES = {"service": {"backend", "unknown"}, "frontend": {"frontend"},
                 "fullstack": {"fullstack-node", "fullstack-separated", "monorepo"}}
STATE_FILE = "docs/.helixharness-init-state.json"


class InitStateError(ValueError):
    """A blocked transition; callers must retain artifacts and not claim success."""


def _timestamp(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _read_state(repo: Path, mode: str) -> tuple[Path, dict[str, Any]]:
    target = repo / STATE_FILE
    if target.is_symlink() or not target.resolve().is_relative_to(repo):
        raise InitStateError("init_state_path_invalid")
    try:
        state = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise InitStateError("init_state_missing_or_invalid") from exc
    if not isinstance(state, dict):
        raise InitStateError("init_state_missing_or_invalid")
    if not isinstance(state.get("projectType"), str) or state["projectType"] not in PROJECT_TYPES[mode]:
        raise InitStateError("init_state_project_type_mismatch")
    plan = state.get("scanPlan")
    if not isinstance(plan, list) or any(not isinstance(item, str) or not item.strip() for item in plan):
        raise InitStateError("init_state_scan_plan_invalid")
    if state.get("scanStatus", "pending") not in ("pending", "completed"):
        raise InitStateError("init_state_scan_status_invalid")
    if state.get("bootstrapStatus", "pending") not in ("pending", "backend_completed", "completed"):
        raise InitStateError("init_state_bootstrap_status_invalid")
    for key in ("completedAt", "backendCompletedAt", "frontendCompletedAt"):
        if key in state and not _timestamp(state[key]):
            raise InitStateError("init_state_checkpoint_time_invalid")
    if state.get("bootstrapStatus") != "completed" and "completedAt" in state:
        raise InitStateError("init_state_premature_completion")
    if state.get("bootstrapStatus") == "backend_completed":
        if mode != "fullstack" or not _timestamp(state.get("backendCompletedAt")):
            raise InitStateError("backend_bootstrap_incomplete")
    if state.get("bootstrapStatus") == "completed" and not _timestamp(state.get("completedAt")):
        raise InitStateError("init_state_completion_time_invalid")
    return target, state


def _deliverables(repo: Path, supplied: Any) -> dict[str, Any]:
    if not isinstance(supplied, dict) or set(supplied) != {"knowledge", "harness", "hooks"}:
        raise InitStateError("init_state_deliverables_invalid")
    result = {}
    for group, paths in supplied.items():
        if not isinstance(paths, list) or not paths:
            raise InitStateError("init_state_deliverables_invalid")
        normalized = set()
        for relative in paths:
            if not isinstance(relative, str) or not relative.strip():
                raise InitStateError("init_state_deliverable_path_invalid")
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or str(path) == ".":
                raise InitStateError("init_state_deliverable_path_invalid")
            target = repo / str(path)
            try:
                if not target.resolve().is_relative_to(repo) or not target.is_file() or target.stat().st_size == 0:
                    raise InitStateError("init_state_deliverable_missing_or_invalid")
            except (OSError, ValueError) as exc:
                raise InitStateError("init_state_deliverable_missing_or_invalid") from exc
            normalized.add(str(path))
        files = sorted(normalized)
        result[group] = {"files": files, "count": len(files)}
    return result


def _write_atomic(target: Path, state: dict[str, Any]) -> None:
    temporary: str | None = None
    try:
        encoded = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".helixharness-init-state-", suffix=".tmp", dir=target.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, target.stat().st_mode & 0o777)
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError) as exc:
        raise InitStateError("init_state_update_failed") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def transition(repo_root: Path, mode: str, action: str, *,
               deliverables: Any = None, verified: dict[str, bool] | None = None,
               now: int | float | None = None) -> dict[str, Any]:
    """Update only checkpoint fields; the Init parent is the sole caller/writer.

    An interrupted or failed parent does not call complete. A new Init run must
    first execute project detection, which replaces the prior run's state.
    """
    if mode not in SCAN_PLANS:
        raise InitStateError("unsupported_init_mode")
    if action not in ("scan-complete", "backend-complete", "complete"):
        raise InitStateError("unsupported_init_state_action")
    repo = Path(repo_root).expanduser().resolve()
    target, state = _read_state(repo, mode)
    updated = copy.deepcopy(state)
    timestamp = time.time() if now is None else now
    if not _timestamp(timestamp):
        raise InitStateError("init_state_timestamp_invalid")
    gates = {} if verified is None else verified
    if not isinstance(gates, dict):
        raise InitStateError("init_completion_gates_incomplete")

    if action == "scan-complete":
        if state.get("bootstrapStatus") == "completed":
            raise InitStateError("init_state_already_completed")
        if not updated["scanPlan"]:
            updated["scanPlan"] = list(SCAN_PLANS[mode])
        updated["scanStatus"] = "completed"
        updated.setdefault("bootstrapStatus", "pending")
    else:
        if state.get("scanStatus") != "completed":
            raise InitStateError("init_scan_incomplete")
        required = ("artifacts",) if action == "backend-complete" else ("artifacts", "hooks", "telemetry")
        if any(gates.get(gate) is not True for gate in required):
            raise InitStateError("init_completion_gates_incomplete")
        manifest = _deliverables(repo, deliverables)
        old_manifest = state.get("deliverables", {})
        if not isinstance(old_manifest, dict):
            raise InitStateError("init_state_deliverables_invalid")
        if action == "complete" and mode == "fullstack" and state.get("bootstrapStatus") in ("backend_completed", "completed"):
            for group, facts in manifest.items():
                previous = old_manifest.get(group)
                if (not isinstance(previous, dict) or not isinstance(previous.get("files"), list)
                        or any(not isinstance(path, str) for path in previous["files"])):
                    raise InitStateError("init_state_deliverables_invalid")
                if not set(previous["files"]).issubset(facts["files"]):
                    raise InitStateError("backend_deliverables_missing")
        # Retain extension groups and group-specific metadata while replacing only files/count.
        merged_manifest = copy.deepcopy(old_manifest)
        for group, facts in manifest.items():
            previous = merged_manifest.get(group, {})
            if not isinstance(previous, dict):
                raise InitStateError("init_state_deliverables_invalid")
            merged_manifest[group] = {**previous, **facts}

        if state.get("bootstrapStatus") == "completed":
            if action != "complete" or old_manifest != merged_manifest:
                raise InitStateError("init_state_already_completed")
            if mode == "fullstack" and not all(_timestamp(state.get(key)) for key in ("backendCompletedAt", "frontendCompletedAt")):
                raise InitStateError("backend_bootstrap_incomplete")
        elif action == "backend-complete":
            if mode != "fullstack":
                raise InitStateError("backend_checkpoint_requires_fullstack")
            updated["deliverables"] = merged_manifest
            updated["bootstrapStatus"] = "backend_completed"
            updated.setdefault("backendCompletedAt", timestamp)
        else:
            if mode == "fullstack":
                if state.get("bootstrapStatus") != "backend_completed" or not _timestamp(state.get("backendCompletedAt")):
                    raise InitStateError("backend_bootstrap_incomplete")
                updated["frontendCompletedAt"] = timestamp
            updated["deliverables"] = merged_manifest
            updated["bootstrapStatus"] = "completed"
            updated["completedAt"] = timestamp

    changed = updated != state
    if changed:
        _write_atomic(target, updated)
    return {"status": "updated" if changed else "unchanged", "changed": changed,
            "action": action, "state_file": STATE_FILE,
            "scan_status": updated.get("scanStatus"),
            "bootstrap_status": updated.get("bootstrapStatus")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--mode", choices=tuple(SCAN_PLANS), required=True)
    parser.add_argument("action", choices=("scan-complete", "backend-complete", "complete"))
    parser.add_argument("--deliverables-json", help="Object with knowledge/harness/hooks relative path arrays")
    parser.add_argument("--artifacts-verified", action="store_true")
    parser.add_argument("--hooks-verified", action="store_true")
    parser.add_argument("--telemetry-verified", action="store_true")
    args = parser.parse_args()
    try:
        supplied = json.loads(args.deliverables_json) if args.deliverables_json is not None else None
        result = transition(args.repo_root, args.mode, args.action, deliverables=supplied,
                            verified={"artifacts": args.artifacts_verified,
                                      "hooks": args.hooks_verified,
                                      "telemetry": args.telemetry_verified})
    except (InitStateError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
