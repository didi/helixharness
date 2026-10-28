"""Durable admission history is not workflow time or a rebuildable index."""
import sqlite3
from datetime import datetime, timezone

from eval.tests.test_dashboard_scope import _write
from eval.tests.test_usage_loader import _workflow_log
from eval.usage_first_seen import UsageFirstSeenStore
from eval.usage_loader import _recent_growth_counts, aggregate_all, discover_dashboard_projects


def test_resync_restart_and_updates_never_change_admission_dates(tmp_path):
    store = UsageFirstSeenStore(tmp_path)
    store.bootstrap([])
    root = tmp_path / 'repo'
    _write(root, 'new', _workflow_log())  # Old workflow first discovered in September.
    features = aggregate_all(discover_dashboard_projects([str(root)]))['features']
    first = datetime(2026, 9, 10, 4, tzinfo=timezone.utc)
    store.observe(features, now=first)
    expected = store.snapshot()
    restarted = UsageFirstSeenStore(tmp_path)
    restarted.bootstrap([str(root)])
    features[0]['entries'].append({**features[0]['entries'][0], 'command': 'archive'})
    restarted.observe(features, now=datetime(2026, 9, 11, tzinfo=timezone.utc))
    assert restarted.snapshot() == expected
    today = _recent_growth_counts(features, days=1, now=first, first_seen=expected)
    assert today['projects'] == today['users'] == 1
    tomorrow = _recent_growth_counts(features, days=1, now=datetime(2026, 9, 11, 8, tzinfo=timezone.utc), first_seen=expected)
    assert tomorrow['projects'] == tomorrow['users'] == 0


def test_bootstrap_uses_retained_discovery_and_preserves_unknown_history(tmp_path):
    root = tmp_path / 'repo'
    known = _write(root, 'known', _workflow_log())
    _write(root, 'unknown', _workflow_log())
    ledger = tmp_path / 'stage_supplements.db'
    with sqlite3.connect(ledger) as c:
        c.execute('CREATE TABLE ledger_entries (source_path TEXT, entry_id TEXT, discovered_at TEXT)')
        c.execute('INSERT INTO ledger_entries VALUES (?, ?, ?)', (known, 'entry-0123456789abcdef', '2026-09-10T04:01:31Z'))
    store = UsageFirstSeenStore(tmp_path)
    store.bootstrap([str(root)], ledger)
    features = aggregate_all(discover_dashboard_projects([str(root)]))['features']
    store.observe(features)
    snapshot = store.snapshot()
    assert snapshot['projects'][('repo', 'known')] == '2026-09-10T04:01:31Z'
    assert snapshot['projects'][('repo', 'unknown')] is None
    assert snapshot['users']['Demo'] is None  # Already existed in an undated historical project.
    now = datetime(2026, 9, 10, 8, tzinfo=timezone.utc)
    counts = _recent_growth_counts(features, now=now, first_seen=snapshot)
    assert counts['projects'] == 1 and counts['users'] == 0 and counts['unknown_projects'] == 1


def test_new_user_in_old_project_and_existing_user_in_new_workflow(tmp_path):
    store = UsageFirstSeenStore(tmp_path)
    store.bootstrap([])
    old = {'repository': 'repo', 'feature': 'old', 'entries': [{'command': 'apply', 'actor': {'user_name': 'Existing'}}]}
    store.observe([old], now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    today = datetime(2026, 9, 10, tzinfo=timezone.utc)
    old['entries'].append({'command': 'apply', 'actor': {'user_name': 'New'}})
    new = {**old, 'feature': 'new', 'entries': [old['entries'][0]]}
    store.observe([old, new], now=today)
    assert _recent_growth_counts([new], now=today, first_seen=store.snapshot())['users'] == 0
    counts = _recent_growth_counts([old], now=today, first_seen=store.snapshot())
    assert counts['projects'] == 0 and counts['users'] == 1
