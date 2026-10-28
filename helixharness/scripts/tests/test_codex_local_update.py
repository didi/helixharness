"""Version checks must not mutate the executing Codex installation."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from update_hosts.base import BoundedCommandRunner, UpdateHostError
from update_hosts.codex import CodexUpdateAdapter


class CodexMarketplaceRefreshTest(unittest.TestCase):
    def setUp(self):
        self.adapter = CodexUpdateAdapter(str, lambda path: json.loads(path.read_text()))
        self.runner = Mock()

    def source(self, kind, root="/synthetic/marketplace", source="https://example.invalid/plugin.git"):
        self.runner.run_json.return_value = {
            "marketplaces": [{"name": "helixharness-marketplace", "root": root,
                              "marketplaceSource": {"sourceType": kind, "source": source}}]
        }

    def test_local_source_needs_no_git_upgrade(self):
        self.source("local")
        with self.adapter.refresh_marketplace(self.runner) as snapshot:
            self.assertEqual(Path("/synthetic/marketplace"), snapshot)
        self.runner.run.assert_not_called()

    def test_git_check_reads_new_ref_without_touching_host_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "remote"
            remote.mkdir()
            def git(*args):
                return subprocess.run(["git", "-C", str(remote), "-c", "core.hooksPath=/dev/null",
                                       "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                                       *args], check=True, capture_output=True, text=True).stdout.strip()
            git("init", "-b", "main")
            (remote / "version").write_text("old")
            git("add", ".")
            git("commit", "-m", "old")
            old = git("rev-parse", "HEAD")
            (remote / "version").write_text("new")
            git("commit", "-am", "new")
            host = root / "installed"
            subprocess.run(["git", "clone", str(remote), str(host)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(host), "reset", "--hard", old], check=True, capture_output=True)
            for ref, expected in (("main", "new"), (old, "old")):
                with self.subTest(ref=ref):
                    subprocess.run(["git", "-C", str(host), "-c", "core.hooksPath=/dev/null", "checkout", ref], check=True, capture_output=True)
                    self.source("git", str(host), str(remote))
                    self.runner.run.side_effect = BoundedCommandRunner().run
                    with self.adapter.refresh_marketplace(self.runner) as snapshot:
                        self.assertEqual(expected, (snapshot / "version").read_text())
                        self.assertEqual("old", (host / "version").read_text())
                    self.assertFalse(snapshot.exists())
            self.assertFalse(any(call.args[0][0] == "codex" for call in self.runner.run.call_args_list))

    def test_native_refresh_is_an_authorized_install_step(self):
        self.source("git")
        steps = self.adapter.install_steps("default", self.runner)
        self.assertEqual(("codex", "plugin", "marketplace", "upgrade", "--json", "helixharness-marketplace"), steps[0].command)
        self.assertEqual(("codex", "plugin", "add", "helix@helixharness-marketplace", "--json"), steps[1].command)

    def test_local_install_does_not_attempt_git_marketplace_upgrade(self):
        self.source("local")
        steps = self.adapter.install_steps("default", self.runner)
        self.assertEqual(["update-default"], [step.step_id for step in steps])

    def test_unknown_source_is_rejected(self):
        self.source("unknown")
        with self.assertRaises(UpdateHostError), self.adapter.refresh_marketplace(self.runner):
            self.fail("unsupported source yielded a snapshot")
        self.runner.run.assert_not_called()

    def test_ambiguous_marketplace_is_rejected(self):
        self.source("local")
        self.runner.run_json.return_value["marketplaces"] *= 2
        with self.assertRaises(UpdateHostError), self.adapter.refresh_marketplace(self.runner):
            self.fail("ambiguous source yielded a snapshot")


if __name__ == "__main__":
    unittest.main()
