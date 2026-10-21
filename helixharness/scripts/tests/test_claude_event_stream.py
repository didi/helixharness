from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from claude_event_stream import EventRelay, JsonLineDecoder

class ClaudeEventStreamTest(unittest.TestCase):
    def test_terminal_task_is_relayed_once_and_empty_notification_result_does_not_end_run(self):
        relay = EventRelay()
        relay.accept({"type":"system","subtype":"task_started","task_id":"scan-a"})
        event = {"type":"system","subtype":"task_notification","task_id":"scan-a",
                 "status":"completed","output_file":"/tmp/synthetic-result","summary":"actual scanner result"}
        message = relay.accept(event)
        self.assertIn("actual scanner result", message["message"]["content"])
        self.assertIsNone(relay.accept(event))
        relay.accept({"type":"result","subtype":"success","num_turns":0,"result":"",
                      "origin":{"kind":"task-notification"}})
        self.assertFalse(relay.finished)
        relay.accept({"type":"result","num_turns":3,"is_error":False,"result":"waiting"})
        self.assertFalse(relay.finished)
        relay.accept({"type":"user","message":message["message"]})
        relay.accept({"type":"result","num_turns":3,"is_error":False,"result":"archived"})
        self.assertTrue(relay.finished)
        self.assertFalse(relay.failed)

    def test_native_coalesced_user_echo_acknowledges_each_terminal_notice(self):
        relay = EventRelay()
        notices = [relay.accept({"type": "system", "subtype": "task_notification",
                                "task_id": task, "status": "completed", "output_file": "/tmp/" + task})
                   for task in ("scan-a", "scan-b")]
        # Claude may coalesce adjacent user inputs into one newline-joined message.
        content = "\n".join(notice["message"]["content"] for notice in notices)
        relay.accept({"type": "user", "message": {"content": content}})
        self.assertEqual(relay.pending_messages, set())
        relay.accept({"type": "result", "num_turns": 1, "result": "actual final"})
        self.assertTrue(relay.finished)

    def test_parent_waiting_result_cannot_finish_while_child_is_active(self):
        relay = EventRelay()
        relay.accept({"type":"system","subtype":"task_started","task_id":"review"})
        relay.accept({"type":"result","num_turns":3,"is_error":False,"result":"waiting"})
        self.assertFalse(relay.finished)

    def test_failed_job_is_forwarded_as_failed_not_synthesized_as_success(self):
        relay = EventRelay()
        message = relay.accept({"type":"system","subtype":"task_notification","task_id":"a",
                               "status":"failed","output_file":"/tmp/result","summary":"real failure"})
        self.assertIn('"status": "failed"', message["message"]["content"])
        self.assertIn("real failure", message["message"]["content"])

    def test_result_is_error_wins_over_misleading_success_subtype(self):
        relay = EventRelay()
        relay.accept({"type":"result","subtype":"success","num_turns":64,"is_error":True,
                      "terminal_reason":"api_error","result":"API Error: The operation timed out."})
        self.assertTrue(relay.finished)
        self.assertTrue(relay.failed)

    def test_child_result_does_not_end_parent_and_running_notice_is_not_relayed(self):
        relay = EventRelay()
        relay.accept({"type":"result","parent_tool_use_id":"child","num_turns":2,"result":"done"})
        self.assertFalse(relay.finished)
        self.assertIsNone(relay.accept({"type":"system","subtype":"task_notification","task_id":"a", "status":"running"}))

    def test_decoder_drains_multiple_events_and_preserves_split_utf8(self):
        decoder = JsonLineDecoder()
        raw = '{"text":"完成"}\n{"type":"result"}\n'.encode()
        boundary = raw.index("完".encode()) + 1
        self.assertEqual(decoder.feed(raw[:boundary]), [])
        self.assertEqual(decoder.feed(raw[boundary:]), [{"text":"完成"},{"type":"result"}])

    @unittest.skipUnless(os.name == "posix", "requires POSIX process groups")
    def test_timeout_kills_pipe_holding_descendant_and_persists_result(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            native = folder / "fake-claude"
            native.write_text("#!" + sys.executable + "\n" + r'''
import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"])
pathlib.Path("child.pid").write_text(str(child.pid))
print("child started", flush=True)
time.sleep(30)
''')
            native.chmod(0o700)
            prompt = folder / "prompt.txt"
            prompt.write_text("synthetic timeout test")
            root = Path(__file__).resolve().parents[2]
            output = folder / "output"
            try:
                result = subprocess.run([
                    sys.executable, str(root / "scripts/claude_event_stream.py"),
                    "--plugin-root", str(root), "--project", str(folder),
                    "--prompt-file", str(prompt), "--output", str(output),
                    "--claude", str(native), "--timeout", "2",
                ], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1)
                evidence = json.loads((output / "driver-result.json").read_text())
                self.assertTrue(evidence["timed_out"])
                self.assertLess(evidence["seconds"], 8)
                child = int((folder / "child.pid").read_text())
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(child)], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                if (folder / "child.pid").exists():
                    try:
                        os.kill(int((folder / "child.pid").read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_cli_keeps_input_open_until_terminal_notice_is_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            native = folder / "fake-claude"
            native.write_text("#!" + sys.executable + "\n" + r'''
import json, sys
initial = json.loads(sys.stdin.readline())
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type":"system","subtype":"task_started","task_id":"scan"})
emit({"type":"result","num_turns":2,"result":"waiting"})
emit({"type":"system","subtype":"task_notification","task_id":"scan",
      "status":"completed","output_file":"/tmp/synthetic-output","summary":"actual done"})
emit({"type":"result","num_turns":0,"result":"","origin":{"kind":"task-notification"}})
notice = json.loads(sys.stdin.readline())
assert '"status": "completed"' in notice["message"]["content"]
emit({"type":"user","message":notice["message"]})
emit({"type":"result","num_turns":3,"is_error":False,"result":"finished"})
assert sys.stdin.read() == ""
''')
            native.chmod(0o700)
            prompt = folder / "prompt.txt"
            prompt.write_text("synthetic test")
            root = Path(__file__).resolve().parents[2]
            output = folder / "output"
            result = subprocess.run([
                sys.executable, str(root / "scripts/claude_event_stream.py"),
                "--plugin-root", str(root), "--project", str(folder),
                "--prompt-file", str(prompt), "--output", str(output),
                "--claude", str(native), "--timeout", "10",
            ], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            evidence = json.loads((output / "driver-result.json").read_text())
            self.assertEqual(evidence["terminal"]["result"], "finished")
            self.assertEqual(evidence["pending_relays"], 0)
            self.assertEqual(evidence["active_tasks"], [])
            self.assertIsNone(evidence["driver_error"])

if __name__ == "__main__":
    unittest.main()
