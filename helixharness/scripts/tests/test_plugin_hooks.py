import json
import os
import shlex
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def shell_command(hook: dict, environment: dict[str, str]) -> str:
    """Return a shell line for either the string form or the argv form.

    Host path variables such as ``${QODER_PLUGIN_ROOT}`` are expanded from the
    fixture environment first: quoting a token that still contains ``${...}``
    would prevent the shell from expanding it.
    """

    command = hook["command"]
    args = hook.get("args") or []
    if not isinstance(command, str) or not isinstance(args, list):
        raise AssertionError("invalid hook command form")

    def expand(token: str) -> str:
        for name, value in environment.items():
            token = token.replace("${" + name + "}", value)
        return token

    parts = [command, *(str(item) for item in args)]
    if len(parts) == 1:
        # String form is already a shell line; quoting it would run it as a binary.
        return expand(command)
    return " ".join(shlex.quote(expand(part)) for part in parts)


class PluginHooksTest(unittest.TestCase):
    def test_installed_manifest_binds_only_its_own_host(self):
        for host, manifest_path in (
            ("codex", ".codex-plugin/plugin.json"),
            ("claude-code", ".claude-plugin/plugin.json"),
            ("qoder", ".qoder-plugin/plugin.json"),
        ):
            with self.subTest(host=host):
                manifest = json.loads((ROOT / manifest_path).read_text())
                path = manifest.get("hooks", "./hooks/hooks.json")
                hooks = json.loads((ROOT / path).read_text())["hooks"]
                # Both products expose compatibility environment variables. They
                # must never decide the identity of this installed entrypoint.
                env = {
                    "PATH": os.environ["PATH"],
                    "PLUGIN_ROOT": str(ROOT),
                    "CLAUDE_PLUGIN_ROOT": str(ROOT),
                    "CODEX_THREAD_ID": "fixture-codex",
                    "CLAUDE_SESSION_ID": "fixture-claude",
                    "QODER_PLUGIN_ROOT": str(ROOT),
                    "QODER_SESSION_ID": "fixture-qoder",
                }
                command = shell_command(hooks["SessionStart"][0]["hooks"][0], env)
                process = subprocess.run(
                    ["sh", "-c", command],
                    input=json.dumps(
                        {
                            "hook_event_name": "SessionStart",
                            "session_id": "fixture",
                            "platform": "wrong-host",
                        }
                    ),
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(0, process.returncode, process.stderr)
                context = json.loads(process.stdout)["hookSpecificOutput"][
                    "additionalContext"
                ]
                binding = json.loads(context.split(": ", 1)[1].split("\n", 1)[0])
                self.assertEqual(host, binding["canonical_host_id"])
                self.assertEqual(
                    str(ROOT / f"hosts/{host}/host.json"), binding["host_manifest"]
                )
                self.assertEqual(f"./hosts/{host}/hooks.json", path)


if __name__ == "__main__":
    unittest.main()
