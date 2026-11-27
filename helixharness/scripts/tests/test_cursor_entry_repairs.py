"""Regressions for native startup ordering and complete multiline command input.

The shell gate and Router are executed. Template checks protect conditional
examples; they do not claim that an LLM has generated the correct knowledge.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from host_runtime import HostRegistry
from lifecycle_service import startup_context_text

ROOT = Path(__file__).resolve().parents[2]


class StartupPreflightGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cursor-entry-repair-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.plugin = self.base / "plugin 'quoted' path"
        (self.plugin / "scripts").mkdir(parents=True)
        for name in ("runtime_preflight.sh", "python_runtime.sh"):
            shutil.copyfile(ROOT / "scripts" / name, self.plugin / "scripts" / name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.marker = self.base / "resolver-called"
        self.shell, dirname = shutil.which("sh"), shutil.which("dirname")
        # Linking sh and dirname into an otherwise empty bin directory is how
        # this fixture isolates PATH. Windows has no such isolated runtime: MSYS
        # only starts from its own directory, which also ships a real git and
        # python3 that these cases remove on purpose.
        self.posix_ready = os.name != "nt" and bool(self.shell and dirname)
        if self.posix_ready:
            (self.bin / "sh").symlink_to(self.shell)
            (self.bin / "dirname").symlink_to(dirname)
        self.env = {"PATH": str(self.bin), "SYNTHETIC_RESOLVER_CALL": str(self.marker)}
        self.registry = HostRegistry.discover(ROOT / "hosts")

    def executable(self, name: str, body: str):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def runtimes(self, python: str | None = "3.12.0", git: bool = True):
        if python is not None:
            self.executable("python3", (
                'if [ "${1:-}" = -c ]; then\n'
                f"  printf '%s\\n' {shlex.quote(python)}\n"
                'else\n'
                '  printf "%s\\n" "$@" >> "$SYNTHETIC_RESOLVER_CALL"\n'
                '  printf \'%s\\n\' \'{"status":"resolved"}\'\n'
                'fi\n'
            ))
        if git:
            self.executable("git", "exit 0\n")

    def command(self, host: str) -> str:
        manifest = replace(self.registry.require(host), root=self.plugin / "hosts" / host)
        context = startup_context_text(manifest, command_shell="bash" if host == "claude-code" else None)
        commands = [line for line in context.splitlines() if line.startswith("sh ")]
        self.assertEqual(1, len(commands))
        return commands[0]

    def require_posix_shell(self) -> None:
        if not self.posix_ready:
            self.skipTest("this case runs the emitted sh preflight line under a sandboxed PATH")

    def run_command(self, host: str):
        self.require_posix_shell()
        return subprocess.run(
            [self.shell, "-c", self.command(host)], env=self.env,
            text=True, capture_output=True, timeout=10,
        )

    def assert_blocked_all_hosts(self, python_status: str, git_status: str):
        for host in ("cursor", "claude-code", "codex"):
            with self.subTest(host=host):
                result = self.run_command(host)
                self.assertEqual(2, result.returncode, result.stderr)
                receipt = json.loads(result.stdout)
                self.assertFalse(receipt["ok"])
                self.assertEqual(python_status, receipt["python"]["status"])
                self.assertEqual(git_status, receipt["git"]["status"])
                self.assertFalse(self.marker.exists(), "Resolver ran despite failed shell preflight")
                self.assertEqual("", result.stderr)

    def test_ready_gate_resolves_all_hosts_and_preserves_quoted_install_root(self):
        self.runtimes()
        for host in ("cursor", "claude-code", "codex"):
            with self.subTest(host=host):
                result = self.run_command(host)
                self.assertEqual(0, result.returncode, result.stderr)
                check, resolved = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertTrue(check["ok"])
                self.assertEqual("resolved", resolved["status"])
                self.assertEqual([
                    "-X", "utf8", str(self.plugin / "scripts/host_runtime_cli.py"), "resolve", "--platform", host,
                    "--expected-plugin-root", str(self.plugin),
                ], self.marker.read_text().splitlines())
                self.marker.unlink()

    def test_missing_python_stops_before_resolver_on_all_hosts(self):
        self.runtimes(python=None)
        self.assert_blocked_all_hosts("missing", "ready")

    def test_unsupported_python_stops_before_resolver_on_all_hosts(self):
        self.runtimes(python="3.8.20")
        self.assert_blocked_all_hosts("unsupported_version", "ready")

    def test_unusable_python_stops_before_resolver_on_all_hosts(self):
        self.runtimes(python="not-a-version")
        self.assert_blocked_all_hosts("unusable", "ready")

    def test_missing_git_stops_before_resolver_even_when_python_ready(self):
        self.runtimes(git=False)
        self.assert_blocked_all_hosts("ready", "missing")

    def test_windows_claude_bash_gate_blocks_failures_and_accepts_crlf(self):
        # Synthetic Windows selection executed by a real POSIX shell. Only the
        # generated Windows command text is asserted natively below; executing
        # it under Git Bash is not covered anywhere, and this case skips there.
        with patch("lifecycle_service.os", SimpleNamespace(name="nt")):
            for version, git, code in (("3.12.0\r", True, 0), ("3.9.6\r", True, 2),
                                       ("broken\r", True, 2), (None, True, 2),
                                       ("3.12.0\r", False, 2)):
                with self.subTest(version=version, git=git):
                    for path in (self.bin / "python3", self.bin / "git", self.marker):
                        path.unlink(missing_ok=True)
                    self.runtimes(python=version, git=git)
                    result = self.run_command("claude-code")
                    self.assertEqual(code, result.returncode, result.stderr)
                    self.assertEqual(code == 0, self.marker.exists())

    def test_check_keeps_diagnostic_exit_contract_while_require_gates(self):
        self.require_posix_shell()
        self.runtimes(python=None, git=False)
        outputs = []
        for action, exit_code in (("check", 0), ("require", 2)):
            result = subprocess.run(
                [self.shell, str(self.plugin / "scripts/runtime_preflight.sh"), action],
                env=self.env, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(exit_code, result.returncode)
            outputs.append(json.loads(result.stdout))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual("install_git", outputs[0]["next_action"])

    def test_native_windows_binding_gates_before_using_the_selected_launcher(self):
        manifest = replace(self.registry.require("codex"), root=self.plugin / "hosts/codex")
        with patch("lifecycle_service.os.name", "nt"):
            context = startup_context_text(manifest)
        self.assertIn("runtime_preflight.ps1' check", context)
        self.assertIn("if (-not ($helixPreflight | ConvertFrom-Json).ok) { exit 2 }", context)
        self.assertIn("runtime_preflight.ps1' exec", context)
        self.assertIn("plugin ''quoted'' path", context)
        self.assertNotIn("python3 ", context)
        self.assertNotIn("require &&", context)

    def test_ready_guard_runs_real_resolver_with_each_original_host_contract(self):
        self.require_posix_shell()
        for host in ("cursor", "claude-code", "codex"):
            with self.subTest(host=host):
                context = startup_context_text(self.registry.require(host))
                command = next(line for line in context.splitlines() if line.startswith("sh "))
                env = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
                result = subprocess.run([self.shell, "-c", command], env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)
                decoder = json.JSONDecoder()
                check, offset = decoder.raw_decode(result.stdout)
                resolved = json.loads(result.stdout[offset:].lstrip())
                self.assertTrue(check["ok"])
                self.assertEqual("resolved", resolved["status"])
                self.assertEqual(host, resolved["host_id"])
                self.assertEqual(str(ROOT), resolved["plugin_root"])


class CursorMultilineInputTests(unittest.TestCase):
    def test_one_router_process_keeps_entire_message_and_does_not_execute_shell_text(self):
        with tempfile.TemporaryDirectory(prefix="cursor-router-input-") as directory:
            repo = Path(directory)
            # One message: first-line name/flag plus subsequent requirement body.
            # Shell-looking text is deliberately literal and must not execute.
            argument = (
                'greeting-input --loop\n'
                'Add sample.greeting.greet(name): preserve Unicode and internal spaces.\n'
                'Reject non-str with TypeError; keep "Hello, NAME!" exact.\n'
                '-- "literal --loop $(touch should-not-exist) `touch also-not-exist`"'
            )
            command = shlex.join([
                sys.executable, str(ROOT / "scripts/workflow_router.py"), "--repo-root", str(repo),
                "route", "--command", "propose", "--arguments=" + argument, "--arguments-mode", "auto",
            ])
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            shell = shutil.which("sh")
            if shell is None:
                self.skipTest("argv fidelity is only proved through a real shell")
            result = subprocess.run([shell, "-c", command], cwd=repo, env=env,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            routed = json.loads(result.stdout)
            self.assertEqual("load_workflow", routed["action"])
            self.assertEqual("workflows/single/propose.md", routed["workflow"])
            self.assertTrue(routed["loop_requested"])
            self.assertEqual([
                'greeting-input', 'Add', 'sample.greeting.greet(name):', 'preserve', 'Unicode', 'and',
                'internal', 'spaces.', 'Reject', 'non-str', 'with', 'TypeError;', 'keep', 'Hello, NAME!',
                'exact.', 'literal --loop $(touch should-not-exist) `touch also-not-exist`',
            ], shlex.split(routed["workflow_arguments"]))
            self.assertEqual([], list(repo.iterdir()))


class EmptyDatabaseTemplateTests(unittest.TestCase):
    def no_table_blocks(self):
        text = (ROOT / "agents/scan-db.md").read_text()
        section = text.split("#### 无表情形（database/index.md）", 1)[1].split("### er.md 格式", 1)[0]
        return section, re.findall(r"```markdown\n(.*?)\n```", section, re.S)

    def test_no_database_default_template_does_not_invent_references_or_downstream(self):
        _, blocks = self.no_table_blocks()
        self.assertEqual(2, len(blocks))
        document = blocks[0]
        self.assertIn("数据表总数：0 张", document)
        self.assertIn("本服务不直接持有数据表", document)
        self.assertNotIn("附录", document)
        self.assertNotIn("下游", document)
        self.assertNotIn("文本引用", document)

    def test_text_only_references_have_separate_cited_optional_appendix(self):
        section, blocks = self.no_table_blocks()
        self.assertIn("没有表名引用时到此结束，不添加附录", section)
        appendix = blocks[1]
        self.assertIn("非本服务表", appendix)
        self.assertIn("来源文件:行号", appendix)
        self.assertIn("待确认", appendix)
        self.assertNotIn("权威 schema 需查阅下游", appendix)
        self.assertIn("仍不得作为本服务表或 ER 实体", section)


if __name__ == "__main__":
    unittest.main()
