#!/usr/bin/env python3

from __future__ import annotations

import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[2]


class CodexSubagentAdapterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.protocol = (
            PLUGIN_ROOT / "reference/codex-subagent-runtime-protocol.md"
        ).read_text(encoding="utf-8")

    def test_maps_only_codex_runtime_mechanics(self) -> None:
        for marker in (
            "version: 2",
            "reference/subagent-runtime-protocol.md",
            "`spawn_job` 映射为当前运行时直接暴露的 `spawn_agent`",
            'fork_turns="none"',
            '不得使用 `fork_turns="all"`',
            "映射到同级 `AGENTS.md`",
            "BLOCKED: codex_subagent_capacity_unavailable",
            "不查找、调用或模拟 `close_agent`",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.protocol)

    def test_dispatch_defaults_are_leaf_and_parent_owned_interaction(self) -> None:
        common = (
            PLUGIN_ROOT / "reference/subagent-runtime-protocol.md"
        ).read_text(encoding="utf-8")
        claude = (PLUGIN_ROOT / "hosts/claude-code/subagent.md").read_text(
            encoding="utf-8"
        )

        for marker in (
            "delegation_policy",
            "默认 `leaf`",
            "interaction_mode",
            "默认 `host_native`",
        ):
            with self.subTest(resource="common", marker=marker):
                self.assertIn(marker, common)

        for marker in (
            "delegation_policy=leaf",
            "interaction_mode=afk",
            "不得继续派生子代理",
            "交互返回父流程",
        ):
            with self.subTest(resource="codex", marker=marker):
                self.assertIn(marker, self.protocol)
        for marker in (
            "delegation_policy=leaf",
            "interaction_mode=host_native",
            "不得继续派生子代理",
            "遵循当前 Agent prompt",
        ):
            with self.subTest(resource="claude", marker=marker):
                self.assertIn(marker, claude)

    def test_does_not_duplicate_command_business_contracts(self) -> None:
        for marker in (
            "validate_init_outputs.py",
            "validate_propose_outputs.py",
            "validate_review_output.py",
            "最终整体编译",
            "HELIXHARNESS_EVIDENCE_INDEX",
            "HELIXHARNESS_REVIEW_RESULT",
            "HELIXHARNESS_TASK_RESULTS",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.protocol)

    def test_does_not_override_claude_semantics(self) -> None:
        self.assertIn("不把该限制扩散到 Claude Code 或公共协议", self.protocol)
        self.assertIn("本资源不得新增或覆盖业务语义", self.protocol)


    def test_codex_adapter_keeps_safe_read_batching_and_parent_prefetch(self) -> None:
        for marker in (
            "batch_read",
            "bounded output",
            "parent_prefetch",
            "不得写入",
            "不得绕过 read_scope",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.protocol)

    def test_codex_adapter_reuses_a_resolved_context_packet(self) -> None:
        for marker in (
            "HELIXHARNESS_CODEX_CONTEXT_PACKET",
            "source_sha256",
            "resolved_read_paths",
            "dependency_evidence_refs",
            "不得重新执行全仓 discovery",
            "hash 不一致",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.protocol)



    def test_codex_structured_init_result_requires_evidence_facts(self) -> None:
        init = (PLUGIN_ROOT / "commands/init.md").read_text(encoding="utf-8")
        self.assertIn("`helixharness_structured_text` 必须", init)
        self.assertIn("返回 evidence index，缺失即结果校验失败", init)
        self.assertNotIn("HELIXHARNESS_EVIDENCE_INDEX", init)



    def test_format_reencode_is_codex_only_and_does_not_change_lite(self) -> None:
        codex = self.protocol
        claude = (PLUGIN_ROOT / "hosts/claude-code/subagent.md").read_text(
            encoding="utf-8"
        )
        for marker in (
            "format-only follow-up",
            "同一 Agent",
            "不得派生新 Agent",
            "能力不可用时返回父流程自主判断",
        ):
            with self.subTest(resource="codex", marker=marker):
                self.assertIn(marker, codex)
        self.assertNotIn("result_reencode_budget", claude)
        for path in (
            "workflows/single/apply.md",
            "workflows/single/archive.md",
            "skills/code-review/SKILL.md",
        ):
            with self.subTest(resource=path):
                self.assertNotIn(
                    "result_reencode_budget",
                    (PLUGIN_ROOT / path).read_text(encoding="utf-8"),
                )


if __name__ == "__main__":
    unittest.main()
