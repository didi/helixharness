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

    def list_feedback(self, *, category: str = "", limit: int = 20, offset: int = 0) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM feedback_reports WHERE (? = '' OR category = ?) "
                "ORDER BY created_at DESC, feedback_id LIMIT ? OFFSET ?",
                (category, category, limit, offset),
            ).fetchall()
        return [json.loads(row[0])["document"] for row in rows]

    def count(self, category: str = "") -> int:
        with self._connect() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM feedback_reports WHERE (? = '' OR category = ?)", (category, category)
            ).fetchone()[0]

    def get(self, feedback_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM feedback_reports WHERE feedback_id = ?", (feedback_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None
