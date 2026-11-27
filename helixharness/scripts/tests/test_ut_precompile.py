"""UT precompile freshness guards, using isolated Java sources and real wrappers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import ut_precompile
except ModuleNotFoundError:
    ut_precompile = None

PLUGIN_ROOT = Path(__file__).resolve().parents[2]


@unittest.skipIf(os.name == "nt", "Synthetic executable wrappers use POSIX shebangs")
class UtPrecompileTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ut_precompile, "The UT precompile gate is required")
        self.javac = shutil.which("javac")
        if self.javac is None:
            self.skipTest("Java compiler is required for real-source compilation regressions")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name).resolve() / "synthetic-project"
        self.repo.mkdir()
        self.source = self.repo / "src/main/java/Value.java"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("public class Value { public static int value() { return 1; } }\n")
        self.test_file = self.repo / "src/test/java/ExistingTest.java"
        self.test_file.parent.mkdir(parents=True)
        self.test_file.write_text("// Synthetic existing test; preserve these exact bytes.\n")

    def project(self, tool, *, nested=False, language="java"):
        build_file = self.repo / ("build.gradle" if tool == "gradle" else "pom.xml")
        build_file.write_text("// synthetic build marker\n" if tool == "gradle" else "<project/>\n")
        output = self.repo / ("module" if nested else "") / (
            f"build/classes/{language}/main" if tool == "gradle" else "target/classes")
        wrapper = self.repo / ("gradlew" if tool == "gradle" else "mvnw")
        wrapper.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\nimport json, subprocess, sys\n"
            "root = Path.cwd()\n"
            "with (root / 'build-invocations.jsonl').open('a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"output = Path({str(output)!r})\n"
            "output.mkdir(parents=True, exist_ok=True)\n"
            "sources = sorted(str(p) for p in (root / 'src/main/java').rglob('*.java'))\n"
            f"result = subprocess.run([{self.javac!r}, '-d', str(output), *sources])\n"
            "raise SystemExit(result.returncode)\n"
        )
        wrapper.chmod(0o755)
        return wrapper, output

    def test_changed_source_and_new_package_compile_again_in_both_build_tools(self):
        for tool in ("gradle", "maven"):
            with self.subTest(tool=tool):
                for marker in ("build.gradle", "pom.xml"):
                    (self.repo / marker).unlink(missing_ok=True)
                wrapper, classes = self.project(tool)
                self.assertTrue(ut_precompile.precompile(self.repo)["ready"])
                old_class = (classes / "Value.class").read_bytes()
                self.source.write_text("public class Value { public static int value() { return 2; } }\n")
                added = self.source.parent / "newpackage/Added.java"
                added.parent.mkdir(exist_ok=True)
                added.write_text("package newpackage; public class Added {}\n")
                before = {p: p.read_bytes() for p in (self.source, added, self.test_file)}

                result = ut_precompile.precompile(self.repo)

                self.assertEqual("compile_ready", result["status"])
                self.assertTrue(result["ready"])
                self.assertFalse(result["tests_run"])
                self.assertEqual(str(wrapper), result["command"][0])
                self.assertIn("classes" if tool == "gradle" else "compile", result["command"])
                self.assertNotIn("clean", result["command"])
                self.assertNotEqual(old_class, (classes / "Value.class").read_bytes())
                self.assertTrue((classes / "newpackage/Added.class").is_file())
                self.assertTrue(all(p.read_bytes() == content for p, content in before.items()))
                self.source.write_text("public class Value { public static int value() { return 1; } }\n")

    def test_failed_compile_is_not_ready_even_when_old_class_directory_exists(self):
        for tool in ("gradle", "maven"):
            with self.subTest(tool=tool):
                for marker in ("build.gradle", "pom.xml"):
                    (self.repo / marker).unlink(missing_ok=True)
                self.source.write_text("public class Value {}\n")
                _, classes = self.project(tool)
                self.assertTrue(ut_precompile.precompile(self.repo)["ready"])
                self.source.write_text("public class Value { invalid Java source }\n")

                result = ut_precompile.precompile(self.repo)

                self.assertTrue(classes.is_dir())
                self.assertEqual("blocked", result["status"])
                self.assertFalse(result["ready"])
                self.assertEqual("precompile_failed", result["reason"])
                self.assertNotEqual(0, result["exit_code"])
                self.assertTrue(result["stdout"] or result["stderr"])
                self.assertFalse(result["tests_run"])
                self.assertIn("preserve these exact bytes", self.test_file.read_text())

    def test_zero_exit_without_expected_class_output_is_not_ready(self):
        wrapper, _ = self.project("gradle")
        wrapper.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
        result = ut_precompile.precompile(self.repo)
        self.assertEqual(0, result["exit_code"])
        self.assertFalse(result["ready"])
        self.assertEqual("class_output_missing", result["reason"])

    def test_multimodule_expected_output_is_detected(self):
        for tool in ("gradle", "maven"):
            with self.subTest(tool=tool):
                for marker in ("build.gradle", "pom.xml"):
                    (self.repo / marker).unlink(missing_ok=True)
                _, classes = self.project(tool, nested=True)
                result = ut_precompile.precompile(self.repo)
                self.assertFalse(result["ready"], "Nested modules require verified explicit directories")
                result = ut_precompile.precompile(self.repo, class_directories=[classes])
                self.assertTrue(result["ready"], result)
                self.assertIn(str(classes), result["class_directories"])

    def test_unrelated_nested_stale_output_cannot_grant_readiness(self):
        for tool in ("gradle", "maven"):
            with self.subTest(tool=tool):
                for marker in ("build.gradle", "pom.xml"):
                    (self.repo / marker).unlink(missing_ok=True)
                wrapper, _ = self.project(tool)
                wrapper.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n")
                stale = self.repo / "examples/archived" / (
                    "build/classes/java/main" if tool == "gradle" else "target/classes")
                stale.mkdir(parents=True)
                (stale / "Old.class").write_bytes(b"synthetic stale output")

                result = ut_precompile.precompile(self.repo)

                self.assertEqual(0, result["exit_code"])
                self.assertFalse(result["ready"], result)
                self.assertEqual("class_output_missing", result["reason"])
                self.assertEqual([], result["class_directories"])

    def test_explicit_outputs_all_must_exist_and_stay_under_root_without_symlinks(self):
        self.project("gradle")
        external = self.repo.parent / "outside-classes"
        external.mkdir()
        (self.repo / "alias").symlink_to(external, target_is_directory=True)
        for requested in ([external], [self.repo / "alias"], [Path("../outside-classes")],
                          [Path("build/classes/java/main"), Path("missing-module/classes")]):
            with self.subTest(requested=requested):
                result = ut_precompile.precompile(self.repo, class_directories=requested)
                self.assertFalse(result["ready"], result)
                self.assertIn(result["reason"], ("class_directory_invalid", "class_output_missing"))

    def test_cli_explicit_verified_module_output_is_supported(self):
        self.project("maven", nested=True)
        result = subprocess.run([
            sys.executable, str(PLUGIN_ROOT / "scripts/ut_precompile.py"),
            "--repo-root", str(self.repo), "--class-directory", "module/target/classes",
        ], text=True, capture_output=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ready"])
        self.assertEqual([str(self.repo / "module/target/classes")], payload["class_directories"])

    def test_gradle_kotlin_and_groovy_only_output_paths_are_preserved(self):
        # Path-discovery contract only; the wrapper still executes synthetic javac.
        for language in ("kotlin", "groovy"):
            with self.subTest(language=language):
                _, classes = self.project("gradle", language=language)
                result = ut_precompile.precompile(self.repo)
                self.assertTrue(result["ready"], result)
                self.assertIn(str(classes), result["class_directories"])

    def test_gradle_mixed_jvm_output_paths_are_all_passed_to_workers(self):
        _, java = self.project("gradle")
        other_outputs = [self.repo / f"build/classes/{language}/main" for language in ("kotlin", "groovy")]
        for directory in other_outputs:
            directory.mkdir(parents=True)
        result = ut_precompile.precompile(self.repo)
        self.assertTrue(result["ready"])
        self.assertEqual(sorted(str(p) for p in (java, *other_outputs)), result["class_directories"])

    def test_system_tool_is_used_only_without_project_wrapper(self):
        wrapper, _ = self.project("gradle")
        binaries = self.repo / "synthetic-bin"
        binaries.mkdir()
        system_tool = binaries / "gradle"
        wrapper.rename(system_tool)
        with patch.dict(os.environ, {"PATH": str(binaries)}):
            result = ut_precompile.precompile(self.repo)
        self.assertTrue(result["ready"])
        self.assertEqual(str(system_tool), result["command"][0])

    def test_relative_path_system_tool_keeps_discovered_location_when_cwd_changes(self):
        wrapper, _ = self.project("gradle")
        binaries = self.repo / "synthetic-bin"
        binaries.mkdir()
        system_tool = binaries / "gradle"
        wrapper.rename(system_tool)
        previous_cwd = Path.cwd()
        try:
            os.chdir(self.repo.parent)
            with patch.dict(os.environ, {"PATH": str(binaries.relative_to(self.repo.parent))}):
                result = ut_precompile.precompile(self.repo)
        finally:
            os.chdir(previous_cwd)
        self.assertTrue(result["ready"], result)
        self.assertEqual(str(system_tool), result["command"][0])

    def test_cli_returns_nonzero_and_diagnostics_before_dispatch_on_compile_failure(self):
        self.project("maven")
        self.source.write_text("public class Value { invalid Java source }\n")
        result = subprocess.run([
            sys.executable, str(PLUGIN_ROOT / "scripts/ut_precompile.py"),
            "--repo-root", str(self.repo),
        ], text=True, capture_output=True)
        self.assertNotEqual(0, result.returncode)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ready"])
        self.assertEqual("precompile_failed", payload["reason"])
        self.assertTrue(payload["stderr"])

    def test_unknown_or_ambiguous_project_is_blocked_without_running_builds(self):
        self.assertFalse(ut_precompile.precompile(self.repo)["ready"])
        self.project("gradle")
        (self.repo / "pom.xml").write_text("<project/>\n")
        self.assertFalse(ut_precompile.precompile(self.repo)["ready"])
        self.assertFalse((self.repo / "build-invocations.jsonl").exists())


class UtPrecompileCommandContractTest(unittest.TestCase):
    def test_every_mode_requires_current_precompile_gate_before_workers(self):
        text = (PLUGIN_ROOT / "commands/ut.md").read_text()
        section = text.split("#### 4.1.6", 1)[1].split("### 4.2", 1)[0]
        self.assertIn("scripts/ut_precompile.py", section)
        self.assertIn("compile_ready", section)
        self.assertIn("ready=false", section)
        self.assertNotIn("存在则跳过", section)
        self.assertIn("不得启动", section)
        self.assertIn("不是测试通过", section)


if __name__ == "__main__":
    unittest.main()
