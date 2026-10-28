#!/usr/bin/env python3
"""Claude Code-native event and shell-environment adapter."""

import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from host_runtime import HostRegistry  # noqa: E402
from lifecycle_service import handle_event, startup_context  # noqa: E402


def persist_environment(payload: dict) -> None:
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return
    values = {
        "HELIXHARNESS_BOUND_PLUGIN_ROOT": str(ROOT),
        "HELIXHARNESS_LITE_SESSION_CWD": payload.get("cwd"),
        "HELIXHARNESS_LITE_SESSION_ID": payload.get("session_id"),
        "HELIXHARNESS_LITE_SESSION_LOG_PATH": payload.get("transcript_path"),
    }
    try:
        with Path(env_file).open("a", encoding="utf-8") as stream:
            for key, value in values.items():
                if value:
                    stream.write(f"export {key}={shlex.quote(str(value))}\n")
    except OSError:
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        event = payload.get("hook_event_name")
        if event in {"PreToolUse", "UserPromptExpansion"}:
            name = payload.get("command_name", "")
            if event == "PreToolUse":
                if payload.get("tool_name") != "Skill":
                    return 0
                name = (payload.get("tool_input") or {}).get("skill", "")
            if not isinstance(name, str) or not name.startswith("helix:"):
                return 0
            from host_runtime_cli import _context
            manifest = HostRegistry.discover(ROOT / "hosts").get("claude-code")
            output = startup_context(manifest)
            specific = output["hookSpecificOutput"]
            specific["hookEventName"] = event
            specific["additionalContext"] += (
                "\nThis command's resources are already located. First read "
                + str(ROOT / "reference/runtime-preflight.md")
                + "; do not locate a plugin via ~/.claude or installed_plugins.json.\n"
                + json.dumps(_context(manifest), ensure_ascii=False)
            )
            print(json.dumps(output, ensure_ascii=False))
            return 0
        if event not in {
            "SessionStart",
            "CwdChanged",
            "SessionEnd",
        }:
            return 0
        if payload["hook_event_name"] in {"SessionStart", "CwdChanged"}:
            persist_environment(payload)
        handle_event(HostRegistry.discover(ROOT / "hosts").get("claude-code"), payload)
    except (OSError, ValueError):
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
