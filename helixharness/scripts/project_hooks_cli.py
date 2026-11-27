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
from host_runtime.project_hooks import StaleBindingPolicy, load_project_hooks, validate_flat_payload

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
WINDOWS_COMMAND_PREFIX = "powershell.exe -NoProfile -NonInteractive -EncodedCommand "
KNOWN_LINTERS = ("invariant-check", "spec-sync-check")
LINTER_SCRIPT_SUFFIX = ("scripts", "project_linter.py")


def windows_command(argv: list[str]) -> str:
    """Keep paths containing spaces, &, % or apostrophes out of cmd.exe syntax."""
    literals = " ".join("'" + item.replace("'", "''") + "'" for item in argv)
    source = f"& {literals}; exit $LASTEXITCODE"
    encoded = base64.b64encode(source.encode("utf-16-le")).decode("ascii")
    return WINDOWS_COMMAND_PREFIX + encoded


def _parse_windows_command(command: str) -> list[str] | None:
    """Decode a Windows powershell-encoded command back to argv."""
    if not command.startswith(WINDOWS_COMMAND_PREFIX):
        return None
    try:
        encoded = command.removeprefix(WINDOWS_COMMAND_PREFIX)
        source = base64.b64decode(encoded, validate=True).decode("utf-16-le")
    except ValueError:
        return None
    match = re.match(r"^& ((?:'[^']*'\s*)+);\s*exit\s+\$LASTEXITCODE\s*$", source)
    if match is None:
        return None
    quoted = match[1].strip()
    argv = []
    for token in re.findall(r"'((?:[^']|'')*)'", quoted):
        argv.append(token.replace("''", "'"))
    return argv


def _parse_posix_command(command: str) -> list[str] | None:
    """Parse a POSIX shell-quoted command into argv."""
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _recognize_linter_name(argv: list[str]) -> str | None:
    """Return the linter name only when argv is exactly our generator shape.

    The generator emits ``<python> -X utf8 <script> [--repo-root <dir>]
    --linter <name>``. Any extra flag or positional option — a team override
    such as ``--team-profile strict``, for instance — disqualifies the
    command, because rewriting it to the canonical form would drop it.
    """
    if len(argv) < 4 or not Path(argv[0]).is_absolute():
        return None
    if argv[1:3] != ["-X", "utf8"]:
        return None
    if tuple(Path(argv[3]).parts[-2:]) != LINTER_SCRIPT_SUFFIX:
        return None
    linter_name = None
    repo_root_seen = False
    index = 4
    while index < len(argv):
        token = argv[index]
        if token == "--repo-root" and not repo_root_seen and index + 1 < len(argv):
            repo_root_seen = True
            index += 2
        elif token == "--linter" and linter_name is None and index + 1 < len(argv):
            if argv[index + 1] not in KNOWN_LINTERS:
                return None
            linter_name = argv[index + 1]
            index += 2
        else:
            return None
    return linter_name


def recognize_linter_command(command: str, policy: StaleBindingPolicy) -> str | None:
    """Return the linter name if command is our generated linter, else None.

    Under exact_args policy, returns the linter name only if argv[1:] matches exactly.
    Under linter_flag policy, returns the linter name if the shape matches.
    """
    if policy.recognize == "exact_args":
        # Legacy behavior: caller must pass expected argv[1:] to compare.
        # This branch is handled by the caller passing linter_args.
        return None
    argv = _parse_windows_command(command) or _parse_posix_command(command)
    if argv is None:
        return None
    return _recognize_linter_name(argv)


def looks_like_linter_command(command: str) -> bool:
    """True when a command references our linter script but is not canonical.

    Used to warn instead of silently rewriting a handler that carries
    arguments the generator does not know about.
    """
    argv = _parse_windows_command(command) or _parse_posix_command(command)
    if argv is None:
        return False
    references_script = any(
        len(Path(arg).parts) >= 2 and tuple(Path(arg).parts[-2:]) == LINTER_SCRIPT_SUFFIX
        for arg in argv
    )
    return references_script and any(arg in KNOWN_LINTERS for arg in argv)


def _warn_preserved(warnings: list[str], command: str) -> None:
    warning = f"Preserved user-modified linter handler: {command[:80]}"
    if warning not in warnings:
        warnings.append(warning)


def is_bound_linter_command(command: str, linter_args: list[str]) -> bool:
    """Recognize our generated command with any absolute interpreter path (legacy exact_args)."""
    if command.startswith(WINDOWS_COMMAND_PREFIX):
        argv = _parse_windows_command(command)
        if argv is None:
            return False
        python = argv[0]
        return Path(python).is_absolute() and command == windows_command([python, *linter_args])
    argv = _parse_posix_command(command)
    if argv is None:
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


def merge_payload(
    current: dict,
    required: dict,
    bound_linters: dict[str, tuple[str, list[str]]],
    policy: StaleBindingPolicy,
) -> tuple[dict, list[str]]:
    """Merge required hooks into current settings, returning (merged, warnings).

    Under collapse policy, duplicate shape-matched entries are removed.
    """
    result = copy.deepcopy(current)
    events = result.setdefault("hooks", {})
    if not isinstance(events, dict):
        raise ValueError("settings.hooks must be an object")
    validate_events(events)
    validate_events(required["hooks"])
    warnings: list[str] = []
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
                handler_linter_name = None
                if policy.recognize == "linter_flag" and isinstance(command, str):
                    handler_linter_name = recognize_linter_command(command, policy)
                found = False
                for group in matching:
                    kept = []
                    for existing in group["hooks"]:
                        old_command = existing.get("command")
                        if not isinstance(old_command, str) or existing.get("type") != handler["type"]:
                            kept.append(existing)
                            continue
                        # Check if this existing entry matches our handler
                        is_match = False
                        if policy.recognize == "linter_flag":
                            # Shape-based recognition
                            existing_linter_name = recognize_linter_command(old_command, policy)
                            if existing_linter_name and handler_linter_name and existing_linter_name == handler_linter_name:
                                is_match = True
                            elif looks_like_linter_command(old_command):
                                # Team-specific arguments are unknown to the generator;
                                # keep the handler verbatim so no option is dropped.
                                _warn_preserved(warnings, old_command)
                            # Also check commandWindows under shape match
                            if not is_match and "commandWindows" in handler:
                                old_windows = existing.get("commandWindows")
                                if isinstance(old_windows, str) and recognize_linter_command(old_windows, policy) == handler_linter_name:
                                    is_match = True
                        else:
                            # Legacy exact_args recognition
                            if linter_args:
                                if old_command == command or old_command == legacy or is_bound_linter_command(old_command, linter_args):
                                    is_match = True
                                # Also check commandWindows under legacy recognition
                                if not is_match and "commandWindows" in handler:
                                    old_windows = existing.get("commandWindows")
                                    if isinstance(old_windows, str) and is_bound_linter_command(old_windows, linter_args):
                                        is_match = True
                            else:
                                # Exact command match
                                if old_command == command:
                                    is_match = True
                        if is_match:
                            if found:
                                # Duplicate: skip it (original dedup behavior)
                                # Under collapse policy, also report as warning
                                if policy.merge == "collapse":
                                    warnings.append(f"Removed duplicate linter handler: {old_command[:80]}")
                                continue
                            found = True
                            # Update to canonical command
                            if command != old_command:
                                existing["command"] = command
                            old_windows = existing.get("commandWindows")
                            if "commandWindows" in handler:
                                if linter_args and isinstance(old_windows, str) and is_bound_linter_command(old_windows, linter_args):
                                    existing["commandWindows"] = handler["commandWindows"]
                                elif policy.recognize == "linter_flag" and isinstance(old_windows, str):
                                    if recognize_linter_command(old_windows, policy):
                                        existing["commandWindows"] = handler["commandWindows"]
                                    elif looks_like_linter_command(old_windows):
                                        _warn_preserved(warnings, old_windows)
                            for key in ("commandWindows", "shell"):
                                if key in handler:
                                    existing.setdefault(key, handler[key])
                        kept.append(existing)
                    group["hooks"] = kept
                if not found:
                    matching[0]["hooks"].append(copy.deepcopy(handler))
    return result, warnings


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

def project_hooks(repo: Path, platform: str, action: str, strict: bool = False) -> dict:
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
    if flat:
        merged, merge_warnings = merge_flat_payload(current, required, bound_linters), []
    else:
        merged, merge_warnings = merge_payload(current, required, bound_linters, contract.stale_binding_policy)
    if action == "check":
        if not exists or original != merged:
            if contract.stale_binding_policy.check == "warn" and not strict:
                return {
                    "status": "pass",
                    "settings_file": contract.settings_file,
                    "warnings": merge_warnings if merge_warnings else ["Project hooks missing or duplicated"],
                }
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
    result = {
        "status": "merged",
        "changed": changed,
        "settings_file": contract.settings_file,
    }
    if merge_warnings:
        result["warnings"] = merge_warnings
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures in check mode")
    parser.add_argument("action", choices=("merge", "check"))
    args = parser.parse_args()
    try:
        result = project_hooks(args.repo_root, args.platform, args.action, strict=args.strict)
    except (OSError, ValueError, HostContractError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
