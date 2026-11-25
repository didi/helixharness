#!/usr/bin/env python3
"""Cursor-native lifecycle boundary, bound only to this installed Host Kit."""

import json
import shlex
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from cli_stdio import configure_utf8_stdio  # noqa: E402
from telemetry_hosts.cursor import HOOK_EVENTS, HOOK_BUDGET_SECONDS, native_identity, native_identity_fields, record_hook  # noqa: E402

EVENTS = {"sessionStart": "SessionStart", "sessionEnd": "SessionEnd"}


def prompt_response(payload: dict, *, deadline: float) -> dict:
    """Best-effort current-prompt binding; no discovery, consent or filesystem reads."""
    response = {"continue": True}
    try:
        roots = payload.get("workspace_roots")
        if time.monotonic() >= deadline or not isinstance(roots, list) or len(roots) > 64:
            return response
        identity = native_identity_fields(payload, deadline=deadline)
        if identity is None:
            return response
        environment = {
            "HELIXHARNESS_BOUND_PLUGIN_ROOT": str(ROOT),
            "HELIXHARNESS_CURSOR_CONVERSATION_ID": identity[0],
            "HELIXHARNESS_CURSOR_WORKSPACE_ROOTS": json.dumps(identity[1], ensure_ascii=True),
        }
        prefix = " ".join(key + "=" + shlex.quote(value) for key, value in environment.items())
        context = (
            "Cursor current prompt identity: use this native binding for this prompt, not an earlier session binding. "
            "Every HelixHarness script invocation must pass these environment assignments to the actual "
            "runtime_preflight exec launcher, or export them in the current shell (hook env is not guaranteed in Shell). "
            "In a require && exec chain, prefix each exec launcher, not only require. "
            "Keep successful runtime preflight before any Python script. "
            "Repository ownership is still checked by the command consumer:\n" + prefix
        )
        if len(context.encode("utf-8")) <= 8192 and time.monotonic() < deadline:
            response["additional_context"] = context
    except (OSError, ValueError):
        pass
    return response


def _spawn_cleanup(workspaces: list[str]) -> None:
    """One-shot, local cleanup only; no payload, queue or shared scheduler work."""
    import os
    import subprocess
    options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("cleanup_worker.py")), *workspaces],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True, **options,
    )


def main() -> int:
    configure_utf8_stdio()
    deadline = time.monotonic() + HOOK_BUDGET_SECONDS
    try:
        raw = sys.stdin.read(4 * 1024 * 1024 + 1)
        if len(raw.encode("utf-8")) > 4 * 1024 * 1024:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        native_event = payload.get("hook_event_name")
        if not isinstance(native_event, str) or native_event not in EVENTS.keys() | HOOK_EVENTS:
            return 0
        if native_event in HOOK_EVENTS:
            # Frequent native events need neither startup context nor a scan of
            # every Host Kit. Keep their permission response before telemetry.
            if native_event == "beforeSubmitPrompt":
                print(json.dumps(prompt_response(payload, deadline=deadline)), flush=True)
            elif native_event == "subagentStart":
                print('{"permission":"allow"}', flush=True)
            record_hook(payload, deadline=deadline)
            return 0
        identity = native_identity(payload, deadline=deadline)
        if native_event == "sessionEnd":
            if identity is not None:
                _spawn_cleanup(identity[1])
            return 0
        from host_runtime import HostRegistry
        from lifecycle_service import handle_event, startup_context_text

        manifest = HostRegistry.discover(ROOT / "hosts").require("cursor")
        if native_event == "sessionStart":
            environment = {"HELIXHARNESS_BOUND_PLUGIN_ROOT": str(ROOT)}
            context = startup_context_text(manifest)
            if identity is not None:
                environment.update(HELIXHARNESS_CURSOR_CONVERSATION_ID=identity[0],
                                   HELIXHARNESS_CURSOR_WORKSPACE_ROOTS=json.dumps(identity[1], ensure_ascii=False))
                prefix = " ".join(key + "=" + shlex.quote(value) for key, value in environment.items())
                context += ("\nCursor session identity: every HelixHarness script invocation must pass these environment "
                            "assignments to the actual runtime_preflight exec launcher, or export them in the current shell "
                            "(hook env is not guaranteed in Shell). In a require && exec chain, prefix each exec launcher, "
                            "not only require. Keep successful runtime preflight before any Python script:\n" + prefix)
            print(json.dumps({
                "additional_context": context,
                "env": environment,
            }, ensure_ascii=False), flush=True)
            if identity is not None:
                # sessionEnd can be lost by the host. A later startup retries
                # only the same repository-scoped, currently-disabled cleanup.
                # Dispatch after flushing context; never wait for the worker.
                try:
                    _spawn_cleanup(identity[1])
                except OSError:
                    pass
        if identity is not None:
            import telemetry_consent_state
            for workspace in identity[1]:
                if time.monotonic() >= deadline:
                    break
                try:
                    repo = telemetry_consent_state._repo_root(workspace, deadline=deadline)
                except (OSError, ValueError):
                    continue
                handle_event(manifest, {"hook_event_name": EVENTS[native_event], "cwd": str(repo)}, emit_startup_context=False)
    except (OSError, ValueError):
        # Session creation is fire-and-forget. The command entrypoint performs
        # strict source/manifest verification before any workflow work.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
