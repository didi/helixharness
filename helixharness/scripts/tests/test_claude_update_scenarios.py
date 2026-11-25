"""Real update state machine with a fake Claude Code CLI in synthetic homes.

Normal regressions and adversarial postcondition probes are intentionally
separate. Neither invokes the installed Claude executable or a real marketplace.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest

import env_sandbox
import test_codex_update_scenarios as fixtures

NATIVE_STUB = r'''
import json, os, sys
from pathlib import Path
state_path = Path(os.environ["HELIX_TEST_NATIVE_STATE"])
state = json.loads(state_path.read_text())
args = sys.argv[1:]
with Path(state["calls"]).open("a") as stream:
    stream.write(json.dumps(args) + "\n")
def emit(value):
    print(json.dumps(value))
if args == ["plugin", "list", "--json"]:
    entries = []
    for scope, version in state["scope_versions"].items():
        item = {"id": "helix@helixharness-marketplace", "enabled": True,
                "scope": scope, "version": version,
                "installPath": state.get("reported_path", state["plugin_root"])}
        if scope == "project":
            item["projectPath"] = state["repo_root"]
        entries.append(item)
    emit(entries)
elif args == ["plugin", "marketplace", "list", "--json"]:
    emit([{"name": "helixharness-marketplace", "source": "directory",
           "installLocation": state["marketplace"]}])
elif args == ["plugin", "marketplace", "update", "helixharness-marketplace"]:
    if state.get("fail_refresh"):
        sys.exit(18)
    emit({"refreshed": True})
elif len(args) == 5 and args[:4] == ["plugin", "update", "helix@helixharness-marketplace", "--scope"]:
    scope = args[4]
    if scope not in state["scope_versions"]:
        sys.exit(92)
    if state.get("fail_scope") == scope:
        print("synthetic native update failure", file=sys.stderr)
        sys.exit(17)
    if not state.get("no_op"):
        source = Path(state["release_root"])
        version = json.loads((source / "reference/version-policy.json").read_text())["latest_version"]
        manifest = Path(state["plugin_root"]) / ".claude-plugin/marketplace.json"
        data = json.loads(manifest.read_text())
        next(item for item in data["plugins"] if item["name"] == "helix")["version"] = version
        manifest.write_text(json.dumps(data))
        state["scope_versions"][scope] = version
        state_path.write_text(json.dumps(state))
    emit({"updated": True})
else:
    print("unexpected simulated Claude argv: " + repr(args), file=sys.stderr)
    sys.exit(91)
'''


@unittest.skipUnless(shutil.which("git"), "synthetic public bundle inventory requires Git")
class ClaudeUpdateScenarioTests(unittest.TestCase):
    # Reuse only public-bundle copying and fixture utilities, not Codex tests.
    release = fixtures.CodexUpdateScenarioTests.release
    native = fixtures.CodexUpdateScenarioTests.native
    git = fixtures.CodexUpdateScenarioTests.git

    def setUp(self):
        fixtures.CodexUpdateScenarioTests.setUp(self)
        self.native(scope_versions={"user": "1.0.0"}, repo_root=str(self.repo))
        env_sandbox.cli_stub(self.root / "bin/claude", NATIVE_STUB)
        self.env.pop("CODEX_THREAD_ID")
        self.env["CLAUDE_PLUGIN_ROOT"] = str(self.plugin)
        self.env["CLAUDE_PROJECT_DIR"] = str(self.repo)

    def cli(self, *args, code=0):
        process = subprocess.run(
            [sys.executable, str(self.plugin / "scripts/version_update.py"), "--platform", "claude-code",
             "--repo-root", str(self.repo), "--cache-path", str(self.cache),
             "--authorization-path", str(self.authorization), *args],
            env=self.env, cwd=self.repo, capture_output=True, text=True, timeout=30,
        )
        if code is None:
            self.assertIn(process.returncode, {0, 2}, process.stdout + process.stderr)
        else:
            self.assertEqual(process.returncode, code, process.stdout + process.stderr)
        result = json.loads(process.stdout)
        if code is None:
            result["process_exit_code"] = process.returncode
        return result

    def check(self):
        return self.cli("check", "--force-refresh")

    def prepare(self, scope="user"):
        return self.cli("prepare", "--scope", scope)["challenge_id"]

    def approve(self, scope="user"):
        self.check()
        challenge = self.prepare(scope)
        self.cli("decide", "--challenge-id", challenge, "--action", "upgrade_now", "--user-confirmed")
        return challenge

    def install(self, challenge, *, code=0):
        return self.cli("install", "--challenge-id", challenge, code=code)

    def calls(self, *, installations_only=False):
        calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()] if self.calls_path.exists() else []
        return [args for args in calls if args[:2] == ["plugin", "update"]] if installations_only else calls

    def installed_version(self):
        data = json.loads((self.plugin / ".claude-plugin/marketplace.json").read_text())
        return next(item["version"] for item in data["plugins"] if item["name"] == "helix")

    def use_baseline_update_core(self):
        """Replay HEAD's real updater and native adapters in the same fixture."""
        for relative in ("scripts/version_update.py", "scripts/update_hosts/__init__.py",
                         "scripts/update_hosts/base.py", "scripts/update_hosts/claude_code.py",
                         "scripts/update_hosts/codex.py"):
            content = subprocess.check_output(
                ["git", "-C", str(fixtures.REPOSITORY), "show", "HEAD:helixharness/" + relative],
                env=self.env, timeout=10,
            )
            (self.plugin / relative).write_bytes(content)

    def test_check_refreshes_marketplace_but_does_not_install_or_authorize(self):
        before = self.installed_version()
        result = self.check()
        self.assertEqual((result["current_version"], result["latest_version"]), ("1.0.0", "1.1.0"))
        self.assertFalse(self.authorization.exists())
        self.assertEqual(self.calls(installations_only=True), [])
        self.assertIn(["plugin", "marketplace", "update", "helixharness-marketplace"], self.calls())
        self.assertEqual(self.installed_version(), before)

    def test_pending_and_unconfirmed_decision_cannot_install(self):
        self.check()
        challenge = self.prepare()
        self.install(challenge, code=2)
        self.cli("decide", "--challenge-id", challenge, "--action", "upgrade_now", code=2)
        self.assertEqual(self.calls(installations_only=True), [])
        self.assertEqual(self.installed_version(), "1.0.0")

    def test_skip_and_cancel_preserve_installation(self):
        for action in ("skip", "cancel"):
            with self.subTest(action=action):
                self.check()
                challenge = self.prepare()
                if action == "skip":
                    self.cli("decide", "--challenge-id", challenge, "--action", "skip_update", "--user-confirmed")
                else:
                    self.cli("cancel", "--challenge-id", challenge)
                self.assertFalse(self.authorization.exists())
                self.install(challenge, code=2)
                self.assertEqual(self.installed_version(), "1.0.0")
                self.assertEqual(self.calls(installations_only=True), [])

    def test_required_skip_blocks_without_installing(self):
        self.release(self.marketplace, "1.1.0", required=True)
        self.assertEqual(self.check()["update_status"], "required")
        challenge = self.prepare()
        result = self.cli("decide", "--challenge-id", challenge, "--action", "skip_update", "--user-confirmed")
        self.assertTrue(result["blocked"])
        self.assertEqual(self.calls(installations_only=True), [])

    def test_confirmed_user_update_installs_expected_version_and_consumes_once(self):
        challenge = self.approve()
        result = self.install(challenge)
        self.assertTrue(result["updated"])
        self.assertTrue(result["authorization_consumed"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(self.installed_version(), "1.1.0")
        self.assertEqual(self.calls(installations_only=True), [
            ["plugin", "update", "helix@helixharness-marketplace", "--scope", "user"]])
        self.install(challenge, code=2)
        self.assertEqual(self.check()["update_status"], "latest")

    def test_project_scope_takes_precedence_and_updates_only_project(self):
        self.native(scope_versions={"user": "1.0.0", "project": "1.0.0"})
        self.assertEqual(self.check()["current_scope"], "project")
        self.install(self.approve("project"))
        self.assertEqual(self.native()["scope_versions"], {"user": "1.0.0", "project": "1.1.0"})
        self.assertEqual(self.calls(installations_only=True), [
            ["plugin", "update", "helix@helixharness-marketplace", "--scope", "project"]])

    def test_all_scope_resumes_failed_project_without_repeating_user_update(self):
        self.native(scope_versions={"user": "1.0.0", "project": "1.0.0"})
        challenge = self.approve("all")
        self.native(fail_scope="project")
        self.assertIn("update-project", self.install(challenge, code=2)["error"])
        journal = json.loads(self.authorization.read_text())
        self.assertEqual(journal["completed_steps"], ["update-user"])
        self.native(fail_scope=None)
        result = self.install(challenge)
        self.assertTrue(result["resumed"])
        self.assertEqual(self.native()["scope_versions"], {"user": "1.1.0", "project": "1.1.0"})
        self.assertEqual([args[-1] for args in self.calls(installations_only=True)], ["user", "project", "project"])

    def test_native_failure_keeps_authorization_and_old_version_for_retry(self):
        challenge = self.approve()
        self.native(fail_scope="user")
        self.install(challenge, code=2)
        self.assertTrue(self.authorization.exists())
        self.assertEqual(self.installed_version(), "1.0.0")
        self.native(fail_scope=None)
        self.assertTrue(self.install(challenge)["resumed"])
        self.assertEqual(self.installed_version(), "1.1.0")

    def test_refreshed_new_target_rejects_stale_authorization(self):
        challenge = self.approve()
        self.release(self.marketplace, "1.2.0")
        self.assertEqual(self.check()["latest_version"], "1.2.0")
        self.assertIn("no longer matches cache", self.install(challenge, code=2)["error"])
        self.assertEqual(self.installed_version(), "1.0.0")
        self.assertEqual(self.calls(installations_only=True), [])

    def test_invalid_scope_or_live_source_mismatch_cannot_install(self):
        self.check()
        self.cli("prepare", "--scope", "local", code=2)
        challenge = self.approve()
        self.native(reported_path=str(self.root / "foreign-install"))
        self.install(challenge, code=2)
        self.assertEqual(self.calls(installations_only=True), [])

    def test_expired_authorization_cannot_install(self):
        challenge = self.approve()
        data = json.loads(self.authorization.read_text())
        data.update(created_at="2020-01-01T00:00:00Z", authorized_at="2020-01-01T00:00:01Z",
                    expires_at="2020-01-01T00:10:00Z")
        self.authorization.write_text(json.dumps(data))
        self.assertIn("expired", self.install(challenge, code=2)["error"])
        self.assertEqual(self.calls(installations_only=True), [])


def probe_native_postconditions(*, baseline=False):
    """Return explicit failures; never count these as standard-suite passes."""
    results = []
    for scenario in ("native_zero_exit_without_install", "source_advances_after_approval"):
        fixture = ClaudeUpdateScenarioTests(methodName="runTest")
        try:
            fixture.setUp()
            if baseline:
                fixture.use_baseline_update_core()
            challenge = fixture.approve()
            approved = json.loads(fixture.authorization.read_text())["target_version"]
            before = fixture.installed_version()
            if scenario == "native_zero_exit_without_install":
                fixture.native(no_op=True)
            else:
                fixture.release(fixture.marketplace, "1.2.0")
            result = fixture.install(challenge, code=None)
            actual = fixture.installed_version()
            correct_success = result.get("updated") is True and actual == approved
            safe_rejection = result["process_exit_code"] == 2 and actual == before and fixture.authorization.exists()
            results.append({
                "scenario": scenario, "approved_version": approved, "actual_version": actual,
                "reported_updated": result.get("updated"),
                "authorization_consumed": not fixture.authorization.exists(),
                "status": "pass" if correct_success or safe_rejection else "fail",
            })
        finally:
            fixture.doCleanups()
    return {"simulation": True, "real_claude_executed": False,
            "update_core": "HEAD" if baseline else "working_tree", "results": results}


if __name__ == "__main__":
    if "--probe-postconditions" in sys.argv[1:]:
        findings = probe_native_postconditions(baseline="--baseline" in sys.argv[1:])
        print(json.dumps(findings, ensure_ascii=False, indent=2))
        raise SystemExit(any(item["status"] != "pass" for item in findings["results"]))
    unittest.main()
