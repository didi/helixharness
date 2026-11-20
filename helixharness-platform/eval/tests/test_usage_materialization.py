"""Collection completeness is independent from observed numeric usage."""
from __future__ import annotations

import pytest

from eval.telemetry_materialization import _materialize_usage
from eval.tests.test_usage_loader import _repeated_apply_log
from eval.usage_loader import _aggregate_resource_usages, _resource_usage, render_feature_timeline_html


@pytest.mark.parametrize("usage,total", [
    ({"input_tokens": 0, "output_tokens": 0}, 0),
    ({"input_tokens": 150, "output_tokens": 10, "cache_read_tokens": 30, "cache_write_tokens": 20}, 160),
    ({"total_tokens": 0}, 0),
    ({"total_tokens": 160, "cache_read_tokens": 30}, 160),
    ({"input_tokens": 10}, None),
    ({"cache_read_tokens": 10, "cache_write_tokens": 5}, None),
    ({"input_tokens": True, "output_tokens": 10}, None),
    ({"input_tokens": -1, "output_tokens": 10}, None),
    ({"input_tokens": "10", "output_tokens": 10}, None),
    ({"input_tokens": 1.5, "output_tokens": 10}, None),
])
def test_materializer_requires_a_real_total_or_both_primary_counts(usage: dict, total: int | None) -> None:
    result = _materialize_usage(usage)
    assert result["total_tokens"] == total
    assert result["collection_status"] == ("collected" if total is not None else "unavailable")


def test_explicit_unavailable_usage_is_not_counted_despite_stale_numbers() -> None:
    usage = _resource_usage({"resource_usage": _materialize_usage({
        "input_tokens": 100, "output_tokens": 10,
        "collection_status": "unavailable", "unavailable_reason": "no_complete_generation_in_stage_window",
        "scope": "parent_only", "attribution": "complete_generations_within_stage",
    })})
    assert usage["total_tokens"] is None
    assert usage["unavailable_reason"] == "no_complete_generation_in_stage_window"
    assert usage["attribution"] == "complete_generations_within_stage"


@pytest.mark.parametrize("status", ["collected", "partial"])
def test_nested_rollups_keep_parent_only_usage_partial(status: str) -> None:
    entry = {"resource_usage": _materialize_usage({
        "input_tokens": 0, "output_tokens": 0, "collection_status": status,
        "scope": "parent_only", "attribution": "complete_generations_within_stage",
    })}
    first = _aggregate_resource_usages([_resource_usage(entry)])
    second = _aggregate_resource_usages([first], "global")
    for result in (first, second):
        assert result["total_tokens"] == 0
        assert result["collection_status"] == "partial"
        assert result["collected_entries"] == 1
        assert result["partial_entries"] == 1
        assert result["exact"] is False
        assert result["exact_total_tokens"] is None
        assert result["limited_scopes"] == ["parent_only"]


def test_legacy_complete_usage_keeps_existing_attribution() -> None:
    result = _materialize_usage({"input_tokens": 20, "output_tokens": 10, "cached_input_tokens": 4})
    assert result["total_tokens"] == 30
    assert result["cache_read_tokens"] == 4
    assert result["attribution"] == "exact_session_window"
    assert result["usage_scope"] == "stage_session"
    assert result["confidence"] == "high"


def test_incomplete_collected_snapshot_does_not_use_success_reason_as_missing_reason() -> None:
    result = _materialize_usage({
        "input_tokens": 10, "collection_status": "collected", "reason": "native_generation_snapshots",
    })
    assert result["total_tokens"] is None
    assert result["collection_status"] == "unavailable"
    assert result["unavailable_reason"] == "incomplete_token_fields"


@pytest.mark.parametrize("execution,tools,expected", [
    ({}, {}, "未采集"),
    ({"tool_error_count": 0}, {}, "0 次"),
    ({}, {"failed": 0}, "0 次"),
])
def test_missing_execution_count_is_distinct_from_explicit_zero(execution: dict, tools: dict, expected: str) -> None:
    log = _repeated_apply_log()
    for entry in log["entries"]:
        entry.update(execution_stats=execution, async_evidence={"tools": tools})
    timeline = render_feature_timeline_html(log)
    assert f"调用未成功 <b>{expected}</b>" in timeline
    if expected == "未采集":
        assert "调用未成功 <b>0 次</b>" not in timeline
