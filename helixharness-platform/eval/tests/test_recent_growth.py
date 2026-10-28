"""Recent counts use durable platform admissions, independent of workflow dates."""
from datetime import datetime, timezone

from eval.usage_loader import _latest_runtime_records, _recent_growth_counts


def entry(name, time, **extra):
    return {'command': 'apply', 'actor': {'user_name': name}, 'started_at': time, **extra}


def test_every_project_counts_once_even_with_shared_or_unknown_users():
    features = [
        {'feature': 'one', 'entries': [entry('Same', '2026-09-01T00:00:00Z'), entry('Other', '2026-09-10T00:00:00Z')]},
        {'feature': 'two', 'entries': [entry('Same', '2026-09-09T00:00:00Z')]},
        {'feature': 'no-date', 'entries': [entry('', '')]},
        {'feature': 'empty', 'entries': []},
    ]
    records = _latest_runtime_records(features)
    assert len(records) == 4
    assert records[0][1]['actor']['user_name'] == 'Other'
    assert records[-1][1] == {}


def test_recent_growth_uses_platform_admission_and_beijing_dates():
    dates = ['2026-09-03T15:59:59Z', '2026-09-03T16:00:00Z', '2026-09-10T16:00:00Z', None]
    features = [{'repository': 'repo', 'feature': str(i), 'entries': [entry(str(i), '2000-01-01T00:00:00Z')]} for i in range(4)]
    snapshot = {'projects': {('repo', str(i)): time for i, time in enumerate(dates)},
                'users': {str(i): time for i, time in enumerate(dates)}}
    now = datetime(2026, 9, 10, 8, tzinfo=timezone.utc)
    result = _recent_growth_counts(features, 7, now=now, first_seen=snapshot)
    assert result['projects'] == result['users'] == 1
    assert result['unknown_projects'] == 1
    assert result['start'] == '2026-09-04' and result['end'] == '2026-09-10'
    assert _recent_growth_counts(features, 3, now=now, first_seen=snapshot)['projects'] == 0
    assert _recent_growth_counts(features, now=now)['projects'] == 0  # Never substitute workflow dates.


def test_default_recent_growth_is_seven_days_in_beijing():
    dates = ['2026-09-09T15:59:59Z', '2026-09-09T16:00:00Z', '2026-09-10T16:00:00Z']
    features = [{'repository': 'repo', 'feature': str(i), 'entries': [entry(str(i), '2000-01-01T00:00:00Z')]} for i in range(3)]
    snapshot = {'projects': {('repo', str(i)): time for i, time in enumerate(dates)},
                'users': {str(i): time for i, time in enumerate(dates)}}
    result = _recent_growth_counts(features, now=datetime(2026, 9, 10, 8, tzinfo=timezone.utc), first_seen=snapshot)
    assert result['days'] == 7
    assert result['projects'] == result['users'] == 2
    assert result['start'] == '2026-09-04' and result['end'] == '2026-09-10'


def test_distribution_units_and_version_order_are_independent():
    import re

    from eval.usage_loader import _runtime_distribution_html

    versions = ['2.0.14', '2.2.9', '2.2.10', '2.2.2-v2-SNAPSHOT', '2.2.2-v12-SNAPSHOT', '2.2.2', 'unknown']
    features = [{'feature': str(i),
                 'entries': [entry(str(i), '2026-09-10T00:00:00Z', helixharness_version=v, platform='codex')]}
                for i, v in enumerate(versions)]
    features.append({'feature': 'another-project-same-person',
                     'entries': [entry('0', '2026-08-01T00:00:00Z', helixharness_version='1.0.0', platform='claude')]})
    chart = _runtime_distribution_html(features, 'version')
    labels = re.findall(r'<li data-label="([^"]+)"', chart)
    assert labels == ['2.2.10', '2.2.9', '2.2.2', '2.2.2-v12-SNAPSHOT', '2.2.2-v2-SNAPSHOT', '2.0.14', '未采集']
    assert '共 7 人' in chart
    platform = _runtime_distribution_html(features, 'platform')
    assert 'Codex 7 人' in platform and 'data-distribution="workflow"' not in platform and 'Legacy' not in platform
    assert 'data-label="Claude Code"' not in platform


def test_platform_distribution_does_not_invent_users_for_unnamed_projects():
    from eval.usage_loader import _runtime_distribution_html

    html = _runtime_distribution_html([{"entries": []}], "platform")
    assert "暂无可统计的用户记录" in html
    assert 'data-distribution="workflow"' not in html
    assert "1 个项目" not in html
