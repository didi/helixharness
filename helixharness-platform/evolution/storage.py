"""Private SQLite persistence for evolution orchestration."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from pysqlite3 import dbapi2 as sqlite3
except ImportError:  # pragma: no cover - standard Python builds provide sqlite3
    import sqlite3
from typing import Any

from evolution.analysis_context import AnalysisContextSnapshot
from evolution.models import CandidateVersion, CycleOutcome, ExperimentState, Hypothesis, HypothesisKind


class EvolutionStore:
    """Owns evolution state without querying eval or benchmark databases."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        """Initialize the current schema; imported database migrations are unsupported."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))

    def source_digest_exists(self, source_digest: str) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM cycles WHERE source_digest = ?", (source_digest,)
            ).fetchone() is not None

    def cycle_id_for_source_digest(self, source_digest: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cycle_id FROM cycles WHERE source_digest = ?",
                (source_digest,),
            ).fetchone()
        return str(row["cycle_id"]) if row else None

    def cycle_for_source_digest(self, source_digest: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM cycles WHERE source_digest = ?",
                (source_digest,),
            ).fetchone()
        return dict(row) if row else None

    def known_evidence_ids(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT ce.evidence_id
                  FROM cycle_evidence ce
                  JOIN cycles c ON c.cycle_id = ce.cycle_id
                 WHERE c.state NOT IN ('FAILED', 'RUNNING')
                """
            ).fetchall()
        return {str(row["evidence_id"]) for row in rows}

    def record_cycle_evidence(self, cycle_id: str, evidence_ids: tuple[str, ...]) -> None:
        with self.connect() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO cycle_evidence(cycle_id, evidence_id) VALUES (?, ?)",
                ((cycle_id, evidence_id) for evidence_id in evidence_ids),
            )

    def latest_collection_at(self) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(value) AS value
                  FROM (
                        SELECT MAX(started_at) AS value
                          FROM cycles
                         WHERE cycle_id NOT GLOB 'cycle_bench_*'
                        UNION ALL
                        SELECT checked_at AS value
                          FROM collection_watermark
                         WHERE singleton = 1
                       )
                """
            ).fetchone()
        return str(row["value"]) if row and row["value"] else None

    def record_collection_check(self, checked_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO collection_watermark(singleton, checked_at)
                VALUES (1, ?)
                ON CONFLICT(singleton) DO UPDATE SET checked_at = excluded.checked_at
                """,
                (checked_at,),
            )

    def begin_cycle(self, cycle_id: str, source_digest: str, started_at: str, evidence_count: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO cycles(cycle_id, source_digest, started_at, state, reason, evidence_count)
                VALUES (?, ?, ?, 'RUNNING', '', ?)
                """,
                (cycle_id, source_digest, started_at, evidence_count),
            )

    def begin_or_resume_cycle(
        self,
        cycle_id: str,
        source_digest: str,
        started_at: str,
        evidence_count: int,
    ) -> str:
        """Start a new cycle or resume the failed cycle for the same source snapshot."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT cycle_id, state FROM cycles WHERE source_digest = ?",
                (source_digest,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO cycles(cycle_id, source_digest, started_at, state, reason, evidence_count)
                    VALUES (?, ?, ?, 'RUNNING', '', ?)
                    """,
                    (cycle_id, source_digest, started_at, evidence_count),
                )
                return cycle_id
            existing_cycle_id = str(row["cycle_id"])
            if str(row["state"]) not in {"FAILED", "RUNNING"}:
                return existing_cycle_id
            connection.execute(
                """
                UPDATE cycles
                   SET started_at = ?, completed_at = NULL, state = 'RUNNING', reason = '',
                       evidence_count = ?, experiment_id = NULL
                 WHERE cycle_id = ?
                """,
                (started_at, evidence_count, existing_cycle_id),
            )
        return existing_cycle_id

    def save_analysis_context(self, snapshot: AnalysisContextSnapshot, created_at: str) -> dict[str, Any]:
        """Persist one immutable model input; retries reuse the exact same payload."""

        payload_json = json.dumps(snapshot.payload, ensure_ascii=False, sort_keys=True)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO analysis_context_snapshots(
                    analysis_context_id, cycle_id, capability_id, source_digest, status,
                    payload_json, payload_hash, trigger_count, context_count, created_at
                ) VALUES (?, ?, ?, ?, 'PREPARED', ?, ?, ?, ?, ?)
                ON CONFLICT(cycle_id, capability_id) DO NOTHING
                """,
                (
                    snapshot.analysis_context_id,
                    snapshot.cycle_id,
                    snapshot.capability_id,
                    snapshot.source_digest,
                    payload_json,
                    snapshot.payload_hash,
                    snapshot.trigger_count,
                    snapshot.context_count,
                    created_at,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM analysis_context_snapshots
                 WHERE cycle_id = ? AND capability_id = ?
                """,
                (snapshot.cycle_id, snapshot.capability_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("analysis context snapshot was not persisted")
        stored = dict(row)
        if stored["payload_hash"] != snapshot.payload_hash:
            raise ValueError("analysis context snapshot is immutable for this cycle and capability")
        return stored

    def list_analysis_contexts(self, cycle_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM analysis_context_snapshots
                 WHERE cycle_id = ?
                 ORDER BY capability_id
                """,
                (cycle_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_analysis_context(self, analysis_context_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM analysis_context_snapshots
                 WHERE analysis_context_id = ?
                """,
                (analysis_context_id,),
            ).fetchone()
        return dict(row) if row else None

    def begin_analysis_attempt(self, analysis_context_id: str, started_at: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(attempt_number), 0) + 1 AS attempt_number
                  FROM analysis_context_attempts
                 WHERE analysis_context_id = ?
                """,
                (analysis_context_id,),
            ).fetchone()
            attempt_number = int(row["attempt_number"])
            attempt_id = f"{analysis_context_id}:attempt-{attempt_number}"
            connection.execute(
                """
                INSERT INTO analysis_context_attempts(
                    attempt_id, analysis_context_id, attempt_number, status, started_at
                ) VALUES (?, ?, ?, 'RUNNING', ?)
                """,
                (attempt_id, analysis_context_id, attempt_number, started_at),
            )
            connection.execute(
                """
                UPDATE analysis_context_snapshots
                   SET status = 'RUNNING'
                 WHERE analysis_context_id = ?
                """,
                (analysis_context_id,),
            )
        return attempt_id

    def complete_analysis_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        completed_at: str,
        error: str | None = None,
    ) -> None:
        if status not in {"ANALYZED", "FAILED"}:
            raise ValueError("analysis attempt status must be ANALYZED or FAILED")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT analysis_context_id FROM analysis_context_attempts
                 WHERE attempt_id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise ValueError("analysis context attempt not found")
            context_id = str(row["analysis_context_id"])
            connection.execute(
                """
                UPDATE analysis_context_attempts
                   SET status = ?, completed_at = ?, error = ?
                 WHERE attempt_id = ?
                """,
                (status, completed_at, error, attempt_id),
            )
            connection.execute(
                """
                UPDATE analysis_context_snapshots
                   SET status = ?, analyzed_at = ?, last_error = ?
                 WHERE analysis_context_id = ?
                """,
                (
                    status,
                    completed_at if status == "ANALYZED" else None,
                    error,
                    context_id,
                ),
            )

    def list_analysis_attempts(self, analysis_context_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM analysis_context_attempts
                 WHERE analysis_context_id = ?
                 ORDER BY attempt_number
                """,
                (analysis_context_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_hypotheses(self, cycle_id: str, hypotheses: tuple[Hypothesis, ...]) -> tuple[Hypothesis, ...]:
        inserted: list[Hypothesis] = []
        with self.connect() as connection:
            for hypothesis in hypotheses:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO hypotheses(
                        hypothesis_id, cycle_id, kind, capability_id, title, confidence,
                        requires_human_selection, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hypothesis.hypothesis_id,
                        cycle_id,
                        hypothesis.kind.value,
                        hypothesis.capability_id,
                        hypothesis.title,
                        hypothesis.confidence,
                        int(hypothesis.requires_human_selection),
                        json.dumps(asdict(hypothesis), ensure_ascii=False, sort_keys=True),
                    ),
                )
                if cursor.rowcount == 1:
                    inserted.append(hypothesis)
        return tuple(inserted)

    def create_experiment(
        self,
        experiment_id: str,
        cycle_id: str,
        hypothesis_id: str,
        state: ExperimentState,
        reason: str,
        now: str,
        *,
        candidate_id: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO experiments(
                    experiment_id, cycle_id, hypothesis_id, state, candidate_id,
                    decision_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    cycle_id,
                    hypothesis_id,
                    state.value,
                    candidate_id,
                    reason,
                    now,
                    now,
                ),
            )

    def record_retry_hypothesis(self, cycle_id: str, source_digest: str, hypothesis: Hypothesis) -> None:
        """Atomically append one idempotent retry dossier without replacing history."""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO cycles(
                    cycle_id, source_digest, started_at, completed_at, state, reason, evidence_count
                ) VALUES (?, ?, ?, ?, 'AWAITING_HUMAN_SELECTION', ?, ?)
                """,
                (cycle_id, source_digest, hypothesis.created_at, hypothesis.created_at,
                 "blocked experiment retry; generate a new Candidate for human review", len(hypothesis.evidence_ids)),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO hypotheses(
                    hypothesis_id, cycle_id, kind, capability_id, title, confidence,
                    requires_human_selection, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (hypothesis.hypothesis_id, cycle_id, hypothesis.kind.value, hypothesis.capability_id,
                 hypothesis.title, hypothesis.confidence, int(hypothesis.requires_human_selection),
                 json.dumps(asdict(hypothesis), ensure_ascii=False, sort_keys=True)),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO cycle_evidence(cycle_id, evidence_id) VALUES (?, ?)",
                [(cycle_id, evidence_id) for evidence_id in hypothesis.evidence_ids],
            )

    def record_candidate(self, experiment_id: str, candidate: CandidateVersion) -> None:
        now = _utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO candidates(candidate_id, experiment_id, payload_json) VALUES (?, ?, ?)",
                (candidate.candidate_id, experiment_id, json.dumps(asdict(candidate), ensure_ascii=False, sort_keys=True)),
            )
            connection.execute(
                """
                UPDATE experiments
                   SET candidate_id = ?, state = ?, decision_reason = ?, updated_at = ?
                 WHERE experiment_id = ?
                """,
                (
                    candidate.candidate_id,
                    ExperimentState.AWAITING_EVALUATION.value,
                    "candidate built; awaiting immutable Benchmark Run",
                    now,
                    experiment_id,
                ),
            )

    def record_hypothesis_candidate(self, candidate: CandidateVersion) -> None:
        """Freeze the reviewable Candidate before an evaluation Experiment exists."""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO hypothesis_candidates(
                    hypothesis_id, candidate_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(hypothesis_id) DO NOTHING
                """,
                (
                    candidate.hypothesis_id,
                    candidate.candidate_id,
                    json.dumps(asdict(candidate), ensure_ascii=False, sort_keys=True),
                    _utc_now(),
                ),
            )

    def get_hypothesis_candidate(self, hypothesis_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM hypothesis_candidates WHERE hypothesis_id = ?",
                (hypothesis_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_hypothesis_candidates(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM hypothesis_candidates ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def attach_benchmark_run(
        self,
        experiment_id: str,
        run_id: str,
        *,
        evaluation_plan: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE experiments
                   SET benchmark_run_id = ?, evaluation_plan_json = ?, updated_at = ?
                 WHERE experiment_id = ?
                """,
                (
                    run_id,
                    json.dumps(evaluation_plan or {}, ensure_ascii=False, sort_keys=True),
                    _utc_now(),
                    experiment_id,
                ),
            )

    def transition_experiment(self, experiment_id: str, state: ExperimentState, reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE experiments SET state = ?, decision_reason = ?, updated_at = ? WHERE experiment_id = ?",
                (state.value, reason, _utc_now(), experiment_id),
            )

    def record_promotion_bundle(self, experiment_id: str, payload: dict[str, Any]) -> None:
        """Persist the immutable handoff for human release; never releases automatically."""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO promotion_bundles(experiment_id, payload_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET payload_json = excluded.payload_json
                """,
                (experiment_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), _utc_now()),
            )

    def get_promotion_bundle(self, experiment_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM promotion_bundles WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def complete_cycle(self, outcome: CycleOutcome) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE cycles
                   SET completed_at = ?, state = ?, reason = ?, experiment_id = ?
                 WHERE cycle_id = ?
                """,
                (_utc_now(), outcome.state, outcome.reason, outcome.experiment_id, outcome.cycle_id),
            )

    def select_hypothesis(self, hypothesis_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE hypotheses
                   SET selected_at = ?
                 WHERE hypothesis_id = ?
                   AND selected_at IS NULL
                   AND ignored_at IS NULL
                   AND deleted_at IS NULL
                """,
                (_utc_now(), hypothesis_id),
            )
        return cursor.rowcount == 1

    def ignore_hypothesis(self, hypothesis_id: str, reason: str) -> bool:
        """Keep a rejected hypothesis for audit while excluding it from selection."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE hypotheses
                   SET ignored_at = ?, ignore_reason = ?
                 WHERE hypothesis_id = ?
                   AND selected_at IS NULL
                   AND ignored_at IS NULL
                   AND deleted_at IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM experiments e WHERE e.hypothesis_id = hypotheses.hypothesis_id
                   )
                """,
                (_utc_now(), reason, hypothesis_id),
            )
        return cursor.rowcount == 1

    def delete_hypothesis(self, hypothesis_id: str, reason: str) -> bool:
        """Remove a hypothesis from worklists while retaining its audit record."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE hypotheses
                   SET deleted_at = ?, deletion_reason = ?
                 WHERE hypothesis_id = ?
                   AND deleted_at IS NULL
                """,
                (_utc_now(), reason, hypothesis_id),
            )
        return cursor.rowcount == 1

    def list_hypotheses(
        self,
        limit: int | None = None,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT h.*, c.started_at AS cycle_started_at
              FROM hypotheses h JOIN cycles c ON c.cycle_id = h.cycle_id
        """
        if not include_deleted:
            query += " WHERE h.deleted_at IS NULL"
        query += " ORDER BY c.started_at DESC, h.confidence DESC"
        parameters: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (limit,)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def get_hypothesis(self, hypothesis_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id = ?", (hypothesis_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_source_run_hypotheses(self, run_id: str) -> list[dict[str, Any]]:
        """Return the explicit Evolution projection associated with one Bench Run."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT h.hypothesis_id,
                       h.kind,
                       h.title,
                       h.requires_human_selection,
                       h.selected_at,
                       c.state AS cycle_state,
                       e.experiment_id,
                       e.state AS experiment_state,
                       e.candidate_id,
                       e.benchmark_run_id
                  FROM hypotheses h
                  JOIN cycles c ON c.cycle_id = h.cycle_id
                 LEFT JOIN experiments e ON e.hypothesis_id = h.hypothesis_id
                 WHERE json_extract(h.payload_json, '$.source_run_id') = ?
                   AND h.deleted_at IS NULL
                   AND (e.experiment_id IS NULL OR e.deleted_at IS NULL)
                 ORDER BY c.started_at DESC, h.confidence DESC
                """,
                (run_id,),
            ).fetchall()
        return [
            {
                "hypothesis_id": str(row["hypothesis_id"]),
                "kind": str(row["kind"]),
                "title": str(row["title"]),
                "requires_human_selection": bool(row["requires_human_selection"]),
                "selected_at": row["selected_at"],
                "cycle_state": str(row["cycle_state"]),
                "experiment_id": row["experiment_id"],
                "experiment_state": row["experiment_state"],
                "candidate_id": row["candidate_id"],
                "benchmark_run_id": row["benchmark_run_id"],
            }
            for row in rows
        ]

    def delete_experiment(self, experiment_id: str, reason: str) -> bool:
        """Remove an experiment from worklists while retaining its evidence graph."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE experiments
                   SET deleted_at = ?, deletion_reason = ?
                 WHERE experiment_id = ?
                   AND deleted_at IS NULL
                """,
                (_utc_now(), reason, experiment_id),
            )
        return cursor.rowcount == 1

    def list_experiments(
        self,
        limit: int | None = None,
        *,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT e.*, h.kind, h.capability_id, h.title,
                   h.payload_json AS hypothesis_json,
                   COALESCE(hc.payload_json, c.payload_json) AS candidate_json
              FROM experiments e
              JOIN hypotheses h ON h.hypothesis_id = e.hypothesis_id
              LEFT JOIN hypothesis_candidates hc ON hc.hypothesis_id = e.hypothesis_id
              LEFT JOIN candidates c ON c.experiment_id = e.experiment_id
        """
        if not include_deleted:
            query += " WHERE e.deleted_at IS NULL"
        query += " ORDER BY e.created_at DESC"
        parameters: tuple[int, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            parameters = (limit,)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def get_experiment(self, experiment_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT e.*, h.payload_json AS hypothesis_json,
                       COALESCE(hc.payload_json, c.payload_json) AS candidate_json
                  FROM experiments e
                  JOIN hypotheses h ON h.hypothesis_id = e.hypothesis_id
                  LEFT JOIN hypothesis_candidates hc ON hc.hypothesis_id = e.hypothesis_id
                  LEFT JOIN candidates c ON c.experiment_id = e.experiment_id
                 WHERE e.experiment_id = ?
                """,
                (experiment_id,),
            ).fetchone()
        return dict(row) if row else None

    def cycle_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            latest = connection.execute("SELECT * FROM cycles ORDER BY started_at DESC LIMIT 1").fetchone()
            hypothesis_count = connection.execute(
                "SELECT COUNT(*) AS count FROM hypotheses WHERE deleted_at IS NULL"
            ).fetchone()["count"]
            hypothesis_rows = connection.execute(
                "SELECT kind, COUNT(*) AS count FROM hypotheses "
                "WHERE deleted_at IS NULL GROUP BY kind"
            ).fetchall()
            experiment_rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM experiments WHERE deleted_at IS NULL GROUP BY state"
            ).fetchall()
        hypothesis_counts = {kind.value: 0 for kind in HypothesisKind}
        hypothesis_counts.update({row["kind"]: row["count"] for row in hypothesis_rows})
        return {
            "latest": dict(latest) if latest else None,
            "hypothesis_count": hypothesis_count,
            "hypothesis_counts": hypothesis_counts,
            "experiment_counts": {row["state"]: row["count"] for row in experiment_rows},
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
