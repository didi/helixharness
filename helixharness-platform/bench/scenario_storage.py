"""Isolated v2 storage for workflow-neutral Evaluation Scenarios."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bench.scenarios import ScenarioSource
from bench.workload_policy import (
    FORMAL_EVALUATION,
    SCENARIO_EXECUTION_WORKLOADS,
    workload_kinds_for_pool,
)

try:
    import sqlite3
except ImportError:  # pragma: no cover - depends on the host Python build
    from pysqlite3 import dbapi2 as sqlite3


@dataclass(frozen=True)
class PublishedScenario:
    """One current published Scenario or immutable historical version."""

    case_id: str
    source_hash: str
    title: str
    case_kind: str
    snapshot_json: str
    publication_status: str
    calibration_state: str
    published_at: str


@dataclass(frozen=True)
class StoredScenarioCalculation:
    """One immutable calculation appended to a v2 Run."""

    calculation_id: str
    run_id: str
    sequence_number: int
    reason: str
    contract_hash: str
    contract_json: str
    result_json: str
    created_at: str


@dataclass(frozen=True)
class StoredScenarioJobSummary:
    """Read-only progress for one immutable v2 execution side."""

    job_id: str
    case_id: str
    side: str
    status: str
    attempt_number: int
    worker_id: str | None
    started_at: str | None
    completed_at: str | None
    error: str | None
    completed_stage_ids: tuple[str, ...]
    terminal_reason: str | None


def _completed_stage_ids(raw: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if isinstance(item, str))


def _terminal_reason(raw: object) -> str | None:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    execution = parsed.get("execution")
    if not isinstance(execution, dict):
        return None
    reason = execution.get("terminal_reason")
    return str(reason) if isinstance(reason, str) and reason else None


class ScenarioStore:
    """Persist only v2 Scenarios without reading or migrating the v1 database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        """Initialize the current schema; imported database migrations are unsupported."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.executescript(Path(__file__).with_suffix(".sql").read_text(encoding="utf-8"))

    def publish(self, source: ScenarioSource) -> PublishedScenario:
        """Publish a valid source and retain every distinct immutable version."""

        with sqlite3.connect(self.database_path) as connection:
            return self._publish(connection, source)

    def publish_many(self, sources: tuple[ScenarioSource, ...]) -> tuple[PublishedScenario, ...]:
        """Publish a validated source batch in one SQLite transaction."""

        if not sources:
            return ()
        if any(not source.preflight_passed for source in sources):
            raise ValueError("Scenario Source did not pass publication preflight")
        with sqlite3.connect(self.database_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            return tuple(self._publish(connection, source) for source in sources)

    def _publish(self, connection: sqlite3.Connection, source: ScenarioSource) -> PublishedScenario:
        if not source.preflight_passed:
            raise ValueError("Scenario Source did not pass publication preflight")
        row = (
            source.case_id,
            source.snapshot.source_hash,
            source.title,
            source.case_kind,
            source.snapshot.snapshot_json,
            "PUBLISHED",
            "PENDING_DETERMINISTIC_CALIBRATION",
            datetime.now(timezone.utc).isoformat(),
        )
        existing = connection.execute(
            "SELECT published_at FROM scenario_versions WHERE case_id = ? AND source_hash = ?",
            (source.case_id, source.snapshot.source_hash),
        ).fetchone()
        if existing is not None:
            row = (*row[:-1], str(existing[0]))
        connection.execute(
            """
            INSERT OR IGNORE INTO scenario_versions(
                case_id, source_hash, title, case_kind, snapshot_json,
                publication_status, calibration_state, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        connection.execute(
            """
            INSERT INTO scenarios(
                case_id, source_hash, title, case_kind, snapshot_json,
                publication_status, calibration_state, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(case_id) DO UPDATE SET
                source_hash = excluded.source_hash,
                title = excluded.title,
                case_kind = excluded.case_kind,
                snapshot_json = excluded.snapshot_json,
                publication_status = excluded.publication_status,
                calibration_state = excluded.calibration_state,
                published_at = excluded.published_at
            """,
            row,
        )
        return self._from_row(row)

    def get(self, case_id: str) -> PublishedScenario | None:
        """Return the current Scenario version."""

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT case_id, source_hash, title, case_kind, snapshot_json,
                       publication_status, calibration_state, published_at
                FROM scenarios WHERE case_id = ?
                """,
                (case_id,),
            ).fetchone()
        return self._from_row(row) if row is not None else None

    def list_versions(self, case_id: str) -> list[PublishedScenario]:
        """Return distinct versions in publication order."""

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT case_id, source_hash, title, case_kind, snapshot_json,
                       publication_status, calibration_state, published_at
                FROM scenario_versions WHERE case_id = ? ORDER BY rowid
                """,
                (case_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_current(self) -> list[PublishedScenario]:
        """Return current published Scenarios in stable Case order."""

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT case_id, source_hash, title, case_kind, snapshot_json,
                       publication_status, calibration_state, published_at
                FROM scenarios ORDER BY case_id
                """
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def formally_verified_versions(self) -> set[tuple[str, str]]:
        """Return current Case versions with a successful scored or Base-only Formal side."""

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT run_cases.case_id, run_cases.source_hash, jobs.outcome_json
                FROM scenario_run_cases AS run_cases
                JOIN scenarios AS current
                  ON current.case_id = run_cases.case_id
                 AND current.source_hash = run_cases.source_hash
                JOIN scenario_runs AS runs ON runs.run_id = run_cases.run_id
                JOIN scenario_jobs AS jobs
                  ON jobs.run_id = run_cases.run_id AND jobs.case_id = run_cases.case_id
                WHERE runs.run_kind = 'FORMAL'
                  AND runs.status = 'COMPLETED'
                  AND (
                    runs.current_calculation_id IS NOT NULL
                    OR (
                      json_extract(runs.runtime_policy_json, '$.execution_scope') = 'BASE_ONLY'
                      AND NOT EXISTS (
                        SELECT 1 FROM scenario_jobs AS candidate_jobs
                        WHERE candidate_jobs.run_id = runs.run_id
                          AND candidate_jobs.side = 'CANDIDATE'
                      )
                    )
                  )
                  AND runs.deleted_at IS NULL
                  AND jobs.status = 'COMPLETED'
                  AND jobs.outcome_json IS NOT NULL
                """
            ).fetchall()
        verified: set[tuple[str, str]] = set()
        for case_id, source_hash, outcome_json in rows:
            try:
                outcome = json.loads(str(outcome_json))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if _is_successful_formal_outcome(outcome):
                verified.add((str(case_id), str(source_hash)))
        return verified

    def active_case_run_ids(self, case_id: str) -> tuple[str, ...]:
        """Return unfinished Runs that currently lock one Case."""

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT run_cases.run_id
                FROM scenario_run_cases AS run_cases
                JOIN scenario_runs AS runs ON runs.run_id = run_cases.run_id
                WHERE run_cases.case_id = ?
                  AND runs.status IN ('QUEUED', 'RUNNING', 'SUSPENDED')
                ORDER BY run_cases.run_id
                """,
                (case_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def delete_current(self, case_id: str) -> None:
        """Remove one Case from the current library while preserving frozen history."""

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT 1 FROM scenarios WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if current is None:
                raise ValueError(f"Case 不存在或已删除：{case_id}")
            active_runs = connection.execute(
                """
                SELECT DISTINCT run_cases.run_id
                FROM scenario_run_cases AS run_cases
                JOIN scenario_runs AS runs ON runs.run_id = run_cases.run_id
                WHERE run_cases.case_id = ?
                  AND runs.status IN ('QUEUED', 'RUNNING', 'SUSPENDED')
                ORDER BY run_cases.run_id
                """,
                (case_id,),
            ).fetchall()
            if active_runs:
                run_ids = "、".join(str(row[0]) for row in active_runs)
                raise ValueError(f"Case 正被未完成的评测任务使用，暂不能删除：{run_ids}")
            connection.execute("DELETE FROM scenarios WHERE case_id = ?", (case_id,))

    def overview_metrics(self) -> dict[str, int]:
        """Return durable Scenario and Run counters for the evaluation overview."""

        with sqlite3.connect(self.database_path) as connection:
            scenario_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS published_scenarios,
                    SUM(CASE WHEN calibration_state = 'CALIBRATED' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN calibration_state = 'PENDING_DETERMINISTIC_CALIBRATION' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN calibration_state = 'FAILED' THEN 1 ELSE 0 END)
                FROM scenarios
                WHERE publication_status = 'PUBLISHED'
                """
            ).fetchone()
            run_row = connection.execute(
                """
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN status IN ('QUEUED', 'RUNNING') THEN 1 ELSE 0 END)
                FROM scenario_runs
                WHERE deleted_at IS NULL
                """
            ).fetchone()
            report_row = connection.execute(
                """
                SELECT COUNT(DISTINCT reports.run_id)
                FROM scenario_reports AS reports
                JOIN scenario_runs AS runs ON runs.run_id = reports.run_id
                WHERE runs.deleted_at IS NULL
                """
            ).fetchone()

        scenario_values = scenario_row or (0, 0, 0, 0)
        run_values = run_row or (0, 0, 0, 0)
        return {
            "published_scenarios": int(scenario_values[0] or 0),
            "calibrated_scenarios": int(scenario_values[1] or 0),
            "pending_scenarios": int(scenario_values[2] or 0),
            "failed_scenarios": int(scenario_values[3] or 0),
            "runs": int(run_values[0] or 0),
            "completed_runs": int(run_values[1] or 0),
            "failed_runs": int(run_values[2] or 0),
            "active_runs": int(run_values[3] or 0),
            "reported_runs": int(report_row[0] or 0) if report_row is not None else 0,
        }

    def run_counts_by_origin(self) -> dict[str, int]:
        """Count v2 Runs by their explicit product origin.

        Run origins distinguish manual evaluation, discovery and remediation.
        """

        return {
            origin: values["runs"]
            for origin, values in self.run_metrics_by_origin().items()
        }

    def run_metrics_by_origin(self) -> dict[str, dict[str, int]]:
        """Aggregate Run completion and report counters by product origin."""

        metrics = {
            origin: {
                "runs": 0,
                "completed_runs": 0,
                "failed_runs": 0,
                "active_runs": 0,
                "reported_runs": 0,
            }
            for origin in (
                "MANUAL",
                "EVOLUTION",
                "EVOLUTION_DISCOVERY",
                "EVOLUTION_REMEDIATION",
            )
        }
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT runs.status, runs.runtime_policy_json,
                       CASE WHEN reports.run_id IS NULL THEN 0 ELSE 1 END AS has_report
                FROM scenario_runs AS runs
                LEFT JOIN (SELECT DISTINCT run_id FROM scenario_reports) AS reports
                  ON reports.run_id = runs.run_id
                WHERE runs.deleted_at IS NULL
                """
            ).fetchall()
        for row in rows:
            try:
                policy = json.loads(str(row[1]))
            except json.JSONDecodeError:
                policy = {}
            origin = str(policy.get("origin") or "MANUAL") if isinstance(policy, dict) else "MANUAL"
            if origin == "EVOLUTION_DISCOVERY":
                groups = ("EVOLUTION", "EVOLUTION_DISCOVERY")
            elif origin == "EVOLUTION_REMEDIATION":
                groups = ("EVOLUTION", "EVOLUTION_REMEDIATION")
            else:
                groups = ("MANUAL",)
            status = str(row[0])
            for group in groups:
                values = metrics[group]
                values["runs"] += 1
                if status == "COMPLETED":
                    values["completed_runs"] += 1
                elif status == "FAILED":
                    values["failed_runs"] += 1
                elif status in {"QUEUED", "RUNNING"}:
                    values["active_runs"] += 1
                if bool(row[2]):
                    values["reported_runs"] += 1
        return metrics

    def record_worker_status(
        self,
        worker_id: str,
        adapter: str,
        status: str,
        *,
        workload_kind: str = FORMAL_EVALUATION,
    ) -> None:
        """Publish one lightweight v2 Worker heartbeat for the creation page."""

        if (
            not worker_id
            or status not in {"IDLE", "RUNNING"}
            or workload_kind not in SCENARIO_EXECUTION_WORKLOADS
        ):
            raise ValueError("Scenario Worker status is invalid")
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO scenario_worker_status(
                    worker_id, adapter, workload_kind, status, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    adapter = excluded.adapter,
                    workload_kind = excluded.workload_kind,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    worker_id,
                    adapter,
                    workload_kind,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def resource_summary(
        self,
        *,
        adapter: str = "CODEX",
        workload_kind: str = FORMAL_EVALUATION,
        stale_seconds: int = 15,
    ) -> dict[str, object]:
        """Return live v2 Worker capacity plus durable queue occupancy."""

        if workload_kind not in SCENARIO_EXECUTION_WORKLOADS:
            raise ValueError("Scenario workload kind is invalid")

        pool_kinds = workload_kinds_for_pool(workload_kind)
        placeholders = ", ".join("?" for _ in pool_kinds)
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
        with sqlite3.connect(self.database_path) as connection:
            live_rows = connection.execute(
                f"""
                SELECT worker_id, status FROM scenario_worker_status
                WHERE adapter = ? AND workload_kind IN ({placeholders}) AND updated_at >= ?
                """,
                (adapter, *pool_kinds, cutoff),
            ).fetchall()
            busy_rows = connection.execute(
                f"""
                SELECT DISTINCT j.worker_id
                FROM scenario_jobs j
                JOIN scenario_runs r ON r.run_id = j.run_id
                WHERE r.execution_adapter = ? AND r.workload_kind IN ({placeholders})
                  AND r.deleted_at IS NULL
                  AND j.status = 'RUNNING'
                  AND j.worker_id IS NOT NULL
                """,
                (adapter, *pool_kinds),
            ).fetchall()
            queue_row = connection.execute(
                f"""
                SELECT
                    SUM(CASE WHEN j.status = 'PENDING' THEN 1 ELSE 0 END),
                    COUNT(DISTINCT CASE WHEN r.status = 'RUNNING' THEN r.run_id END),
                    COUNT(DISTINCT CASE WHEN r.status IN ('QUEUED', 'RUNNING') THEN r.run_id END)
                FROM scenario_runs r
                LEFT JOIN scenario_jobs j ON j.run_id = r.run_id
                WHERE r.execution_adapter = ? AND r.workload_kind IN ({placeholders})
                  AND r.deleted_at IS NULL
                  AND r.status IN ('QUEUED', 'RUNNING')
                """,
                (adapter, *pool_kinds),
            ).fetchone()
            head_row = connection.execute(
                f"""
                SELECT r.run_id
                FROM scenario_runs r
                WHERE r.execution_adapter = ? AND r.workload_kind IN ({placeholders})
                  AND r.deleted_at IS NULL
                  AND r.status IN ('QUEUED', 'RUNNING')
                ORDER BY r.created_at, r.run_id LIMIT 1
                """,
                (adapter, *pool_kinds),
            ).fetchone()
        live_workers = {str(row[0]): str(row[1]) for row in live_rows}
        busy_workers = {
            str(row[0]) for row in busy_rows
        } | {
            worker_id for worker_id, status in live_workers.items() if status == "RUNNING"
        }
        worker_ids = set(live_workers) | busy_workers
        idle_worker_count = sum(
            status == "IDLE" and worker_id not in busy_workers
            for worker_id, status in live_workers.items()
        )
        queue_values = queue_row or (0, 0, 0)
        return {
            "worker_count": len(worker_ids),
            "busy_worker_count": len(busy_workers),
            "idle_worker_count": idle_worker_count,
            "active_work_count": int(queue_values[1] or 0),
            "pending_work_count": int(queue_values[0] or 0),
            "unfinished_run_count": int(queue_values[2] or 0),
            "head_run": {"run_id": str(head_row[0])} if head_row is not None else None,
        }

    def record_calibration(
        self,
        case_id: str,
        source_hash: str,
        *,
        state: str,
    ) -> PublishedScenario:
        """Record deterministic calibration against one exact current version."""

        if state not in {"CALIBRATED", "FAILED"}:
            raise ValueError("Scenario calibration state must be CALIBRATED or FAILED")
        with sqlite3.connect(self.database_path) as connection:
            current = connection.execute(
                "SELECT source_hash FROM scenarios WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if current is None or str(current[0]) != source_hash:
                raise ValueError("Scenario calibration target is not the current version")
            connection.execute(
                """
                UPDATE scenario_versions SET calibration_state = ?
                WHERE case_id = ? AND source_hash = ?
                """,
                (state, case_id, source_hash),
            )
            connection.execute(
                """
                UPDATE scenarios SET calibration_state = ?
                WHERE case_id = ? AND source_hash = ?
                """,
                (state, case_id, source_hash),
            )
        calibrated = self.get(case_id)
        if calibrated is None:  # pragma: no cover - protected by the transaction
            raise ValueError("Scenario disappeared during calibration")
        return calibrated

    def list_run_calculations(self, run_id: str) -> list[StoredScenarioCalculation]:
        """Return every immutable calculation in append order."""

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT calculation_id, run_id, sequence_number, reason,
                       contract_hash, contract_json, result_json, created_at
                FROM scenario_calculations WHERE run_id = ? ORDER BY sequence_number
                """,
                (run_id,),
            ).fetchall()
        return [
            StoredScenarioCalculation(
                calculation_id=str(row[0]),
                run_id=str(row[1]),
                sequence_number=int(row[2]),
                reason=str(row[3]),
                contract_hash=str(row[4]),
                contract_json=str(row[5]),
                result_json=str(row[6]),
                created_at=str(row[7]),
            )
            for row in rows
        ]

    def list_run_jobs(self, run_id: str) -> list[StoredScenarioJobSummary]:
        """Return Base/Candidate progress without exposing mutable job internals."""

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT job_id, case_id, side, status, attempt_number, worker_id,
                       started_at, completed_at, error, workflow_progress_json, outcome_json
                FROM scenario_jobs WHERE run_id = ? ORDER BY sequence_number
                """,
                (run_id,),
            ).fetchall()
        return [
            StoredScenarioJobSummary(
                job_id=str(row[0]),
                case_id=str(row[1]),
                side=str(row[2]),
                status=str(row[3]),
                attempt_number=int(row[4]),
                worker_id=str(row[5]) if row[5] is not None else None,
                started_at=str(row[6]) if row[6] is not None else None,
                completed_at=str(row[7]) if row[7] is not None else None,
                error=str(row[8]) if row[8] is not None else None,
                completed_stage_ids=_completed_stage_ids(row[9]),
                terminal_reason=_terminal_reason(row[10]),
            )
            for row in rows
        ]

    def record_job_workflow_progress(
        self,
        job_id: str,
        lease_token: str,
        completed_stage_ids: tuple[str, ...],
    ) -> None:
        """Persist trusted workflow-stage completions for the active Job lease."""

        with sqlite3.connect(self.database_path) as connection:
            changed = connection.execute(
                """
                UPDATE scenario_jobs SET workflow_progress_json = ?
                WHERE job_id = ? AND status = 'RUNNING' AND lease_token = ?
                """,
                (json.dumps(completed_stage_ids, separators=(",", ":")), job_id, lease_token),
            ).rowcount
        if changed != 1:
            raise ValueError("Scenario Job lease is no longer active")

    def list_job_turn_evidence(self, job_id: str) -> list[dict[str, object]]:
        """Return immutable diagnostic Turns after rebuildable workspaces are gone."""

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT attempt_number, turn_sequence, turn_id, input_kind, input_text,
                       workflow_step_id, milestone_id, classification, started_at_ms,
                       completed_at_ms, duration_ms, usage_json, final_response, error_json, events_json
                FROM scenario_job_turn_evidence
                WHERE job_id = ?
                ORDER BY attempt_number, turn_sequence
                """,
                (job_id,),
            ).fetchall()
        return [
            {
                "attempt_number": int(row[0]),
                "turn_sequence": int(row[1]),
                "turn_id": str(row[2]) if row[2] is not None else None,
                "input_kind": str(row[3]),
                "input_text": str(row[4]) if row[4] is not None else None,
                "workflow_step_id": str(row[5]) if row[5] is not None else None,
                "milestone_id": str(row[6]) if row[6] is not None else None,
                "classification": str(row[7]),
                "started_at_ms": int(row[8]) if row[8] is not None else None,
                "completed_at_ms": int(row[9]) if row[9] is not None else None,
                "duration_ms": int(row[10]) if row[10] is not None else None,
                "usage": json.loads(str(row[11])),
                "final_response": str(row[12]) if row[12] is not None else None,
                "error": json.loads(str(row[13])) if row[13] is not None else None,
                "events": json.loads(str(row[14])),
            }
            for row in rows
        ]

    def list_job_attempt_evidence(self, job_id: str) -> list[dict[str, object]]:
        """Return append-only resource evidence for every observed execution Attempt."""

        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT attempt_number, disposition, terminal_status, started_at_ms,
                       completed_at_ms, duration_ms, usage_json, evidence_complete,
                       error_json, created_at
                FROM scenario_job_attempt_evidence
                WHERE job_id = ?
                ORDER BY attempt_number
                """,
                (job_id,),
            ).fetchall()
        return [
            {
                "attempt_number": int(row[0]),
                "disposition": str(row[1]),
                "terminal_status": str(row[2]),
                "started_at_ms": int(row[3]) if row[3] is not None else None,
                "completed_at_ms": int(row[4]) if row[4] is not None else None,
                "duration_ms": int(row[5]) if row[5] is not None else None,
                "usage": json.loads(str(row[6])),
                "evidence_complete": bool(row[7]),
                "error": json.loads(str(row[8])) if row[8] is not None else None,
                "created_at": str(row[9]),
            }
            for row in rows
        ]

    def get_run_contract(self, run_id: str) -> dict[str, object] | None:
        """Return the frozen evaluation contract for one v2 Run."""

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT contract_json FROM scenario_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        parsed = json.loads(str(row[0]))
        if not isinstance(parsed, dict) or parsed.get("schema_version") != "framework-eval-run-contract.v2":
            raise ValueError("Stored Scenario Run contract schema is invalid")
        return parsed

    def get_run_runtime_policy(self, run_id: str) -> dict[str, object] | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT runtime_policy_json FROM scenario_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        parsed = json.loads(str(row[0]))
        if not isinstance(parsed, dict):
            raise ValueError("Stored Scenario Runtime Policy is invalid")
        return parsed

    def record_scenario_hard_gate_analysis(self, analysis: dict[str, object]) -> None:
        """Append advisory AI analysis without modifying a Run calculation."""

        if analysis.get("status") not in {"READY", "FAILED"}:
            raise ValueError("Scenario hard-gate analysis status is invalid")
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT runs.status, runs.current_calculation_id
                FROM scenario_runs AS runs
                JOIN scenario_calculations AS calculations
                  ON calculations.run_id = runs.run_id
                 AND calculations.calculation_id = ?
                WHERE runs.run_id = ? AND runs.deleted_at IS NULL
                """,
                (analysis["calculation_id"], analysis["run_id"]),
            ).fetchone()
            if row is None or str(row[0]) != "COMPLETED":
                raise ValueError("Only a completed Scenario Run can retain AI analysis")
            connection.execute(
                """
                INSERT INTO scenario_hard_gate_analyses(
                    analysis_id, run_id, calculation_id, status, model,
                    reasoning_effort, prompt_version, input_sha256,
                    input_json, output_json, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis["analysis_id"],
                    analysis["run_id"],
                    analysis["calculation_id"],
                    analysis["status"],
                    analysis["model"],
                    analysis["reasoning_effort"],
                    analysis["prompt_version"],
                    analysis["input_sha256"],
                    json.dumps(
                        analysis.get("input", {}),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    json.dumps(
                        analysis.get("output", {}),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    analysis.get("error"),
                    analysis["created_at"],
                ),
            )

    def get_latest_scenario_hard_gate_analysis(
        self,
        run_id: str,
    ) -> dict[str, object] | None:
        """Return the newest append-only Scenario v2 analysis attempt."""

        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM scenario_hard_gate_analyses
                WHERE run_id = ?
                ORDER BY created_at DESC, analysis_id DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "analysis_id": str(row["analysis_id"]),
            "run_id": str(row["run_id"]),
            "calculation_id": str(row["calculation_id"]),
            "status": str(row["status"]),
            "model": str(row["model"]),
            "reasoning_effort": str(row["reasoning_effort"]),
            "prompt_version": str(row["prompt_version"]),
            "input_sha256": str(row["input_sha256"]),
            "input": json.loads(str(row["input_json"])),
            "output": json.loads(str(row["output_json"])),
            "error": str(row["error"]) if row["error"] else None,
            "created_at": str(row["created_at"]),
        }

    def ensure_remediation_projection(self, run_id: str) -> None:
        """Create one pending v2 remediation projection for a terminal source Run."""

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT status, runtime_policy_json FROM scenario_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or str(row[0]) != "COMPLETED":
                return
            try:
                policy = json.loads(str(row[1]))
            except json.JSONDecodeError:
                policy = {}
            if isinstance(policy, dict) and policy.get("origin") == "EVOLUTION_REMEDIATION":
                return
            connection.execute(
                """
                INSERT OR IGNORE INTO scenario_remediation_projections(
                    run_id, status, updated_at
                ) VALUES (?, 'PENDING', ?)
                """,
                (run_id, datetime.now(timezone.utc).isoformat()),
            )

    def list_pending_remediation_run_ids(self) -> tuple[str, ...]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM scenario_remediation_projections
                WHERE status = 'PENDING' ORDER BY updated_at, run_id
                """
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def get_remediation_projection(self, run_id: str) -> dict[str, object] | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                """
                SELECT run_id, status, reason, hypothesis_id, updated_at
                FROM scenario_remediation_projections WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "run_id": str(row[0]),
            "status": str(row[1]),
            "reason": str(row[2]),
            "hypothesis_id": str(row[3]) if row[3] is not None else None,
            "updated_at": str(row[4]),
        }

    def complete_remediation_projection(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        hypothesis_id: str | None = None,
    ) -> None:
        if status not in {"READY", "NOT_ELIGIBLE", "FAILED"} or not reason.strip():
            raise ValueError("Scenario remediation projection completion is invalid")
        with sqlite3.connect(self.database_path) as connection:
            updated = connection.execute(
                """
                UPDATE scenario_remediation_projections
                SET status = ?, reason = ?, hypothesis_id = ?, updated_at = ?
                WHERE run_id = ? AND status = 'PENDING'
                """,
                (
                    status,
                    reason.strip(),
                    hypothesis_id,
                    datetime.now(timezone.utc).isoformat(),
                    run_id,
                ),
            ).rowcount
        if updated != 1:
            raise ValueError("Scenario remediation projection is not pending")

    def health(self) -> dict[str, object]:
        """Return a compact integrity and queue snapshot for deployment probes."""

        with sqlite3.connect(self.database_path) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            published = connection.execute(
                "SELECT COUNT(*) FROM scenarios WHERE publication_status = 'PUBLISHED'"
            ).fetchone()
            queued = connection.execute(
                """
                SELECT COUNT(*)
                FROM scenario_jobs AS jobs
                JOIN scenario_runs AS runs ON runs.run_id = jobs.run_id
                WHERE jobs.status = 'PENDING'
                  AND runs.status IN ('QUEUED', 'RUNNING')
                  AND runs.deleted_at IS NULL
                """
            ).fetchone()
            running = connection.execute(
                """
                SELECT COUNT(*)
                FROM scenario_jobs AS jobs
                JOIN scenario_runs AS runs ON runs.run_id = jobs.run_id
                WHERE jobs.status = 'RUNNING'
                  AND runs.status = 'RUNNING'
                  AND runs.deleted_at IS NULL
                """
            ).fetchone()
        integrity = "ok" if [str(row[0]) for row in integrity_rows] == ["ok"] else "failed"
        return {
            "status": "ok" if integrity == "ok" else "error",
            "database_integrity": integrity,
            "published_scenarios": int(published[0]) if published is not None else 0,
            "queued_jobs": int(queued[0]) if queued is not None else 0,
            "running_jobs": int(running[0]) if running is not None else 0,
        }

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> PublishedScenario:
        snapshot_json = str(row[4])
        parsed = json.loads(snapshot_json)
        if parsed.get("schema_version") != "framework-eval-scenario-snapshot.v2":
            raise ValueError("Stored Scenario snapshot schema is invalid")
        return PublishedScenario(
            case_id=str(row[0]),
            source_hash=str(row[1]),
            title=str(row[2]),
            case_kind=str(row[3]),
            snapshot_json=snapshot_json,
            publication_status=str(row[5]),
            calibration_state=str(row[6]),
            published_at=str(row[7]),
        )


def _is_successful_formal_outcome(value: object) -> bool:
    """Accept only a fully closed execution side whose deterministic checks all passed."""

    if not isinstance(value, dict):
        return False
    verification = value.get("verification")
    if not isinstance(verification, dict) or verification.get("status") != "PASS":
        return False
    checks = verification.get("checks")
    if not isinstance(checks, list) or not checks:
        return False
    return (
        value.get("terminal_status") == "COMPLETED"
        and value.get("reached_comparable_business_terminal") is True
        and value.get("assessment_compatible") is True
        and not value.get("case_hard_gates")
        and not value.get("global_hard_gates")
        and all(isinstance(check, dict) and check.get("status") == "PASS" for check in checks)
    )
