"""User message counts across current and partially collected stage windows."""
from __future__ import annotations

import copy
import json

import pytest

from eval.stage_supplements import LinkedStageSupplement
from eval.telemetry_materialization import materialize_eval_log
from eval.tests.test_usage_loader import _workflow_log, _workflow_supplement
from eval.usage_loader import (
    _dimension_cards,
    _feature_table_rows,
    _stage_seg,
    _summarize_user_rows,
    _user_interactions,
    aggregate_all,
    aggregate_feature,
    discover_dashboard_projects,
    render_usage_overview_html,
)


def _projected_log(entries):
    return {**_workflow_log(), "entries": entries}


def entry(entry_id, turns, *, actor="Demo User"):
    result = materialize_eval_log(_workflow_log())["entries"][0]
    result.update(entry_id=entry_id, telemetry_status="complete")
    result["actor"]["user_name"] = actor
    result["collection"]["user_turn_count"] = turns
    result["friction"]["interventions"] = 99  # Must never be used as a fallback.
    return result


def test_workflow_supplement_user_turns_reach_project_table_and_stage(tmp_path):
    log = _workflow_log()
    log_path = tmp_path / "docs/eval/workflow-demo/eval-log.json"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(json.dumps(log), encoding="utf-8")
    payload = _workflow_supplement()
    payload["evidence"]["interaction_summary"]["user_turns"] = 7
    record = LinkedStageSupplement(
        source_path=str(log_path.resolve()), payload_hash="a" * 64, payload=payload,
        received_at="2026-09-10T00:00:00Z", effective_at="2026-09-10T00:00:00Z",
    )
    agg = aggregate_all(discover_dashboard_projects([str(tmp_path)], linked_supplements=[record]))
    feature = agg["features"][0]
    assert feature["total_interventions"] == 0
    assert feature["user_interactions"] == {"total": 7, "collected_entries": 1, "eligible_entries": 1, "exact": True}
    assert agg["summary"]["user_interactions"]["total"] == 7
    html = render_usage_overview_html([str(tmp_path)], linked_supplements=[record])
    assert '用户交互轮次' in html and '<b class="helixharness-num">7</b>' in html
    assert '已采集 · 1/1 条阶段记录' in html
    assert '交互 7' in html
    assert '用户交互轮次 <b>7 轮</b>' in html
    assert '流程介入' not in html and '轮介入' not in html


def test_pending_workflow_shows_missing_and_never_uses_interventions(tmp_path):
    path = tmp_path / "docs/eval/workflow-demo/eval-log.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_workflow_log()), encoding="utf-8")
    html = render_usage_overview_html([str(tmp_path)], linked_supplements=[])
    assert '<b class="helixharness-num">未采集</b>' in html
    assert '0/1 条阶段记录' in html


@pytest.mark.parametrize(("turns", "total", "collected"), [
    ([0], 0, 1), ([2, 5], 7, 2), ([3, None], 3, 1),
    ([None, None], None, 0), ([-1, True, None], None, 0),
])
def test_zero_missing_partial_and_invalid_counts_remain_distinct(turns, total, collected):
    entries = [entry(f"e-{i}", value) for i, value in enumerate(turns)]
    result = _user_interactions(entries)
    assert result["total"] == total
    assert result["collected_entries"] == collected
    assert result["eligible_entries"] == len(turns)
    assert result["exact"] is (collected == len(turns))
    feature = aggregate_feature(_projected_log(entries))
    rows, _ = _feature_table_rows([feature], None)
    assert f'<b class="helixharness-num">{total if total is not None else "未采集"}</b>' in rows
    if collected and collected < len(turns):
        assert '已采集小计' in rows


def test_global_project_and_user_rollups_preserve_missing_counts():
    logs = [
        _projected_log([entry("first", 3, actor="Alice"), entry("second", None, actor="Bob")]),
        _projected_log([entry("third", 0, actor="Alice"), entry("fourth", 4, actor="Bob")]),
    ]
    logs[1]["feature"] = "another-feature"
    agg = aggregate_all([{"features": logs}])
    expected = {"total": 7, "collected_entries": 3, "eligible_entries": 4, "exact": False}
    assert agg["summary"]["user_interactions"] == expected
    assert agg["project_view"][0]["user_interactions"] == expected
    users = {row["user_id"]: row for row in _summarize_user_rows(agg["user_project"])}
    assert users["Alice"]["user_interactions"]["total"] == 3
    assert users["Alice"]["user_interactions"]["exact"] is True
    assert users["Bob"]["user_interactions"] == {"total": 4, "collected_entries": 1, "eligible_entries": 2, "exact": False}
    for mode, key in (("project", "project_view"), ("user", "user_project")):
        cards = _dimension_cards(agg["summary"], agg[key], mode)
        assert '用户交互轮次' in cards and '已采集小计 · 3/4 条阶段记录' in cards


def test_stage_color_depends_on_execution_status_not_message_volume():
    few = aggregate_feature(_projected_log([entry("first", 1)]))["stage_timeline"][1]
    many = copy.deepcopy(few)
    many["user_interactions"]["total"] = 100
    first = _stage_seg(few, "coding")
    second = _stage_seg(many, "coding")
    assert 'style="background:#52C41A;' in first and 'style="background:#52C41A;' in second
    assert '交互 100' in second
