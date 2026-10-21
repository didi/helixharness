from __future__ import annotations

import unittest

import telemetry_protocol as telemetry_protocol


class TelemetryProtocolV2Test(unittest.TestCase):
    def test_evolution_evidence_filters_and_redacts_exact_window_events(self) -> None:
        events = [
            {"type": "user", "timestamp": "2026-08-12T01:00:01Z", "text": "修复超时，token=secret-123"},
            {"type": "assistant", "timestamp": "2026-08-12T01:00:02Z", "text": "我会定位。"},
            {"type": "thinking", "timestamp": "2026-08-12T01:00:03Z", "text": "private chain"},
            {"type": "tool", "timestamp": "2026-08-12T01:00:04Z", "name": "exec", "status": "completed", "input": "cat source.py", "output": "source"},
            {"type": "tool", "timestamp": "2026-08-12T01:00:05Z", "name": "test", "status": "failed", "input": "password=hunter2", "error": "mailto:user@example.com"},
            {"type": "assistant", "timestamp": "2026-08-12T01:00:06Z", "text": "已修复并验证。", "final": True},
            {"type": "user", "timestamp": "2026-08-12T02:00:00Z", "text": "outside"},
        ]
        evidence = telemetry_protocol.build_evolution_evidence(
            events,
            started_at="2026-08-12T01:00:00Z",
            ended_at="2026-08-12T01:01:00Z",
        )

        serialized = str(evidence)
        self.assertNotIn("private chain", serialized)
        self.assertNotIn("source.py", serialized)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("user@example.com", serialized)
        self.assertNotIn("outside", serialized)
        self.assertIn("[REDACTED_SECRET]", serialized)
        self.assertEqual(1, evidence["tools"]["succeeded"])
        self.assertEqual(1, evidence["tools"]["failed"])
        self.assertEqual(1, len(evidence["failed_tools"]))
        self.assertGreaterEqual(evidence["coverage"]["omitted"], 2)

    def test_evolution_evidence_never_preserves_code_diff_or_key_material(self) -> None:
        events = [
            {
                "type": "user",
                "timestamp": "2026-08-12T01:00:01Z",
                "text": "请看代码：\n```python\nprint('private source')\n```\n然后修复。",
            },
            {
                "type": "tool",
                "timestamp": "2026-08-12T01:00:02Z",
                "name": "apply_patch",
                "status": "failed",
                "input": "*** Begin Patch\n+secret source\n*** End Patch",
                "error": "-----BEGIN PRIVATE KEY-----\nprivate-key-body",
            },
        ]
        evidence = telemetry_protocol.build_evolution_evidence(
            events,
            started_at="2026-08-12T01:00:00Z",
            ended_at="2026-08-12T01:01:00Z",
        )
        serialized = str(evidence)
        self.assertNotIn("private source", serialized)
        self.assertNotIn("secret source", serialized)
        self.assertNotIn("private-key-body", serialized)
        self.assertIn("[OMITTED_CODE]", serialized)
        self.assertEqual("str chars=44", evidence["failed_tools"][0]["input_summary"])
        self.assertEqual(
            "error category=unknown chars=44",
            evidence["failed_tools"][0]["error_summary"],
        )
        self.assertEqual(2, evidence["coverage"]["omission_reasons"]["high_risk_content"])
        self.assertGreaterEqual(evidence["coverage"]["omitted"], 2)

    def test_failed_tool_summaries_never_echo_raw_json_sql_or_xml(self) -> None:
        events = [
            {
                "type": "tool",
                "timestamp": "2026-08-12T01:00:01Z",
                "name": "exec",
                "status": "failed",
                "input": '{"command":"SELECT private_column FROM secret_table"}',
                "error": '<error><source>private implementation</source></error>',
            }
        ]

        evidence = telemetry_protocol.build_evolution_evidence(
            events,
            started_at="2026-08-12T01:00:00Z",
            ended_at="2026-08-12T01:01:00Z",
        )

        serialized = str(evidence)
        for raw in ("SELECT", "private_column", "secret_table", "private implementation"):
            self.assertNotIn(raw, serialized)
        failed = evidence["failed_tools"][0]
        self.assertEqual("object fields=1", failed["input_summary"])
        self.assertEqual("error category=unknown chars=54", failed["error_summary"])

    def test_sanitizer_covers_unlabelled_provider_secrets_and_system_paths(self) -> None:
        text, redactions = telemetry_protocol.sanitize_text(
            r"keys sk-proj-abcdefghijklmnopqrstuvwxyz and ghp_abcdefghijklmnopqrstuvwxyz123456; "
            r"files C:\Users\alice\private.txt /etc/ssh/ssh_config"
        )
        self.assertNotIn("sk-proj-", text)
        self.assertNotIn("ghp_", text)
        self.assertNotIn("alice", text)
        self.assertNotIn("/etc/ssh", text)
        self.assertGreaterEqual(redactions, 4)

    def test_evolution_evidence_preserves_full_sanitized_text(self) -> None:
        long_requirement = "需求背景" + ("保持完整上下文" * 600) + " token=private-value"
        evidence = telemetry_protocol.build_evolution_evidence(
            [
                {
                    "type": "user",
                    "timestamp": "2026-08-12T01:00:01Z",
                    "text": long_requirement,
                }
            ],
            started_at="2026-08-12T01:00:00Z",
            ended_at="2026-08-12T01:01:00Z",
        )

        retained = evidence["user_inputs"][0]
        self.assertGreater(len(retained), 2_000)
        self.assertTrue(retained.startswith("需求背景"))
        self.assertTrue(retained.endswith("[REDACTED_SECRET]"))
        self.assertNotIn("private-value", retained)

    def test_sanitizer_omits_unlabelled_source_diff_and_structured_code(self) -> None:
        samples = (
            '{"token":"secret-123","enabled":true}',
            '请使用配置 {"token":"secret-123","enabled":true} 继续执行',
            "SELECT private_column FROM secret_table WHERE id = 1",
            "<config><password>private</password></config>",
            "+result=private_implementation",
            "-old_call()\n+new_call()",
            "result = build_private_implementation()",
        )

        for sample in samples:
            with self.subTest(sample=sample):
                sanitized, redactions = telemetry_protocol.sanitize_text(sample)
                self.assertEqual("[OMITTED_HIGH_RISK]", sanitized)
                self.assertGreaterEqual(redactions, 1)

    def test_evolution_evidence_keeps_every_non_thinking_assistant_message(self) -> None:
        evidence = telemetry_protocol.build_evolution_evidence(
            [
                {"type": "assistant", "timestamp": "2026-08-12T01:00:01Z", "text": "先确认范围。"},
                {"type": "assistant", "timestamp": "2026-08-12T01:00:02Z", "text": "然后开始实现。"},
                {"type": "assistant", "timestamp": "2026-08-12T01:00:03Z", "text": "最后完成验证。", "final": True},
            ],
            started_at="2026-08-12T01:00:00Z",
            ended_at="2026-08-12T01:01:00Z",
        )

        self.assertEqual(
            ["先确认范围。", "然后开始实现。", "最后完成验证。"],
            evidence["assistant_replies"],
        )

    def test_evolution_evidence_preserves_ordered_interaction_events(self) -> None:
        evidence = telemetry_protocol.build_evolution_evidence(
            [
                {
                    "type": "assistant",
                    "timestamp": "2026-08-12T01:00:01Z",
                    "text": "请选择处理方式。",
                },
                {
                    "type": "user",
                    "timestamp": "2026-08-12T01:00:02Z",
                    "text": "按推荐方案处理。",
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-08-12T01:00:03Z",
                    "text": "继续执行并完成。",
                },
            ],
            started_at="2026-08-12T01:00:00Z",
            ended_at="2026-08-12T01:01:00Z",
        )

        self.assertEqual(
            [
                {"role": "assistant", "text": "请选择处理方式。"},
                {"role": "user", "text": "按推荐方案处理。"},
                {"role": "assistant", "text": "继续执行并完成。"},
            ],
            evidence["interactions"],
        )
        self.assertEqual(
            {"user_turns": 1, "assistant_messages": 2},
            evidence["interaction_summary"],
        )

    def test_supplement_is_content_addressed_and_carries_join_keys(self) -> None:
        fixed = {
            "entry_id": "entry-123",
            "requirement_id": "req_0123456789abcdef01234567",
            "requirement_name": "checkout-timeout",
            "platform": "codex",
            "session_id": "019fea93-7e0e-7c60-82ec-8cf9f66f518d",
        }
        first = telemetry_protocol.build_stage_supplement(
            fixed,
            session_hash="session-abc",
            usage={"input_tokens": 20, "output_tokens": 10, "model": "gpt-test"},
            execution={"tool_call_count": 2, "subagent_spawn_count": 1},
            evidence={"schema": "helixharness-evolution-evidence/1.0"},
        )
        second = telemetry_protocol.build_stage_supplement(
            fixed,
            session_hash="session-abc",
            usage={"input_tokens": 20, "output_tokens": 10, "model": "gpt-test"},
            execution={"tool_call_count": 2, "subagent_spawn_count": 1},
            evidence={"schema": "helixharness-evolution-evidence/1.0"},
            workflow_trace={
                "schema": "helixharness-workflow-trace/1.0",
                "status": "collected",
                "route": {"decision_code": "variant_resolved"},
            },
        )
        repeated = telemetry_protocol.build_stage_supplement(
            fixed,
            session_hash="session-abc",
            usage={"input_tokens": 20, "output_tokens": 10, "model": "gpt-test"},
            execution={"tool_call_count": 2, "subagent_spawn_count": 1},
            evidence={"schema": "helixharness-evolution-evidence/1.0"},
            workflow_trace={
                "schema": "helixharness-workflow-trace/1.0",
                "status": "collected",
                "route": {"decision_code": "variant_resolved"},
            },
        )
        self.assertNotEqual(first["supplement_id"], second["supplement_id"])
        self.assertEqual(second["supplement_id"], repeated["supplement_id"])
        self.assertEqual("entry-123", first["entry_id"])
        self.assertEqual("checkout-timeout", first["requirement_name"])
        self.assertNotIn("logical_stage_run_id", first)
        self.assertEqual(1, first["execution"]["subagent_spawn_count"])
        self.assertEqual(
            "variant_resolved",
            second["workflow_trace"]["route"]["decision_code"],
        )
        self.assertEqual(
            {
                "platform": "codex",
                "session_id": "019fea93-7e0e-7c60-82ec-8cf9f66f518d",
                "session_hash": "session-abc",
                "session_kind": "primary",
            },
            first["session"],
        )

        revised = telemetry_protocol.build_stage_supplement(
            fixed,
            session_hash="session-abc",
            usage={"input_tokens": 21, "output_tokens": 10, "model": "gpt-test"},
            execution={"tool_call_count": 2, "subagent_spawn_count": 1},
            evidence={"schema": "helixharness-evolution-evidence/1.0"},
            revision=2,
            supersedes=first["supplement_id"],
        )
        self.assertNotEqual(first["supplement_id"], revised["supplement_id"])
        self.assertEqual(first["supplement_id"], revised["supersedes"])


if __name__ == "__main__":
    unittest.main()
