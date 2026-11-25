"""Bounded current-event identity injection, without filesystem or host discovery."""
import io
import json
import os
import shlex
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_cursor_hook_budget import load
from telemetry_hosts.cursor import native_identity


def native_root(path: str) -> str:
    """A synthetic root that is absolute on the platform running the test.

    Cursor sends drive-qualified roots on Windows, and the identity binder requires
    `Path(root).is_absolute()` because guessing a drive for a bare `/synthetic/...`
    would bind a prompt to the wrong workspace. A drive-less root therefore gets no
    context there, which silently vacuates every rejection case in this file.
    """
    return path if os.name != "nt" else "C:" + path.replace("/", "\\")


class CursorPromptContextTest(unittest.TestCase):
    def setUp(self):
        self.entry = load("hook_entry")
        self.payload = {"hook_event_name": "beforeSubmitPrompt", "conversation_id": "synthetic-prompt",
                        "workspace_roots": [native_root("/synthetic/repo")]}

    def response(self, payload):
        class Output(io.StringIO):
            flushed = False
            def flush(self):
                self.flushed = True
        stream = Output()
        deadlines = []
        def record(value, *, deadline):
            self.assertTrue(stream.flushed)
            self.assertEqual(payload, value)
            deadlines.append(deadline)
            self.assertIs(True, json.loads(stream.getvalue())["continue"])
        with patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(stream), patch.object(
            self.entry, "record_hook", side_effect=record
        ):
            self.assertEqual(0, self.entry.main())
        self.assertEqual(1, len(stream.getvalue().splitlines()))
        self.assertEqual(1, len(deadlines))
        return json.loads(stream.getvalue())

    def bindings(self, response):
        return dict(value.split("=", 1) for value in shlex.split(response["additional_context"].splitlines()[-1]))

    def test_real_current_fields_are_shell_quoted_and_flushed_before_recording(self):
        roots = [native_root("/synthetic/a b/'quote'/$literal/`literal`"), native_root("/synthetic/中文")]
        response = self.response({**self.payload, "workspace_roots": roots})
        env = self.bindings(response)
        self.assertEqual("synthetic-prompt", env["HELIXHARNESS_CURSOR_CONVERSATION_ID"])
        self.assertEqual(roots, json.loads(env["HELIXHARNESS_CURSOR_WORKSPACE_ROOTS"]))
        self.assertEqual(str(self.entry.ROOT), env["HELIXHARNESS_BOUND_PLUGIN_ROOT"])
        self.assertLessEqual(len(response["additional_context"].encode()), 8192)

    def test_context_path_does_not_resolve_files_or_discover_hosts(self):
        roots = [native_root("/synthetic/link/../repo"), native_root("/synthetic/link/../repo")]
        with patch.object(Path, "resolve", side_effect=AssertionError("context resolved filesystem")), patch.dict(
            sys.modules, {"host_runtime": None, "lifecycle_service": None, "telemetry_consent_state": None}
        ):
            response = self.response({**self.payload, "workspace_roots": roots})
        self.assertEqual(roots, json.loads(self.bindings(response)["HELIXHARNESS_CURSOR_WORKSPACE_ROOTS"]))

    def test_prompt_and_startup_bind_identity_to_actual_exec_launcher(self):
        prompt = self.response(self.payload)
        startup_payload = {**self.payload, "hook_event_name": "sessionStart"}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(startup_payload))), redirect_stdout(output), patch.object(
            self.entry, "native_identity", return_value=("synthetic-prompt", [native_root("/synthetic/repo")])
        ), patch.object(self.entry, "_spawn_cleanup"), patch("lifecycle_service.handle_event"), patch(
            "telemetry_consent_state._repo_root", return_value=Path("/synthetic/repo")
        ):
            self.assertEqual(0, self.entry.main())
        startup = json.loads(output.getvalue())
        for response in (prompt, startup):
            context = response["additional_context"]
            self.assertIn("runtime_preflight exec launcher", context)
            self.assertIn("export them in the current shell", context)
            self.assertIn("prefix each exec launcher, not only require", context)
            self.assertEqual("synthetic-prompt", self.bindings(response)["HELIXHARNESS_CURSOR_CONVERSATION_ID"])

    def test_missing_conflicting_and_invalid_identity_allow_without_guessing(self):
        cases = [{}, {"conversation_id": None}, {"conversation_id": "../wrong"},
                 {"session_id": "different"}, {"workspace_roots": []}, {"workspace_roots": "bad"},
                 {"workspace_roots": ["relative"]}, {"workspace_roots": [1]},
                 {"workspace_roots": ["/bad\x00root"]}]
        for case in cases:
            payload = {"hook_event_name": "beforeSubmitPrompt"} if not case else {**self.payload, **case}
            with self.subTest(case=case):
                self.assertEqual({"continue": True}, self.response(payload))

    def test_context_caps_do_not_limit_existing_consumer_identity(self):
        for roots in ([native_root("/synthetic/r" + str(i)) for i in range(65)], [native_root("/" + "x" * 9000)]):
            with self.subTest(size=len(roots)):
                payload = {**self.payload, "workspace_roots": roots}
                self.assertEqual({"continue": True}, self.response(payload))
                with patch.object(Path, "resolve", autospec=True, side_effect=lambda value: value):
                    self.assertEqual("synthetic-prompt", native_identity(payload)[0])

    def test_expired_context_budget_returns_neutral_permission(self):
        result = self.entry.prompt_response(self.payload, deadline=time.monotonic() - 1)
        self.assertEqual({"continue": True}, result)

    def test_two_sessions_never_reuse_previous_binding(self):
        other = native_root("/synthetic/other")
        first = self.bindings(self.response(self.payload))
        second = self.bindings(self.response({**self.payload, "conversation_id": "other-session",
                                              "workspace_roots": [other]}))
        self.assertNotEqual(first, second)
        self.assertEqual("other-session", second["HELIXHARNESS_CURSOR_CONVERSATION_ID"])
        self.assertEqual([other], json.loads(second["HELIXHARNESS_CURSOR_WORKSPACE_ROOTS"]))
        self.assertEqual({"continue": True}, self.response({"hook_event_name": "beforeSubmitPrompt"}))

    def test_consumer_still_resolves_and_deduplicates_native_roots(self):
        real = native_root("/synthetic/real")
        with patch.object(Path, "resolve", return_value=Path(real)) as resolve:
            result = native_identity({**self.payload, "workspace_roots": [native_root("/synthetic/alias"), real]})
        self.assertEqual(2, resolve.call_count)
        self.assertEqual(("synthetic-prompt", [real]), result)


if __name__ == "__main__":
    unittest.main()
