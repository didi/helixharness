import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(shutil.which("node"), "Node.js required for native d2c guards")
class D2CHookBindingTest(unittest.TestCase):
    def run_hook(self, root, project, event, path=None):
        source = (root / "skills/d-d2c/SKILL.md").read_text().split("---", 2)[1]
        section = source.split(f"  {event}:", 1)[1]
        command = re.search(r"command: >-\n((?: {12,}.+\n)+)", section)
        self.assertIsNotNone(command)
        command = " ".join(line.strip() for line in command.group(1).splitlines())
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            input=json.dumps({"cwd": str(project), "hook_event_name": event}),
            env={"PATH": path if path is not None else os.environ["PATH"], "CLAUDE_PLUGIN_ROOT": str(root),
                 "CLAUDE_PROJECT_DIR": str(project), "CLAUDE_CONFIG_DIR": str(project / "config")},
            cwd=project, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def test_clean_install_and_stale_skill_cannot_change_binding(self):
        with tempfile.TemporaryDirectory(prefix="helixharness-d2c-") as tmp:
            project = Path(tmp)
            for event in ("PreToolUse", "Stop"):
                self.assertEqual({}, self.run_hook(ROOT, project, event))
            for relative in (".claude/skills/d-d2c", ".agent/skills/d-d2c", "config/skills/d-d2c"):
                scripts = project / relative / "scripts"
                scripts.mkdir(parents=True)
                for name in ("d2c-user-question-guard.cjs", "d2c-stop-guard.cjs"):
                    (scripts / name).write_text("process.stdout.write('{\"stale\":true}');\n")
            for event in ("PreToolUse", "Stop"):
                self.assertEqual({}, self.run_hook(ROOT, project, event))

    def test_missing_bound_guard_blocks_without_fallback(self):
        with tempfile.TemporaryDirectory(prefix="helixharness-d2c-") as tmp:
            root = Path(tmp) / "plugin"
            project = Path(tmp) / "project"
            project.mkdir()
            for relative in ("skills/d-d2c/SKILL.md", "hosts/claude-code/d2c_hook_entry.cjs"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            denied = self.run_hook(root, project, "PreToolUse")
            self.assertEqual("deny", denied["hookSpecificOutput"]["permissionDecision"])
            self.assertEqual("block", self.run_hook(root, project, "Stop")["decision"])
            (root / "hosts/claude-code/d2c_hook_entry.cjs").unlink()
            self.assertEqual("deny", self.run_hook(root, project, "PreToolUse")["hookSpecificOutput"]["permissionDecision"])
            self.assertEqual("block", self.run_hook(root, project, "Stop")["decision"])

    def test_missing_node_blocks(self):
        with tempfile.TemporaryDirectory(prefix="helixharness-d2c-") as tmp:
            project = Path(tmp)
            bindir = project / "bin"
            bindir.mkdir()
            (bindir / "sh").symlink_to("/bin/sh")
            self.assertEqual("deny", self.run_hook(ROOT, project, "PreToolUse", str(bindir))["hookSpecificOutput"]["permissionDecision"])
            self.assertEqual("block", self.run_hook(ROOT, project, "Stop", str(bindir))["decision"])


if __name__ == "__main__":
    unittest.main()
