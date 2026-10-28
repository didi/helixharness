import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PluginHooksTest(unittest.TestCase):
    def test_installed_manifest_binds_only_its_own_host(self):
        for host, manifest_path in (
            ("codex", ".codex-plugin/plugin.json"),
            ("claude-code", ".claude-plugin/plugin.json"),
        ):
            with self.subTest(host=host):
                manifest = json.loads((ROOT / manifest_path).read_text())
                path = manifest.get("hooks", "./hooks/hooks.json")
                hooks = json.loads((ROOT / path).read_text())["hooks"]
                command = hooks["SessionStart"][0]["hooks"][0]["command"]
                # Both products expose compatibility environment variables. They
                # must never decide the identity of this installed entrypoint.
                env = {
                    "PATH": os.environ["PATH"],
                    "PLUGIN_ROOT": str(ROOT),
                    "CLAUDE_PLUGIN_ROOT": str(ROOT),
                    "CODEX_THREAD_ID": "fixture-codex",
                    "CLAUDE_SESSION_ID": "fixture-claude",
                }
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
