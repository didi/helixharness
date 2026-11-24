"""Synthetic checks for host-labelled Init navigation; no model execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import validate_init_outputs


PLUGIN = Path(__file__).resolve().parents[2]
SCRIPT = PLUGIN / "scripts/init_guidance.py"
START = "<!-- helix:commands:start -->"
END = "<!-- helix:commands:end -->"
COMMANDS = ("init", "propose", "apply", "archive", "ut", "bug-fix", "update", "status", "config")
LEGACY_SINGLE = (
    "## 常用命令\n\n"
    "`/helix:init`、`/helix:propose`、`/helix:apply`、`/helix:archive`、\n"
    "`/helix:ut`、`/helix:bug-fix`、`/helix:update`。\n"
    "`propose --loop` 按顺序执行 Propose → Apply → Archive。\n"
)
LEGACY_WORKSPACE = (
    "## 常用命令\n\n"
    "`/helix:init --workspace` 初始化根目录。`/helix:propose <feature> <requirement-or-path>`\n"
    "建立需求；`--loop` 串联 Propose → Apply → Archive。`apply` 与 `archive` 按 Router 的 Workspace\n"
    "范围调度子仓；其他命令的适用范围仍以各自 Router 和完整命令为准。\n"
)


class InitGuidanceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="helix-init-guidance-")
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)

    def cli(self, platform="cursor", mode="service", action="merge", code=0):
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--repo-root", str(self.repo),
             "--platform", platform, "--mode", mode, action],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def template(self, mode):
        name = "workspace-entry.md.tpl" if mode == "workspace" else "project-entry.md.tpl"
        return (PLUGIN / "templates" / name).read_text().replace("{{PROJECT_CONTEXT}}", "Synthetic project context.")

    def test_shared_templates_label_both_entry_styles_and_preserve_parameters(self):
        for mode in ("service", "workspace"):
            with self.subTest(mode=mode):
                source = self.template(mode)
                self.assertEqual(source.count(START), 1)
                self.assertEqual(source.count(END), 1)
                block = source.split(START)[1].split(END)[0]
                self.assertIn("Cursor", block)
                self.assertIn("Claude Code / Codex", block)
                commands = COMMANDS if mode == "service" else ("init", "propose", "apply", "archive")
                for command in commands:
                    self.assertIn(f"/helix-{command}", block)
                    self.assertIn(f"/helix:{command}", block)
                self.assertIn("--loop", block)
                if mode == "workspace":
                    self.assertIn("--workspace", block)
                    self.assertIn("<feature> <requirement-or-path>", block)
                    self.assertIn("Workspace 根不支持 BugFix", block)

    def test_all_hosts_and_modes_generate_check_and_repeat_without_rewriting(self):
        for platform in ("cursor", "codex", "claude-code"):
            for mode in ("service", "frontend", "fullstack", "greenfield", "workspace"):
                with self.subTest(platform=platform, mode=mode):
                    entry = self.repo / ("CLAUDE.md" if platform == "claude-code" else "AGENTS.md")
                    entry.write_text(self.template(mode))
                    initial = entry.read_bytes()
                    self.assertFalse(self.cli(platform, mode)["changed"])
                    self.assertEqual(self.cli(platform, mode, "check")["status"], "pass")
                    self.assertEqual(entry.read_bytes(), initial)
                    modified = entry.stat().st_mtime_ns
                    self.assertFalse(self.cli(platform, mode)["changed"])
                    self.assertEqual(entry.stat().st_mtime_ns, modified)

    def test_both_host_orders_preserve_custom_bytes_and_identical_guidance(self):
        original = ('# 用户规则\r\n保留 `/helix:propose "需求 空格.md" --loop` 原文。\r\n'
                    '```text\r\n## 常用命令\r\n这是引用示例，不是导航区。\r\n```\r\n').encode()
        final = []
        for order in (("cursor", "codex"), ("codex", "cursor")):
            entry = self.repo / "AGENTS.md"
            entry.write_bytes(original)
            self.assertTrue(self.cli(order[0])["changed"])
            before = entry.read_bytes()
            self.assertTrue(before.startswith(original))
            self.assertFalse(self.cli(order[1])["changed"])
            self.assertEqual(before, entry.read_bytes())
            final.append(before)
        self.assertEqual(*final)

    def test_claude_entry_is_separate_and_other_hosts_are_untouched(self):
        agents = self.repo / "AGENTS.md"
        agents.write_bytes(b"# Existing Codex and Cursor rules\n")
        self.assertTrue(self.cli("claude-code")["changed"])
        self.assertEqual(agents.read_bytes(), b"# Existing Codex and Cursor rules\n")
        self.assertEqual(self.cli("claude-code", action="check")["status"], "pass")
        self.assertEqual({p.name for p in self.repo.iterdir()}, {"AGENTS.md", "CLAUDE.md"})

    def test_exact_legacy_blocks_migrate_only_the_owned_section(self):
        for mode, legacy, title in (("service", LEGACY_SINGLE, "HelixHarness 导航"),
                                    ("workspace", LEGACY_WORKSPACE, "HelixHarness Workspace")):
            with self.subTest(mode=mode):
                prefix = f"# 用户说明\n命令引号与路径不能变：`/helix:ut --packages 'a b'`。\n\n# {title}\n\n"
                suffix = "\n## Agent 工作规则\n用户自行扩展的约束。\n"
                entry = self.repo / "AGENTS.md"
                entry.write_text(prefix + legacy + suffix)
                self.assertTrue(self.cli(mode=mode)["changed"])
                actual = entry.read_text()
                self.assertTrue(actual.startswith(prefix))
                self.assertTrue(actual.endswith(suffix))
                self.assertNotIn(legacy, actual)
                self.assertEqual(self.cli(mode=mode, action="check")["status"], "pass")

    def test_custom_or_ambiguous_navigation_is_never_rewritten(self):
        cases = (
            "# HelixHarness 导航\n\n## 常用命令\n`/helix:propose` 自定义调用规则。\n",
            "## 常用命令（项目自定义）\n保留用户命令。\n",
            "# HelixHarness 导航\n\n" + LEGACY_SINGLE + "新增用户说明，不能作为默认区迁移。\n",
            LEGACY_SINGLE,  # A matching section alone does not prove Helix ownership.
            START + "\nmissing end\n",
            END + "\nreversed\n" + START,
            START + "\nold\n" + END + "\n" + START + "\nold\n" + END,
            "<!-- helix:commands:start\nbroken marker\n",
            "```markdown\n" + START + "\nexample\n" + END + "\n```\n",
            START + "\nold\n" + END + "\n\n## 常用命令\ncustom conflict\n",
        )
        entry = self.repo / "AGENTS.md"
        for content in cases:
            with self.subTest(content=content):
                entry.write_text(content)
                original = entry.read_bytes()
                self.cli(code=1)
                self.assertEqual(entry.read_bytes(), original)
                self.assertEqual({p.name for p in self.repo.iterdir()}, {"AGENTS.md"})

    def test_managed_old_block_can_be_refreshed_without_replacing_user_body(self):
        entry = self.repo / "AGENTS.md"
        entry.write_text("prefix\n" + START + "\n## 常用命令\nold generated block\n" + END + "\nsuffix\n")
        self.cli(action="check", code=1)
        self.assertTrue(self.cli()["changed"])
        self.assertTrue(entry.read_text().startswith("prefix\n"))
        self.assertTrue(entry.read_text().endswith("\nsuffix\n"))
        self.assertEqual(self.cli(action="check")["status"], "pass")

    def test_unknown_host_invalid_mode_and_symlink_do_not_write(self):
        import init_guidance
        before = sorted(self.repo.iterdir())
        self.cli("unknown", code=1)
        with self.assertRaisesRegex(ValueError, "unsupported_init_mode"):
            init_guidance.project_guidance(self.repo, "cursor", "unknown", "merge")
        self.assertEqual(sorted(self.repo.iterdir()), before)
        other = self.repo / "user-rules.md"
        other.write_text("preserve\n")
        (self.repo / "AGENTS.md").symlink_to(other)
        self.cli(code=1)
        self.assertEqual(other.read_text(), "preserve\n")
        self.assertTrue((self.repo / "AGENTS.md").is_symlink())

    def test_check_is_read_only_and_refuses_an_entry_without_guidance(self):
        self.cli(action="check", code=1)
        self.assertEqual(list(self.repo.iterdir()), [])
        entry = self.repo / "AGENTS.md"
        entry.write_text("arbitrary nonempty text\n")
        before = entry.read_bytes()
        self.cli(action="check", code=1)
        self.assertEqual(entry.read_bytes(), before)

    def test_workspace_merge_does_not_modify_children_hooks_or_registry(self):
        for name in ("api/AGENTS.md", "web/CLAUDE.md", "api/.cursor/hooks.json", "helixharness.workspace.yaml"):
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic existing content\n")
        before = {p.relative_to(self.repo): p.read_bytes() for p in self.repo.rglob("*") if p.is_file()}
        self.cli(mode="workspace")
        self.assertEqual(before, {p: (self.repo / p).read_bytes() for p in before})
        self.assertFalse((self.repo / ".cursor").exists())

    def test_ci_validator_detects_nonempty_but_incorrect_navigation(self):
        for relative in validate_init_outputs.WORKSPACE_DIRS:
            (self.repo / relative).mkdir(parents=True, exist_ok=True)
        for relative in validate_init_outputs.WORKSPACE_FILES:
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("synthetic\n")
        (self.repo / "api").mkdir()
        (self.repo / "helixharness.workspace.yaml").write_text("version: 1\nrepos:\n  api:\n    path: ./api\n")
        entry = self.repo / "AGENTS.md"
        entry.write_text("arbitrary nonempty entry\n")
        report = validate_init_outputs.validate(self.repo, "workspace", "cursor", "disabled")
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any(e["check"] == "init_guidance" for e in report["errors"]))
        entry.write_text(self.template("workspace"))
        before = entry.read_bytes()
        report = validate_init_outputs.validate(self.repo, "workspace", "cursor", "disabled")
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(entry.read_bytes(), before)
        entry.write_text(entry.read_text().replace("/helix-propose", "/helix:propose"))
        report = validate_init_outputs.validate(self.repo, "workspace", "cursor", "disabled")
        self.assertEqual(report["status"], "fail")

    def test_atomic_write_failure_preserves_entry_and_removes_temporary_file(self):
        import init_guidance
        entry = self.repo / "AGENTS.md"
        entry.write_bytes(b"preserve user content\n")
        with patch.object(init_guidance.os, "replace", side_effect=OSError("synthetic replace failure")):
            with self.assertRaises(OSError):
                init_guidance.project_guidance(self.repo, "cursor", "service", "merge")
        self.assertEqual(entry.read_bytes(), b"preserve user content\n")
        self.assertEqual({p.name for p in self.repo.iterdir()}, {"AGENTS.md"})

    def test_concurrent_edit_is_retained_instead_of_being_replaced(self):
        import init_guidance
        entry = self.repo / "AGENTS.md"
        entry.write_bytes(b"original user content\n")
        original_fsync = init_guidance.os.fsync
        def edit_during_preparation(fd):
            original_fsync(fd)
            entry.write_bytes(b"newer user content\n")
        with patch.object(init_guidance.os, "fsync", side_effect=edit_during_preparation):
            with self.assertRaisesRegex(ValueError, "init_guidance_entry_changed"):
                init_guidance.project_guidance(self.repo, "cursor", "service", "merge")
        self.assertEqual(entry.read_bytes(), b"newer user content\n")
        self.assertEqual({p.name for p in self.repo.iterdir()}, {"AGENTS.md"})

    def test_crlf_entry_is_left_untouched_and_stays_idempotent(self):
        # Windows editors save AGENTS.md with CRLF. check_text tolerates that, so a
        # merge must not answer "changed" by rewriting the block back to LF.
        for platform in ("cursor", "codex", "claude-code"):
            with self.subTest(platform=platform):
                entry = self.repo / ("CLAUDE.md" if platform == "claude-code" else "AGENTS.md")
                entry.write_bytes(self.template("service").replace("\n", "\r\n").encode())
                before = entry.read_bytes()
                self.assertFalse(self.cli(platform, "service")["changed"])
                self.assertEqual(entry.read_bytes(), before)
                self.assertEqual(self.cli(platform, "service", "check")["status"], "pass")
                entry.write_bytes("# 用户规则\r\n\r\n正文\r\n".encode())
                self.assertTrue(self.cli(platform, "service")["changed"])
                merged = entry.read_bytes()
                self.assertEqual(merged.count(b"\n"), merged.count(b"\r\n"))
                self.assertFalse(self.cli(platform, "service")["changed"])

    def test_entry_permissions_and_render_without_filesystem_mutation(self):
        entry = self.repo / "AGENTS.md"
        entry.write_text("user rules\n")
        entry.chmod(0o640)
        rendered = self.cli(action="render")
        self.assertIn("/helix-bug-fix", rendered["block"])
        self.assertEqual(entry.read_text(), "user rules\n")
        self.cli()
        if os.name != "nt":
            # Windows records only the read-only bit, so a preserved mode is unprovable there.
            self.assertEqual(entry.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
