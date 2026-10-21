"""Durable platform admission history, independent of rebuildable telemetry indexes."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pysqlite3 as sqlite3
except ImportError:  # pragma: no cover
    import sqlite3


class UsageFirstSeenStore:
    """Keep first admissions through resyncs, updates, exclusions and index rebuilds.

    NULL dates identify pre-existing records whose admission time is unknown.
    This database is runtime history and must be backed up, not rebuilt.
    """

    def __init__(self, cache_dir: Path, *, database_path: Path | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.path = database_path or Path(os.environ.get('HELIXHARNESS_PLATFORM_USAGE_FIRST_SEEN_DB_PATH', str(self.cache_dir / 'usage_first_seen.db')))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS first_seen_projects (
                    repository TEXT NOT NULL, feature TEXT NOT NULL,
                    first_seen_at TEXT, source TEXT NOT NULL,
                    PRIMARY KEY(repository, feature)
                );
                CREATE TABLE IF NOT EXISTS first_seen_users (
                    user_name TEXT PRIMARY KEY, first_seen_at TEXT, source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS first_seen_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def bootstrap(self, roots: list[str], ledger_path: Path | None = None) -> None:
        """Seed cached projects once using retained discovery timestamps, never event times."""
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM first_seen_metadata WHERE key='bootstrapped'").fetchone():
                return
        from eval.usage_loader import _actor, _is_countable_user, _requirement_entries, discover_dashboard_projects

        ledger = ledger_path or Path(os.environ.get('HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH', str(self.cache_dir / 'stage_supplements.db')))
        discovered: dict[tuple[str, str], str] = {}
        if ledger.exists():
            with sqlite3.connect(f'{ledger.resolve().as_uri()}?mode=ro', uri=True) as source:
                exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ledger_entries'").fetchone()
                if exists:
                    for path, entry_id, timestamp in source.execute('SELECT source_path, entry_id, discovered_at FROM ledger_entries'):
                        discovered[(str(Path(path).resolve()), str(entry_id))] = str(timestamp)
        projects = []
        users: dict[str, str | None] = {}
        for project in discover_dashboard_projects(roots):
            repository = Path(project['project_dir']).name
            for log in project['features']:
                source_path = str(Path(log['_log_path']).resolve())
                dates = [time for (path, _), time in discovered.items() if path == source_path]
                first = min(dates) if dates else None
                projects.append((repository, Path(log['_log_path']).parent.name, first, 'ledger_discovery' if first else 'historical_unknown'))
                for entry in _requirement_entries(log):
                    name = _actor(entry)['user_name']
                    if not _is_countable_user(name):
                        continue
                    time = discovered.get((source_path, str(entry.get('entry_id') or '')))
                    if name not in users:
                        users[name] = time
                    elif users[name] is None or time is None:
                        users[name] = None
                    else:
                        users[name] = min(users[name], time)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute("SELECT 1 FROM first_seen_metadata WHERE key='bootstrapped'").fetchone():
                return
            connection.executemany('INSERT OR IGNORE INTO first_seen_projects VALUES (?, ?, ?, ?)', projects)
            connection.executemany('INSERT OR IGNORE INTO first_seen_users VALUES (?, ?, ?)',
                                   [(name, time, 'ledger_discovery' if time else 'historical_unknown') for name, time in users.items()])
            connection.execute("INSERT INTO first_seen_metadata VALUES ('bootstrapped', ?)", (datetime.now(timezone.utc).isoformat(),))

    def observe(self, features: list[dict], *, now: datetime | None = None) -> None:
        """Insert new admissions atomically; never update an existing first-seen date."""
        from eval.usage_loader import _actor, _is_countable_user, _requirement_entries

        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            for feature in features:
                connection.execute('INSERT OR IGNORE INTO first_seen_projects VALUES (?, ?, ?, ?)',
                                   (feature['repository'], feature.get('feature_identity', feature['feature']), timestamp, 'observed'))
                for entry in _requirement_entries(feature):
                    name = _actor(entry)['user_name']
                    if _is_countable_user(name):
                        connection.execute('INSERT OR IGNORE INTO first_seen_users VALUES (?, ?, ?)', (name, timestamp, 'observed'))

    def snapshot(self) -> dict[str, Any]:
        """Return only identities and admission timestamps for dashboard counting."""
        with self._connect() as connection:
            return {
                'projects': {(r['repository'], r['feature']): r['first_seen_at'] for r in connection.execute('SELECT * FROM first_seen_projects')},
                'users': {r['user_name']: r['first_seen_at'] for r in connection.execute('SELECT * FROM first_seen_users')},
            }
