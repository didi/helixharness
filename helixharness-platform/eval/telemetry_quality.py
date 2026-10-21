"""Consumer-side quality gate and rebuildable projection for eval-log telemetry.

The Git-synchronized ``eval-log.json`` remains the fixed-fact and activation
authority.  HelixHarness Ledger 1.0 dynamic facts come from exact-key, asynchronously received
Supplements.  This module independently classifies their materialized view
before it can contribute to dashboard metrics or automated evolution.  Its
SQLite database is an index rebuildable from the Git roots and Supplement inbox.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from eval.feature_exclusions import FeatureExclusionStore
from eval.stage_supplements import (
    LEDGER_SCHEMA,
    LEDGER_SCHEMA_VERSION,
    LinkedStageSupplement,
)
from eval.telemetry_materialization import (
    index_linked_supplements,
    materialize_eval_log,
    materialized_hash,
)

try:
    from pysqlite3 import dbapi2 as sqlite3
except ImportError:  # pragma: no cover - standard Python builds provide sqlite3
    import sqlite3


class QualityStatus(str, Enum):
    """Consumer confidence assigned to one telemetry document."""

    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"


@dataclass(frozen=True)
class QualityIssue:
    """One stable, machine-readable reason produced by the quality gate."""

    code: str
    field: str
    message: str
    blocking: bool


@dataclass(frozen=True)
class QualityAssessment:
    """Quality verdict for one complete eval-log document."""

    status: QualityStatus
    automation_eligible: bool
    issues: tuple[QualityIssue, ...]


@dataclass(frozen=True)
class IngestResult:
    """Summary of one projection refresh."""

    discovered: int
    valid: int
    partial: int
    invalid: int
    unchanged: int


_ENTRY_STATUSES = {"in_progress", "continued", "completed", "blocked", "partial"}
_TERMINAL_FINALIZATION = {"complete", "complete_with_unavailable"}


def _issue(code: str, field: str, message: str, *, blocking: bool) -> QualityIssue:
    return QualityIssue(code=code, field=field, message=message, blocking=blocking)


def _timestamp(value: object, field: str, issues: list[QualityIssue]) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        issues.append(_issue("INVALID_TIMESTAMP", field, "expected a UTC timestamp ending in Z", blocking=True))
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        issues.append(_issue("INVALID_TIMESTAMP", field, "timestamp cannot be parsed", blocking=True))
        return None


def assess_eval_log(data: object) -> QualityAssessment:
    """Admit only the current HelixHarness ledger contract."""
    if not isinstance(data, dict) or data.get("schema") != LEDGER_SCHEMA:
        return QualityAssessment(QualityStatus.INVALID, False,
            (_issue("UNSUPPORTED_SCHEMA", "schema", "expected current HelixHarness ledger", blocking=True),))
    return _assess_workflow_eval_log(data)


def _assess_workflow_eval_log(data: dict[str, Any]) -> QualityAssessment:
    """Apply the consumer Gate to the materialized HelixHarness Ledger 1.0 contract."""

    issues: list[QualityIssue] = []
    required_root = (
        "schema",
        "schema_version",
        "feature",
        "project",
        "requirement",
        "created_at",
        "last_updated",
        "status",
        "entries",
    )
    for key in required_root:
        if key not in data:
            issues.append(_issue("MISSING_FIELD", key, "required HelixHarness ledger field is missing", blocking=True))
    if issues:
        return QualityAssessment(QualityStatus.INVALID, False, tuple(issues))
    if data.get("schema_version") != LEDGER_SCHEMA_VERSION:
        issues.append(_issue("UNSUPPORTED_SCHEMA", "schema_version", f"only HelixHarness schema {LEDGER_SCHEMA_VERSION} is accepted", blocking=True))
    if not isinstance(data.get("feature"), str) or not str(data.get("feature")).strip():
        issues.append(_issue("INVALID_FEATURE", "feature", "feature must be non-empty", blocking=True))
    if data.get("status") not in {"in_progress", "archived"}:
        issues.append(_issue("INVALID_STATUS", "status", "unsupported publication status", blocking=True))
    created_at = _timestamp(data.get("created_at"), "created_at", issues)
    last_updated = _timestamp(data.get("last_updated"), "last_updated", issues)
    if created_at and last_updated and last_updated < created_at:
        issues.append(_issue("TIME_ORDER", "last_updated", "last_updated precedes created_at", blocking=True))
    collection_state = data.get("collection_state")
    if not isinstance(collection_state, dict):
        issues.append(_issue("INVALID_COLLECTION_STATE", "collection_state", "must be an object", blocking=True))
    elif collection_state.get("coverage") != "observed_from_start":
        issues.append(
            _issue(
                "PARTIAL_COVERAGE",
                "collection_state.coverage",
                "HelixHarness fixed ledger does not cover propose, apply, and archive",
                blocking=False,
            )
        )

    entries = data.get("entries")
    if not isinstance(entries, list):
        issues.append(_issue("ENTRIES_NOT_ARRAY", "entries", "entries must be an array", blocking=True))
        entries = []
    seen_entry_ids: set[str] = set()
    for index, entry in enumerate(entries):
        prefix = f"entries[{index}]"
        if not isinstance(entry, dict):
            issues.append(_issue("ENTRY_NOT_OBJECT", prefix, "entry must be an object", blocking=True))
            continue
        for key in (
            "entry_id",
            "helixharness_version",
            "command",
            "stage_kind",
            "attempt",
            "trigger",
            "platform",
            "session_hash",
            "started_at",
            "ended_at",
            "status",
            "actor",
            "checkpoint",
            "artifacts",
            "quality",
        ):
            if key not in entry:
                issues.append(_issue("MISSING_FIELD", f"{prefix}.{key}", "required field is missing", blocking=True))
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or not entry_id:
            issues.append(_issue("INVALID_ENTRY_ID", f"{prefix}.entry_id", "entry_id must be non-empty", blocking=True))
        elif entry_id in seen_entry_ids:
            issues.append(_issue("DUPLICATE_ENTRY_ID", f"{prefix}.entry_id", "entry_id is duplicated", blocking=True))
        else:
            seen_entry_ids.add(entry_id)
        if entry.get("stage_kind") not in {"requirement", "coding", "archive"}:
            issues.append(_issue("INVALID_STAGE_KIND", f"{prefix}.stage_kind", "unsupported HelixHarness stage", blocking=True))
        entry_status = entry.get("status")
        if entry_status not in _ENTRY_STATUSES | {"decision_needed", "failed"}:
            issues.append(_issue("INVALID_ENTRY_STATUS", f"{prefix}.status", "unsupported status", blocking=True))
        elif entry_status == "in_progress":
            issues.append(
                _issue(
                    "ENTRY_NOT_TERMINAL",
                    f"{prefix}.status",
                    "entry is not closed for automated evolution",
                    blocking=False,
                )
            )
        started_at = _timestamp(entry.get("started_at"), f"{prefix}.started_at", issues)
        ended_at = _timestamp(entry.get("ended_at"), f"{prefix}.ended_at", issues)
        if started_at and ended_at and ended_at < started_at:
            issues.append(_issue("TIME_ORDER", f"{prefix}.ended_at", "ended_at precedes started_at", blocking=True))
        session_hash = entry.get("session_hash")
        if not isinstance(session_hash, str) or not session_hash.startswith("session-"):
            issues.append(
                _issue("INVALID_SESSION_HASH", f"{prefix}.session_hash", "session hash is invalid", blocking=True)
            )
        if entry.get("telemetry_status") != "complete":
            issues.append(
                _issue(
                    "SUPPLEMENT_PENDING",
                    f"{prefix}.telemetry_status",
                    "async Supplement has not been associated",
                    blocking=False,
                )
            )
        else:
            _timestamp(entry.get("association_effective_at"), f"{prefix}.association_effective_at", issues)
            if not isinstance(entry.get("async_evidence"), dict):
                issues.append(
                    _issue("INVALID_ASYNC_EVIDENCE", f"{prefix}.async_evidence", "must be an object", blocking=True)
                )
        resource_usage = entry.get("resource_usage")
        if not isinstance(resource_usage, dict) or resource_usage.get("collection_status") != "collected":
            issues.append(
                _issue(
                    "RESOURCE_USAGE_UNAVAILABLE",
                    f"{prefix}.resource_usage.collection_status",
                    "resource metrics are not collected",
                    blocking=False,
                )
            )
        quality = entry.get("quality") if isinstance(entry.get("quality"), dict) else {}
        if quality.get("evidence_complete") is not True:
            issues.append(
                _issue(
                    "ASYNC_EVIDENCE_INCOMPLETE",
                    f"{prefix}.quality.evidence_complete",
                    "linked async evidence is required",
                    blocking=False,
                )
            )

    if data.get("status") != "archived":
        issues.append(
            _issue("NOT_FINALIZED", "status", "HelixHarness ledger is not archived for automated evolution", blocking=False)
        )
    finalization = data.get("finalization") if isinstance(data.get("finalization"), dict) else {}
    if data.get("status") == "archived" and finalization.get("status") != "complete":
        issues.append(
            _issue(
                "UNAVAILABLE_METRICS",
                "finalization.unavailable_metrics",
                "one or more async stage Supplements are unavailable",
                blocking=False,
            )
        )

    if any(item.blocking for item in issues):
        return QualityAssessment(QualityStatus.INVALID, False, tuple(issues))
    if issues:
        return QualityAssessment(QualityStatus.PARTIAL, False, tuple(issues))
    return QualityAssessment(QualityStatus.VALID, True, ())


class TelemetryQualityStore:
    """SQLite-backed, rebuildable projection of telemetry quality facts."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        """Initialize the current schema; imported database migrations are unsupported."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(telemetry_documents)")}
            if columns and "document_schema" not in columns:
                # This index is rebuildable; refresh re-reads immutable source ledgers.
                connection.execute("DROP TABLE IF EXISTS telemetry_entries")
                connection.execute("DROP TABLE telemetry_documents")
            connection.executescript(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))

    def refresh(
        self,
        roots: Iterable[str | Path],
        linked_supplements: Iterable[LinkedStageSupplement] = (),
    ) -> IngestResult:
        from eval.ledger_paths import project_ledger_files

        root_values = tuple(roots)
        exclusions = FeatureExclusionStore().active_pairs()
        linked_by_path = index_linked_supplements(linked_supplements)
        discovered = valid = partial = invalid = unchanged = 0
        seen_paths: set[str] = set()
        for root_value in root_values:
            root = Path(root_value).expanduser().resolve()
            source_commit, source_branch = _git_identity(root)
            for path in project_ledger_files(root):
                if (root.name, path.parent.name) in exclusions:
                    continue
                discovered += 1
                source_path = str(path.resolve())
                seen_paths.add(source_path)
                raw = path.read_bytes()
                records = linked_by_path.get(source_path, {})
                try:
                    decoded: object = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    decoded = None
                    file_hash = hashlib.sha256(raw).hexdigest()
                    assessment = QualityAssessment(
                        QualityStatus.INVALID,
                        False,
                        (_issue("INVALID_JSON", "$", str(exc), blocking=True),),
                    )
                else:
                    file_hash = (
                        materialized_hash(raw, records)
                        if isinstance(decoded, dict) and decoded.get("schema") == LEDGER_SCHEMA
                        else hashlib.sha256(raw).hexdigest()
                    )
                    decoded = materialize_eval_log(decoded, records) if isinstance(decoded, dict) else decoded
                    assessment = assess_eval_log(decoded)
                if self._has_hash(source_path, file_hash):
                    unchanged += 1
                    continue
                self._replace_document(
                    root=root,
                    path=Path(source_path),
                    file_hash=file_hash,
                    source_commit=source_commit,
                    source_branch=source_branch,
                    data=decoded,
                    assessment=assessment,
                )
                if assessment.status is QualityStatus.VALID:
                    valid += 1
                elif assessment.status is QualityStatus.PARTIAL:
                    partial += 1
                else:
                    invalid += 1
        self._remove_missing(seen_paths)
        return IngestResult(discovered, valid, partial, invalid, unchanged)

    def _has_hash(self, source_path: str, file_hash: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT file_hash, document_schema FROM telemetry_documents WHERE source_path = ?",
                (source_path,),
            ).fetchone()
        return (
            row is not None
            and row["file_hash"] == file_hash
            and row["document_schema"] in {LEDGER_SCHEMA, "unsupported"}
        )

    def _replace_document(
        self,
        *,
        root: Path,
        path: Path,
        file_hash: str,
        source_commit: str | None,
        source_branch: str | None,
        data: object,
        assessment: QualityAssessment,
    ) -> None:
        payload = data if isinstance(data, dict) else {}
        project = payload.get("project") if isinstance(payload.get("project"), dict) else {}
        finalization = payload.get("finalization") if isinstance(payload.get("finalization"), dict) else {}
        if payload.get("schema") == LEDGER_SCHEMA:
            document_schema = LEDGER_SCHEMA
        else:
            document_schema = "unsupported"
        ingested_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as connection:
            connection.execute("DELETE FROM telemetry_documents WHERE source_path = ?", (str(path),))
            connection.execute(
                """
                INSERT INTO telemetry_documents (
                    source_path, source_root, source_commit, source_branch, file_hash,
                    feature, project_name, document_schema, publication_status, finalization_status,
                    quality_status, automation_eligible, issues_json, last_updated, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(path),
                    str(root),
                    source_commit,
                    source_branch,
                    file_hash,
                    payload.get("feature"),
                    project.get("project_name") or project.get("name"),
                    document_schema,
                    payload.get("status"),
                    finalization.get("status"),
                    assessment.status.value,
                    int(assessment.automation_eligible),
                    json.dumps([asdict(item) for item in assessment.issues], ensure_ascii=False, sort_keys=True),
                    payload.get("last_updated"),
                    ingested_at,
                ),
            )
            if assessment.status is not QualityStatus.INVALID:
                for entry in payload.get("entries", []):
                    if not isinstance(entry, dict) or not isinstance(entry.get("entry_id"), str):
                        continue
                    session = entry.get("session_window") if isinstance(entry.get("session_window"), dict) else {}
                    quality = entry.get("quality") if isinstance(entry.get("quality"), dict) else {}
                    artifacts = entry.get("artifacts") if isinstance(entry.get("artifacts"), dict) else {}
                    async_evidence = entry.get("async_evidence") if isinstance(entry.get("async_evidence"), dict) else {}
                    checkpoint = entry.get("checkpoint") if isinstance(entry.get("checkpoint"), dict) else {}
                    fixed_evidence = _fixed_evolution_evidence(entry)
                    code_evidence = _commit_code_evidence(root, checkpoint)
                    connection.execute(
                        """
                        INSERT INTO telemetry_entries (
                            source_path, entry_id, helixharness_version, command, phase, stage_kind,
                            status, outcome_reason, started_at, ended_at, session_id, gate_result,
                            evidence_complete, admitted_at, fixed_evidence_json, async_evidence_json,
                            code_evidence_json, produced_files_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(path),
                            entry.get("entry_id"),
                            entry.get("helixharness_version"),
                            entry.get("command"),
                            entry.get("phase"),
                            entry.get("stage_kind"),
                            entry.get("status"),
                            entry.get("outcome_reason"),
                            entry.get("started_at"),
                            entry.get("ended_at"),
                            session.get("session_id"),
                            quality.get("gate_result"),
                            _optional_bool(quality.get("evidence_complete")),
                            entry.get("association_effective_at"),
                            json.dumps(fixed_evidence, ensure_ascii=False, sort_keys=True),
                            json.dumps(async_evidence, ensure_ascii=False, sort_keys=True),
                            json.dumps(code_evidence, ensure_ascii=False, sort_keys=True),
                            json.dumps(artifacts.get("produced_files") or [], ensure_ascii=False, sort_keys=True),
                        ),
                    )

    def _remove_missing(self, seen_paths: set[str]) -> None:
        """Project only current sources; immutable ledgers and receipts remain intact."""
        with self._connect() as connection:
            rows = connection.execute("SELECT source_path FROM telemetry_documents").fetchall()
            for row in rows:
                if row["source_path"] not in seen_paths:
                    connection.execute("DELETE FROM telemetry_documents WHERE source_path = ?", (row["source_path"],))

    def summary(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT quality_status, COUNT(*) AS count FROM telemetry_documents GROUP BY quality_status"
            ).fetchall()
            issue_rows = connection.execute(
                "SELECT issues_json FROM telemetry_documents WHERE quality_status != 'VALID'"
            ).fetchall()
            freshness = connection.execute("SELECT MAX(last_updated) AS value FROM telemetry_documents").fetchone()
            eligible = connection.execute(
                "SELECT COUNT(*) AS count FROM telemetry_documents "
                "WHERE quality_status = 'VALID' AND automation_eligible = 1 AND document_schema = ?",
                (LEDGER_SCHEMA,),
            ).fetchone()
            supported_total = connection.execute(
                "SELECT COUNT(*) AS count FROM telemetry_documents WHERE document_schema = ?",
                (LEDGER_SCHEMA,),
            ).fetchone()
        counts = {status.value: 0 for status in QualityStatus}
        for row in rows:
            counts[row["quality_status"]] = row["count"]
        issue_counts: dict[str, int] = {}
        for row in issue_rows:
            for issue in json.loads(row["issues_json"]):
                code = str(issue.get("code") or "UNKNOWN")
                issue_counts[code] = issue_counts.get(code, 0) + 1
        return {
            "total": sum(counts.values()),
            "supported_total": int(supported_total["count"] if supported_total else 0),
            "counts": counts,
            "automation_eligible": int(eligible["count"] if eligible else 0),
            "latest_event_at": freshness["value"] if freshness else None,
            "issue_counts": dict(sorted(issue_counts.items(), key=lambda item: (-item[1], item[0]))),
        }

    def automation_evidence(self) -> list[dict[str, Any]]:
        """Return sanitized facts that can seed automatic repair discovery."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.source_path, d.source_commit, d.file_hash, d.feature, d.project_name,
                       e.entry_id, e.helixharness_version, e.command, e.phase, e.stage_kind,
                       e.status, e.outcome_reason, e.started_at, e.ended_at, e.session_id,
                       e.gate_result, e.evidence_complete, e.admitted_at,
                       e.fixed_evidence_json, e.async_evidence_json, e.code_evidence_json,
                       e.produced_files_json
                  FROM telemetry_entries e
                  JOIN telemetry_documents d ON d.source_path = e.source_path
                 WHERE d.automation_eligible = 1
                   AND d.document_schema = ?
                 ORDER BY e.started_at, e.entry_id
                """,
                (LEDGER_SCHEMA,),
            ).fetchall()
        evidence: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["produced_files"] = json.loads(item.pop("produced_files_json"))
            item["fixed_evidence"] = json.loads(item.pop("fixed_evidence_json"))
            item["async_evidence"] = json.loads(item.pop("async_evidence_json"))
            item["code_evidence"] = json.loads(item.pop("code_evidence_json"))
            if item["evidence_complete"] is not None:
                item["evidence_complete"] = bool(item["evidence_complete"])
            evidence.append(item)
        return evidence

    def partial_rehearsal_evidence(self, file_hash: str) -> dict[str, Any]:
        """Return one explicitly selected terminal PARTIAL as non-automatic rehearsal input.

        This is intentionally separate from :meth:`automation_evidence`: PARTIAL
        documents never become automation eligible merely because an operator chose
        to exercise the downstream Candidate and Benchmark workflow.
        """

        normalized_hash = file_hash.strip()
        if not normalized_hash:
            raise ValueError("PARTIAL rehearsal requires an exact telemetry file hash")
        with self._connect() as connection:
            documents = connection.execute(
                """
                SELECT source_path, file_hash, feature, publication_status,
                       finalization_status, quality_status, issues_json, document_schema
                  FROM telemetry_documents
                 WHERE file_hash = ?
                """,
                (normalized_hash,),
            ).fetchall()
            if len(documents) != 1:
                raise ValueError("PARTIAL rehearsal hash must identify exactly one telemetry document")
            document = documents[0]
            if document["quality_status"] != QualityStatus.PARTIAL.value:
                raise ValueError("PARTIAL rehearsal only accepts a document currently classified PARTIAL")
            if document["document_schema"] != LEDGER_SCHEMA:
                raise ValueError("PARTIAL rehearsal only accepts HelixHarness telemetry")
            if document["publication_status"] != "archived":
                raise ValueError("PARTIAL rehearsal requires an archived telemetry document")
            if document["finalization_status"] not in _TERMINAL_FINALIZATION:
                raise ValueError("PARTIAL rehearsal requires terminal finalization")
            rows = connection.execute(
                """
                SELECT d.source_path, d.source_commit, d.file_hash, d.feature, d.project_name,
                       e.entry_id, e.helixharness_version, e.command, e.phase, e.stage_kind,
                       e.status, e.outcome_reason, e.started_at, e.ended_at, e.session_id,
                       e.gate_result, e.evidence_complete, e.admitted_at,
                       e.fixed_evidence_json, e.async_evidence_json, e.code_evidence_json,
                       e.produced_files_json
                  FROM telemetry_entries e
                  JOIN telemetry_documents d ON d.source_path = e.source_path
                 WHERE d.source_path = ?
                 ORDER BY e.started_at, e.entry_id
                """,
                (document["source_path"],),
            ).fetchall()
        evidence: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["produced_files"] = json.loads(item.pop("produced_files_json"))
            item["fixed_evidence"] = json.loads(item.pop("fixed_evidence_json"))
            item["async_evidence"] = json.loads(item.pop("async_evidence_json"))
            item["code_evidence"] = json.loads(item.pop("code_evidence_json"))
            if item["evidence_complete"] is not None:
                item["evidence_complete"] = bool(item["evidence_complete"])
            evidence.append(item)
        issues = json.loads(document["issues_json"])
        return {
            "admission": "PARTIAL_REHEARSAL",
            "file_hash": str(document["file_hash"]),
            "feature": str(document["feature"] or ""),
            "issue_codes": sorted(
                {str(item.get("code") or "UNKNOWN") for item in issues if isinstance(item, dict)}
            ),
            "evidence": evidence,
        }

    def invalid_source_paths(self) -> set[str]:
        """Return quarantined documents that must not enter dashboard aggregation."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT source_path FROM telemetry_documents WHERE quality_status = 'INVALID'"
            ).fetchall()
        return {str(row["source_path"]) for row in rows}


def _fixed_evolution_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    """Build a bounded fixed-fact package without copying raw source content."""

    checkpoint = entry.get("checkpoint") if isinstance(entry.get("checkpoint"), dict) else {}
    artifacts = entry.get("artifacts") if isinstance(entry.get("artifacts"), dict) else {}
    quality = entry.get("quality") if isinstance(entry.get("quality"), dict) else {}
    workflow = entry.get("workflow") if isinstance(entry.get("workflow"), dict) else {}
    invocation = entry.get("invocation") if isinstance(entry.get("invocation"), dict) else {}
    usage = entry.get("resource_usage") if isinstance(entry.get("resource_usage"), dict) else {}
    produced_files = artifacts.get("produced_files") if isinstance(artifacts.get("produced_files"), list) else []
    return {
        "stage": {
            key: entry.get(key)
            for key in (
                "command",
                "phase",
                "stage_kind",
                "status",
                "outcome_reason",
                "started_at",
                "ended_at",
            )
            if entry.get(key) is not None
        },
        "checkpoint": {
            key: checkpoint.get(key)
            for key in ("base_commit", "head_commit", "checkpoint_commit")
            if checkpoint.get(key) is not None
        },
        "artifacts": [str(item)[:500] for item in produced_files[:50]],
        "quality": {
            key: quality.get(key)
            for key in (
                "gate_result",
                "verification",
                "standards_verdict",
                "spec_verdict",
                "review_findings_count",
                "must_fix_rounds",
                "evidence_complete",
            )
            if quality.get(key) is not None
        },
        "workflow": {
            key: workflow.get(key)
            for key in (
                "previous_entry_id",
                "continuation_of_entry_id",
                "stage_repeated",
                "order_valid",
                "revises_previous_stage",
                "revised_stage_kind",
                "revision_reason",
            )
            if workflow.get(key) is not None
        },
        "invocation": {
            key: invocation.get(key)
            for key in ("platform", "surface", "trigger", "mode", "loop", "tdd")
            if invocation.get(key) is not None
        },
        "resource_usage": _bounded_fixed_usage(usage),
    }


def _bounded_fixed_usage(usage: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: usage.get(key)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "total_tokens",
            "model",
            "collection_status",
        )
        if usage.get(key) is not None
    }
    models = usage.get("models") if isinstance(usage.get("models"), list) else []
    if models:
        result["models"] = [
            {
                key: item.get(key)
                for key in (
                    "model",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
                if item.get(key) is not None
            }
            for item in models[:20]
            if isinstance(item, dict)
        ]
    return result


def _commit_code_evidence(root: Path, checkpoint: dict[str, Any]) -> dict[str, Any]:
    """Resolve checkpoint commits into bounded diff metadata for semantic analysis."""

    head_value = checkpoint.get("head_commit") or checkpoint.get("checkpoint_commit")
    head = _resolve_git_commit(root, head_value)
    if head is None:
        return {"status": "unavailable", "reason": "checkpoint_commit_missing_or_unresolvable"}
    base = _resolve_git_commit(root, checkpoint.get("base_commit"))
    if base is None:
        base = _resolve_git_commit(root, f"{head}^")
    if base is None:
        return {
            "status": "unavailable",
            "reason": "base_commit_missing_or_unresolvable",
            "head_commit": head,
        }
    numstat = _run_git(root, "diff", "--numstat", base, head, "--")
    shortstat = _run_git(root, "diff", "--shortstat", base, head, "--")
    if numstat is None:
        return {
            "status": "unavailable",
            "reason": "commit_diff_unavailable",
            "base_commit": base,
            "head_commit": head,
        }
    files: list[dict[str, Any]] = []
    for line in numstat.splitlines()[:100]:
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        files.append(
            {
                "path": path[:500],
                "added": int(added) if added.isdigit() else None,
                "deleted": int(deleted) if deleted.isdigit() else None,
            }
        )
    return {
        "status": "collected",
        "base_commit": base,
        "head_commit": head,
        "summary": (shortstat or "no changed lines")[:1000],
        "changed_files": files,
        "files_truncated": len(numstat.splitlines()) > len(files),
    }


def _resolve_git_commit(root: Path, value: object) -> str | None:
    raw = str(value or "").strip()
    candidate = raw[:-1] if raw.endswith("^") else raw
    if not (7 <= len(candidate) <= 64) or any(character not in "0123456789abcdefABCDEF" for character in candidate):
        return None
    suffix = "^" if raw.endswith("^") else ""
    return _run_git(root, "rev-parse", "--verify", f"{candidate}{suffix}^{{commit}}")


def _run_git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = completed.stdout.strip()
    return value if completed.returncode == 0 else None


def _optional_bool(value: object) -> int | None:
    if value is None:
        return None
    return int(bool(value))


def _git_identity(root: Path) -> tuple[str | None, str | None]:
    def run(*args: str) -> str | None:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        value = completed.stdout.strip()
        return value if completed.returncode == 0 and value else None

    return run("rev-parse", "HEAD"), run("branch", "--show-current")
