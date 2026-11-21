"""Shared lifecycle operations; host identity is supplied by an installed Host Kit."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from host_runtime import HostManifest


def startup_context_text(manifest: HostManifest, *, command_shell: str | None = None) -> str:
    """Render binding text for the Host Kit's command shell, not the Hook's OS alone."""

    if command_shell is None:
        command_shell = "powershell" if os.name == "nt" else "bash"
    if command_shell not in {"bash", "powershell"}:
        raise ValueError(f"Unsupported command shell: {command_shell}")

    root = manifest.root.parent.parent
    binding = {
        "canonical_host_id": manifest.host_id,
        "plugin_root": str(root),
        "host_manifest": str(manifest.root / "host.json"),
    }
    if command_shell == "powershell":
        def literal(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"

        launcher = literal(str(root / "scripts/runtime_preflight.ps1"))
        guarded = (
            "$helixPreflight = & " + launcher + " check\n"
            "$helixPreflight\n"
            "if (-not ($helixPreflight | ConvertFrom-Json).ok) { exit 2 }\n"
            "& " + launcher + " exec " + literal(str(root / "scripts/host_runtime_cli.py"))
            + " resolve --platform " + literal(manifest.host_id)
            + " --expected-plugin-root " + literal(str(root))
        )
        guard_description = "Only ok=true allows the PowerShell exec step to run. "
    else:
        # Git Bash accepts drive/UNC paths with forward slashes. Keep the
        # native paths in the binding JSON; normalize only shell arguments.
        launcher = "sh " + shlex.quote((root / "scripts/runtime_preflight.sh").as_posix())
        guarded = (
            launcher + " require && " + launcher + " exec "
            + shlex.quote((root / "scripts/host_runtime_cli.py").as_posix())
            + " resolve --platform " + shlex.quote(manifest.host_id)
            + " --expected-plugin-root " + shlex.quote(root.as_posix())
        )
        guard_description = "Only ok=true allows the resolver after && to run; do not replace && with a separator. "
    return (
        "HelixHarness installed Host Context: "
        + json.dumps(binding, ensure_ascii=False)
        + "\nFor HelixHarness commands and skills, use this plugin_root and read this host_manifest to bind its resources. "
        "Resolve plugin resources against this root, not the project directory. "
        "Do not search another checkout or cached version. Invoke the registered HelixHarness command/Skill; "
        "do not discover command bodies in user plugin caches. Before any HelixHarness Python script, read "
        + str(root / "reference/runtime-preflight.md")
        + " and run this guarded source-binding command with the native shell (the preflight must run first):\n"
        + guarded
        + "\nThe first JSON is the runtime check. " + guard_description
        + "A failed preflight exits 2 and must follow the bound runtime-preflight recovery rules, without "
        "running Python separately or starting workflow work. The exec launcher uses the validated interpreter. "
        "After the successful check, consume plugin_root, host_manifest and resolved_resources only from "
        "the status=resolved response. "
        "Pass the same bound root and exact resource paths to every child; a different version is not a fallback. "
        "If a bound resource is missing or the source differs, report BLOCKED:helixharness_host_contract_invalid."
    )


def startup_context(manifest: HostManifest, *, command_shell: str | None = None) -> dict:
    """Preserve the existing nested response used by the 1.1 Host Kits."""

    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": startup_context_text(manifest, command_shell=command_shell),
        }
    }


def repair_session(manifest: HostManifest, data: dict) -> None:
    cwd = data.get("cwd")
    if not cwd or manifest.capability("telemetry") == "unsupported":
        return
    root = manifest.root.parent.parent
    subprocess.Popen(
        [
            sys.executable,
            str(root / "scripts/telemetry_async.py"),
            "--plugin-root",
            str(root),
            "repair",
            "--repo-root",
            str(cwd),
            "--platform",
            manifest.host_id,
            "--require-ledger",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


def cleanup_session(manifest: HostManifest, data: dict) -> int:
    if not data.get("cwd") or manifest.capability("telemetry") == "unsupported":
        return 0
    import metrics
    import telemetry_consent_state

    repo = Path(data["cwd"]).expanduser().resolve()
    consent = telemetry_consent_state.resolve_for_platform(repo, manifest.host_id)
    if consent.get("effective_mode") == "enabled":
        return 0
    removed = metrics.purge_private_states(repo, manifest.host_id)
    if consent.get("effective_scope") == "user":
        metrics.telemetry_async.remove_scheduler()
    return removed


def handle_event(
    manifest: HostManifest, data: dict, *, emit_startup_context: bool = True,
    command_shell: str | None = None,
) -> None:
    event = data.get("hook_event_name")
    if event == "SessionStart" and emit_startup_context:
        print(json.dumps(startup_context(manifest, command_shell=command_shell), ensure_ascii=False))
    try:
        if event in {"SessionStart", "CwdChanged"}:
            repair_session(manifest, data)
        elif event == "SessionEnd":
            cleanup_session(manifest, data)
    except Exception:
        # Optional telemetry must not block startup or workflow execution.
        pass
