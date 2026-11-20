"""Cursor command simulations run from an isolated public plugin bundle.

The harness supplies synthetic workflow artifacts and native invocation context;
it does not run a Cursor model or treat fixture reviews as actual agent reviews.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow
import validate_init_outputs
import workflow

PLUGIN = Path(__file__).resolve().parents[2]
REPOSITORY = PLUGIN.parent
COMMANDS = {"init", "propose", "apply", "archive", "bug-fix", "ut", "update", "status", "config"}
PUBLIC_ROOTS = {".claude-plugin", ".codex-plugin", ".cursor-plugin", "scripts", "hosts", "commands", "workflows", "reference", "skills", "agents", "templates"}
PRIVATE_PARTS = {".git", ".codex", ".claude", ".cursor", ".agents", ".aws", "output", "temp", "runtime", "sessions", "rollouts", "cache", ".cache", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}


class CursorCommandScenariosTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="helix-cursor-command-scenarios-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.plugin = self.root / "isolated Cursor 插件"
        self.repo = self.root / "synthetic project 中文 空格"
        self.repo.mkdir()
        self.env = {
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "TMPDIR": str(self.root), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
            "CODEX_HOME": str(self.root / "codex"), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "HELIXHARNESS_CURSOR_HOME": str(self.root / "cursor"),
            "HELIXHARNESS_TELEMETRY_HOME": str(self.root / "telemetry"),
            "HELIXHARNESS_FEEDBACK_HOME": str(self.root / "feedback"),
            "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
            "HELIXHARNESS_BOUND_PLUGIN_ROOT": str(self.plugin),
            "HELIXHARNESS_CURSOR_CONVERSATION_ID": "synthetic-cursor-command-session",
            "HELIXHARNESS_CURSOR_WORKSPACE_ROOTS": json.dumps([str(self.repo)]),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
        }
        inventory = subprocess.check_output(
            ["git", "-C", str(REPOSITORY), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "helixharness"],
            env=self.env, timeout=10,
        ).decode().split("\0")
        for name in set(inventory):
            if not name:
                continue
            relative = Path(name).relative_to("helixharness")
            if relative.parts[0] not in PUBLIC_ROOTS or relative.parts[:2] == ("scripts", "tests"):
                continue
            if any(part in PRIVATE_PARTS or part.startswith(".env") for part in relative.parts):
                continue
            source = REPOSITORY / name
            self.assertFalse(source.is_symlink(), name)
            if source.is_file():
                destination = self.plugin / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
        isolated = patch.dict(os.environ, self.env, clear=True)
        isolated.start()
        self.addCleanup(isolated.stop)
        # Existing artifact factories also execute CLI validations. Keep those
        # calls anchored to this installed bundle, not the developer checkout.
        helper_script = patch.object(test_workflow, "SCRIPT", self.plugin / "scripts/workflow.py")
        helper_script.start()
        self.addCleanup(helper_script.stop)

    def process(self, arguments, *, repo=None, env=None, code=0):
        result = subprocess.run(list(map(str, arguments)), cwd=repo or self.repo,
                                env=self.env if env is None else env, capture_output=True,
                                text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def cli(self, script, *arguments, **options):
        result = self.process([sys.executable, "-B", self.plugin / "scripts" / script, *arguments], **options)
        return json.loads(result.stdout)

    def entry(self, name, *, root=None):
        root = root or self.plugin
        return self.cli("cursor_skill_projection.py", "--root", root, "--resolve-entry",
                        root / f"hosts/cursor/skills/helix-{name}/SKILL.md")

    def route(self, command, arguments, *, repo=None, code=0):
        self.assertEqual(self.entry(command)["canonical"], f"commands/{command}.md")
        return self.cli("workflow_router.py", "--repo-root", repo or self.repo, "route",
                        "--command", command, "--arguments", arguments, code=code)

    def snapshot(self, root=None):
        root = root or self.repo
        return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()}

    def fixture(self, *, reviewed=False):
        fixture = test_workflow.LiteWorkflowCliTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        if reviewed:
            fixture.write_spec(status="implemented", body="Synthetic reviewed feature.\n")
            fixture.write_tasks(status="completed", rows="| T-01 | Synthetic task | AC-01 | — | completed |")
            fixture.write_evidence(body="Synthetic evidence fixture; no model review was run.\n")
        return fixture

    def test_relocated_bundle_resolves_all_nine_commands_and_eight_methods_without_checkout_imports(self):
        preflight = self.process(["sh", self.plugin / "scripts/runtime_preflight.sh", "check"])
        self.assertTrue(json.loads(preflight.stdout)["ok"])
        conflicting = {**self.env, "CODEX_THREAD_ID": "synthetic-other-host", "CLAUDE_SESSION_ID": "synthetic-claude",
                       "HELIXHARNESS_PLATFORM": "codex"}
        context = self.cli("host_runtime_cli.py", "resolve", "--platform", "cursor",
                           "--expected-plugin-root", self.plugin, env=conflicting)
        self.assertEqual(context["host_id"], "cursor")
        self.assertEqual(context["plugin_root"], str(self.plugin))
        self.assertNotIn("PYTHONPATH", self.env)
        for resource in context["resolved_resources"].values():
            path = Path(resource)
            self.assertTrue(path.is_relative_to(self.plugin))
            self.assertTrue(path.is_file())
        canonical_methods = {path.parent.name for path in (self.plugin / "skills").glob("*/SKILL.md")}
        self.assertEqual(len(canonical_methods), 8)
        for name in sorted(COMMANDS | canonical_methods):
            with self.subTest(entry=name):
                receipt = self.entry(name)
                kind = "command" if name in COMMANDS else "method"
                prefix = "commands" if kind == "command" else "skills"
                canonical = f"{prefix}/{name}.md" if kind == "command" else f"{prefix}/{name}/SKILL.md"
                self.assertEqual(receipt["entry_kind"], kind)
                self.assertEqual(receipt["canonical"], canonical)
                content = (self.plugin / canonical).read_bytes()
                self.assertEqual(hashlib.sha256(content).digest(), hashlib.sha256((PLUGIN / canonical).read_bytes()).digest())
        ut_body = (self.plugin / self.entry("ut")["canonical"]).read_text()
        for resource in ("runtime", "ut", "subagent_protocol"):
            self.assertIn(f"resources.{resource}", ut_body)
            self.assertTrue((self.plugin / context["resources"][resource]).is_file())
        self.assertTrue((self.plugin / "agents/unit-test-worker.md").is_file())
        self.assertEqual(self.snapshot(), {})

    def test_bound_source_and_missing_canonical_contract_stop_before_business_mutation(self):
        before = self.snapshot()
        foreign = {**self.env, "HELIXHARNESS_BOUND_PLUGIN_ROOT": str(PLUGIN)}
        rejected = self.cli("host_runtime_cli.py", "resolve", "--platform", "cursor",
                            "--expected-plugin-root", self.plugin, env=foreign, code=2)
        self.assertEqual(rejected["reason"], "plugin_source_mismatch")
        (self.plugin / "commands/apply.md").unlink()
        failed = self.process([sys.executable, self.plugin / "scripts/cursor_skill_projection.py", "--root", self.plugin,
                               "--resolve-entry", self.plugin / "hosts/cursor/skills/helix-apply/SKILL.md"], code=2)
        self.assertIn("projection source is missing", failed.stderr)
        self.assertEqual(before, self.snapshot())

    def test_command_argument_binding_preserves_literal_flags_newlines_and_shell_characters(self):
        tokens = ["demo", '中文需求 "quoted"\nsecond line', "--lite", "--revise", "$(synthetic-no-execution)", "a;b"]
        arguments = "demo --loop -- " + shlex.join(tokens[1:])
        before = self.snapshot()
        routed = self.route("propose", arguments)
        self.assertEqual(routed["workflow"], "workflows/single/propose.md")
        self.assertTrue(routed["loop_requested"])
        self.assertEqual(shlex.split(routed["workflow_arguments"]), tokens)
        self.assertEqual(before, self.snapshot())

    def test_single_apply_resume_review_and_archive_finalization_use_the_installed_cli(self):
        fixture = self.fixture()
        fixture.write_spec(status="ready", body="Synthetic implementation requirement.\n")
        fixture.write_tasks(status="ready", rows="| T-01 | First | AC-01 | — | pending |\n| T-02 | Second | AC-02 | T-01 | pending |")
        notes = fixture.repo / "user-notes.txt"
        notes.write_text("Synthetic pre-existing notes.\n")
        self.assertEqual(self.route("propose", "checkout --loop", repo=fixture.repo)["stage"], "apply")
        bind_args = ("--repo-root", fixture.repo, "bind-apply", "--feature", "checkout")
        first = self.cli("workflow.py", *bind_args)
        self.assertEqual(first, self.cli("workflow.py", *bind_args))
        baseline = fixture.repo / ".helixharness/workflow/checkout/apply-state.json"
        baseline_bytes = baseline.read_bytes()
        spec = fixture.feature_dir / "spec.md"
        tasks = fixture.feature_dir / "tasks.md"
        spec.write_text(spec.read_text().replace("status: ready", "status: applying"))
        tasks.write_text(tasks.read_text().replace("status: ready", "status: applying").replace(
            "| T-01 | First | AC-01 | — | pending |", "| T-01 | First | AC-01 | — | completed |"))
        output = fixture.repo / "synthetic-output.py"
        output.write_text("result = 1\n")
        checked = self.cli("workflow.py", "--repo-root", fixture.repo, "validate-tasks", "--feature", "checkout", "--stage", "apply")
        self.assertEqual(checked["next_task"], "T-02")
        scope = self.cli("workflow.py", "--repo-root", fixture.repo, "review-scope", "--feature", "checkout")
        self.assertEqual(scope["changed_paths"], ["synthetic-output.py"])
        self.assertIn("user-notes.txt", scope["excluded_preexisting_paths"])
        self.assertEqual(self.route("propose", "checkout --loop", repo=fixture.repo)["stage"], "apply")
        spec.write_text(spec.read_text().replace("status: applying", "status: implemented"))
        tasks.write_text(tasks.read_text().replace("status: applying", "status: completed").replace("| pending |", "| completed |"))
        fixture.write_evidence(body="Synthetic task/test/review receipts for gate simulation.\n")
        self.assertEqual(self.route("propose", "checkout --loop", repo=fixture.repo)["stage"], "archive")
        routed = self.route("archive", "checkout --no-commit", repo=fixture.repo)
        self.assertEqual(shlex.split(routed["workflow_arguments"]), ["checkout", "--no-commit"])
        self.cli("workflow.py", "--repo-root", fixture.repo, "archive-preflight", "--feature", "checkout")
        spec.write_text(spec.read_text().replace("status: implemented", "status: completed"))
        evidence = fixture.feature_dir / "verify-report.md"
        evidence.write_text(evidence.read_text().replace("archive: pending", "archive: completed"))
        final_args = ("--repo-root", fixture.repo, "finalize-preflight", "--feature", "checkout")
        final = self.cli("workflow.py", *final_args)
        self.assertEqual(final["archive_state"], "finalizing")
        self.assertEqual(final, self.cli("workflow.py", *final_args))
        completed = fixture.repo / "docs/specs/completed/checkout"
        completed.parent.mkdir(parents=True)
        fixture.feature_dir.rename(completed)  # Simulate the canonical business move.
        blocked = self.route("propose", "checkout --loop", repo=fixture.repo, code=2)
        self.assertEqual(blocked["reason"], "feature_already_archived")
        self.assertEqual(baseline.read_bytes(), baseline_bytes)
        self.assertEqual(notes.read_text(), "Synthetic pre-existing notes.\n")

    def test_missing_or_stale_review_evidence_cannot_reach_archive(self):
        fixture = self.fixture(reviewed=True)
        evidence = fixture.feature_dir / "verify-report.md"
        preserved = evidence.read_bytes()
        evidence.unlink()
        self.assertEqual(self.route("propose", "checkout --loop", repo=fixture.repo)["stage"], "apply")
        evidence.write_bytes(preserved)
        self.assertEqual(self.route("propose", "checkout --loop", repo=fixture.repo)["stage"], "archive")
        spec = fixture.feature_dir / "spec.md"
        spec.write_text(spec.read_text() + "Unreviewed requirement.\n")
        self.assertEqual(self.route("propose", "checkout --loop", repo=fixture.repo)["stage"], "apply")
        result = self.process([sys.executable, self.plugin / "scripts/workflow.py", "--repo-root", fixture.repo,
                               "archive-preflight", "--feature", "checkout"], code=2)
        self.assertIn("receipt", result.stderr)

    def test_bug_fix_begin_and_repeated_preflight_preserve_original_apply_baseline(self):
        fixture = self.fixture(reviewed=True)
        baseline = fixture.repo / ".helixharness/workflow/checkout/apply-state.json"
        before = baseline.read_bytes()
        routed = self.route("bug-fix", 'checkout "修复 边界条件"', repo=fixture.repo)
        self.assertEqual(routed["workflow"], "workflows/single/bug-fix.md")
        arguments = ("--repo-root", fixture.repo, "begin-repair", "--feature", "checkout", "--repair-key", "c" * 64,
                     "--source-sha256", "d" * 64, "--repair-kind", "implementation_regression",
                     "--expected-spec-receipt", workflow.spec_receipt((fixture.feature_dir / "spec.md").read_text()),
                     "--expected-tasks-receipt", workflow.artifact_receipt((fixture.feature_dir / "tasks.md").read_text()))
        first = self.cli("workflow.py", *arguments)
        second = self.cli("workflow.py", *arguments)
        self.assertFalse(first["resumed"])
        self.assertTrue(second["resumed"])
        result = self.cli("workflow.py", "--repo-root", fixture.repo, "repair-preflight", "--feature", "checkout")
        self.assertEqual(result["repair_state"]["repair_key"], "c" * 64)
        self.assertEqual(self.route("bug-fix", "checkout", repo=fixture.repo)["action"], "repair_feature")
        self.assertEqual(baseline.read_bytes(), before)

    def test_workspace_routes_revision_and_loop_without_mutating_child_repositories(self):
        for name in ("api", "web"):
            child = self.repo / name
            child.mkdir()
            (child / "user-file.txt").write_text(f"Synthetic {name} content.\n")
        (self.repo / "helixharness.workspace.yaml").write_text("version: 1\nrepos:\n  api:\n    path: ./api\n  web:\n    path: ./web\n")
        feature = self.repo / "docs/specs/active/demo"
        feature.mkdir(parents=True)
        for state, expected in (("planning", "propose"), ("ready", "apply"), ("applying", "apply")):
            with self.subTest(state=state):
                (feature / "spec.md").write_text(f"---\nfeature: demo\nexecution_scope: workspace\nstatus: {state}\n---\nSynthetic multi-repo change.\n")
                before = self.snapshot()
                routed = self.route("propose", "demo --loop")
                self.assertEqual(routed["workflow"], f"workflows/workspace/{expected}.md")
                revision = self.route("propose", 'demo "revised requirement.md" --revise --loop')
                self.assertTrue(revision["revision_requested"])
                self.assertEqual(revision["workflow"], "workflows/workspace/propose.md")
                self.assertEqual(before, self.snapshot())

    def test_init_merges_flat_hooks_and_synthetic_after_edit_handlers_run_from_project_root(self):
        for mode in ("service", "frontend"):
            with self.subTest(mode=mode):
                repo = self.repo / mode
                repo.mkdir()
                for relative in validate_init_outputs.SERVICE_COMMON_DIRS:
                    (repo / relative).mkdir(parents=True, exist_ok=True)
                for relative in (*validate_init_outputs.SERVICE_COMMON_FILES, "AGENTS.md", "docs/knowledge/service-meta.yaml"):
                    (repo / relative).write_text("Synthetic initialized project; preserve custom content.\n")
                entry = repo / "AGENTS.md"
                entry.write_text(entry.read_text() + "\n" + (self.plugin / "templates/project-entry.md.tpl").read_text().replace(
                    "{{PROJECT_CONTEXT}}", "Synthetic project context."))
                self.assertEqual(self.cli("init_guidance.py", "--repo-root", repo, "--platform", "cursor", "--mode", mode, "check")["status"], "pass")
                (repo / "docs/harness/linters").mkdir(parents=True, exist_ok=True)
                for name in ("invariant", "spec-sync"):
                    (repo / f"docs/harness/linters/{name}-check.sh").write_text(f"#!/bin/sh\nprintf '{name}\\n' >> hook-observed.txt\n")
                other_hosts = {}
                for relative in (".claude/settings.json", ".codex/hooks.json"):
                    path = repo / relative
                    path.parent.mkdir()
                    path.write_text('{"synthetic_user_setting": true}')
                    other_hosts[path] = path.read_bytes()
                settings = repo / ".cursor/hooks.json"
                settings.parent.mkdir()
                custom = {"version": 1, "synthetic_user_setting": True, "hooks": {"afterFileEdit": [
                    {"command": "printf 'custom\\n' >> hook-observed.txt"}]}}
                settings.write_text(json.dumps(custom))
                validation = ("--repo-root", repo, "--mode", mode, "--platform", "cursor", "--telemetry-mode", "disabled")
                self.assertEqual(self.cli("validate_init_outputs.py", *validation, code=1)["status"], "fail")
                merged = self.cli("project_hooks_cli.py", "--repo-root", repo, "--platform", "cursor", "merge")
                self.assertTrue(merged["changed"])
                before = self.snapshot(repo)
                self.assertFalse(self.cli("project_hooks_cli.py", "--repo-root", repo, "--platform", "cursor", "merge")["changed"])
                self.assertEqual(before, self.snapshot(repo))
                self.assertEqual(self.cli("validate_init_outputs.py", *validation)["status"], "pass")
                handlers = json.loads(settings.read_text())["hooks"]["afterFileEdit"]
                self.assertEqual(len(handlers), 3)
                for handler in handlers:
                    self.process(["sh", "-c", handler["command"]], repo=repo)
                self.assertEqual((repo / "hook-observed.txt").read_text().splitlines(), ["custom", "invariant", "spec-sync"])
                self.assertEqual(other_hosts, {path: path.read_bytes() for path in other_hosts})


if __name__ == "__main__":
    unittest.main()
