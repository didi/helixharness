"""Persistent operator exclusions for eval Features still present in Git facts."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

try:
    import pysqlite3 as sqlite3
except ImportError:  # pragma: no cover - standard Python fallback
    import sqlite3


def default_feature_exclusion_path() -> Path:
    """Return the operator-owned runtime database path."""

    configured = os.environ.get("HELIXHARNESS_PLATFORM_FEATURE_EXCLUSION_DB_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent / ".cache" / "feature_exclusions.db"


class FeatureExclusionStore:
    """Store exact repository/Feature exclusions without rewriting eval-log history."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path or default_feature_exclusion_path()).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS feature_exclusions (
                    repository TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    excluded_at TEXT NOT NULL,
                    PRIMARY KEY (repository, feature)
                )
                """
            )

    def active_pairs(self) -> set[tuple[str, str]]:
        """Return exact repository/Feature pairs excluded from all consumers."""

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT repository, feature FROM feature_exclusions ORDER BY repository, feature"
            ).fetchall()
        return {(str(repository), str(feature)) for repository, feature in rows}

    def exclude(self, repository: str, feature: str, *, reason: str) -> None:
        """Create or refresh one idempotent operator exclusion."""

        repository = _identity(repository, "repository")
        feature = _identity(feature, "feature")
        reason = reason.strip()
        if not reason:
            raise ValueError("reason must be non-empty")
        excluded_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            connection.execute(
                """
                INSERT INTO feature_exclusions (repository, feature, reason, excluded_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repository, feature) DO UPDATE SET
                    reason = excluded.reason,
                    excluded_at = excluded.excluded_at
                """,
                (repository, feature, reason[:1000], excluded_at),
            )

    def restore(self, repository: str, feature: str) -> bool:
        """Remove one exclusion without changing the underlying Git fact."""

        with sqlite3.connect(self.database_path, timeout=30) as connection:
            changed = connection.execute(
                "DELETE FROM feature_exclusions WHERE repository = ? AND feature = ?",
                (_identity(repository, "repository"), _identity(feature, "feature")),
            ).rowcount
        return changed == 1


def _identity(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 255:
        raise ValueError(f"{label} must be 1-255 characters")
    return normalized
