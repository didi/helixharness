#!/usr/bin/env python3
"""Opt-in native Claude CLI driver for synthetic acceptance, preserving task notifications.

Uses existing host authentication without reading or copying credentials. Raw event logs must
stay in a private/ignored output directory; they are not public test fixtures.
"""
from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


class JsonLineDecoder:
    def __init__(self) -> None:
        self.buffer = b""

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        self.buffer += chunk
        events = []
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue  # Raw bytes are retained separately by the driver.
            if isinstance(event, dict):
                events.append(event)
        return events


class EventRelay:
    """Translate only observed native terminal notices; never infer workflow success."""
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.delivered: set[tuple[str, str]] = set()
        self.pending_messages: set[str] = set()
        self.terminal: dict[str, Any] | None = None
        self.finished = False
        self.failed = False

    def accept(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind, subtype = event.get("type"), event.get("subtype")
        if kind == "user":
            content = event.get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
            if isinstance(content, str):
                # The native CLI may merge consecutive user inputs before replay.
                acknowledged = {message for message in self.pending_messages if message in content}
                self.pending_messages.difference_update(acknowledged)
        if kind == "system" and subtype == "task_started":
            if isinstance(event.get("task_id"), str):
                self.active.add(event["task_id"])
        if kind == "system" and subtype == "task_notification":
            task_id, status = event.get("task_id"), event.get("status")
            if not isinstance(task_id, str) or status not in {"completed", "failed", "stopped", "cancelled"}:
                return None
            self.active.discard(task_id)
            key = (task_id, status)
            if key in self.delivered:
                return None
            self.delivered.add(key)
            # Empty-output shell completion notices need no extra conversational turn.
            if not event.get("output_file"):
                return None
            actual = {name: event[name] for name in ("task_id", "status", "output_file", "summary") if name in event}
            content = ("HelixHarness acceptance driver: actual native task terminal notice. "
                       "Read the exact native result, validate status/artifacts against the existing "
                       "contract, then continue only if its gates pass. Do not invent evidence, "
                       "retry failed jobs, or keep sleeping for this terminal task.\n"
                       + json.dumps(actual, ensure_ascii=False))
            self.pending_messages.add(content)
            return {"type": "user", "message": {"role": "user", "content": content}}
        if kind == "result" and not event.get("parent_tool_use_id"):
            if event.get("origin", {}).get("kind") == "task-notification":
                return None
            failed = bool(event.get("is_error")) or str(subtype).startswith("error")
            if failed or (event.get("num_turns", 0) > 0 and event.get("result") and
                          not self.active and not self.pending_messages):
                self.terminal = event
                self.failed = failed
                self.finished = True
        return None


def _stop_process_tree(process: subprocess.Popen) -> None:
    """Bound cleanup, including descendants that inherited the output pipe."""
    def terminate(force: bool) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif process.poll() is None:
                process.kill() if force else process.terminate()
        except ProcessLookupError:
            pass

    terminate(False)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # The leader can already have exited while a descendant ignores SIGTERM.
    terminate(True)
    process.wait(timeout=2)


def _drain_available_output(process: subprocess.Popen, log: Any) -> None:
    """Retain available trailing bytes without waiting for another process's EOF."""
    descriptor = process.stdout.fileno()
    os.set_blocking(descriptor, False)
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            chunk = os.read(descriptor, 65536)
        except BlockingIOError:
            break
        if not chunk:
            break
        log.write(chunk)


def run(args: argparse.Namespace) -> int:
    root, project, output = args.plugin_root.resolve(), args.project.resolve(), args.output.resolve()
    if not (root / "hosts/claude-code/host.json").is_file() or not project.is_dir():
        raise ValueError("plugin root or synthetic project is missing")
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("events.jsonl", "driver-result.json")):
        raise ValueError("use a new output directory; existing evidence is never overwritten")
    cmd = [args.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--replay-user-messages", "--plugin-dir", str(root),
           "--permission-mode", args.permission_mode, "--strict-mcp-config", "--mcp-config",
           '{"mcpServers":{}}', "--setting-sources", "user,project,local"]
    if args.resume:
        cmd += ["--resume", args.resume]
    if args.settings:
        cmd += ["--settings", str(args.settings.resolve())]
    env = os.environ.copy()
    # This invocation explicitly selects the test source; no inherited other-host binding.
    env["HELIXHARNESS_BOUND_PLUGIN_ROOT"] = str(root)
    relay, decoder = EventRelay(), JsonLineDecoder()
    start, timed_out = time.monotonic(), False
    driver_error = None
    prompt = args.prompt_file.read_text(encoding="utf-8")
    with (output / "events.jsonl").open("xb") as log, (output / "relays.jsonl").open("x", encoding="utf-8") as notices:
        process = subprocess.Popen(cmd, cwd=project, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                                   start_new_session=os.name == "posix")
        (output / "process.json").write_text(json.dumps({"pid": process.pid, "plugin_root": str(root),
                                                         "project": str(project)}))
        def send(message: dict[str, Any]) -> None:
            if process.stdin.closed:
                raise RuntimeError("native task notice arrived after input was closed")
            wire = json.dumps(message, ensure_ascii=False) + "\n"
            process.stdin.write(wire.encode("utf-8"))
            process.stdin.flush()
            notices.write(wire)
            notices.flush()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            send({"type": "user", "message": {"role": "user", "content": prompt}})
            eof = False
            while not eof:
                if time.monotonic() - start >= args.timeout:
                    timed_out = True
                    break
                ready = selector.select(timeout=min(1, max(0, args.timeout - (time.monotonic() - start))))
                if not ready and process.poll() is not None:
                    break  # A descendant may hold the pipe after the native leader exits.
                for key, _ in ready:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        eof = True
                        break
                    log.write(chunk)
                    log.flush()
                    for event in decoder.feed(chunk):
                        message = relay.accept(event)
                        if message is not None:
                            send(message)
                        if relay.finished and not process.stdin.closed:
                            process.stdin.close()
            if not timed_out:
                process.wait(timeout=2)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            driver_error = str(error)
        finally:
            selector.close()
            try:
                _stop_process_tree(process)
                _drain_available_output(process, log)
            except (OSError, subprocess.TimeoutExpired) as error:
                driver_error = driver_error or str(error)
            code = process.returncode
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
    result = {"exit_code": code, "timed_out": timed_out, "driver_error": driver_error,
              "seconds": round(time.monotonic() - start, 1), "terminal": relay.terminal,
              "active_tasks": sorted(relay.active), "pending_relays": len(relay.pending_messages)}
    (output / "driver-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    # Native exit 0 and a final reply still do not establish workflow acceptance.
    return 0 if code == 0 and relay.terminal and not relay.failed and not timed_out and not driver_error else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume")
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--permission-mode", choices=("default", "bypassPermissions"), default="default")
    parser.add_argument("--timeout", type=float, default=2400)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
