"""Exercise the real updater CLI with an isolated, explicitly simulated Codex CLI.

The fixture models native command effects and records argv; it does not test the
real Codex binary, marketplace service, or model. All data and Git repos are synthetic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import env_sandbox

PLUGIN = Path(__file__).resolve().parents[2]
REPOSITORY = PLUGIN.parent
PUBLIC_ROOTS = {".codex-plugin", ".claude-plugin", ".cursor-plugin", ".qoder-plugin", "package.json", "scripts", "hosts", "commands", "workflows", "reference", "skills", "agents", "templates"}
PRIVATE_PARTS = {".git", ".codex", ".claude", ".cursor", ".agents", ".aws", "output", "temp", "runtime", "sessions", "rollouts", "cache", ".cache", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}
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
def install():
    if state.get("no_op"):
        return
    source = Path(state["release_root"])
    version = json.loads((source / "reference/version-policy.json").read_text())["latest_version"]
    manifest = Path(state["plugin_root"]) / ".codex-plugin/plugin.json"
    data = json.loads(manifest.read_text())
    data["version"] = version
    manifest.write_text(json.dumps(data))
    state["version"] = version
    state_path.write_text(json.dumps(state))
if args == ["plugin", "list", "--json", "--marketplace", "helixharness-marketplace"]:
    emit({"installed": [{"pluginId": "helix@helixharness-marketplace", "installed": True,
        "enabled": True, "version": state["version"],
        "source": {"path": state.get("reported_path", state["plugin_root"])}}]})
elif args == ["plugin", "marketplace", "list", "--json"]:
    emit({"marketplaces": [{"name": "helixharness-marketplace", "root": state["marketplace"],
        "marketplaceSource": {"sourceType": state["kind"], "source": state["release_root"]}}]})
elif args == ["plugin", "marketplace", "upgrade", "--json", "helixharness-marketplace"]:
    install()
    emit({"upgraded": True})
elif args == ["plugin", "add", "helix@helixharness-marketplace", "--json"]:
    if state.get("fail_add"):
        print("synthetic native install failure", file=sys.stderr)
        sys.exit(17)
    install()
    emit({"installed": True})
else:
    print("unexpected simulated Codex argv: " + repr(args), file=sys.stderr)
    sys.exit(91)
'''


@unittest.skipUnless(shutil.which("git"), "synthetic Git releases require Git")
class CodexUpdateScenarioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="helix-codex-update-scenarios-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.plugin = self.root / "installed plugin 空格"
        inventory = subprocess.check_output(
            ["git", "-C", str(REPOSITORY), "ls-files", "-z", "--cached", "--others",
             "--exclude-standard", "--", "helixharness"], timeout=10,
        ).decode().split("\0")
        for name in set(inventory):
            if not name:
                continue
            relative = Path(name).relative_to("helixharness")
            if relative.parts[0] not in PUBLIC_ROOTS or relative.parts[:2] == ("scripts", "tests"):
                continue
            if any(part in PRIVATE_PARTS or part.startswith(".env") for part in relative.parts):
                continue
            original = REPOSITORY / name
            self.assertFalse(original.is_symlink(), name)
            if not original.is_file():
                continue
            destination = self.plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
        self.repo = self.root / "application 空格"
        self.repo.mkdir()
        self.marketplace = self.root / "local marketplace"
        self.marketplace.mkdir()
        self.release(self.marketplace, "1.1.0")
        self.calls_path = self.root / "native-calls.jsonl"
        self.native_state = self.root / "native-state.json"
        self.native_state.write_text(json.dumps({
            "calls": str(self.calls_path), "version": "1.0.0", "plugin_root": str(self.plugin),
            "marketplace": str(self.marketplace), "release_root": str(self.marketplace), "kind": "local",
        }))
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "codex"
        stub.write_text("#!" + sys.executable + "\n" + NATIVE_STUB)
        stub.chmod(0o700)
        # An allowlist avoids inheriting real host identity, credentials, Python
        # import paths, endpoints or configuration. The actual Codex CLI is shadowed.
        self.env = {
            **env_sandbox.boot_vars(),
            "PATH": str(bin_dir) + os.pathsep + env_sandbox.search_path() + os.pathsep + os.environ.get("PATH", os.defpath),
            "TMPDIR": str(self.root), "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "CODEX_HOME": str(self.root / "codex-home"),
            "CODEX_THREAD_ID": "synthetic-" + self.root.name,
            "CLAUDE_CONFIG_DIR": str(self.root / "claude-home"),
            "HELIXHARNESS_CURSOR_HOME": str(self.root / "cursor-home"),
            "HELIXHARNESS_TELEMETRY_HOME": str(self.root / "telemetry-home"),
            "HELIXHARNESS_FEEDBACK_HOME": str(self.root / "feedback-home"),
            "HELIXHARNESS_BOUND_PLUGIN_ROOT": str(self.plugin),
            "HELIX_TEST_NATIVE_STATE": str(self.native_state),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.cache = self.root / "state/cache.json"
        self.authorization = self.root / "state/authorization.json"

    def release(self, root, version, *, required=False):
        files = {
            ".claude-plugin/marketplace.json": {"name": "helixharness-marketplace", "plugins": [
                {"name": "helix", "source": ".", "version": version}]},
            ".codex-plugin/plugin.json": {"name": "helix", "version": version},
            "reference/version-policy.json": {
                "schema_version": "1.0", "plugin": "helix", "marketplace": "helixharness-marketplace",
                "latest_version": version, "minimum_supported_version": "1.0.0",
                "force_update_below": "1.0.0", "force_update_message": "Synthetic policy",
                "releases": [{"version": version, "date": "2026-11-18",
                              "update_type": "required" if required else "recommended",
                              "title": "Synthetic release", "changes": ["Synthetic update"],
                              "restart_required": True}],
            },
        }
        if required:
            files["reference/version-policy.json"]["releases"][0]["required_reason"] = "Synthetic required compatibility change"
        for relative, data in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        (root / "commands").mkdir(exist_ok=True)
        (root / "commands/apply.md").write_text("Synthetic public command\n")

    def native(self, **changes):
        state = json.loads(self.native_state.read_text())
        state.update(changes)
        self.native_state.write_text(json.dumps(state))
        return state

    def git(self, root, *args):
        return subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=Synthetic Update",
             "-c", "user.email=test@example.invalid", *args],
            env=self.env, capture_output=True, text=True, check=True, timeout=20,
        ).stdout.strip()

    def git_source(self):
        self.git(self.marketplace, "init", "--template=", "-b", "main")
        self.git(self.marketplace, "add", ".")
        self.git(self.marketplace, "commit", "-m", "Synthetic release")
        host_checkout = self.root / "host marketplace checkout"
        self.git(self.root, "clone", str(self.marketplace), str(host_checkout))
        self.native(kind="git", marketplace=str(host_checkout))

    def cli(self, *args, code=0):
        process = subprocess.run(
            [sys.executable, str(self.plugin / "scripts/version_update.py"), "--platform", "codex",
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

    def prepare(self):
        return self.cli("prepare", "--scope", "default")["challenge_id"]

    def approve(self):
        self.check()
        challenge = self.prepare()
        self.cli("decide", "--challenge-id", challenge, "--action", "upgrade_now", "--user-confirmed")
        return challenge

    def install(self, challenge, *, code=0):
        return self.cli("install", "--challenge-id", challenge, code=code)

    def calls(self, *, mutations_only=False):
        calls = [json.loads(line) for line in self.calls_path.read_text().splitlines()] if self.calls_path.exists() else []
        return [args for args in calls if args[:2] == ["plugin", "add"] or args[:3] == ["plugin", "marketplace", "upgrade"]] if mutations_only else calls

    def installed_version(self):
        return json.loads((self.plugin / ".codex-plugin/plugin.json").read_text())["version"]

    def test_check_never_installs_or_creates_authorization(self):
        before = {str(path.relative_to(self.plugin)): path.read_bytes() for path in self.plugin.rglob("*") if path.is_file()}
        result = self.check()
        self.assertEqual((result["current_version"], result["latest_version"]), ("1.0.0", "1.1.0"))
        self.assertFalse(self.authorization.exists())
        self.assertEqual(self.calls(mutations_only=True), [])
        self.assertEqual(before, {str(path.relative_to(self.plugin)): path.read_bytes() for path in self.plugin.rglob("*") if path.is_file()})

    def test_pending_or_unconfirmed_decision_cannot_install(self):
        self.check()
        challenge = self.prepare()
        self.install(challenge, code=2)
        self.cli("decide", "--challenge-id", challenge, "--action", "upgrade_now", code=2)
        self.assertEqual(self.calls(mutations_only=True), [])
        self.assertEqual(self.installed_version(), "1.0.0")

    def test_recommended_skip_consumes_challenge_and_preserves_installation(self):
        self.check()
        challenge = self.prepare()
        result = self.cli("decide", "--challenge-id", challenge, "--action", "skip_update", "--user-confirmed")
        self.assertFalse(result["blocked"])
        self.assertFalse(self.authorization.exists())
        self.install(challenge, code=2)
        self.assertEqual(self.installed_version(), "1.0.0")
        self.assertEqual(self.calls(mutations_only=True), [])

    def test_required_skip_blocks_work_without_installing(self):
        self.release(self.marketplace, "1.1.0", required=True)
        self.assertEqual(self.check()["update_status"], "required")
        challenge = self.prepare()
        result = self.cli("decide", "--challenge-id", challenge, "--action", "skip_update", "--user-confirmed")
        self.assertTrue(result["blocked"])
        self.assertEqual(self.calls(mutations_only=True), [])
        self.assertFalse(self.authorization.exists())

    def test_confirmed_local_update_changes_simulated_version_and_consumes_once(self):
        challenge = self.approve()
        result = self.install(challenge)
        self.assertTrue(result["updated"])
        self.assertTrue(result["authorization_consumed"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(self.installed_version(), "1.1.0")
        self.assertEqual(self.calls(mutations_only=True), [["plugin", "add", "helix@helixharness-marketplace", "--json"]])
        self.install(challenge, code=2)
        self.assertEqual(self.check()["update_status"], "latest")

    def test_git_add_failure_resumes_without_repeating_completed_marketplace_upgrade(self):
        self.git_source()
        challenge = self.approve()
        self.assertEqual(self.calls(mutations_only=True), [])
        self.native(fail_add=True)
        self.assertIn("update-default", self.install(challenge, code=2)["error"])
        journal = json.loads(self.authorization.read_text())
        self.assertEqual(journal["state"], "installing")
        self.assertEqual(journal["completed_steps"], ["refresh-marketplace"])
        self.native(fail_add=False)
        self.assertTrue(self.install(challenge)["resumed"])
        calls = self.calls(mutations_only=True)
        self.assertEqual(sum(args[:3] == ["plugin", "marketplace", "upgrade"] for args in calls), 1)
        self.assertEqual(sum(args[:2] == ["plugin", "add"] for args in calls), 2)
        self.assertEqual(self.installed_version(), "1.1.0")

    def test_refreshed_new_target_rejects_stale_authorization(self):
        challenge = self.approve()
        self.release(self.marketplace, "1.2.0")
        self.assertEqual(self.check()["latest_version"], "1.2.0")
        self.assertIn("no longer matches cache", self.install(challenge, code=2)["error"])
        self.assertEqual(self.installed_version(), "1.0.0")
        self.assertEqual(self.calls(mutations_only=True), [])

    def test_invalid_scope_and_changed_live_source_are_rejected(self):
        self.check()
        self.cli("prepare", "--scope", "local", code=2)
        self.assertFalse(self.authorization.exists())
        challenge = self.prepare()
        self.cli("decide", "--challenge-id", challenge, "--action", "upgrade_now", "--user-confirmed")
        self.native(reported_path=str(self.root / "foreign installation"))
        self.install(challenge, code=2)
        self.assertEqual(self.calls(mutations_only=True), [])

    def test_expired_authorization_cannot_execute_native_install(self):
        challenge = self.approve()
        data = json.loads(self.authorization.read_text())
        data.update(created_at="2020-01-01T00:00:00Z", authorized_at="2020-01-01T00:00:01Z",
                    expires_at="2020-01-01T00:10:00Z")
        self.authorization.write_text(json.dumps(data))
        self.assertIn("expired", self.install(challenge, code=2)["error"])
        self.assertEqual(self.calls(mutations_only=True), [])

    def test_resume_with_unknown_step_is_rejected(self):
        challenge = self.approve()
        data = json.loads(self.authorization.read_text())
        data.update(state="installing", install_started_at=data["authorized_at"], completed_steps=["foreign-step"])
        self.authorization.write_text(json.dumps(data))
        self.assertIn("unknown completed step", self.install(challenge, code=2)["error"])
        self.assertEqual(self.calls(mutations_only=True), [])


def probe_native_postconditions():
    """Report adversarial simulation failures separately from passing regressions.

    A successful updater response must imply the simulated installed version
    equals the approved version. Exit status zero alone is not that evidence.
    """
    results = []
    for scenario in ("native_zero_exit_without_install", "source_advances_after_approval"):
        fixture = CodexUpdateScenarioTests(methodName="runTest")
        try:
            fixture.setUp()
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
                "scenario": scenario, "approved_version": approved,
                "actual_version": actual, "reported_updated": result.get("updated"),
                "authorization_consumed": not fixture.authorization.exists(),
                "status": "pass" if correct_success or safe_rejection else "fail",
            })
        finally:
            fixture.doCleanups()
    return {"simulation": True, "real_codex_executed": False, "results": results}


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe-postconditions"]:
        findings = probe_native_postconditions()
        print(json.dumps(findings, ensure_ascii=False, indent=2))
        raise SystemExit(any(item["status"] != "pass" for item in findings["results"]))
    unittest.main()
