#!/usr/bin/env python3
"""Merge or verify the current Host Kit's project hooks without replacing user settings."""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path

from host_runtime import HostContractError, HostRegistry
from host_runtime.project_hooks import load_project_hooks

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def validate_events(events: dict) -> None:
    for event, groups in events.items():
        if not isinstance(groups, list):
            raise ValueError(f"invalid project hook groups: {event}")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"invalid project hook group: {event}")
            if "matcher" in group and not isinstance(group["matcher"], str):
                raise ValueError(f"invalid project hook matcher: {event}")
            for handler in group["hooks"]:
                if not isinstance(handler, dict) or not isinstance(handler.get("type"), str):
                    raise ValueError(f"invalid project hook handler: {event}")
                if handler["type"] == "command" and not any(
                    isinstance(handler.get(key), str) and handler[key].strip()
                    for key in ("command", "command_windows")
                ):
                    raise ValueError(f"missing project hook command: {event}")


def merge_payload(current: dict, required: dict) -> dict:
    result = copy.deepcopy(current)
    events = result.setdefault("hooks", {})
    if not isinstance(events, dict):
        raise ValueError("settings.hooks must be an object")
    validate_events(events)
    validate_events(required["hooks"])
    for event, expected_groups in required["hooks"].items():
        groups = events.setdefault(event, [])
        for expected in expected_groups:
            matching = [
                group
                for group in groups
                if group.get("matcher") == expected.get("matcher")
            ]
            if not matching:
                groups.append(copy.deepcopy(expected))
                continue
            for handler in expected["hooks"]:
                found = False
                for group in matching:
                    kept = []
                    for existing in group["hooks"]:
                        if all(
                            existing.get(key) == handler.get(key)
                            for key in ("type", "command")
                        ):
                            if found:
                                continue
                            found = True
                        kept.append(existing)
                    group["hooks"] = kept
                if not found:
                    matching[0]["hooks"].append(copy.deepcopy(handler))
    return result


def project_hooks(repo: Path, platform: str, action: str) -> dict:
    manifest = HostRegistry.discover(PLUGIN_ROOT / "hosts").get(platform)
    if manifest is None:
        raise ValueError("unsupported_host")
    contract = load_project_hooks(manifest)
    if contract.settings_file is None:
        return {"status": "skipped", "reason": "project_hooks_unsupported"}
    repo = repo.resolve()
    if not repo.is_dir():
        raise ValueError("repository directory does not exist")
    target = repo / contract.settings_file
    if target.is_symlink() or not target.resolve().is_relative_to(repo):
        raise ValueError("project hook target escapes repository")
    exists = target.exists()
    current = json.loads(target.read_text()) if exists else {}
    if not isinstance(current, dict):
        raise ValueError("project settings must be an object")
    merged = merge_payload(current, contract.payload)
    if action == "check":
        if not exists or current != merged:
            raise ValueError("project_hooks_missing_or_duplicated")
        return {"status": "pass", "settings_file": contract.settings_file}
    changed = not exists or current != merged
    if changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=".helixharness-hooks-", dir=target.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
            if exists:
                os.chmod(temporary, target.stat().st_mode & 0o777)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {
        "status": "merged",
        "changed": changed,
        "settings_file": contract.settings_file,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("action", choices=("merge", "check"))
    args = parser.parse_args()
    try:
        result = project_hooks(args.repo_root, args.platform, args.action)
    except (OSError, ValueError, HostContractError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
