"""Persist explicit feedback independently of workflow telemetry consent."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from eval.json_schema import SchemaValidationError, validate

FEEDBACK_SCHEMA = "helixharness-feedback/1.0"


class FeedbackStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.schema = json.loads((Path(__file__).parent / "schemas/feedback-schema-v1.json").read_text())
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS feedback_reports (
                feedback_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                category TEXT NOT NULL, payload_json TEXT NOT NULL)""")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def ingest(self, item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict) or item.get("schema") != FEEDBACK_SCHEMA:
            return {"status": "rejected", "error": {"code": "unknown_schema"}}
        document = item.get("document")
        try:
            validate(document, self.schema)
        except SchemaValidationError as exc:
            return {"status": "rejected", "error": {"code": "schema_violation", "message": str(exc)}}
        if item.get("feedback_id") != document["feedback_id"]:
            return {"status": "rejected", "error": {"code": "feedback_id_mismatch"}}
        transcript = item.get("transcript_md", "")
        if not isinstance(transcript, str) or len(transcript.encode()) > 512 * 1024:
            return {"status": "rejected", "error": {"code": "invalid_transcript"}}
        stats = item.get("transcript_stats", {})
        if not isinstance(stats, dict):
            return {"status": "rejected", "error": {"code": "invalid_transcript_stats"}}
        payload = {"schema": FEEDBACK_SCHEMA, "feedback_id": document["feedback_id"],
                   "document": document, "transcript_md": transcript, "transcript_stats": stats}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO feedback_reports VALUES (?, ?, ?, ?)",
                (document["feedback_id"], document["created_at"], document["category"], encoded),
            ).rowcount
            if not inserted:
                previous = connection.execute(
                    "SELECT payload_json FROM feedback_reports WHERE feedback_id = ?",
                    (document["feedback_id"],),
                ).fetchone()[0]
                if previous != encoded:
                    return {"status": "rejected", "error": {"code": "feedback_id_conflict"}}
                return {"status": "duplicate"}
        return {"status": "accepted"}

    def _all_documents(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM feedback_reports ORDER BY created_at DESC, feedback_id"
            ).fetchall()
        return [json.loads(row[0])["document"] for row in rows]

    @staticmethod
    def _matches_filters(document: dict, filters: dict[str, str]) -> bool:
        for field, value in filters.items():
            if document.get(field) != value:
                return False
        return True

    def list_feedback(
        self, *,
        category: str = "", project: str = "", plugin_version: str = "", feature: str = "",
        limit: int = 20, offset: int = 0,
    ) -> list[dict]:
        filters = {key: value for key, value in {
            "category": category, "project": project,
            "plugin_version": plugin_version, "feature": feature,
        }.items() if value}
        matched = [document for document in self._all_documents()
                   if self._matches_filters(document, filters)]
        return matched[offset:offset + limit]

    def count(
        self, *,
        category: str = "", project: str = "", plugin_version: str = "", feature: str = "",
    ) -> int:
        filters = {key: value for key, value in {
            "category": category, "project": project,
            "plugin_version": plugin_version, "feature": feature,
        }.items() if value}
        return sum(1 for document in self._all_documents()
                   if self._matches_filters(document, filters))

    def stats(self) -> dict[str, Any]:
        """按分类/项目/插件版本聚合反馈数量，供列表页统计卡使用。"""
        from collections import Counter

        documents = self._all_documents()

        def group(field: str) -> list[dict[str, Any]]:
            counts = Counter(document.get(field) for document in documents if document.get(field))
            return [{"key": key, "count": count} for key, count in counts.most_common()]

        return {
            "total": len(documents),
            "by_category": group("category"),
            "by_project": group("project"),
            "by_plugin_version": group("plugin_version"),
        }

    def get(self, feedback_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM feedback_reports WHERE feedback_id = ?", (feedback_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None
