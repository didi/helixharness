#!/usr/bin/env python3

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath

import eval_json
import package_qoder_plugin as packager
import telemetry_async
from host_runtime import HostRegistry
from host_runtime.command_projection import WORKFLOW_COMMANDS, load_command_projection
from host_runtime.project_hooks import load_project_hooks


PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LOGICAL_ACTIONS = (
    "read_file",
    "edit_file",
    "run_command",
    "invoke_skill",
    "spawn_job",
    "wait_job",
    "ask_user",
    "update_plan",
    "stage_finish",
)


class QoderHostContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = HostRegistry.discover(PLUGIN_ROOT / "hosts").require("qoder")
        cls.host_dir = PLUGIN_ROOT / "hosts" / "qoder"

    def test_manifest_declares_preview_support_without_python_adapters(self) -> None:
        self.assertEqual(self.manifest.support_level, "preview")
        self.assertEqual(self.manifest.display_name, "Qoder")
        self.assertTrue(self.manifest.limitations)
        for name in sorted(self.manifest.adapters):
            reference = self.manifest.adapter_reference(name)
            self.assertEqual(
                reference.kind,
                "resource",
                f"qoder.{name} 必须是资源适配器；Python 适配器会把更新与遥测重新引入本宿主",
            )

    def test_unsupported_capabilities_have_no_host_specific_entrypoints(self) -> None:
        for capability in ("telemetry", "plugin_update", "stop_hook", "failure_hook"):
            self.assertEqual(self.manifest.capability(capability), "unsupported", capability)

    def test_measured_surfaces_match_the_manifest(self) -> None:
        # Qoder keeps plugin commands/ on disk but never expands /helix:<name>, while project
        # level .qoder/settings.json hooks load and run. The two capabilities record that.
        self.assertEqual(self.manifest.capability("command_surface"), "emulated")
        self.assertEqual(self.manifest.capability("project_hooks"), "native")

    def test_skill_surface_projects_thin_entries_onto_the_shared_contract(self) -> None:
        projection = load_command_projection(self.manifest)
        self.assertEqual(projection.surface, "skill")
        self.assertEqual(set(projection.entrypoints), set(WORKFLOW_COMMANDS))
        for command, (entry, contract) in projection.entrypoints.items():
            self.assertEqual(entry, f"hosts/qoder/skills/{command}/SKILL.md")
            self.assertEqual(contract, f"commands/{command}.md")
            self.assertTrue((PLUGIN_ROOT / entry).is_file(), entry)
            self.assertTrue((PLUGIN_ROOT / contract).is_file(), contract)

    def test_thin_entries_only_bind_the_host_kit_and_forward_arguments(self) -> None:
        for command in sorted(WORKFLOW_COMMANDS):
            text = (PLUGIN_ROOT / "hosts" / "qoder" / "skills" / command / "SKILL.md").read_text(
                encoding="utf-8"
            )
            header = text.split("---")[1]
            self.assertIn(f"name: {command}", header, command)
            self.assertIn("hosts/qoder/skill-entry.md", text, command)
            self.assertIn(f"commands/{command}.md", text, command)
            self.assertIn("$ARGUMENTS", text, command)

    def test_manifest_declares_the_skill_roots_it_ships(self) -> None:
        payload = json.loads(
            (PLUGIN_ROOT / ".qoder-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["name"], "helix")
        self.assertEqual(
            packager.declared_skill_roots(PLUGIN_ROOT),
            ("skills", "hosts/qoder/skills"),
        )
        self.assertEqual(payload["skills"], ["./skills", "./hosts/qoder/skills"])

    def test_project_hooks_target_the_qoder_settings_layer_only(self) -> None:
        hooks = load_project_hooks(self.manifest)
        self.assertEqual(hooks.owner, "parent")
        self.assertEqual(hooks.merge_strategy, "json_append_dedup")
        self.assertEqual(hooks.settings_file, ".qoder/settings.json")
        self.assertEqual(hooks.dedupe_key, "hooks.PostToolUse[].matcher=Write|Edit|NotebookEdit")
        payload = json.loads(
            (self.host_dir / "project-hooks.json").read_text(encoding="utf-8")
        )
        matchers = [item["matcher"] for item in payload["payload"]["hooks"]["PostToolUse"]]
        # Qoder 的 matcher 语义未文档化，因此把三个写入类工具名逐一列出而不是依赖子串匹配。
        self.assertEqual(matchers, ["Write|Edit|NotebookEdit"])

    def test_hook_surface_is_session_start_only(self) -> None:
        path = self.host_dir / "hooks.json"
        hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(list(hooks), ["SessionStart"])
        entries = hooks["SessionStart"][0]["hooks"]
        self.assertEqual(len(entries), 1)
        command = entries[0]["command"]
        self.assertIn("${QODER_PLUGIN_ROOT}/scripts/python_hook_bootstrap.sh", command)
        self.assertIn("${QODER_PLUGIN_ROOT}/hosts/qoder/hook_entry.py", command)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", command)

    def test_hook_entry_uses_two_level_plugin_root_resolution(self) -> None:
        source = (self.host_dir / "hook_entry.py").read_text(encoding="utf-8")
        compile(source, str(self.host_dir / "hook_entry.py"), "exec")
        self.assertIn("parents[2]", source)
        self.assertIn("from lifecycle_service import handle_event", source)
        self.assertIn('.get("qoder")', source)

    def test_runtime_maps_every_logical_action(self) -> None:
        runtime = (self.host_dir / "runtime.md").read_text(encoding="utf-8")
        for action in LOGICAL_ACTIONS:
            self.assertIn(f"`{action}`", runtime, action)
        self.assertIn("`TodoWrite`", runtime)
        self.assertIn("`TaskCreate`", runtime)

    def test_workflow_transition_is_inline_for_every_host_read_document(self) -> None:
        for name in ("workflow-transition.md", "ut.md", "subagent.md", "lifecycle.md"):
            text = (self.host_dir / name).read_text(encoding="utf-8")
            self.assertTrue(text.strip(), name)
            self.assertNotIn("../claude-code", text, name)
            self.assertNotIn("../codex/", text, name)
        transition = (self.host_dir / "workflow-transition.md").read_text(encoding="utf-8")
        self.assertIn("模式：`inline`", transition)

    def test_host_manifest_resources_all_exist(self) -> None:
        for name in sorted(self.manifest.adapters):
            resolved = self.manifest.resolve_resource_adapter(name)
            self.assertTrue(resolved.is_file(), name)

    def test_runtime_snapshot_covers_the_declared_distribution_manifest(self) -> None:
        # The telemetry runtime snapshot copies directories named by each Host Kit,
        # so a new distribution surface cannot be left out of an installed snapshot.
        distribution = self.manifest.resolve_resource_adapter("distribution")
        declared = telemetry_async._distribution_targets(PLUGIN_ROOT)
        self.assertIn(PurePosixPath(distribution.relative_to(PLUGIN_ROOT)), declared)
        self.assertIn(
            distribution.relative_to(PLUGIN_ROOT).parts[0],
            telemetry_async.runtime_resource_directories(PLUGIN_ROOT),
        )


class QoderPackagingTest(unittest.TestCase):
    def test_agent_name_injection_is_idempotent_and_validated(self) -> None:
        source = "---\ndescription: \"扫描接口。\"\n---\n\n# 正文\n"
        once = packager.agent_with_name(source, "scan-api")
        self.assertEqual(once, "---\nname: scan-api\ndescription: \"扫描接口。\"\n---\n\n# 正文\n")
        self.assertEqual(packager.agent_with_name(once, "scan-api"), once)
        with self.assertRaises(packager.PackagingError):
            packager.agent_with_name(once, "scan-db")

    def test_every_shared_agent_file_can_be_injected(self) -> None:
        for path in sorted((PLUGIN_ROOT / "agents").glob("*.md")):
            packaged = packager.agent_with_name(
                path.read_text(encoding="utf-8"), path.stem
            )
            header = packaged.split("---")[1]
            self.assertIn(f"name: {path.stem}", header, path.name)

    def test_packaging_allowlist_covers_only_the_qoder_surface(self) -> None:
        for relative in (
            PurePosixPath(".qoder-plugin/plugin.json"),
            PurePosixPath("agents/scan-api.md"),
            PurePosixPath("commands/init.md"),
            PurePosixPath("hosts/qoder/host.json"),
            PurePosixPath("hosts/qoder/skills/propose/SKILL.md"),
            PurePosixPath("hosts/shared/telemetry.md"),
            PurePosixPath("hosts/host-manifest-v1.schema.json"),
            PurePosixPath("reference/tool-semantics.md"),
            PurePosixPath("scripts/package_qoder_plugin.py"),
            PurePosixPath("skills/brainstorming/SKILL.md"),
            PurePosixPath("templates/harness/api.md.tpl"),
            PurePosixPath("workflows/single/apply.md"),
            PurePosixPath("LICENSE"),
            PurePosixPath("README.md"),
        ):
            self.assertTrue(packager.is_packaged(relative), str(relative))
        for relative in (
            PurePosixPath("AGENTS.md"),
            PurePosixPath("CLAUDE.md"),
            PurePosixPath(".gitignore"),
            PurePosixPath("package.json"),
            PurePosixPath("tsconfig.json"),
            PurePosixPath(".cursor-plugin/marketplace.json"),
            PurePosixPath("hosts/cursor/host.json"),
            PurePosixPath("hosts/opencode/host.json"),
            PurePosixPath("hosts/claude-code/host.json"),
            PurePosixPath("hosts/codex/host.json"),
            PurePosixPath("opencode/project-checks.ts"),
            PurePosixPath("hooks/hooks.json"),
            PurePosixPath("dist/helix-1.1.0.zip"),
            PurePosixPath("scripts/__pycache__/x.pyc"),
        ):
            self.assertFalse(packager.is_packaged(relative), str(relative))

    def _minimal_plugin_root(self, root: Path) -> None:
        (root / ".qoder-plugin").mkdir(parents=True)
        (root / ".qoder-plugin" / "plugin.json").write_text(
            '{"name":"helix","version":"0.0.1"}', encoding="utf-8"
        )
        (root / "hosts" / "qoder").mkdir(parents=True)
        (root / "hosts" / "qoder" / "hooks.json").write_text(
            '{"hooks":{}}', encoding="utf-8"
        )
        (root / "reference").mkdir()

    def test_collect_entries_rejects_symlink_escaping_the_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            root.mkdir()
            self._minimal_plugin_root(root)
            secret = Path(directory) / "secret.txt"
            secret.write_text("private", encoding="utf-8")
            try:
                (root / "reference" / "leak.md").symlink_to(secret)
            except OSError as exc:
                self.skipTest(f"symlink unsupported: {exc}")
            with self.assertRaises(packager.PackagingError):
                packager.collect_entries(root)

    def test_collect_entries_rejects_untracked_files_in_a_git_checkout(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._minimal_plugin_root(root)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            (root / "reference" / "local-note.md").write_text("local only", encoding="utf-8")
            with self.assertRaisesRegex(packager.PackagingError, "local-note.md"):
                packager.collect_entries(root)

    def test_collect_entries_without_git_still_applies_the_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._minimal_plugin_root(root)
            if packager._git_untracked(root) is not None:
                self.skipTest("temporary directory is inside a git checkout")
            (root / "AGENTS.md").write_text("repo instructions", encoding="utf-8")
            entries = packager.collect_entries(root)
            self.assertIn(PurePosixPath(".qoder-plugin/plugin.json"), entries)
            self.assertNotIn(PurePosixPath("AGENTS.md"), entries)

    def test_other_host_surfaces_are_excluded(self) -> None:
        for relative in (
            PurePosixPath(".claude-plugin/marketplace.json"),
            PurePosixPath(".codex-plugin/plugin.json"),
            PurePosixPath("SKILL.md"),
            PurePosixPath("hosts/codex/host.json"),
            PurePosixPath("hosts/claude-code/host.json"),
            PurePosixPath("scripts/__pycache__/x.pyc"),
            PurePosixPath("dist/helix-1.1.0.zip"),
        ):
            self.assertTrue(packager.is_excluded(relative), str(relative))
        for relative in (
            PurePosixPath("commands/init.md"),
            PurePosixPath("hosts/qoder/host.json"),
            PurePosixPath("hosts/qoder/skills/propose/SKILL.md"),
            PurePosixPath(".qoder-plugin/plugin.json"),
            PurePosixPath("reference/tool-semantics.md"),
        ):
            self.assertFalse(packager.is_excluded(relative), str(relative))

    def test_declared_skill_roots_accept_both_manifest_shapes(self) -> None:
        self.assertEqual(
            packager.declared_skill_roots(PLUGIN_ROOT),
            ("skills", "hosts/qoder/skills"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".qoder-plugin").mkdir()
            for skills, expected in (
                ('"./skills/"', ("skills",)),
                ('["./skills", "skills"]', ("skills",)),
                ('"./hosts/qoder/skills"', ("hosts/qoder/skills",)),
            ):
                (root / ".qoder-plugin" / "plugin.json").write_text(
                    f'{{"name":"helix","version":"1.0.0","skills":{skills}}}',
                    encoding="utf-8",
                )
                self.assertEqual(packager.declared_skill_roots(root), expected, skills)

    def test_built_archive_is_loadable_and_self_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = packager.write_archive(
                PLUGIN_ROOT, Path(directory)
            )
            summary = packager.verify_archive(PLUGIN_ROOT, archive)
            self.assertEqual(summary["agents"], len(list((PLUGIN_ROOT / "agents").glob("*.md"))))
            with zipfile.ZipFile(archive) as bundle:
                names = {PurePosixPath(item.filename) for item in bundle.infolist()}
                self.assertIn(PurePosixPath("hooks/hooks.json"), names)
                self.assertNotIn(PurePosixPath("SKILL.md"), names)
                for relative in names:
                    self.assertTrue(
                        relative in packager.GENERATED_ARCHIVE_PATHS
                        or packager.is_packaged(relative),
                        f"ZIP 混入非 Qoder 表面文件: {relative}",
                    )
                for command in sorted(WORKFLOW_COMMANDS):
                    self.assertIn(
                        PurePosixPath(f"hosts/qoder/skills/{command}/SKILL.md"), names, command
                    )
                    self.assertIn(PurePosixPath(f"commands/{command}.md"), names, command)
                root_hooks = json.loads(
                    bundle.read("hooks/hooks.json").decode("utf-8")
                )
                host_hooks = json.loads(
                    (PLUGIN_ROOT / "hosts" / "qoder" / "hooks.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(root_hooks, host_hooks)
                payload = eval_json.loads(
                    bundle.read(".qoder-plugin/plugin.json").decode("utf-8")
                )
                self.assertEqual(payload["name"], "helix")
                self.assertTrue(payload["version"])
                self.assertIn("./hosts/qoder/skills", payload["skills"])


if __name__ == "__main__":
    unittest.main()
