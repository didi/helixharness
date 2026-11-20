"""Black-box installed bundle test, using only selected public Git file paths."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

from update_hosts.cursor import _distributed


REPOSITORY = Path(__file__).resolve().parents[3]
PLUGIN = REPOSITORY / "helixharness"


@unittest.skipUnless(shutil.which("git") and sys.platform in {"darwin", "linux"}, "Git and atomic directory exchange required")
class CursorFullBundleUpdateTests(unittest.TestCase):
    def test_native_opencode_distribution_includes_runtime_without_dependencies_or_private_files(self):
        for name in ("package.json", "opencode/index.ts", "opencode/core.ts"):
            with self.subTest(name=name):
                self.assertTrue(_distributed(PurePosixPath(name)))
        for name in ("bun.lock", "node_modules/dependency/index.ts", "opencode/node_modules/dependency/index.ts",
                     "opencode/.bun/cache/index.ts", "opencode/.env.local", "opencode/credentials.json",
                     "opencode/output/session.json", "opencode/tests/native-fixture.ts", "opencode/runtime/session.json"):
            with self.subTest(name=name):
                self.assertFalse(_distributed(PurePosixPath(name)))

    def test_installed_bundle_runs_without_checkout_pythonpath_and_updates_itself(self):
        with tempfile.TemporaryDirectory(prefix="helix-cursor-bundle-") as temporary:
            root = Path(temporary).resolve()
            source = root / "synthetic public distribution"
            source.mkdir()
            home = root / "isolated cursor"
            workspace = root / "synthetic workspace"
            workspace.mkdir()
            environment = {key: value for key, value in os.environ.items() if not key.startswith(("HELIXHARNESS_", "CLAUDE_", "CODEX_")) and key != "PYTHONPATH"}
            environment.update({"HELIXHARNESS_CURSOR_HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"})
            inventory = subprocess.run(["git", "-C", str(REPOSITORY), "-c", "core.hooksPath=/dev/null", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "helixharness"], capture_output=True, check=True, timeout=10).stdout.decode().split("\0")
            selected = set()
            for name in inventory:
                if not name:
                    continue
                relative = PurePosixPath(name).relative_to("helixharness")
                if not _distributed(relative):
                    continue
                original = REPOSITORY / name
                self.assertFalse(original.is_symlink(), name)
                if not original.is_file():
                    continue
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
                selected.add(relative.as_posix())
            self.assertIn("scripts/update_hosts/cursor.py", selected)
            self.assertIn("hosts/cursor/hooks.json", selected)
            native_files = ("package.json", "opencode/index.ts", "opencode/core.ts")
            for name in native_files:
                self.assertIn(name, selected)
            native_contents = {name: (PLUGIN / name).read_bytes() for name in native_files}
            root_manifest = source / ".cursor-plugin/marketplace.json"
            root_manifest.parent.mkdir()
            root_manifest.write_bytes((REPOSITORY / ".cursor-plugin/marketplace.json").read_bytes())

            def git(*arguments):
                return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Synthetic Bundle", "-c", "user.email=bundle@example.invalid", "-C", str(source), *arguments], env=environment, check=True, capture_output=True, text=True, timeout=15).stdout.strip()

            excluded_native_files = ("node_modules/synthetic/index.ts", "opencode/.bun/cache/index.ts",
                                     "opencode/.env.local", "opencode/credentials.json", "opencode/runtime/session.json")
            for name in excluded_native_files:
                synthetic = source / "helixharness" / name
                synthetic.parent.mkdir(parents=True, exist_ok=True)
                synthetic.write_text("synthetic excluded packaging fixture")

            def run(script, *arguments, input_data=None, expected=0, env=None, structured=True):
                process = subprocess.run([sys.executable, str(script), *arguments], cwd=workspace, env=env or environment, input=json.dumps(input_data) if input_data is not None else None, capture_output=True, text=True, timeout=45)
                self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
                return json.loads(process.stdout) if structured and process.stdout.strip() else process.stdout

            def release(version):
                for manifest_path in [root_manifest, *list((source / "helixharness").glob(".*-plugin/*.json"))]:
                    data = json.loads(manifest_path.read_text())
                    if data.get("name") == "helix" and "version" in data:
                        data["version"] = version
                    for item in data.get("plugins", []):
                        if item.get("name") == "helix":
                            item["version"] = version
                    manifest_path.write_text(json.dumps(data, ensure_ascii=False))
                policy_path = source / "helixharness/reference/version-policy.json"
                policy = json.loads(policy_path.read_text())
                policy["latest_version"] = version
                if version != "1.0.0":
                    policy["releases"].insert(0, {"version": version, "date": "2026-11-18", "update_type": "recommended", "title": "Synthetic installed bundle update", "changes": ["Synthetic complete distribution"], "restart_required": True})
                policy_path.write_text(json.dumps(policy, ensure_ascii=False))
                (source / "helixharness/reference/cursor-synthetic-release.txt").write_text(version)
                git("add", ".")
                git("commit", "-m", "Synthetic release " + version)
                return git("rev-parse", "HEAD")

            git("init", "--template=", "-b", "main")
            git("add", "--force", *["helixharness/" + name for name in excluded_native_files])
            release("1.0.0")
            installed = run(source / "helixharness/scripts/cursor_managed_install.py", "install", "--source", str(source), "--ref", "main", "--user-confirmed")
            plugin = Path(installed["plugin_root"])
            for name, content in native_contents.items():
                self.assertEqual((plugin / name).read_bytes(), content)
            for name in excluded_native_files:
                self.assertFalse((plugin / name).exists(), name)
            environment["HELIXHARNESS_BOUND_PLUGIN_ROOT"] = str(plugin)
            runtime = run(plugin / "scripts/host_runtime_cli.py", "resolve", "--platform", "cursor", "--expected-plugin-root", str(plugin))
            self.assertEqual(runtime["plugin_root"], str(plugin))
            self.assertEqual(runtime["host_id"], "cursor")
            # Resolve every installed host using the shipped CLI; this validates
            # the OpenCode package resource together with the existing kits.
            for host in ("claude-code", "codex", "opencode"):
                resolved = run(plugin / "scripts/host_runtime_cli.py", "resolve", "--platform", host, "--expected-plugin-root", str(plugin))
                self.assertEqual(resolved["host_id"], host)
            for generator in ("cursor_skill_projection.py", "codex_skill_projection.py"):
                run(plugin / "scripts" / generator, "--check", structured=False)
            commands = json.loads((plugin / "hosts/cursor/command-projection.json").read_text())["entrypoints"]
            self.assertEqual(len(commands), 9)
            methods = list((plugin / "skills").glob("*/SKILL.md"))
            self.assertEqual(len(methods), 8)
            for method in methods:
                receipt = run(plugin / "scripts/cursor_skill_projection.py", "--resolve-entry", str(plugin / "hosts/cursor/skills" / ("helix-" + method.parent.name) / "SKILL.md"))
                self.assertEqual(receipt["entry_kind"], "method")
                self.assertEqual(receipt["canonical"], "skills/" + method.parent.name + "/SKILL.md")
            lifecycle = run(plugin / "hosts/cursor/hook_entry.py", input_data={"hook_event_name": "sessionStart"})
            self.assertEqual(lifecycle["env"]["HELIXHARNESS_BOUND_PLUGIN_ROOT"], str(plugin))
            self.assertIn("cursor", lifecycle["additional_context"])
            denied = run(plugin / "scripts/host_runtime_cli.py", "resolve", "--platform", "cursor", "--expected-plugin-root", str(source / "helixharness"), expected=2)
            self.assertEqual(denied["reason"], "plugin_source_mismatch")
            before = (plugin / "reference/cursor-synthetic-release.txt").read_bytes()
            approved_commit = release("1.0.1")
            updater = plugin / "scripts/version_update.py"
            prefix = ("--platform", "cursor", "--repo-root", str(workspace))
            checked = run(updater, *prefix, "check", "--force-refresh")
            self.assertEqual(checked["current_version"], "1.0.0")
            self.assertEqual(checked["latest_version"], "1.0.1")
            prepared = run(updater, *prefix, "prepare", "--scope", "local")
            self.assertEqual((plugin / "reference/cursor-synthetic-release.txt").read_bytes(), before)
            challenge = prepared["challenge_id"]
            run(updater, *prefix, "decide", "--challenge-id", challenge, "--action", "upgrade_now", "--user-confirmed")
            updated = run(updater, *prefix, "install", "--challenge-id", challenge)
            self.assertEqual(updated["verified_commit"], approved_commit)
            self.assertEqual(updated["verified_version"], "1.0.1")
            self.assertEqual((plugin / "reference/cursor-synthetic-release.txt").read_text(), "1.0.1")
            for name, content in native_contents.items():
                self.assertEqual((plugin / name).read_bytes(), content)
            for directory in ("node_modules", ".bun", "opencode/tests", "opencode/runtime"):
                self.assertFalse((plugin / directory).exists(), directory)
            for name in excluded_native_files:
                self.assertFalse((plugin / name).exists(), name)
            self.assertTrue(updated["authorization_consumed"])
            self.assertNotIn("PYTHONPATH", environment)
            for generator in ("cursor_skill_projection.py", "codex_skill_projection.py"):
                run(plugin / "scripts" / generator, "--check", structured=False)
            reloaded = run(plugin / "scripts/host_runtime_cli.py", "resolve", "--platform", "cursor", "--expected-plugin-root", str(plugin))
            self.assertEqual(reloaded["plugin_root"], str(plugin))
            latest = run(updater, *prefix, "check", "--force-refresh")
            self.assertEqual(latest["update_status"], "latest")
            self.assertEqual(latest["current_version"], "1.0.1")


if __name__ == "__main__":
    unittest.main()
