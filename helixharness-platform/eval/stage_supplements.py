"""Persistent ingestion and Git-ledger linkage for stage supplements.

Supplement data can arrive before its fixed ``eval-log.json`` evidence.  This
store keeps those uploads inert until an exact entry, requirement, and feature
match is discovered in the configured Git roots.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from eval.runtime_health import RUNTIME_HEALTH_V2_SCHEMA, validate_v2

try:
    from pysqlite3 import dbapi2 as sqlite3
except ImportError:  # pragma: no cover - standard Python builds provide sqlite3
    import sqlite3


SUPPLEMENT_SCHEMA = "helixharness-stage-supplement/1.0"
EVIDENCE_SCHEMA = "helixharness-evolution-evidence/1.0"
LEDGER_SCHEMA_VERSION = "1.0"
LEDGER_SCHEMA = f"helixharness-eval-ledger/{LEDGER_SCHEMA_VERSION}"
RUNTIME_HEALTH_SCHEMA = RUNTIME_HEALTH_V2_SCHEMA
RUNTIME_HEALTH_SCHEMAS = {RUNTIME_HEALTH_SCHEMA}
LINKED_RAW_RETENTION_DAYS = 7
UNLINKED_RETENTION_DAYS = 30

_REQUIRED_TEXT_FIELDS = (
    "supplement_id",
    "entry_id",
    "requirement_id",
    "requirement_name",
    "feature",
    "session_hash",
    "platform",
)



class StageSupplementError(ValueError):
    """Permanent validation or identity error for one supplement."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StageSupplementStorageError(RuntimeError):
    """Transient persistence error that a client may retry."""


@dataclass(frozen=True)
class LinkedStageSupplement:
    """Newest async revision joined to the fixed Git ledger that activates it."""

    source_path: str
    effective_at: str
    received_at: str
    payload_hash: str
    payload: dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _retention_cutoff(now: str, days: int) -> str:
    parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
    return (parsed - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _snapshot_payload(item: dict[str, Any]) -> dict[str, Any]:
    """Keep the bounded structured facts consumed by the dashboard."""

    snapshot = {
        key: item[key]
        for key in (
            "schema",
            "supplement_id",
            "revision",
            "entry_id",
            "requirement_id",
            "requirement_name",
            "feature",
            "platform",
            "collected_at",
        )
        if key in item
    }
    usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
    snapshot["usage"] = {
        key: usage[key]
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
            "model",
        )
        if key in usage
    }
    models = usage.get("models") if isinstance(usage.get("models"), list) else []
    if models:
        snapshot["usage"]["models"] = [
            {
                key: model[key]
                for key in (
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
                if key in model
            }
            for model in models[:20]
            if isinstance(model, dict)
        ]

    execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
    snapshot["execution"] = {
        key: execution[key]
        for key in (
            "tool_call_count",
            "tool_error_count",
            "subagent_spawn_count",
            "child_agent_scope",
            "persistence_latency_ms",
            "record_write_count",
            "collection_status",
        )
        if key in execution
    }
    context_reads = execution.get("context_reads")
    if isinstance(context_reads, dict):
        snapshot["execution"]["context_reads"] = {
            key: context_reads[key]
            for key in ("harness", "knowledge", "specs", "hooks")
            if key in context_reads
        }

    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    snapshot["evidence"] = {"schema": EVIDENCE_SCHEMA}
    for field, allowed in (
        ("interaction_summary", ("user_turns", "assistant_messages")),
        ("coverage", ("in_window", "retained")),
        ("tools", ("total", "failed")),
    ):
        value = evidence.get(field)
        if isinstance(value, dict):
            snapshot["evidence"][field] = {key: value[key] for key in allowed if key in value}
    failed_tools = evidence.get("failed_tools") if isinstance(evidence.get("failed_tools"), list) else []
    if failed_tools:
        snapshot["evidence"]["failed_tools"] = [
            {"name": str(tool.get("name") or "unknown")[:128]}
            for tool in failed_tools[:20]
            if isinstance(tool, dict)
        ]

    trace = item.get("workflow_trace") if isinstance(item.get("workflow_trace"), dict) else {}
    sanitized_trace: dict[str, Any] = {}
    if trace.get("status") is not None:
        sanitized_trace["status"] = str(trace["status"])[:64]
    gates = trace.get("gates") if isinstance(trace.get("gates"), list) else []
    if gates:
        sanitized_trace["gates"] = [
            {
                key: str(gate[key])[:128]
                for key in ("name", "gate", "id", "status")
                if gate.get(key) is not None
            }
            for gate in gates[:20]
            if isinstance(gate, dict)
        ]
    fallbacks = trace.get("fallbacks") if isinstance(trace.get("fallbacks"), list) else []
    if fallbacks:
        sanitized_trace["fallbacks"] = [
            {
                key: str(fallback[key])[:128]
                for key in ("name", "code", "reason_code", "status")
                if fallback.get(key) is not None
            }
            if isinstance(fallback, dict)
            else str(fallback)[:128]
            for fallback in fallbacks[:20]
        ]
    snapshot["workflow_trace"] = sanitized_trace
    return snapshot


def _validated_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise StageSupplementError("invalid_timestamp", f"{field} must be a UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StageSupplementError("invalid_timestamp", f"{field} is not a valid timestamp") from exc
    return value


def validate_stage_supplement(payload: object) -> dict[str, Any]:
    """Validate the stable public contract while preserving extension fields."""

    if not isinstance(payload, dict):
        raise StageSupplementError("item_not_object", "supplement item must be an object")
    if payload.get("schema") != SUPPLEMENT_SCHEMA:
        raise StageSupplementError("unsupported_schema", f"schema must be {SUPPLEMENT_SCHEMA}")

    for field in _REQUIRED_TEXT_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise StageSupplementError("invalid_field", f"{field} must be a non-empty string")
        if len(value) > 512:
            raise StageSupplementError("invalid_field", f"{field} exceeds 512 characters")

    supplement_id = str(payload["supplement_id"])
    if not supplement_id.startswith("supp_"):
        raise StageSupplementError("invalid_supplement_id", "supplement_id must start with supp_")

    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise StageSupplementError("invalid_revision", "revision must be a positive integer")
    supersedes = payload.get("supersedes")
    if supersedes is not None and (not isinstance(supersedes, str) or not supersedes.strip()):
        raise StageSupplementError("invalid_supersedes", "supersedes must be null or a non-empty string")

    if "collected_at" in payload:
        _validated_timestamp(payload.get("collected_at"), "collected_at")
    if not isinstance(payload.get("usage"), dict):
        raise StageSupplementError("invalid_usage", "usage must be an object")
    evidence = payload.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("schema") != EVIDENCE_SCHEMA:
        raise StageSupplementError("invalid_evidence", f"evidence.schema must be {EVIDENCE_SCHEMA}")
    return dict(payload)


def validate_runtime_health_event(payload: object) -> dict[str, Any]:
    """Validate the current runtime health contract."""
    if not isinstance(payload, dict) or payload.get("schema") != RUNTIME_HEALTH_SCHEMA:
        raise StageSupplementError("unsupported_schema", f"schema must be {RUNTIME_HEALTH_SCHEMA}")
    try:
        return validate_v2(payload)
    except ValueError as exc:
        raise StageSupplementError("invalid_runtime_health", str(exc)) from exc


class StageSupplementStore:
    """SQLite-backed inbox and linkage projection for asynchronous evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ledger_entries (
                    entry_id TEXT NOT NULL,
                    requirement_id TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    discovered_at TEXT NOT NULL,
                    PRIMARY KEY (entry_id, requirement_id, feature)
                );
                CREATE TABLE IF NOT EXISTS stage_supplements (
                    supplement_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    entry_id TEXT NOT NULL,
                    requirement_id TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    linked INTEGER NOT NULL DEFAULT 0,
                    received_at TEXT NOT NULL,
                    linked_at TEXT,
                    PRIMARY KEY (supplement_id, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_stage_supplements_link
                    ON stage_supplements(entry_id, requirement_id, feature, linked);
                CREATE INDEX IF NOT EXISTS idx_stage_supplements_active
                    ON stage_supplements(entry_id, revision DESC, received_at DESC);
                CREATE TABLE IF NOT EXISTS final_stage_supplements (
                    entry_id TEXT NOT NULL,
                    requirement_id TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    supplement_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    effective_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    snapshot_hash TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    finalized_at TEXT NOT NULL,
                    PRIMARY KEY (entry_id, requirement_id, feature),
                    UNIQUE (supplement_id, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_final_stage_supplements_source
                    ON final_stage_supplements(source_path, entry_id);
                CREATE TABLE IF NOT EXISTS runtime_health_events (
                    event_id TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at TEXT NOT NULL
                );
                """
            )

    def ingest(self, payload: object) -> dict[str, object]:
        """Persist one supplement and return an idempotent item acknowledgement."""

        item = validate_stage_supplement(payload)
        canonical = _canonical_json(item)
        payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        supplement_id = str(item["supplement_id"])
        revision = int(item["revision"])
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                final_by_upload = connection.execute(
                    """
                    SELECT entry_id, requirement_id, feature, payload_hash
                    FROM final_stage_supplements
                    WHERE supplement_id = ? AND revision = ?
                    """,
                    (supplement_id, revision),
                ).fetchone()
                if final_by_upload is not None:
                    if str(final_by_upload["payload_hash"]) != payload_hash:
                        raise StageSupplementError(
                            "identity_conflict",
                            "supplement_id and revision already identify different content",
                        )
                    return {
                        "supplement_id": supplement_id,
                        "status": "duplicate",
                        "linked": True,
                    }

                final_by_entry = connection.execute(
                    """
                    SELECT supplement_id, revision, payload_hash
                    FROM final_stage_supplements
                    WHERE entry_id = ? AND requirement_id = ? AND feature = ?
                    """,
                    (item["entry_id"], item["requirement_id"], item["feature"]),
                ).fetchone()
                if final_by_entry is not None:
                    raise StageSupplementError(
                        "finalized_entry",
                        "the matching eval-log entry already has final telemetry",
                    )

                existing = connection.execute(
                    """
                    SELECT payload_hash, linked
                    FROM stage_supplements
                    WHERE supplement_id = ? AND revision = ?
                    """,
                    (supplement_id, revision),
                ).fetchone()
                if existing is not None:
                    if str(existing["payload_hash"]) != payload_hash:
                        raise StageSupplementError(
                            "identity_conflict",
                            "supplement_id and revision already identify different content",
                        )
                    return {
                        "supplement_id": supplement_id,
                        "status": "duplicate",
                        "linked": bool(existing["linked"]),
                    }

                ledger = connection.execute(
                    """
                    SELECT source_path, discovered_at FROM ledger_entries
                    WHERE entry_id = ? AND requirement_id = ? AND feature = ?
                    """,
                    (item["entry_id"], item["requirement_id"], item["feature"]),
                ).fetchone()
                linked = ledger is not None
                now = _utc_now()
                connection.execute(
                    """
                    INSERT INTO stage_supplements (
                        supplement_id, revision, entry_id, requirement_id, feature,
                        payload_hash, payload_json, linked, received_at, linked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        supplement_id,
                        revision,
                        item["entry_id"],
                        item["requirement_id"],
                        item["feature"],
                        payload_hash,
                        canonical,
                        int(linked),
                        now,
                        now if linked else None,
                    ),
                )
                if ledger is not None:
                    self._insert_final_snapshot(
                        connection,
                        item,
                        payload_hash=payload_hash,
                        received_at=now,
                        source_path=str(ledger["source_path"]),
                        effective_at=str(ledger["discovered_at"]),
                        finalized_at=now,
                    )
        except StageSupplementError:
            raise
        except sqlite3.Error as exc:
            raise StageSupplementStorageError("stage supplement persistence failed") from exc
        return {"supplement_id": supplement_id, "status": "accepted", "linked": linked}

    @staticmethod
    def _insert_final_snapshot(
        connection: sqlite3.Connection,
        item: dict[str, Any],
        *,
        payload_hash: str,
        received_at: str,
        source_path: str,
        effective_at: str,
        finalized_at: str,
    ) -> None:
        snapshot_json = _canonical_json(_snapshot_payload(item))
        connection.execute(
            """
            INSERT INTO final_stage_supplements (
                entry_id, requirement_id, feature, supplement_id, revision,
                source_path, effective_at, received_at, payload_hash,
                snapshot_hash, snapshot_json, finalized_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["entry_id"],
                item["requirement_id"],
                item["feature"],
                item["supplement_id"],
                item["revision"],
                source_path,
                effective_at,
                received_at,
                payload_hash,
                hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
                snapshot_json,
                finalized_at,
            ),
        )

    def ingest_runtime_health(self, payload: object) -> dict[str, object]:
        """Persist immutable current health events and reject identity conflicts."""

        item = validate_runtime_health_event(payload)
        canonical = _canonical_json(item)
        event_id = str(item["event_id"])
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT payload_hash FROM runtime_health_events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != hashlib.sha256(canonical.encode("utf-8")).hexdigest():
                        raise StageSupplementError("identity_conflict", "event_id already exists with different content")
                    return {"event_id": event_id, "status": "duplicate"}
                connection.execute(
                    """
                    INSERT INTO runtime_health_events (
                        event_id, payload_hash, payload_json, received_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                        canonical,
                        _utc_now(),
                    ),
                )
        except sqlite3.Error as exc:
            raise StageSupplementStorageError("runtime health persistence failed") from exc
        return {"event_id": event_id, "status": "accepted"}

    def refresh_links(self, roots: Iterable[str | Path]) -> int:
        """Discover current ledgers and finalize each matching entry at most once."""

        discovered_at = _utc_now()
        ledger_entries = list(_iter_ledger_entries(roots))
        finalized = 0
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing_discovery = {
                    (str(row["entry_id"]), str(row["requirement_id"]), str(row["feature"])): str(
                        row["discovered_at"]
                    )
                    for row in connection.execute(
                        "SELECT entry_id, requirement_id, feature, discovered_at FROM ledger_entries"
                    ).fetchall()
                }
                connection.execute("DELETE FROM ledger_entries")
                connection.executemany(
                    """
                    INSERT OR REPLACE INTO ledger_entries (
                        entry_id, requirement_id, feature, source_path, discovered_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            entry_id,
                            requirement_id,
                            feature,
                            source_path,
                            existing_discovery.get((entry_id, requirement_id, feature), discovered_at),
                        )
                        for entry_id, requirement_id, feature, source_path in ledger_entries
                    ],
                )
                candidates = connection.execute(
                    """
                    SELECT ss.supplement_id, ss.revision, ss.entry_id,
                           ss.requirement_id, ss.feature, ss.payload_hash,
                           ss.payload_json, ss.received_at,
                           le.source_path, le.discovered_at
                    FROM stage_supplements ss
                    JOIN ledger_entries le
                      ON le.entry_id = ss.entry_id
                     AND le.requirement_id = ss.requirement_id
                     AND le.feature = ss.feature
                    LEFT JOIN final_stage_supplements fs
                      ON fs.entry_id = ss.entry_id
                     AND fs.requirement_id = ss.requirement_id
                     AND fs.feature = ss.feature
                    WHERE fs.entry_id IS NULL
                    ORDER BY ss.entry_id, ss.requirement_id, ss.feature,
                             ss.revision DESC, ss.received_at DESC, ss.supplement_id DESC
                    """
                ).fetchall()
                seen: set[tuple[str, str, str]] = set()
                for row in candidates:
                    identity = (str(row["entry_id"]), str(row["requirement_id"]), str(row["feature"]))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    item = json.loads(str(row["payload_json"]))
                    self._insert_final_snapshot(
                        connection,
                        item,
                        payload_hash=str(row["payload_hash"]),
                        received_at=str(row["received_at"]),
                        source_path=str(row["source_path"]),
                        effective_at=str(row["discovered_at"]),
                        finalized_at=discovered_at,
                    )
                    connection.execute(
                        """
                        UPDATE stage_supplements
                        SET linked = 1, linked_at = ?
                        WHERE supplement_id = ? AND revision = ?
                        """,
                        (row["discovered_at"], row["supplement_id"], row["revision"]),
                    )
                    finalized += 1
        except (json.JSONDecodeError, sqlite3.Error) as exc:
            raise StageSupplementStorageError("stage supplement linkage refresh failed") from exc
        self.cleanup_expired(now=discovered_at)
        return finalized

    def cleanup_expired(self, *, now: str | None = None) -> dict[str, int]:
        """Delete raw payloads after their grace periods without deleting final snapshots."""

        current = now or _utc_now()
        linked_cutoff = _retention_cutoff(current, LINKED_RAW_RETENTION_DAYS)
        unlinked_cutoff = _retention_cutoff(current, UNLINKED_RETENTION_DAYS)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                linked_cursor = connection.execute(
                    """
                    DELETE FROM stage_supplements
                    WHERE linked = 1
                      AND EXISTS (
                          SELECT 1
                          FROM final_stage_supplements fs
                          WHERE fs.supplement_id = stage_supplements.supplement_id
                            AND fs.revision = stage_supplements.revision
                            AND fs.finalized_at <= ?
                      )
                    """,
                    (linked_cutoff,),
                )
                unlinked_cursor = connection.execute(
                    """
                    DELETE FROM stage_supplements
                    WHERE linked = 0 AND received_at <= ?
                    """,
                    (unlinked_cutoff,),
                )
        except sqlite3.Error as exc:
            raise StageSupplementStorageError("stage supplement retention cleanup failed") from exc
        return {
            "linked_raw": max(int(linked_cursor.rowcount), 0),
            "unlinked": max(int(unlinked_cursor.rowcount), 0),
        }

    def active_linked_records(self) -> list[LinkedStageSupplement]:
        """Return immutable, sanitized final snapshots for materialization."""

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT source_path, effective_at, received_at, payload_hash, snapshot_json
                    FROM final_stage_supplements
                    ORDER BY source_path, snapshot_json
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise StageSupplementStorageError("stage supplement query failed") from exc
        return [
            LinkedStageSupplement(
                source_path=str(row["source_path"]),
                effective_at=str(row["effective_at"]),
                received_at=str(row["received_at"]),
                payload_hash=str(row["payload_hash"]),
                payload=json.loads(str(row["snapshot_json"])),
            )
            for row in rows
        ]

    def active_linked(self) -> list[dict[str, Any]]:
        """Return the final sanitized snapshot for each fixed ledger entry."""

        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT snapshot_json
                    FROM final_stage_supplements
                    ORDER BY snapshot_json
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise StageSupplementStorageError("stage supplement query failed") from exc
        return [json.loads(str(row["snapshot_json"])) for row in rows]


def _iter_ledger_entries(roots: Iterable[str | Path]) -> Iterable[tuple[str, str, str, str]]:
    from eval.ledger_paths import project_ledger_files

    for root_value in roots:
        root = Path(root_value)
        for path in project_ledger_files(root, recursive=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("schema") != LEDGER_SCHEMA:
                continue
            requirement = payload.get("requirement")
            entries = payload.get("entries")
            feature = payload.get("feature")
            if not isinstance(requirement, dict) or not isinstance(entries, list) or not isinstance(feature, str):
                continue
            requirement_id = requirement.get("id")
            if not isinstance(requirement_id, str) or not requirement_id or not feature:
                continue
            for entry in entries:
                entry_id = entry.get("entry_id") if isinstance(entry, dict) else None
                if isinstance(entry_id, str) and entry_id:
                    yield entry_id, requirement_id, feature, str(path.resolve())
