"""Claude token totals distinguish reported zero from incomplete source usage."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from telemetry_hosts.supplement_parsers import collect_claude_window


class ClaudeSupplementParserTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "synthetic-session.jsonl"

    def assistant(self, usage: object, **changes: object) -> dict:
        return {
            "type": "assistant", "timestamp": "2026-08-01T00:01:00Z",
            "message": {"content": "Synthetic answer", "usage": usage}, **changes,
        }

    def collect(self, records: list[dict]) -> dict:
        self.source.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
        return collect_claude_window(
            self.source, started_at="2026-08-01T00:00:00Z", ended_at="2026-08-01T00:10:00Z",
        ).usage

    def test_reported_zero_primary_and_cache_counts_survive(self) -> None:
        for input_tokens, output_tokens in ((0, 10), (10, 0), (0, 0), (10, 20)):
            with self.subTest(input_tokens=input_tokens, output_tokens=output_tokens):
                usage = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                         "cache_read_tokens": 0, "cache_write_tokens": 0}
                self.assertEqual(usage, self.collect([self.assistant(usage)]))

    def test_multiple_complete_records_preserve_zero_and_sum_observed_counts(self) -> None:
        result = self.collect([
            self.assistant({"input_tokens": 0, "output_tokens": 10, "cache_read_tokens": 0}),
            self.assistant({"input_tokens": 0, "output_tokens": 5, "cache_read_tokens": 0}),
        ])
        self.assertEqual({"input_tokens": 0, "output_tokens": 15, "cache_read_tokens": 0}, result)

    def test_incomplete_or_invalid_field_is_not_repaired_by_another_record(self) -> None:
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            for value in (None, True, False, -1, 1.5, "0"):
                for reverse in (False, True):
                    with self.subTest(field=field, value=value, reverse=reverse):
                        complete = {"input_tokens": 10, "output_tokens": 20,
                                    "cache_read_tokens": 0, "cache_write_tokens": 0}
                        incomplete = {**complete, field: value}
                        if value is None:
                            incomplete.pop(field)
                        records = [self.assistant(complete), self.assistant(incomplete)]
                        if reverse:
                            records.reverse()
                        result = self.collect(records)
                        self.assertNotIn(field, result)
                        for key in complete.keys() - {field}:
                            self.assertEqual(complete[key] * 2, result.get(key))

    def test_complementary_incomplete_records_never_form_complete_usage(self) -> None:
        result = self.collect([
            self.assistant({"input_tokens": 10}), self.assistant({"output_tokens": 20}),
        ])
        self.assertEqual({}, result)

    def test_missing_usage_never_becomes_a_zero_or_a_complete_total(self) -> None:
        for usage in (None, {}, [], "unavailable"):
            with self.subTest(usage=usage):
                self.assertEqual({}, self.collect([self.assistant(usage)]))
                self.assertEqual({}, self.collect([
                    self.assistant({"input_tokens": 10, "output_tokens": 20}), self.assistant(usage),
                ]))
        missing = self.assistant(None)
        missing["message"].pop("usage")
        self.assertEqual({}, self.collect([missing]))
        self.assertEqual({}, self.collect([]))

    def test_outside_window_meta_and_user_records_do_not_change_completeness(self) -> None:
        usage = {"input_tokens": 0, "output_tokens": 10}
        result = self.collect([
            self.assistant(None, timestamp="2026-07-31T23:59:59Z"),
            self.assistant(None, timestamp="2026-08-01T00:10:01Z"),
            self.assistant(None, isMeta=True),
            self.assistant(None, message={"isMeta": True}),
            self.assistant(None, message={"isSidechain": True}),
            self.assistant(None, type="user"),
            self.assistant(usage),
        ])
        self.assertEqual(usage, result)


if __name__ == "__main__":
    unittest.main()
