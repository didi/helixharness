#!/usr/bin/env python3
"""Codex method projection regressions."""

from __future__ import annotations

import unittest
from pathlib import Path

from codex_skill_projection import projected_methods


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class CodexSkillProjectionTest(unittest.TestCase):
    def test_internal_tdd_skill_preserves_user_invocation_boundary(self) -> None:
        target = PLUGIN_ROOT / "hosts/codex/skills/tdd/SKILL.md"
        projected = projected_methods(PLUGIN_ROOT)[target]

        self.assertIn("user-invocable: false\n", projected)

    def test_tdd_requires_active_workflow_context_before_writes(self) -> None:
        canonical = (PLUGIN_ROOT / "skills/tdd/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("BLOCKED: helixharness_tdd_context_missing", canonical)
        self.assertIn("普通需求文件", canonical)

    def test_method_entries_require_native_runtime_preflight_before_python(self) -> None:
        for target, projected in projected_methods(PLUGIN_ROOT).items():
            with self.subTest(skill=target.parent.name):
                self.assertIn("reference/runtime-preflight.md", projected)
                self.assertIn("scripts/runtime_preflight.ps1", projected)
                self.assertIn("任何 Python 调用前", projected)


if __name__ == "__main__":
    unittest.main()
