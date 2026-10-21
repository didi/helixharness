"""Overview duration excludes unfinished and wholly undated projects."""
import re

from eval.tests.test_dashboard_scope import _write
from eval.tests.test_usage_loader import _workflow_log
from eval.usage_loader import _archived_duration_average, render_usage_global_overview_html


def test_archived_average_keeps_measured_zero_and_long_samples():
    def feature(status, seconds):
        return {"status": status, "total_duration_sec": seconds or 0,
                "entries": [{"duration_sec": seconds}]}
    features = [
        feature("archived", 139 * 3600),
        feature("archived", 3600),
        feature("archived", 0),
        feature("archived", None),
        feature("in_progress", 999 * 3600),
    ]
    assert _archived_duration_average(features) == (168000, 3, 1)
    assert _archived_duration_average(features[-2:]) == (None, 0, 1)
    assert _archived_duration_average([]) == (None, 0, 0)


def test_overview_average_and_tip_use_all_archived_features(tmp_path):
    root = tmp_path / "repo"
    for name, status, hours in [
        ("first-done", "archived", 2),
        ("second-done", "archived", 4),
        ("active", "in_progress", 100),
    ]:
        log = _workflow_log()
        log["status"] = status
        log["entries"][0].update(
            started_at="2026-09-01T00:00:00Z",
            ended_at=f"2026-09-{1 + hours // 24:02d}T{hours % 24:02d}:00:00Z",
        )
        _write(root, name, log)
    html = render_usage_global_overview_html([str(root)])
    card = re.search(r'<span class="helixharness-tip-wrap">平均耗时.*?class="helixharness-metric-sub">.*?</div></div>', html)
    assert card is not None
    assert 'class="helixharness-metric-num">3.00h</div>' in card[0]
    assert "已归档项目的阶段累计耗时之和 ÷ 有效项目数。" in card[0]
    assert "已归档项目平均耗时" not in card[0]
