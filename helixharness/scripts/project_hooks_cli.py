#!/usr/bin/env python3
"""Merge or verify the current Host Kit's project hooks without replacing user settings."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

from host_runtime import HostContractError, HostRegistry
from host_runtime.project_hooks import load_project_hooks, validate_flat_payload

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_COMMAND_PREFIX = "powershell.exe -NoProfile -NonInteractive -EncodedCommand "


def windows_command(argv: list[str]) -> str:
    """Keep paths containing spaces, &, % or apostrophes out of cmd.exe syntax."""
    literals = " ".join("'" + item.replace("'", "''") + "'" for item in argv)
    source = f"& {literals}; exit $LASTEXITCODE"
    encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    return WINDOWS_COMMAND_PREFIX + encoded


def is_bound_linter_command(command: str, linter_args: list[str]) -> bool:
    """Recognize our generated command with any absolute interpreter path."""
    if command.startswith(WINDOWS_COMMAND_PREFIX):
        try:
            encoded = command.removeprefix(WINDOWS_COMMAND_PREFIX)
            source = base64.b64decode(encoded, validate=True).decode("utf-16-le")
        except ValueError:
            return False
        match = re.match(r"^& '((?:[^']|'')*)' ", source)
        if match is None:
            return False
        python = match[1].replace("''", "'")
        return Path(python).is_absolute() and command == windows_command([python, *linter_args])
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return (
        argv[1:] == linter_args
        and Path(argv[0]).is_absolute()
        and command == shlex.join(argv)
    )


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
                    for key in ("command", "command_windows", "commandWindows")
                ):
                    raise ValueError(f"missing project hook command: {event}")


def merge_payload(current: dict, required: dict, bound_linters: dict[str, tuple[str, list[str]]]) -> dict:
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
                command = handler.get("command")
                legacy, linter_args = bound_linters.get(command, ("", [])) if isinstance(command, str) else ("", [])
                found = False
                for group in matching:
                    kept = []
                    for existing in group["hooks"]:
                        old_command = existing.get("command")
                        if existing.get("type") == handler["type"] and (
                            old_command == command
                            or (
                                linter_args and isinstance(old_command, str)
                                and (old_command == legacy or is_bound_linter_command(old_command, linter_args))
                            )
                        ):
                            if found:
                                continue
                            found = True
                            if command != old_command:
                                existing["command"] = command
                            old_windows = existing.get("commandWindows")
                            if (
                                "commandWindows" in handler and linter_args
                                and isinstance(old_windows, str)
                                and is_bound_linter_command(old_windows, linter_args)
                            ):
                                existing["commandWindows"] = handler["commandWindows"]
                            for key in ("commandWindows", "shell"):
                                if key in handler:
                                    existing.setdefault(key, handler[key])
                        kept.append(existing)
                    group["hooks"] = kept
                if not found:
                    matching[0]["hooks"].append(copy.deepcopy(handler))
    return result


def merge_flat_payload(current: dict, required: dict, bound_linters: dict[str, tuple[str, list[str]]] | None = None) -> dict:
    """Append native handlers while retaining user options and refreshing owned linters."""

    result = copy.deepcopy(current)
    result.setdefault("version", 1)
    events = result.setdefault("hooks", {})
    validate_flat_payload(result)
    validate_flat_payload(required)
    for event, expected_handlers in required["hooks"].items():
        handlers = events.setdefault(event, [])
        for expected in expected_handlers:
            command = expected.get("command")
            legacy, linter_args = (bound_linters or {}).get(command, ("", []))
            found = False
            kept = []
            for existing in handlers:
                old_command = existing.get("command")
                same_command = old_command == command or (
                    linter_args and isinstance(old_command, str)
                    and (old_command == legacy or is_bound_linter_command(old_command, linter_args))
                )
                if same_command and existing.get("matcher") == expected.get("matcher"):
                    if found:
                        continue
                    found = True
                    existing["command"] = command
                kept.append(existing)
            if not found:
                kept.append(copy.deepcopy(expected))
            handlers[:] = kept
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
    current = json.loads(target.read_text(encoding="utf-8")) if exists else {}
    if not isinstance(current, dict):
        raise ValueError("project settings must be an object")
    original = copy.deepcopy(current)
    if not isinstance(current.get("hooks", {}), dict):
        raise ValueError("settings.hooks must be an object")
    flat = contract.merge_strategy == "json_flat_append_dedup"
    if not flat:
        validate_events(current.get("hooks", {}))
    required = copy.deepcopy(contract.payload)
    bound_linters: dict[str, tuple[str, list[str]]] = {}
    # Bind the interpreter that actually passed preflight, including paths with
    # spaces. No dependency on python3 aliases, WSL or head in project hooks.
    for groups in required["hooks"].values():
        for group in groups:
            for handler in ([group] if flat else group["hooks"]):
                template = handler.get("command", "")
                for name in ("invariant-check", "spec-sync-check"):
                    expected = '"{python}" -X utf8 "{plugin_root}/scripts/project_linter.py" --repo-root "{repo_root}" --linter ' + name
                    legacy = f"bash docs/harness/linters/{name}.sh 2>&1 | head -50"
                    if template == expected:
                        argv = [sys.executable, "-X", "utf8", str(PLUGIN_ROOT / "scripts/project_linter.py"),
                                "--repo-root", str(repo), "--linter", name]
                        command = windows_command(argv) if os.name == "nt" else shlex.join(argv)
                        handler["command"] = command
                        if platform == "codex":
                            handler["commandWindows"] = windows_command(argv)
                        elif platform == "claude-code" and os.name == "nt":
                            # Claude shell form is explicitly Git Bash; POSIX quote
                            # native paths rather than sending them through cmd.exe.
                            handler["command"] = shlex.join(argv)
                            handler["shell"] = "bash"
                        bound_linters[handler["command"]] = (legacy, argv[1:])
    merged = (merge_flat_payload(current, required, bound_linters) if flat
              else merge_payload(current, required, bound_linters))
    if action == "check":
        if not exists or original != merged:
            raise ValueError("project_hooks_missing_or_duplicated")
        return {"status": "pass", "settings_file": contract.settings_file}
    changed = not exists or original != merged
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
