"""Codex command simulations using real CLI entrypoints and synthetic projects.

The harness supplies prepared workflow artifacts; it does not run a Codex model,
generate business implementations, or count synthetic reviews as real approvals.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import env_sandbox
import test_workflow
import validate_init_outputs

PLUGIN = Path(__file__).resolve().parents[2]
COMMANDS = {"init", "propose", "apply", "archive", "ut", "bug-fix", "update", "status", "config"}


class CodexCommandScenariosTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="helix-codex-command-scenarios-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "合成 application with spaces"
        self.repo.mkdir()
        self.state = self.root / "entry snapshots"
        self.env = {
            **env_sandbox.boot_vars(),
            "PATH": env_sandbox.search_path(),
            "TMPDIR": str(self.root),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CODEX_HOME": str(self.root / "codex"),
            "CODEX_THREAD_ID": "synthetic-codex-command-thread",
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "HELIXHARNESS_CURSOR_HOME": str(self.root / "cursor"),
            "HELIXHARNESS_TELEMETRY_HOME": str(self.root / "telemetry"),
            "HELIXHARNESS_FEEDBACK_HOME": str(self.root / "feedback"),
            "HELIXHARNESS_BOUND_PLUGIN_ROOT": str(PLUGIN),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull,
        }
        # Fixture helpers also spawn subprocesses. Clear inherited user settings,
        # credentials and session identities before calling those helpers.
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def cli(self, script: str, *arguments: object, code: int = 0, env: dict | None = None) -> dict:
        process = subprocess.run(
            [sys.executable, "-B", str(PLUGIN / "scripts" / script), *map(str, arguments)],
            cwd=self.repo, env=self.env if env is None else env,
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(process.returncode, code, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def files(self, root: Path | None = None) -> dict:
        location = root or self.root
        return {
            str(path.relative_to(location)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in location.rglob("*") if path.is_file()
        }

    def entry(self, *arguments: object, **options: object) -> dict:
        return self.cli(
            "codex_entry_load.py", "--plugin-root", PLUGIN,
            "--state-root", self.state, *arguments, **options,
        )

    def route(self, command: str, arguments: str, *, repo: Path | None = None, code: int = 0) -> dict:
        return self.cli(
            "workflow_router.py", "--repo-root", repo or self.repo, "route",
            "--command", command, "--arguments", arguments, code=code,
        )

    def feature(self, repo: Path, status: str, scope: str = "single") -> Path:
        directory = repo / "docs/specs/active/demo"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "spec.md").write_text(
            f"---\nfeature: demo\nexecution_scope: {scope}\nstatus: {status}\n---\nSynthetic requirement.\n",
            encoding="utf-8",
        )
        (directory / "tasks.md").write_text("---\nfeature: demo\n---\nSynthetic tasks.\n", encoding="utf-8")
        return directory

    def reviewed_fixture(self) -> test_workflow.LiteWorkflowCliTest:
        fixture = test_workflow.LiteWorkflowCliTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.write_spec(status="implemented")
        fixture.write_tasks(status="completed", rows="| T-01 | Synthetic task | AC-01 | — | completed |")
        fixture.write_evidence(body="Synthetic test evidence; no model review was executed.\n")
        return fixture

    def test_nine_skill_entry_clis_reconstruct_exact_contracts_and_close_snapshots(self) -> None:
        preflight = subprocess.run(
            ["sh", str(PLUGIN / "scripts/runtime_preflight.sh"), "check"],
            cwd=self.repo, env=self.env, text=True, capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(preflight.returncode, 0, preflight.stderr)
        self.assertTrue(json.loads(preflight.stdout)["ok"])
        projection = json.loads((PLUGIN / "hosts/codex/command-projection.json").read_text())
        self.assertEqual(set(projection["entrypoints"]), COMMANDS)
        for command in sorted(COMMANDS):
            with self.subTest(command=command):
                skill = PLUGIN / f"hosts/codex/skills/{command}/SKILL.md"
                self.assertEqual((skill.parent / "../../../..").resolve(), PLUGIN)
                resources = re.findall(r"--resource ([a-z_]+)", skill.read_text())
                flags = [item for resource in resources for item in ("--resource", resource)]
                prepared = self.entry("--command", command, *flags)
                self.assertEqual(prepared["host_id"], "codex")
                self.assertLessEqual(len(prepared["packets"]), 2)
                hashes = {
                    resource["path"]: resource["source_sha256"]
                    for packet in prepared["packets"] for resource in packet["resources"]
                }
                reconstructed: dict[str, str] = {}
                for packet in prepared["packets"]:
                    for index in range(packet["page_count"]):
                        # Separate real processes exercise the persistent page protocol.
                        page = self.entry(
                            "--read-page", "--invocation-key", prepared["invocation_key"],
                            "--packet-round", packet["round"], "--page-index", index,
                        )
                        self.assertLess(len(json.dumps(page, ensure_ascii=False).encode()), 16 * 1024)
                        self.assertEqual(hashlib.sha256(page["content"].encode()).hexdigest(), page["page_sha256"])
                        reconstructed[page["path"]] = reconstructed.get(page["path"], "") + page["content"]
                self.assertEqual(set(hashes), set(reconstructed))
                self.assertEqual(
                    {name for name in reconstructed if name.startswith("commands/")},
                    {f"commands/{command}.md"},
                )
                for relative, content in reconstructed.items():
                    self.assertEqual(content, (PLUGIN / relative).read_text())
                    self.assertEqual(hashlib.sha256(content.encode()).hexdigest(), hashes[relative])
                if command == "ut":
                    self.assertIn("hosts/codex/ut.md", reconstructed)
                    self.assertIn("reference/codex-subagent-runtime-protocol.md", reconstructed)
                finished = self.entry("--finish", "--invocation-key", prepared["invocation_key"])
                self.assertEqual(finished["status"], "finished")
                closed = self.entry(
                    "--read-page", "--invocation-key", prepared["invocation_key"],
                    "--packet-round", 1, "--page-index", 0, code=2,
                )
                self.assertEqual(closed["status"], "blocked")
        self.assertEqual(self.files(self.repo), {})

    def test_entry_cli_blocks_skipped_pages_and_premature_finish(self) -> None:
        prepared = self.entry("--command", "ut", "--resource", "ut")
        self.assertGreater(prepared["packets"][0]["page_count"], 1)
        for flags in (
            ("--read-page", "--packet-round", 1, "--page-index", 1),
            ("--finish",),
        ):
            result = self.entry(*flags, "--invocation-key", prepared["invocation_key"], code=2)
            self.assertEqual(result["status"], "blocked")
        first = self.entry(
            "--read-page", "--invocation-key", prepared["invocation_key"],
            "--packet-round", 1, "--page-index", 0,
        )
        self.assertEqual(first["page_index"], 0)

    def test_session_source_conflict_cannot_be_replaced_by_an_explicit_entry_root(self) -> None:
        foreign = {**self.env, "HELIXHARNESS_BOUND_PLUGIN_ROOT": str(self.root / "different plugin")}
        before = self.files()
        rejected = self.entry("--command", "apply", "--expected-plugin-root", PLUGIN, env=foreign, code=2)
        self.assertEqual(rejected["reason"], "plugin_source_mismatch")
        routed = self.cli(
            "workflow_router.py", "--repo-root", self.repo, "route", "--command", "propose",
            "--arguments", "demo synthetic", env=foreign, code=2,
        )
        self.assertEqual(routed["reason"], "plugin_source_mismatch")
        self.assertEqual(before, self.files())

    def test_explicit_codex_binding_survives_conflicting_other_host_signals(self) -> None:
        env = {**self.env, "HELIXHARNESS_PLATFORM": "cursor", "CLAUDE_SESSION_ID": "synthetic-claude"}
        result = self.cli(
            "host_runtime_cli.py", "resolve", "--platform", "codex",
            "--expected-plugin-root", PLUGIN, env=env,
        )
        self.assertEqual(result["host_id"], "codex")
        self.assertEqual(result["resources"]["runtime"], "hosts/codex/runtime.md")
        self.assertEqual(result["plugin_root"], str(PLUGIN))

    def test_propose_cli_preserves_chinese_spaces_quotes_and_literal_flags(self) -> None:
        tokens = ["demo", '中文需求 "quoted" with spaces', "--lite", "--revise", "$(synthetic-no-execution)"]
        arguments = "demo --loop -- " + shlex.join(tokens[1:])
        before = self.files()
        result = self.route("propose", arguments)
        self.assertEqual(result["workflow"], "workflows/single/propose.md")
        self.assertTrue(result["loop_requested"])
        self.assertEqual(shlex.split(result["workflow_arguments"]), tokens)
        self.assertEqual(before, self.files())

    def test_single_and_workspace_loop_resume_first_unfinished_stage_without_writes(self) -> None:
        for scope in ("single", "workspace"):
            repo = self.repo / scope
            repo.mkdir()
            if scope == "workspace":
                (repo / "helixharness.workspace.yaml").write_text("repos: []\n")
            for state, stage in (("planning", "propose"), ("ready", "apply"), ("applying", "apply")):
                with self.subTest(scope=scope, state=state):
                    self.feature(repo, state, scope)
                    before = self.files(repo)
                    result = self.route("propose", "demo --loop", repo=repo)
                    self.assertEqual(result["workflow"], f"workflows/{scope}/{stage}.md")
                    self.assertEqual(before, self.files(repo))

    def test_outdated_command_recommends_current_stage_without_rewriting_artifacts(self) -> None:
        self.feature(self.repo, "applying")
        before = self.files()
        result = self.route("propose", 'demo "new wording without explicit revision"')
        self.assertEqual(result["action"], "recommend_command")
        self.assertEqual(result["recommendation"], "/helix:apply demo")
        self.assertEqual(before, self.files())

    def test_archived_identity_and_unsafe_feature_are_blocked(self) -> None:
        self.feature(self.repo, "ready")
        (self.repo / "docs/specs/completed/demo").mkdir(parents=True)
        before = self.files()
        archived = self.route("propose", "demo --loop", code=2)
        self.assertEqual(archived["reason"], "feature_already_archived")
        unsafe = self.route("apply", "../outside", code=2)
        self.assertEqual(unsafe["reason"], "feature_name_invalid")
        self.assertEqual(before, self.files())

    def test_archive_cli_requires_reviewed_scope_and_preserves_commit_choice(self) -> None:
        fixture = self.reviewed_fixture()
        baseline = fixture.repo / ".helixharness/workflow/checkout/apply-state.json"
        baseline_before = baseline.read_bytes()
        ready = self.route("propose", "checkout --loop", repo=fixture.repo)
        self.assertEqual(ready["stage"], "archive")
        self.cli("workflow.py", "--repo-root", fixture.repo, "archive-preflight", "--feature", "checkout")
        explicit = self.route("archive", "checkout --no-commit", repo=fixture.repo)
        self.assertEqual(shlex.split(explicit["workflow_arguments"]), ["checkout", "--no-commit"])
        # A changed business contract invalidates its sealed evidence and must
        # return to Apply. Code-only drift has a separate explicit review choice.
        spec = fixture.feature_dir / "spec.md"
        spec.write_text(spec.read_text() + "\nUnreviewed requirement change.\n")
        changed = self.route("propose", "checkout --loop", repo=fixture.repo)
        self.assertEqual(changed["stage"], "apply")
        rejected = fixture.run_cli("archive-preflight", "--feature", "checkout", check=False)
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(baseline.read_bytes(), baseline_before)

    def test_apply_binding_and_resume_keep_baseline_and_select_next_unfinished_task(self) -> None:
        fixture = test_workflow.LiteWorkflowCliTest()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.write_spec(status="ready")
        fixture.write_tasks(status="ready", rows=(
            "| T-01 | First | AC-01 | — | pending |\n"
            "| T-02 | Second | AC-02 | T-01 | pending |"
        ))
        notes = fixture.repo / "user-notes.txt"
        notes.write_text("Synthetic pre-existing user notes.\n")
        arguments = ("--repo-root", fixture.repo, "bind-apply", "--feature", "checkout")
        first = self.cli("workflow.py", *arguments)
        self.assertEqual(first, self.cli("workflow.py", *arguments))
        baseline = fixture.repo / ".helixharness/workflow/checkout/apply-state.json"
        baseline_before = baseline.read_bytes()
        spec = fixture.feature_dir / "spec.md"
        spec.write_text(spec.read_text().replace("status: ready", "status: applying"))
        tasks = fixture.feature_dir / "tasks.md"
        tasks.write_text(tasks.read_text().replace("status: ready", "status: applying").replace(
            "| T-01 | First | AC-01 | — | pending |", "| T-01 | First | AC-01 | — | completed |",
        ))
        output = fixture.repo / "synthetic-output.py"
        output.write_text("result = 1\n")
        resumed = self.route("propose", "checkout --loop", repo=fixture.repo)
        self.assertEqual(resumed["stage"], "apply")
        checked = self.cli(
            "workflow.py", "--repo-root", fixture.repo, "validate-tasks",
            "--feature", "checkout", "--stage", "apply",
        )
        self.assertEqual(checked["next_task"], "T-02")
        scope = self.cli("workflow.py", "--repo-root", fixture.repo, "review-scope", "--feature", "checkout")
        self.assertEqual(scope["changed_paths"], ["synthetic-output.py"])
        self.assertIn("user-notes.txt", scope["excluded_preexisting_paths"])
        self.assertEqual(baseline.read_bytes(), baseline_before)
        self.assertIn("| T-01 | First | AC-01 | — | completed |", tasks.read_text())
        self.assertEqual(notes.read_text(), "Synthetic pre-existing user notes.\n")

    def test_bug_fix_cli_preflight_keeps_existing_apply_baseline(self) -> None:
        fixture = self.reviewed_fixture()
        baseline = fixture.repo / ".helixharness/workflow/checkout/apply-state.json"
        before = baseline.read_bytes()
        result = self.route("bug-fix", 'checkout "修复 边界条件"', repo=fixture.repo)
        self.assertEqual(result["workflow"], "workflows/single/bug-fix.md")
        self.assertEqual(shlex.split(result["workflow_arguments"]), ["checkout", "修复 边界条件"])
        self.cli("workflow.py", "--repo-root", fixture.repo, "repair-preflight", "--feature", "checkout")
        self.assertEqual(baseline.read_bytes(), before)

    def test_init_service_frontend_hook_merge_is_idempotent_and_preserves_other_hosts(self) -> None:
        for mode in ("service", "frontend"):
            with self.subTest(mode=mode):
                repo = self.repo / mode
                repo.mkdir()
                for relative in validate_init_outputs.SERVICE_COMMON_DIRS:
                    (repo / relative).mkdir(parents=True, exist_ok=True)
                for relative in (*validate_init_outputs.SERVICE_COMMON_FILES, "AGENTS.md", "docs/knowledge/service-meta.yaml"):
                    (repo / relative).write_text("Synthetic initialized project; keep custom text.\n")
                entry = repo / "AGENTS.md"
                entry.write_text(entry.read_text() + "\n" + (PLUGIN / "templates/project-entry.md.tpl").read_text().replace(
                    "{{PROJECT_CONTEXT}}", "Synthetic project context."))
                self.assertEqual(self.cli("init_guidance.py", "--repo-root", repo, "--platform", "codex", "--mode", mode, "check")["status"], "pass")
                for relative in (".claude/settings.json", ".cursor/hooks.json"):
                    target = repo / relative
                    target.parent.mkdir()
                    target.write_text('{"synthetic_user_setting": true}\n')
                custom = {"custom": True, "hooks": {"PostToolUse": [{"matcher": "Read", "hooks": [{"type": "command", "command": "echo synthetic-custom"}]}]}}
                settings = repo / ".codex/hooks.json"
                settings.parent.mkdir()
                settings.write_text(json.dumps(custom))
                preserved = {path: path.read_bytes() for path in (repo / "AGENTS.md", repo / ".claude/settings.json", repo / ".cursor/hooks.json")}
                validation = ("--repo-root", repo, "--mode", mode, "--platform", "codex", "--telemetry-mode", "disabled")
                self.assertEqual(self.cli("validate_init_outputs.py", *validation, code=1)["status"], "fail")
                merged = self.cli("project_hooks_cli.py", "--repo-root", repo, "--platform", "codex", "merge")
                self.assertTrue(merged["changed"])
                after = self.files(repo)
                repeated = self.cli("project_hooks_cli.py", "--repo-root", repo, "--platform", "codex", "merge")
                self.assertFalse(repeated["changed"])
                self.assertEqual(after, self.files(repo))
                self.assertEqual(self.cli("validate_init_outputs.py", *validation)["status"], "pass")
                self.assertEqual(preserved, {path: path.read_bytes() for path in preserved})
                contents = json.loads(settings.read_text())
                self.assertTrue(contents["custom"])
                self.assertIn(custom["hooks"]["PostToolUse"][0], contents["hooks"]["PostToolUse"])

    def test_workspace_init_validation_does_not_create_root_or_child_hooks(self) -> None:
        for relative in validate_init_outputs.WORKSPACE_DIRS:
            (self.repo / relative).mkdir(parents=True, exist_ok=True)
        for relative in (*validate_init_outputs.WORKSPACE_FILES, "AGENTS.md"):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("Synthetic workspace.\n")
        (self.repo / "AGENTS.md").write_text((PLUGIN / "templates/workspace-entry.md.tpl").read_text())
        for name in ("api", "web"):
            (self.repo / name).mkdir()
        (self.repo / "helixharness.workspace.yaml").write_text("version: 1\nrepos:\n  api:\n    path: ./api\n  web:\n    path: ./web\n")
        before = self.files()
        result = self.cli(
            "validate_init_outputs.py", "--repo-root", self.repo, "--mode", "workspace",
            "--platform", "codex", "--telemetry-mode", "disabled",
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(before, self.files())
        self.assertFalse(any(self.repo.rglob(".codex")))

    def test_init_hook_merge_rejects_malformed_settings_without_overwriting(self) -> None:
        settings = self.repo / ".codex/hooks.json"
        settings.parent.mkdir()
        settings.write_text("{invalid synthetic configuration")
        before = self.files()
        result = self.cli("project_hooks_cli.py", "--repo-root", self.repo, "--platform", "codex", "merge", code=1)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(before, self.files())


if __name__ == "__main__":
    unittest.main()
