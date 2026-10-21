"""Shared lifecycle operations; host identity is supplied by an installed Host Kit."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path

from host_runtime import HostManifest


def startup_context(manifest: HostManifest) -> dict:
    root = manifest.root.parent.parent
    binding = {
        "canonical_host_id": manifest.host_id,
        "plugin_root": str(root),
        "host_manifest": str(manifest.root / "host.json"),
    }
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "HelixHarness installed Host Context: "
                + json.dumps(binding, ensure_ascii=False)
                + "\nFor HelixHarness commands and skills, use this plugin_root and read this host_manifest to bind its resources. "
                "Resolve plugin resources against this root, not the project directory. "
                "Do not search another checkout or cached version. Invoke the registered HelixHarness command/Skill; "
                "do not discover command bodies in user plugin caches. Before workflow work, verify the source with: "
                + "python3 " + shlex.quote(str(root / "scripts/host_runtime_cli.py"))
                + " resolve --platform " + shlex.quote(manifest.host_id)
                + " --expected-plugin-root " + shlex.quote(str(root))
                + ". Consume plugin_root, host_manifest and resolved_resources from that verified response. "
                "Pass the same bound root and exact resource paths to every child; a different version is not a fallback. "
                "If a bound resource is missing or the source differs, report BLOCKED:helixharness_host_contract_invalid."
            ),
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


def handle_event(manifest: HostManifest, data: dict) -> None:
    event = data.get("hook_event_name")
    if event == "SessionStart":
        print(json.dumps(startup_context(manifest), ensure_ascii=False))
    try:
        if event in {"SessionStart", "CwdChanged"}:
            repair_session(manifest, data)
        elif event == "SessionEnd":
            cleanup_session(manifest, data)
    except Exception:
        # Optional telemetry must not block startup or workflow execution.
        pass
