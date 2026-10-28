"""Isolated v2 Run lifecycle, queue, scoring, and trusted report runtime."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from bench.evaluation_contracts import (
    UNIFIED_SCORING_PROFILE,
    ArtifactSource,
    ContractValidationError,
    ExecutionStrategy,
    FindingClosure,
    ScoringProfile,
    StrategyStage,
    freeze_evaluation_contract,
    validate_scoring_profile,
)
from bench.helixharness_source import HelixHarnessSourceError, frozen_plugin_tree, git_repository_root
from bench.resource_accounting import summarize_resource_observation
from bench.scenario_execution_support import (
    ScenarioExecutionSupportError,
    git_file,
    safe_relative_path,
)
from bench.scenario_storage import ScenarioStore, StoredScenarioCalculation
from bench.scenario_strategy import HelixHarnessRepositoryRefreshError, resolve_reference_commit
from bench.scoring import CaseEvaluation, ResourceObservation, SideEvaluation, aggregate_run
from bench.workflow_goal_control import (
    WorkflowGoalContractError,
    parse_strategy_workflow_recovery,
)
from bench.workload_policy import (
    FORMAL_EVALUATION,
    MAX_SCENARIOS_PER_RUN,
    MAX_WORKLOAD_CONCURRENCY,
    SCENARIO_EXECUTION_WORKLOADS,
    workload_kind_from_runtime_policy,
    workload_kinds_for_pool,
)

try:
    import sqlite3
except ImportError:  # pragma: no cover - depends on the host Python build
    from pysqlite3 import dbapi2 as sqlite3

ScenarioRunKind: TypeAlias = Literal["DEVELOPMENT", "FORMAL"]
ScenarioExecutionAdapterName: TypeAlias = Literal["MOCK", "CODEX"]
ScenarioSide: TypeAlias = Literal["BASE", "CANDIDATE"]
PAIRED_EXECUTION_SCOPE = "PAIRED"
BASE_ONLY_EXECUTION_SCOPE = "BASE_ONLY"

PAIRED_EXECUTION_SCOPE = "PAIRED"
BASE_ONLY_EXECUTION_SCOPE = "BASE_ONLY"


class ScenarioRuntimeError(ValueError):
    """Raised when a v2 Run transition would violate its frozen contract."""


class ScenarioExecutionInterruption(RuntimeError):
    """Execution interruption carrying bounded resource evidence for one Attempt."""

    def __init__(
        self,
        message: str,
        *,
        attempt_record: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.attempt_record = attempt_record


class ScenarioRetryableExecutionError(ScenarioExecutionInterruption):
    """Raised for a bounded infrastructure failure before trusted completion."""


class ScenarioPauseExecutionError(ScenarioExecutionInterruption):
    """Raised when operator recovery is required before checkpoint resume."""


@dataclass(frozen=True)
class ScenarioRunCreateRequest:
    """User-confirmed inputs required to preview and freeze one v2 Run."""

    scenario_ids: tuple[str, ...]
    run_kind: ScenarioRunKind
    execution_adapter: ScenarioExecutionAdapterName
    evaluation_contract: dict[str, object]
    created_by: str
    runtime_policy: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ScenarioRunPreview:
    """Deterministic creation preview used as a confirmation token."""

    preview_hash: str
    ready: bool
    blockers: tuple[str, ...]
    scenario_versions: tuple[tuple[str, str], ...]
    contract_hash: str


@dataclass(frozen=True)
class ScenarioRun:
    """One immutable v2 task plus its mutable lifecycle status."""

    run_id: str
    status: str
    run_kind: str
    execution_adapter: str
    workload_kind: str
    contract_hash: str
    preview_hash: str
    created_by: str
    created_at: str
    completed_at: str | None
    current_calculation_id: str | None
    error: str | None
    scenario_ids: tuple[str, ...]


@dataclass(frozen=True)
class ClaimedScenarioJob:
    """One leased Scenario/side execution job."""

    job_id: str
    run_id: str
    case_id: str
    side: ScenarioSide
    attempt_number: int
    lease_token: str
    contract: dict[str, object]
    runtime_policy: dict[str, object]
    snapshot: dict[str, object]
    transient_failure_count: int = 0


@dataclass(frozen=True)
class ScenarioCalculation:
    """One immutable aggregation appended to a completed Run."""

    calculation_id: str
    run_id: str
    sequence_number: int
    reason: str
    contract_hash: str
    result: dict[str, object]
    created_at: str


@dataclass(frozen=True)
class ScenarioWorkerPollResult:
    """Observable result of processing at most one v2 queue item."""

    status: str
    job_id: str | None


class ScenarioExecutionAdapter(Protocol):
    """Replaceable execution seam shared by Mock smoke and real Codex jobs."""

    name: str

    def execute(self, job: ClaimedScenarioJob) -> dict[str, object]:
        """Return immutable execution evidence and absolute side scores."""


@runtime_checkable
class ScenarioRunWorkspaceCleaner(Protocol):
    """Optional adapter seam for applying terminal Run workspace retention."""

    def cleanup_run_workspace(self, run_id: str, job_ids: tuple[str, ...]) -> None:
        """Delete heavy runtime state and retain bounded diagnostics by policy."""


@runtime_checkable
class ScenarioStrategyValidator(Protocol):
    """Validate frozen Strategy facts during stateless Formal preview."""

    def validate(self, contract: dict[str, object]) -> None:
        """Raise when exact Commit, Adapter, or source document facts are invalid."""


class GitScenarioStrategyValidator:
    """Resolve Formal Strategy facts from the deployed HelixHarness Git cache."""

    def __init__(
        self, repository: Path, *, reference_ref: str = "master", plugin_path: str | None = None,
    ) -> None:
        self.repository = repository.resolve()
        self.reference_ref = reference_ref
        self.plugin_path = plugin_path

    def validate(self, contract: dict[str, object]) -> None:
        """Require the configured baseline and both sides' exact versioned documents."""

        try:
            self.repository = git_repository_root(self.repository)
            baseline = resolve_reference_commit(self.repository, self.reference_ref)
        except (HelixHarnessSourceError, HelixHarnessRepositoryRefreshError) as exc:
            raise ScenarioRuntimeError(str(exc)) from exc
        strategies = _object(contract.get("strategies"), "Strategies")
        for side in ("BASE", "CANDIDATE"):
            strategy = _object(strategies.get(side), f"{side} Strategy")
            adapter = _object(strategy.get("adapter"), f"{side} Strategy Adapter")
            if adapter.get("adapter_id") != "known-helixharness" or adapter.get("adapter_version") != "1":
                raise ScenarioRuntimeError(f"{side} Strategy Adapter is unsupported by the v2 pilot")
            commit = str(adapter.get("source_commit", ""))
            resolved = self._commit(commit)
            if resolved != commit:
                raise ScenarioRuntimeError(f"{side} Strategy Commit is not exact")
            if side == "BASE" and commit != baseline:
                raise ScenarioRuntimeError("BASE Strategy must freeze the latest configured baseline Commit")
            hashes = _object(adapter.get("source_document_hashes"), f"{side} source document Hashes")
            try:
                plugin_tree = frozen_plugin_tree(
                    self.repository, commit, plugin_path=adapter.get("source_plugin_path"),
                )
            except HelixHarnessSourceError as exc:
                raise ScenarioRuntimeError(str(exc)) from exc
            for name, expected in hashes.items():
                try:
                    relative = safe_relative_path(name, label=f"{side} source document")
                    content = git_file(self.repository, plugin_tree, relative.as_posix())
                except ScenarioExecutionSupportError as exc:
                    raise ScenarioRuntimeError(str(exc)) from exc
                if sha256(content).hexdigest() != expected:
                    raise ScenarioRuntimeError(f"{side} Strategy source document Hash mismatch: {name}")

    def _commit(self, revision: str) -> str:
        completed = subprocess.run(
            ("git", "-C", str(self.repository), "rev-parse", "--verify", f"{revision}^{{commit}}"),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise ScenarioRuntimeError(f"HelixHarness Strategy revision is unavailable: {revision}")
        return completed.stdout.strip()


class MockScenarioExecutionAdapter:
    """Deterministic offline adapter used only by deployment smoke tests."""

    name = "MOCK"

    def execute(self, job: ClaimedScenarioJob) -> dict[str, object]:
        """Produce contract-shaped evidence with a controlled Candidate gain."""

        strategy = _strategy(job.contract, job.side)
        candidate = job.side == "CANDIDATE"
        score = 80.0 if candidate else 60.0
        evidence = [
            {
                "evidence_id": str(item["source_id"]),
                "kind": "STAGE_ARTIFACT_SOURCE",
                "locator": str(item["root"]),
                "status": "PRESENT",
                "sha256": sha256(
                    f"{job.run_id}:{job.case_id}:{job.side}:{item['source_id']}".encode("utf-8")
                ).hexdigest(),
            }
            for item in _object_list(strategy.get("artifact_sources"), "Strategy artifact sources")
        ]
        profile = _object(job.contract.get("scoring_profile"), "Scoring Profile")
        workflow_axes = _object(profile.get("workflow_axes"), "Workflow axes")
        outcome_axes = _object(profile.get("outcome_weights"), "Outcome weights")
        axis_scores = {str(axis): score for axis in workflow_axes}
        agent_tokens = 800 if candidate else 1_000
        active_execution_seconds = 80 if candidate else 100
        return {
            "schema_version": "framework-eval-execution-outcome.v2",
            "adapter": self.name,
            "terminal_status": "COMPLETED",
            "outcome_quality": score,
            "outcome_axis_scores": {str(axis): score for axis in outcome_axes},
            "framework_workflow_quality": score,
            "workflow_axis_scores": axis_scores,
            "resources": {
                "agent_tokens": agent_tokens,
                "active_execution_seconds": active_execution_seconds,
                "workflow_rework_actions": 1 if candidate else 2,
            },
            "case_hard_gates": [],
            "global_hard_gates": [],
            "reached_comparable_business_terminal": True,
            "assessment_compatible": True,
            "incompatibility_reasons": [],
            "evidence": evidence,
            "verification": {"status": "PASS", "checks": []},
            "execution": {
                "attempt_record": {
                    "schema_version": "framework-eval-attempt-resource.v1",
                    "terminal_status": "COMPLETED",
                    "started_at_ms": 1_000,
                    "completed_at_ms": 1_000 + active_execution_seconds * 1_000,
                    "duration_ms": active_execution_seconds * 1_000,
                    "usage": {"total_tokens": agent_tokens},
                    "evidence_complete": True,
                    "turn_evidence": [],
                }
            },
        }


class ScenarioRunService:
    """Create, query, calculate, and report isolated Scenario v2 Runs."""

    def __init__(
        self,
        store: ScenarioStore,
        *,
        strategy_validator: ScenarioStrategyValidator | None = None,
    ) -> None:
        self.store = store
        self.strategy_validator = strategy_validator
        self.store.initialize()

    def preview(self, request: ScenarioRunCreateRequest) -> ScenarioRunPreview:
        """Validate a prospective Run and freeze the exact Scenario versions."""

        _validate_request(request)
        blockers: list[str] = []
        versions: list[tuple[str, str]] = []
        for case_id in request.scenario_ids:
            scenario = self.store.get(case_id)
            if scenario is None or scenario.publication_status != "PUBLISHED":
                blockers.append(f"{case_id} is not published")
                continue
            versions.append((case_id, scenario.source_hash))
            if request.run_kind == "FORMAL" and (
                scenario.case_kind != "REPOSITORY_CASE" or scenario.calibration_state != "CALIBRATED"
            ):
                blockers.append(f"{case_id} is not a calibrated REPOSITORY_CASE")
        if request.run_kind == "FORMAL" and request.execution_adapter != "CODEX":
            blockers.append("Formal Run requires the CODEX execution adapter")
        if request.run_kind == "FORMAL":
            if self.strategy_validator is None:
                blockers.append("Formal Run requires the deployed HelixHarness Strategy repository")
            else:
                try:
                    self.strategy_validator.validate(request.evaluation_contract)
                except (ScenarioRuntimeError, OSError) as exc:
                    blockers.append(str(exc))
        runtime_policy = _frozen_runtime_policy(request)
        payload = {
            "schema_version": "framework-eval-run-preview.v2",
            "scenario_versions": versions,
            "run_kind": request.run_kind,
            "execution_adapter": request.execution_adapter,
            "contract_hash": str(request.evaluation_contract["contract_hash"]),
            "created_by": request.created_by.strip(),
            "runtime_policy": runtime_policy,
            "blockers": blockers,
        }
        return ScenarioRunPreview(
            preview_hash=_hash_json(payload),
            ready=not blockers,
            blockers=tuple(blockers),
            scenario_versions=tuple(versions),
            contract_hash=str(request.evaluation_contract["contract_hash"]),
        )

    def create(self, request: ScenarioRunCreateRequest, *, expected_preview_hash: str) -> ScenarioRun:
        """Freeze one confirmed preview and enqueue its declared execution sides."""

        preview = self.preview(request)
        if expected_preview_hash != preview.preview_hash:
            raise ScenarioRuntimeError("Run preview changed; preview and confirm again")
        if not preview.ready:
            raise ScenarioRuntimeError("Run is not ready: " + "; ".join(preview.blockers))
        now = _now()
        run_id = f"sv2_{uuid.uuid4().hex}"
        contract_json = _canonical_json(request.evaluation_contract)
        runtime_policy = _frozen_runtime_policy(request)
        workload_kind = workload_kind_from_runtime_policy(runtime_policy)
        with sqlite3.connect(self.store.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO scenario_runs(
                    run_id, status, run_kind, execution_adapter, workload_kind, contract_json,
                    contract_hash, runtime_policy_json, preview_hash, created_by, created_at
                ) VALUES (?, 'QUEUED', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    request.run_kind,
                    request.execution_adapter,
                    workload_kind,
                    contract_json,
                    preview.contract_hash,
                    _canonical_json(runtime_policy),
                    preview.preview_hash,
                    request.created_by.strip(),
                    now,
                ),
            )
            job_sequence = 0
            for case_sequence, (case_id, source_hash) in enumerate(preview.scenario_versions, start=1):
                scenario = self.store.get(case_id)
                if scenario is None or scenario.source_hash != source_hash:
                    raise ScenarioRuntimeError("Scenario version changed during Run creation")
                connection.execute(
                    """
                    INSERT INTO scenario_run_cases(
                        run_id, case_id, sequence_number, source_hash, snapshot_json
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_id, case_id, case_sequence, source_hash, scenario.snapshot_json),
                )
                for side in _execution_sides(request):
                    job_sequence += 1
                    connection.execute(
                        """
                        INSERT INTO scenario_jobs(
                            job_id, run_id, case_id, side, sequence_number, status
                        ) VALUES (?, ?, ?, ?, ?, 'PENDING')
                        """,
                        (f"sj_{uuid.uuid4().hex}", run_id, case_id, side, job_sequence),
                    )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> ScenarioRun:
        """Return one v2 Run and its frozen Case order."""

        with sqlite3.connect(self.store.database_path) as connection:
            row = connection.execute(
                """
                SELECT run_id, status, run_kind, execution_adapter, contract_hash,
                       workload_kind, preview_hash, created_by, created_at, completed_at,
                       current_calculation_id, error
                FROM scenario_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            cases = connection.execute(
                """
                SELECT case_id FROM scenario_run_cases
                WHERE run_id = ? ORDER BY sequence_number
                """,
                (run_id,),
            ).fetchall()
        if row is None:
            raise ScenarioRuntimeError("Scenario Run not found")
        return ScenarioRun(
            run_id=str(row[0]),
            status=str(row[1]),
            run_kind=str(row[2]),
            execution_adapter=str(row[3]),
            contract_hash=str(row[4]),
            workload_kind=str(row[5]),
            preview_hash=str(row[6]),
            created_by=str(row[7]),
            created_at=str(row[8]),
            completed_at=str(row[9]) if row[9] is not None else None,
            current_calculation_id=str(row[10]) if row[10] is not None else None,
            error=str(row[11]) if row[11] is not None else None,
            scenario_ids=tuple(str(item[0]) for item in cases),
        )

    def list_runs(self) -> list[ScenarioRun]:
        """Return all visible v2 Runs without mixing them into the v1 task list."""

        with sqlite3.connect(self.store.database_path) as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM scenario_runs
                WHERE deleted_at IS NULL
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [self.get_run(str(row[0])) for row in rows]

    def delete(self, run_id: str) -> None:
        """Hide one non-running Run while retaining its immutable audit records."""

        now = _now()
        with sqlite3.connect(self.store.database_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, deleted_at FROM scenario_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None or row[1] is not None:
                raise ScenarioRuntimeError("Scenario Run not found")
            status = str(row[0])
            if status == "RUNNING":
                raise ScenarioRuntimeError("执行中的评测任务不能删除")
            if status == "PAUSED":
                raise ScenarioRuntimeError("已暂停的评测任务请先恢复或取消，不能直接删除")
            if status in {"QUEUED", "SUSPENDED"}:
                connection.execute(
                    """
                    UPDATE scenario_jobs
                    SET status = 'CANCELLED', completed_at = ?, error = ?
                    WHERE run_id = ? AND status = 'PENDING'
                    """,
                    (now, "任务已由用户从当前列表删除", run_id),
                )
                connection.execute(
                    """
                    UPDATE scenario_runs
                    SET status = 'CANCELLED', completed_at = ?, error = ?, deleted_at = ?
                    WHERE run_id = ?
                    """,
                    (now, "任务已由用户从当前列表删除", now, run_id),
                )
            else:
                connection.execute(
                    "UPDATE scenario_runs SET deleted_at = ? WHERE run_id = ?",
                    (now, run_id),
                )

    def suspend(self, run_id: str) -> ScenarioRun:
        """Hold one queued Run without changing its frozen Jobs or audit identity."""

        with sqlite3.connect(self.store.database_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM scenario_runs WHERE run_id = ? AND deleted_at IS NULL",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ScenarioRuntimeError("Scenario Run not found")
            if str(row[0]) != "QUEUED":
                raise ScenarioRuntimeError("只能暂停排队中的评测任务")
            active = connection.execute(
                "SELECT COUNT(*) FROM scenario_jobs WHERE run_id = ? AND status = 'RUNNING'",
                (run_id,),
            ).fetchone()
            if int(active[0] if active is not None else 0):
                raise ScenarioRuntimeError("任务已经开始执行，不能暂停排队")
            pending = connection.execute(
                "SELECT COUNT(*) FROM scenario_jobs WHERE run_id = ? AND status = 'PENDING'",
                (run_id,),
            ).fetchone()
            if int(pending[0] if pending is not None else 0) == 0:
                raise ScenarioRuntimeError("排队任务中没有可暂停的执行侧")
            changed = connection.execute(
                "UPDATE scenario_runs SET status = 'SUSPENDED' WHERE run_id = ? AND status = 'QUEUED'",
                (run_id,),
            ).rowcount
            if changed != 1:  # pragma: no cover - serialized by BEGIN IMMEDIATE
                raise ScenarioRuntimeError("评测任务状态已经变化，请刷新后重试")
        return self.get_run(run_id)

    def resume(self, run_id: str) -> ScenarioRun:
        """Resume an operator-held queue entry or a recoverable paused execution."""

        with sqlite3.connect(self.store.database_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM scenario_runs WHERE run_id = ? AND deleted_at IS NULL",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ScenarioRuntimeError("Scenario Run not found")
            status = str(row[0])
            if status == "SUSPENDED":
                pending = connection.execute(
                    "SELECT COUNT(*) FROM scenario_jobs WHERE run_id = ? AND status = 'PENDING'",
                    (run_id,),
                ).fetchone()
                if int(pending[0] if pending is not None else 0) == 0:
                    raise ScenarioRuntimeError("暂停排队的任务中没有可恢复的执行侧")
                changed = connection.execute(
                    "UPDATE scenario_runs SET status = 'QUEUED' WHERE run_id = ? AND status = 'SUSPENDED'",
                    (run_id,),
                ).rowcount
                if changed != 1:  # pragma: no cover - serialized by BEGIN IMMEDIATE
                    raise ScenarioRuntimeError("评测任务状态已经变化，请刷新后重试")
                connection.commit()
                return self.get_run(run_id)
            if status != "PAUSED":
                raise ScenarioRuntimeError("只能恢复暂停排队或异常暂停的评测任务")
            active = connection.execute(
                "SELECT COUNT(*) FROM scenario_jobs WHERE run_id = ? AND status = 'RUNNING'",
                (run_id,),
            ).fetchone()
            if int(active[0] if active is not None else 0):
                raise ScenarioRuntimeError("当前批次仍有执行侧在安全收尾，请稍后再恢复")
            paused = connection.execute(
                "SELECT COUNT(*) FROM scenario_jobs WHERE run_id = ? AND status = 'PAUSED'",
                (run_id,),
            ).fetchone()
            if int(paused[0] if paused is not None else 0) == 0:
                raise ScenarioRuntimeError("暂停任务中没有可恢复的执行侧")
            connection.execute(
                """
                UPDATE scenario_jobs
                SET status = 'PENDING', error = NULL, started_at = NULL, completed_at = NULL,
                    transient_failure_count = 0,
                    worker_id = NULL, lease_token = NULL, lease_expires_at = NULL
                WHERE run_id = ? AND status = 'PAUSED'
                """,
                (run_id,),
            )
            connection.execute(
                """
                UPDATE scenario_runs
                SET status = 'RUNNING', error = NULL, completed_at = NULL
                WHERE run_id = ? AND status = 'PAUSED'
                """,
                (run_id,),
            )
        return self.get_run(run_id)

    def finalize(
        self,
        run_id: str,
        *,
        reason: str = "initial execution",
    ) -> ScenarioCalculation | None:
        """Atomically aggregate completed evidence and append the first calculation."""

        if self._is_base_only_run(run_id):
            self._finalize_base_only_run(run_id)
            return None
        return self._append_calculation(run_id, reason=reason, finalize_run=True)

    def _is_base_only_run(self, run_id: str) -> bool:
        with sqlite3.connect(self.store.database_path) as connection:
            row = connection.execute(
                "SELECT runtime_policy_json FROM scenario_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ScenarioRuntimeError("Scenario Run not found")
        policy = json.loads(str(row[0]))
        return isinstance(policy, dict) and policy.get("execution_scope") == BASE_ONLY_EXECUTION_SCOPE

    def _finalize_base_only_run(self, run_id: str) -> None:
        """Complete a trusted Base validation without manufacturing paired scoring evidence."""

        with sqlite3.connect(self.store.database_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT status FROM scenario_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise ScenarioRuntimeError("Scenario Run not found")
            if str(run_row[0]) == "COMPLETED":
                return
            jobs = connection.execute(
                "SELECT side, status FROM scenario_jobs WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            if not jobs or any(str(side) != "BASE" for side, _status in jobs):
                raise ScenarioRuntimeError("Base-only Run contains an unexpected execution side")
            if any(str(status) != "COMPLETED" for _side, status in jobs):
                raise ScenarioRuntimeError("Run jobs are not all completed")
            connection.execute(
                """
                UPDATE scenario_runs
                SET status = 'COMPLETED', completed_at = ?, current_calculation_id = NULL
                WHERE run_id = ?
                """,
                (_now(), run_id),
            )

    def recalculate(
        self,
        run_id: str,
        *,
        reason: str,
        use_current_scoring_profile: bool = False,
    ) -> ScenarioCalculation:
        """Append a new calculation over the same immutable execution evidence."""

        run = self.get_run(run_id)
        if run.status != "COMPLETED" or not reason.strip():
            raise ScenarioRuntimeError("Only a completed Run can be recalculated with a reason")
        contract = self._current_scoring_contract(run_id) if use_current_scoring_profile else None
        return self._append_calculation(
            run_id,
            reason=reason.strip(),
            contract=contract,
            allow_scoring_profile_upgrade=use_current_scoring_profile,
        )

    def _current_scoring_contract(self, run_id: str) -> dict[str, object]:
        """Keep both frozen strategies while explicitly upgrading only scoring rules."""

        with sqlite3.connect(self.store.database_path) as connection:
            row = connection.execute(
                "SELECT contract_json FROM scenario_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ScenarioRuntimeError("Scenario Run not found")
        frozen = json.loads(str(row[0]))
        strategies = _object(frozen.get("strategies"), "Frozen Strategies")
        return freeze_evaluation_contract(
            base_strategy=_execution_strategy(
                _object(strategies.get("BASE"), "BASE Strategy"),
                "BASE",
            ),
            candidate_strategy=_execution_strategy(
                _object(strategies.get("CANDIDATE"), "CANDIDATE Strategy"),
                "CANDIDATE",
            ),
            scoring_profile=UNIFIED_SCORING_PROFILE,
        )

    def get_report(self, run_id: str, *, calculation_id: str | None = None) -> dict[str, object]:
        """Return one immutable trusted report, defaulting to the current calculation."""

        run = self.get_run(run_id)
        selected = calculation_id or run.current_calculation_id
        if selected is None:
            raise ScenarioRuntimeError("Run has no calculation report")
        with sqlite3.connect(self.store.database_path) as connection:
            row = connection.execute(
                """
                SELECT report_json FROM scenario_reports
                WHERE run_id = ? AND calculation_id = ?
                """,
                (run_id, selected),
            ).fetchone()
        if row is None:
            raise ScenarioRuntimeError("Scenario report not found")
        report = json.loads(str(row[0]))
        if not isinstance(report, dict):  # pragma: no cover - protected on write
            raise ScenarioRuntimeError("Stored Scenario report is invalid")
        return report

    def _append_calculation(
        self,
        run_id: str,
        *,
        reason: str,
        contract: dict[str, object] | None = None,
        finalize_run: bool = False,
        allow_scoring_profile_upgrade: bool = False,
    ) -> ScenarioCalculation:
        prepared: tuple[
            dict[str, object],
            tuple[CaseEvaluation, ...],
            tuple[dict[str, object], ...],
        ] | None = None
        expected_current_calculation_id: str | None = None
        if not finalize_run:
            with sqlite3.connect(self.store.database_path, timeout=30) as read_connection:
                prepared_row = read_connection.execute(
                    "SELECT status, current_calculation_id FROM scenario_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if prepared_row is None:
                    raise ScenarioRuntimeError("Scenario Run not found")
                if str(prepared_row[0]) != "COMPLETED":
                    raise ScenarioRuntimeError("Only a completed Run can be recalculated")
                expected_current_calculation_id = (
                    str(prepared_row[1]) if prepared_row[1] is not None else None
                )
                prepared = self._calculation_inputs(
                    read_connection,
                    run_id,
                    contract=contract,
                    allow_scoring_profile_upgrade=allow_scoring_profile_upgrade,
                )
        with sqlite3.connect(self.store.database_path, timeout=30) as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT status, current_calculation_id FROM scenario_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise ScenarioRuntimeError("Scenario Run not found")
            status = str(run_row[0])
            current_calculation_id = str(run_row[1]) if run_row[1] is not None else None
            if finalize_run and status == "COMPLETED" and current_calculation_id is not None:
                return self._calculation_by_id(connection, current_calculation_id)
            if finalize_run:
                statuses = connection.execute(
                    "SELECT status FROM scenario_jobs WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
                if not statuses or any(str(row[0]) != "COMPLETED" for row in statuses):
                    raise ScenarioRuntimeError("Run jobs are not all completed")
            elif status != "COMPLETED":
                raise ScenarioRuntimeError("Only a completed Run can be recalculated")
            elif current_calculation_id != expected_current_calculation_id:
                raise ScenarioRuntimeError("Run calculation changed during recalculation; retry with current evidence")
            if prepared is None:
                contract, cases, recalculation_judgments = self._calculation_inputs(
                    connection,
                    run_id,
                    contract=contract,
                    allow_scoring_profile_upgrade=allow_scoring_profile_upgrade,
                )
            else:
                contract, cases, recalculation_judgments = prepared
            profile = _scoring_profile(contract)
            aggregation = aggregate_run(cases, scoring_profile=profile)
            result = asdict(aggregation)
            calculation_id = f"calc_{uuid.uuid4().hex}"
            created_at = _now()
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence_number), 0) + 1 FROM scenario_calculations WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            sequence = int(row[0]) if row is not None else 1
            calculation = ScenarioCalculation(
                calculation_id=calculation_id,
                run_id=run_id,
                sequence_number=sequence,
                reason=reason,
                contract_hash=str(contract["contract_hash"]),
                result=result,
                created_at=created_at,
            )
            connection.execute(
                """
                INSERT INTO scenario_calculations(
                    calculation_id, run_id, sequence_number, reason,
                    contract_hash, contract_json, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    calculation_id,
                    run_id,
                    sequence,
                    reason,
                    str(contract["contract_hash"]),
                    _canonical_json(contract),
                    _canonical_json(result),
                    created_at,
                ),
            )
            report = self._report_payload(
                run_id,
                calculation,
                contract=contract,
                recalculation_judgments=recalculation_judgments,
                connection=connection,
            )
            report_without_hash = dict(report)
            report_hash = _hash_json(report_without_hash)
            report["report_hash"] = report_hash
            connection.execute(
                """
                INSERT INTO scenario_reports(
                    calculation_id, run_id, report_json, report_hash, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (calculation_id, run_id, _canonical_json(report), report_hash, created_at),
            )
            if finalize_run:
                connection.execute(
                    """
                    UPDATE scenario_runs
                    SET status = 'COMPLETED', completed_at = ?, current_calculation_id = ?
                    WHERE run_id = ?
                    """,
                    (created_at, calculation_id, run_id),
                )
            else:
                connection.execute(
                    "UPDATE scenario_runs SET current_calculation_id = ? WHERE run_id = ?",
                    (calculation_id, run_id),
                )
        if finalize_run:
            self.store.ensure_remediation_projection(run_id)
        return calculation

    @staticmethod
    def _calculation_by_id(connection: Any, calculation_id: str) -> ScenarioCalculation:
        row = connection.execute(
            """
            SELECT calculation_id, run_id, sequence_number, reason,
                   contract_hash, result_json, created_at
            FROM scenario_calculations WHERE calculation_id = ?
            """,
            (calculation_id,),
        ).fetchone()
        if row is None:
            raise ScenarioRuntimeError("Completed Run calculation is missing")
        result = json.loads(str(row[5]))
        if not isinstance(result, dict):
            raise ScenarioRuntimeError("Stored Scenario calculation is invalid")
        return ScenarioCalculation(
            calculation_id=str(row[0]),
            run_id=str(row[1]),
            sequence_number=int(row[2]),
            reason=str(row[3]),
            contract_hash=str(row[4]),
            result=result,
            created_at=str(row[6]),
        )

    def _calculation_inputs(
        self,
        connection: Any,
        run_id: str,
        *,
        contract: dict[str, object] | None,
        allow_scoring_profile_upgrade: bool = False,
    ) -> tuple[dict[str, object], tuple[CaseEvaluation, ...], tuple[dict[str, object], ...]]:
        run_row = connection.execute(
            "SELECT contract_json, runtime_policy_json, execution_adapter FROM scenario_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT j.case_id, j.side, j.outcome_json, c.snapshot_json
            FROM scenario_jobs j
            JOIN scenario_run_cases c ON c.run_id = j.run_id AND c.case_id = j.case_id
            WHERE j.run_id = ? ORDER BY j.sequence_number
            """,
            (run_id,),
        ).fetchall()
        if run_row is None:
            raise ScenarioRuntimeError("Scenario Run not found")
        frozen_contract = json.loads(str(run_row[0]))
        calculation_contract = frozen_contract
        if contract is not None:
            if contract == frozen_contract:
                calculation_contract = contract
            elif not allow_scoring_profile_upgrade:
                raise ScenarioRuntimeError("A completed Run cannot replace its frozen evaluation contract")
            else:
                frozen_strategies = _object(frozen_contract.get("strategies"), "Frozen Strategies")
                upgraded_strategies = _object(contract.get("strategies"), "Upgraded Strategies")
                if (
                    contract.get("schema_version") != frozen_contract.get("schema_version")
                    or upgraded_strategies != frozen_strategies
                    or contract.get("scoring_profile")
                    != freeze_evaluation_contract(
                        base_strategy=_execution_strategy(
                            _object(frozen_strategies.get("BASE"), "BASE Strategy"),
                            "BASE",
                        ),
                        candidate_strategy=_execution_strategy(
                            _object(frozen_strategies.get("CANDIDATE"), "CANDIDATE Strategy"),
                            "CANDIDATE",
                        ),
                        scoring_profile=UNIFIED_SCORING_PROFILE,
                    ).get("scoring_profile")
                ):
                    raise ScenarioRuntimeError(
                        "Recalculation may only apply the current scoring profile to frozen strategies"
                    )
                calculation_contract = contract
        outcomes: dict[str, dict[str, dict[str, object]]] = {}
        for case_id, side, outcome_json, snapshot_json in rows:
            del snapshot_json
            if outcome_json is None:
                raise ScenarioRuntimeError("Completed Run has missing execution outcome")
            outcome = json.loads(str(outcome_json))
            if not isinstance(outcome, dict):
                raise ScenarioRuntimeError("Execution outcome is invalid")
            outcomes.setdefault(str(case_id), {})[str(side)] = outcome
        cases: list[CaseEvaluation] = []
        for case_id, sides in outcomes.items():
            base = sides.get("BASE")
            candidate = sides.get("CANDIDATE")
            if base is None or candidate is None:
                raise ScenarioRuntimeError("Paired execution outcome is incomplete")
            compatible, reasons = _assessment_compatibility(
                base=base,
                candidate=candidate,
                contract=calculation_contract,
            )
            cases.append(
                CaseEvaluation(
                    case_id=case_id,
                    base=_side_evaluation(base, calculation_contract),
                    candidate=_side_evaluation(candidate, calculation_contract),
                    assessment_compatible=compatible,
                    incompatibility_reasons=reasons,
                )
            )
        return calculation_contract, tuple(cases), ()

    def _report_payload(
        self,
        run_id: str,
        calculation: ScenarioCalculation,
        *,
        contract: dict[str, object],
        recalculation_judgments: tuple[dict[str, object], ...],
        connection: Any,
    ) -> dict[str, object]:
        run_row = connection.execute(
            """
            SELECT run_kind, execution_adapter, workload_kind, contract_hash,
                   preview_hash, created_by, created_at
            FROM scenario_runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        case_rows = connection.execute(
            """
            SELECT case_id, source_hash FROM scenario_run_cases
            WHERE run_id = ? ORDER BY sequence_number
            """,
            (run_id,),
        ).fetchall()
        job_rows = connection.execute(
            """
            SELECT case_id, side, outcome_json FROM scenario_jobs
            WHERE run_id = ? ORDER BY sequence_number
            """,
            (run_id,),
        ).fetchall()
        if run_row is None:  # pragma: no cover - protected by caller
            raise ScenarioRuntimeError("Scenario Run not found")
        return {
            "schema_version": "framework-eval-report.v2",
            "run": {
                "run_id": run_id,
                "run_kind": str(run_row[0]),
                "execution_adapter": str(run_row[1]),
                "workload_kind": str(run_row[2]),
                "contract_hash": str(run_row[3]),
                "preview_hash": str(run_row[4]),
                "created_by": str(run_row[5]),
                "created_at": str(run_row[6]),
                "scenario_versions": [
                    {"case_id": str(case_id), "source_hash": str(source_hash)}
                    for case_id, source_hash in case_rows
                ],
            },
            "scoring_profile": contract["scoring_profile"],
            "calculation": {
                "calculation_id": calculation.calculation_id,
                "sequence_number": calculation.sequence_number,
                "reason": calculation.reason,
                "contract_hash": calculation.contract_hash,
                "created_at": calculation.created_at,
                **calculation.result,
            },
            "recalculation_judgments": list(recalculation_judgments),
            "execution_outcomes": [
                {
                    "case_id": str(case_id),
                    "side": str(side),
                    "outcome": _public_outcome(json.loads(str(outcome_json))),
                }
                for case_id, side, outcome_json in job_rows
            ],
        }


class ScenarioWorker:
    """Lease and execute one v2 job at a time using an explicit adapter."""

    def __init__(
        self,
        store: ScenarioStore,
        adapter: ScenarioExecutionAdapter,
        *,
        worker_id: str = "scenario-worker",
        workload_kind: str = FORMAL_EVALUATION,
    ) -> None:
        if workload_kind not in SCENARIO_EXECUTION_WORKLOADS:
            raise ValueError("Scenario workload kind is invalid")
        self.store = store
        self.adapter = adapter
        self.worker_id = worker_id
        self.workload_kind = workload_kind
        self.service = ScenarioRunService(store)

    def run_once(self, *, run_id: str | None = None) -> ScenarioWorkerPollResult:
        """Process one queued job, or finalize one fully executed Run."""

        self.store.record_worker_status(
            self.worker_id,
            self.adapter.name,
            "IDLE",
            workload_kind=self.workload_kind,
        )
        _cleanup_terminal_run_workspaces(
            self.store,
            self.adapter,
            workload_kind=self.workload_kind,
        )
        job = _claim_next_job(
            self.store,
            self.worker_id,
            adapter=self.adapter.name,
            run_id=run_id,
            workload_kind=self.workload_kind,
        )
        if job is None:
            target = _ready_to_finalize(
                self.store,
                adapter=self.adapter.name,
                run_id=run_id,
                workload_kind=self.workload_kind,
            )
            if target is not None:
                self.service.finalize(target)
                _cleanup_terminal_run_workspaces(
                    self.store,
                    self.adapter,
                    run_id=target,
                    workload_kind=self.workload_kind,
                )
                return ScenarioWorkerPollResult("FINALIZED", None)
            return ScenarioWorkerPollResult("IDLE", None)
        self.store.record_worker_status(
            self.worker_id,
            self.adapter.name,
            "RUNNING",
            workload_kind=self.workload_kind,
        )
        try:
            with _ScenarioJobLeaseKeeper(self.store, job):
                outcome = self.adapter.execute(job)
            _validate_outcome(outcome)
            _complete_job(self.store, job, outcome)
        except ScenarioPauseExecutionError as exc:
            _pause_job(
                self.store,
                job,
                f"{type(exc).__name__}: {exc}",
                attempt_record=exc.attempt_record,
            )
            return ScenarioWorkerPollResult("PAUSED", job.job_id)
        except OSError as exc:
            if exc.errno in {28, 122}:
                _pause_job(self.store, job, f"{type(exc).__name__}: {exc}")
                return ScenarioWorkerPollResult("PAUSED", job.job_id)
            _fail_job(self.store, job, f"{type(exc).__name__}: {exc}")
            return ScenarioWorkerPollResult("FAILED", job.job_id)
        except ScenarioRetryableExecutionError as exc:
            error = f"{type(exc).__name__}: {exc}"
            if job.transient_failure_count < 2:
                _retry_job(
                    self.store,
                    job,
                    error,
                    attempt_record=exc.attempt_record,
                )
                return ScenarioWorkerPollResult("RETRY_PENDING", job.job_id)
            _pause_job(
                self.store,
                job,
                error,
                attempt_record=exc.attempt_record,
            )
            return ScenarioWorkerPollResult("PAUSED", job.job_id)
        except ScenarioExecutionInterruption as exc:
            _fail_job(
                self.store,
                job,
                f"{type(exc).__name__}: {exc}",
                attempt_record=exc.attempt_record,
            )
            return ScenarioWorkerPollResult("FAILED", job.job_id)
        except Exception as exc:
            _fail_job(self.store, job, f"{type(exc).__name__}: {exc}")
            return ScenarioWorkerPollResult("FAILED", job.job_id)
        finally:
            self.store.record_worker_status(
                self.worker_id,
                self.adapter.name,
                "IDLE",
                workload_kind=self.workload_kind,
            )
        return ScenarioWorkerPollResult("PROCESSED", job.job_id)


def _cleanup_terminal_run_workspaces(
    store: ScenarioStore,
    adapter: ScenarioExecutionAdapter,
    *,
    run_id: str | None = None,
    workload_kind: str = FORMAL_EVALUATION,
) -> None:
    """Best-effort sweep that retries cleanup without changing saved results."""

    if not isinstance(adapter, ScenarioRunWorkspaceCleaner):
        return
    pool_kinds = workload_kinds_for_pool(workload_kind)
    placeholders = ", ".join("?" for _ in pool_kinds)
    parameters: list[object] = [adapter.name, *pool_kinds]
    run_clause = ""
    if run_id is not None:
        run_clause = " AND r.run_id = ?"
        parameters.append(run_id)
    with sqlite3.connect(store.database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT r.run_id, j.job_id
            FROM scenario_runs r
            LEFT JOIN scenario_jobs j ON j.run_id = r.run_id
            WHERE r.execution_adapter = ? AND r.workload_kind IN ({placeholders})
              AND r.status IN ('COMPLETED', 'FAILED', 'CANCELLED'){run_clause}
              AND NOT EXISTS (
                  SELECT 1 FROM scenario_jobs active
                  WHERE active.run_id = r.run_id
                    AND active.status IN ('PENDING', 'RUNNING', 'PAUSED')
              )
            ORDER BY r.created_at, j.sequence_number, j.side
            """,
            tuple(parameters),
        ).fetchall()
    jobs_by_run: dict[str, list[str]] = {}
    for stored_run_id, job_id in rows:
        jobs_by_run.setdefault(str(stored_run_id), [])
        if job_id is not None:
            jobs_by_run[str(stored_run_id)].append(str(job_id))
    for terminal_run_id, job_ids in jobs_by_run.items():
        try:
            adapter.cleanup_run_workspace(terminal_run_id, tuple(job_ids))
        except OSError:
            logging.getLogger(__name__).exception(
                "Failed to clean terminal Scenario Run workspace %s",
                terminal_run_id,
            )


def _claim_next_job(
    store: ScenarioStore,
    worker_id: str,
    *,
    adapter: str,
    run_id: str | None,
    workload_kind: str = FORMAL_EVALUATION,
) -> ClaimedScenarioJob | None:
    if workload_kind not in SCENARIO_EXECUTION_WORKLOADS:
        raise ValueError("Scenario workload kind is invalid")
    now = _now()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    token = uuid.uuid4().hex
    with sqlite3.connect(store.database_path, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE scenario_jobs SET status = 'PENDING', worker_id = NULL, lease_token = NULL,
                lease_expires_at = NULL
            WHERE status = 'RUNNING' AND lease_expires_at < ?
            """,
            (now,),
        )
        pool_kinds = workload_kinds_for_pool(workload_kind)
        placeholders = ", ".join("?" for _ in pool_kinds)
        parameters: list[object] = [adapter, *pool_kinds]
        run_clause = ""
        if run_id is not None:
            run_clause = " AND run_id = ?"
            parameters.append(run_id)
        head = connection.execute(
            f"""
            SELECT run_id, runtime_policy_json FROM scenario_runs
            WHERE status IN ('QUEUED', 'RUNNING') AND execution_adapter = ?
              AND workload_kind IN ({placeholders}){run_clause}
            ORDER BY created_at, run_id LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()
        if head is None:
            return None
        policy = json.loads(str(head[1]))
        max_concurrency = max(1, int(policy.get("max_concurrency", 1))) if isinstance(policy, dict) else 1
        running = connection.execute(
            "SELECT COUNT(*) FROM scenario_jobs WHERE run_id = ? AND status = 'RUNNING'",
            (str(head[0]),),
        ).fetchone()
        if int(running[0] if running is not None else 0) >= max_concurrency:
            return None
        row = connection.execute(
            """
            SELECT j.job_id, j.run_id, j.case_id, j.side, j.attempt_number,
                   j.transient_failure_count,
                   r.contract_json, r.runtime_policy_json, c.snapshot_json
            FROM scenario_jobs j
            JOIN scenario_runs r ON r.run_id = j.run_id
            JOIN scenario_run_cases c ON c.run_id = j.run_id AND c.case_id = j.case_id
            WHERE j.status = 'PENDING' AND j.run_id = ?
            ORDER BY j.sequence_number LIMIT 1
            """,
            (str(head[0]),),
        ).fetchone()
        if row is None:
            return None
        changed = connection.execute(
            """
            UPDATE scenario_jobs
            SET status = 'RUNNING', attempt_number = attempt_number + 1,
                worker_id = ?, lease_token = ?, lease_expires_at = ?, started_at = ?
            WHERE job_id = ? AND status = 'PENDING'
            """,
            (worker_id, token, expires, now, str(row[0])),
        ).rowcount
        if changed != 1:  # pragma: no cover - serialized by BEGIN IMMEDIATE
            return None
        connection.execute(
            "UPDATE scenario_runs SET status = 'RUNNING' WHERE run_id = ? AND status = 'QUEUED'",
            (str(row[1]),),
        )
    return ClaimedScenarioJob(
        job_id=str(row[0]),
        run_id=str(row[1]),
        case_id=str(row[2]),
        side=str(row[3]),  # type: ignore[arg-type]
        attempt_number=int(row[4]) + 1,
        lease_token=token,
        contract=json.loads(str(row[6])),
        runtime_policy=json.loads(str(row[7])),
        snapshot=json.loads(str(row[8])),
        transient_failure_count=int(row[5]),
    )


def _complete_job(store: ScenarioStore, job: ClaimedScenarioJob, outcome: dict[str, object]) -> None:
    persisted_outcome = dict(outcome)
    execution = dict(outcome.get("execution")) if isinstance(outcome.get("execution"), dict) else {}
    raw_attempt_record = execution.pop("attempt_record", None)
    attempt_record = dict(raw_attempt_record) if isinstance(raw_attempt_record, dict) else {}
    raw_turn_evidence = execution.pop("turn_evidence", [])
    turn_evidence = _object_list(raw_turn_evidence, "Execution Turn evidence")
    if not turn_evidence and isinstance(attempt_record.get("turn_evidence"), list):
        turn_evidence = _object_list(
            attempt_record.get("turn_evidence"),
            "Attempt Turn evidence",
        )
    attempt_record["turn_evidence"] = turn_evidence
    if execution:
        persisted_outcome["execution"] = execution
    else:
        persisted_outcome.pop("execution", None)
    completed_stage_ids = execution.get("completed_stage_ids", [])
    frozen_progress = [str(item) for item in completed_stage_ids if isinstance(item, str)]
    with sqlite3.connect(store.database_path, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_active_job(connection, job)
        _persist_attempt_evidence(
            connection,
            job,
            attempt_record=attempt_record,
            disposition="COMPLETED",
            error=None,
            fallback_resources=outcome.get("resources"),
        )
        attributable = outcome.get("resources")
        attributable_resources = attributable if isinstance(attributable, dict) else {}
        persisted_outcome["resources"] = summarize_resource_observation(
            attributable=attributable_resources,
            attempts=_attempt_resource_records(connection, job.job_id),
            expected_attempt_count=job.attempt_number,
        )
        changed = connection.execute(
            """
            UPDATE scenario_jobs
            SET status = 'COMPLETED', outcome_json = ?, workflow_progress_json = ?, error = NULL, completed_at = ?,
                worker_id = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE job_id = ? AND status = 'RUNNING' AND lease_token = ?
            """,
            (
                _canonical_json(persisted_outcome),
                _canonical_json(frozen_progress),
                _now(),
                job.job_id,
                job.lease_token,
            ),
        ).rowcount
        if changed != 1:  # pragma: no cover - protected by the active lease check
            raise ScenarioRuntimeError("Scenario Job lease is no longer active")


def _require_active_job(connection: Any, job: ClaimedScenarioJob) -> None:
    row = connection.execute(
        """
        SELECT 1 FROM scenario_jobs
        WHERE job_id = ? AND status = 'RUNNING' AND lease_token = ?
        """,
        (job.job_id, job.lease_token),
    ).fetchone()
    if row is None:
        raise ScenarioRuntimeError("Scenario Job lease is no longer active")


def _persist_attempt_evidence(
    connection: Any,
    job: ClaimedScenarioJob,
    *,
    attempt_record: dict[str, object] | None,
    disposition: str,
    error: str | None,
    fallback_resources: object = None,
) -> None:
    record = dict(attempt_record or {})
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    if not usage and isinstance(fallback_resources, dict):
        attributable = fallback_resources
        if isinstance(attributable, dict):
            tokens = attributable.get("agent_tokens")
            if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens >= 0:
                usage = {"total_tokens": int(tokens)}
    started_at_ms = _optional_integer(record.get("started_at_ms"), "Attempt started_at_ms")
    completed_at_ms = _optional_integer(record.get("completed_at_ms"), "Attempt completed_at_ms")
    duration_ms = _optional_integer(record.get("duration_ms"), "Attempt duration_ms")
    if duration_ms is None and started_at_ms is not None and completed_at_ms is not None:
        duration_ms = max(0, completed_at_ms - started_at_ms)
    total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    evidence_complete = (
        record.get("evidence_complete") is True
        and started_at_ms is not None
        and completed_at_ms is not None
        and duration_ms is not None
        and isinstance(total_tokens, int)
        and not isinstance(total_tokens, bool)
        and total_tokens >= 0
    )
    terminal_status = record.get("terminal_status")
    if not isinstance(terminal_status, str) or not terminal_status:
        terminal_status = disposition
    error_payload = record.get("error") if isinstance(record.get("error"), dict) else None
    if error_payload is None and error:
        error_payload = {"message": error[:4_000]}
    connection.execute(
        """
        INSERT INTO scenario_job_attempt_evidence(
            job_id, attempt_number, disposition, terminal_status, started_at_ms,
            completed_at_ms, duration_ms, usage_json, evidence_complete,
            error_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.job_id,
            job.attempt_number,
            disposition,
            terminal_status[:64],
            started_at_ms,
            completed_at_ms,
            duration_ms,
            _canonical_json(usage),
            int(evidence_complete),
            _canonical_json(error_payload) if error_payload is not None else None,
            _now(),
        ),
    )
    raw_turns = record.get("turn_evidence", [])
    turns = _object_list(raw_turns, "Attempt Turn evidence") if isinstance(raw_turns, list) else []
    _persist_turn_evidence(connection, job, turns)


def _persist_turn_evidence(
    connection: Any,
    job: ClaimedScenarioJob,
    turns: list[dict[str, Any]],
) -> None:
    for position, item in enumerate(turns, start=1):
        turn_sequence = item.get("turn_sequence", position)
        if not isinstance(turn_sequence, int) or isinstance(turn_sequence, bool) or turn_sequence < 1:
            raise ScenarioRuntimeError("Execution Turn sequence is invalid")
        events = item.get("events", [])
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise ScenarioRuntimeError("Execution Turn events are invalid")
        usage = item.get("usage") if isinstance(item.get("usage"), dict) else {}
        connection.execute(
            """
            INSERT INTO scenario_job_turn_evidence(
                job_id, attempt_number, turn_sequence, turn_id, input_kind, input_text,
                workflow_step_id, milestone_id, classification, started_at_ms,
                completed_at_ms, duration_ms, usage_json, final_response, error_json,
                events_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.attempt_number,
                turn_sequence,
                _optional_bounded_text(item.get("turn_id"), 256),
                _required_bounded_text(item.get("input_kind"), 128, "Execution Turn input kind"),
                _optional_bounded_text(item.get("input_text"), 4_000),
                _optional_bounded_text(item.get("workflow_step_id"), 256),
                _optional_bounded_text(item.get("milestone_id"), 256),
                _required_bounded_text(item.get("classification"), 64, "Execution Turn classification"),
                _optional_integer(item.get("started_at_ms"), "Execution Turn started_at_ms"),
                _optional_integer(item.get("completed_at_ms"), "Execution Turn completed_at_ms"),
                _optional_integer(item.get("duration_ms"), "Execution Turn duration_ms"),
                _canonical_json(usage),
                _optional_bounded_text(item.get("final_response"), 16_000),
                _canonical_json(item["error"]) if isinstance(item.get("error"), dict) else None,
                _canonical_json(events),
                _now(),
            ),
        )


def _attempt_resource_records(connection: Any, job_id: str) -> tuple[dict[str, object], ...]:
    rows = connection.execute(
        """
        SELECT started_at_ms, completed_at_ms, duration_ms, usage_json, evidence_complete
        FROM scenario_job_attempt_evidence
        WHERE job_id = ?
        ORDER BY attempt_number
        """,
        (job_id,),
    ).fetchall()
    return tuple(
        {
            "started_at_ms": row[0],
            "completed_at_ms": row[1],
            "duration_ms": row[2],
            "usage": json.loads(str(row[3])),
            "evidence_complete": bool(row[4]),
        }
        for row in rows
    )


def _required_bounded_text(value: object, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioRuntimeError(f"{label} is invalid")
    return value[:limit]


def _optional_bounded_text(value: object, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ScenarioRuntimeError(f"{label} is invalid")
    return value


def _retry_job(
    store: ScenarioStore,
    job: ClaimedScenarioJob,
    error: str,
    *,
    attempt_record: dict[str, object] | None = None,
) -> None:
    with sqlite3.connect(store.database_path, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_active_job(connection, job)
        _persist_attempt_evidence(
            connection,
            job,
            attempt_record=attempt_record,
            disposition="RETRY_PENDING",
            error=error,
        )
        changed = connection.execute(
            """
            UPDATE scenario_jobs
            SET status = 'PENDING', error = ?, started_at = NULL, completed_at = NULL,
                transient_failure_count = transient_failure_count + 1,
                worker_id = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE job_id = ? AND status = 'RUNNING' AND lease_token = ?
            """,
            (error[:4000], job.job_id, job.lease_token),
        ).rowcount
        if changed != 1:  # pragma: no cover - protected by the active lease check
            raise ScenarioRuntimeError("Scenario Job lease is no longer active")


def _pause_job(
    store: ScenarioStore,
    job: ClaimedScenarioJob,
    error: str,
    *,
    attempt_record: dict[str, object] | None = None,
) -> None:
    with sqlite3.connect(store.database_path, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        _require_active_job(connection, job)
        _persist_attempt_evidence(
            connection,
            job,
            attempt_record=attempt_record,
            disposition="PAUSED",
            error=error,
        )
        changed = connection.execute(
            """
            UPDATE scenario_jobs
            SET status = 'PAUSED', error = ?, completed_at = ?,
                worker_id = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE job_id = ? AND status = 'RUNNING' AND lease_token = ?
            """,
            (error[:4000], _now(), job.job_id, job.lease_token),
        ).rowcount
        if changed != 1:
            raise ScenarioRuntimeError("Scenario Job lease is no longer active")
        connection.execute(
            """
            UPDATE scenario_runs
            SET status = 'PAUSED', error = ?, completed_at = NULL
            WHERE run_id = ? AND status IN ('QUEUED', 'RUNNING')
            """,
            (error[:4000], job.run_id),
        )


def _fail_job(
    store: ScenarioStore,
    job: ClaimedScenarioJob,
    error: str,
    *,
    attempt_record: dict[str, object] | None = None,
) -> None:
    with sqlite3.connect(store.database_path, timeout=30) as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            """
            SELECT 1 FROM scenario_jobs
            WHERE job_id = ? AND status = 'RUNNING' AND lease_token = ?
            """,
            (job.job_id, job.lease_token),
        ).fetchone()
        if active is None:
            return
        _persist_attempt_evidence(
            connection,
            job,
            attempt_record=attempt_record,
            disposition="FAILED",
            error=error,
        )
        changed = connection.execute(
            """
            UPDATE scenario_jobs
            SET status = 'FAILED', error = ?, completed_at = ?,
                worker_id = NULL, lease_token = NULL, lease_expires_at = NULL
            WHERE job_id = ? AND lease_token = ?
            """,
            (error[:4000], _now(), job.job_id, job.lease_token),
        ).rowcount
        if changed == 1:
            connection.execute(
                """
                UPDATE scenario_jobs
                SET status = 'CANCELLED', error = ?, completed_at = ?
                WHERE run_id = ? AND status IN ('PENDING', 'PAUSED')
                """,
                ("同一任务已有执行侧失败，剩余排队项不再执行", _now(), job.run_id),
            )
            connection.execute(
                "UPDATE scenario_runs SET status = 'FAILED', error = ? WHERE run_id = ?",
                (error[:4000], job.run_id),
            )


class _ScenarioJobLeaseKeeper:
    """Renew the owned job lease while Codex, verification, and Judge are running."""

    def __init__(self, store: ScenarioStore, job: ClaimedScenarioJob) -> None:
        self.store = store
        self.job = job
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"lease-{job.job_id}", daemon=True)

    def __enter__(self) -> _ScenarioJobLeaseKeeper:
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stopped.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stopped.wait(60):
            expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
            try:
                with sqlite3.connect(self.store.database_path, timeout=30) as connection:
                    changed = connection.execute(
                        """
                        UPDATE scenario_jobs SET lease_expires_at = ?
                        WHERE job_id = ? AND status = 'RUNNING' AND lease_token = ?
                        """,
                        (expires, self.job.job_id, self.job.lease_token),
                    ).rowcount
            except sqlite3.Error:
                continue
            if changed != 1:
                return


def _ready_to_finalize(
    store: ScenarioStore,
    *,
    adapter: str,
    run_id: str | None,
    workload_kind: str = FORMAL_EVALUATION,
) -> str | None:
    pool_kinds = workload_kinds_for_pool(workload_kind)
    placeholders = ", ".join("?" for _ in pool_kinds)
    parameters: list[object] = [adapter, *pool_kinds]
    run_clause = ""
    if run_id is not None:
        run_clause = " AND r.run_id = ?"
        parameters.append(run_id)
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            f"""
            SELECT r.run_id
            FROM scenario_runs r
            WHERE r.status = 'RUNNING' AND r.execution_adapter = ?
              AND r.workload_kind IN ({placeholders}){run_clause}
              AND NOT EXISTS (
                  SELECT 1 FROM scenario_jobs j
                  WHERE j.run_id = r.run_id AND j.status != 'COMPLETED'
              )
            ORDER BY r.created_at LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()
    return str(row[0]) if row is not None else None


def _validate_request(request: ScenarioRunCreateRequest) -> None:
    if request.run_kind not in {"DEVELOPMENT", "FORMAL"}:
        raise ScenarioRuntimeError("Run kind must be DEVELOPMENT or FORMAL")
    if request.execution_adapter not in {"MOCK", "CODEX"}:
        raise ScenarioRuntimeError("Execution adapter must be MOCK or CODEX")
    if not request.scenario_ids or any(not case_id for case_id in request.scenario_ids):
        raise ScenarioRuntimeError("At least one non-empty Scenario ID is required")
    if len(request.scenario_ids) > MAX_SCENARIOS_PER_RUN:
        raise ScenarioRuntimeError(f"A Run can select at most {MAX_SCENARIOS_PER_RUN} Scenarios")
    if len(set(request.scenario_ids)) != len(request.scenario_ids):
        raise ScenarioRuntimeError("Scenario IDs must be unique")
    if not request.created_by.strip():
        raise ScenarioRuntimeError("Run creator is required")
    _validate_contract(request.evaluation_contract)
    _validate_runtime_policy(request)
    if request.execution_adapter == "CODEX":
        strategies = _object(request.evaluation_contract.get("strategies"), "Strategies")
        for side in ("BASE", "CANDIDATE"):
            strategy = _object(strategies.get(side), f"{side} Strategy")
            if strategy.get("finding_policy") != "NONE" or strategy.get("finding_closures") != []:
                raise ScenarioRuntimeError("Scenario v2 pilot does not yet execute declared Finding closures")
            try:
                parse_strategy_workflow_recovery(strategy.get("recovery_policy"))
            except WorkflowGoalContractError as exc:
                raise ScenarioRuntimeError(f"{side} Strategy recovery policy is invalid: {exc}") from exc


def _execution_sides(request: ScenarioRunCreateRequest) -> tuple[ScenarioSide, ...]:
    scope = request.runtime_policy.get("execution_scope", PAIRED_EXECUTION_SCOPE)
    if scope == BASE_ONLY_EXECUTION_SCOPE:
        return ("BASE",)
    return ("BASE", "CANDIDATE")


def _validate_contract(contract: dict[str, object]) -> None:
    if contract.get("schema_version") != "framework-eval-run-contract.v2":
        raise ScenarioRuntimeError("Evaluation contract schema is invalid")
    try:
        strategies = _object(contract.get("strategies"), "Strategies")
        rebuilt = freeze_evaluation_contract(
            base_strategy=_execution_strategy(_object(strategies.get("BASE"), "BASE Strategy"), "BASE"),
            candidate_strategy=_execution_strategy(
                _object(strategies.get("CANDIDATE"), "CANDIDATE Strategy"),
                "CANDIDATE",
            ),
            scoring_profile=_scoring_profile(contract),
        )
    except (ContractValidationError, KeyError, TypeError, ValueError) as exc:
        raise ScenarioRuntimeError(f"Evaluation contract is invalid: {exc}") from exc
    if rebuilt != contract:
        raise ScenarioRuntimeError("Evaluation contract contains untrusted or inconsistent derived fields")


def _execution_sides(request: ScenarioRunCreateRequest) -> tuple[ScenarioSide, ...]:
    scope = request.runtime_policy.get("execution_scope", PAIRED_EXECUTION_SCOPE)
    if scope == BASE_ONLY_EXECUTION_SCOPE:
        return ("BASE",)
    return ("BASE", "CANDIDATE")


def _execution_strategy(raw: dict[str, Any], side: str) -> ExecutionStrategy:
    adapter = _object(raw.get("adapter"), f"{side} Strategy Adapter")
    hashes = _object(adapter.get("source_document_hashes"), f"{side} Strategy source hashes")
    commands = _string_tuple(raw.get("commands"), f"{side} Strategy commands")
    completion_states = _string_tuple(raw.get("completion_states"), f"{side} completion states")
    stages = tuple(
        StrategyStage(
            stage_id=_text(item.get("stage_id"), f"{side} stage_id"),
            semantic_role=_text(item.get("semantic_role"), f"{side} stage semantic_role"),
            command=_text(item.get("command"), f"{side} stage command"),
            completion_state=_text(item.get("completion_state"), f"{side} stage completion_state"),
            checkpoint=_boolean(item.get("checkpoint"), f"{side} stage checkpoint"),
        )
        for item in _object_list(raw.get("stages"), f"{side} Strategy stages")
    )
    artifact_sources = tuple(
        ArtifactSource(
            source_id=_text(item.get("source_id"), f"{side} artifact source_id"),
            root=_text(item.get("root"), f"{side} artifact root"),
            include_glob=_text(item.get("include_glob"), f"{side} artifact include_glob"),
            artifact_schema=_text(item.get("artifact_schema"), f"{side} artifact schema"),
            deterministic_checks=_string_tuple(
                item.get("deterministic_checks"),
                f"{side} artifact deterministic_checks",
            ),
            required=_boolean(item.get("required"), f"{side} artifact required"),
            max_files=_positive_integer(item.get("max_files"), f"{side} artifact max_files"),
            max_total_bytes=_positive_integer(
                item.get("max_total_bytes"),
                f"{side} artifact max_total_bytes",
            ),
        )
        for item in _object_list(raw.get("artifact_sources"), f"{side} Strategy artifact sources")
    )
    closures = tuple(
        FindingClosure(
            finding_type=_text(item.get("finding_type"), f"{side} finding_type"),
            remediation_command=_text(item.get("remediation_command"), f"{side} remediation command"),
            return_stage=_text(item.get("return_stage"), f"{side} return stage"),
            max_rounds=_positive_integer(item.get("max_rounds"), f"{side} finding max_rounds"),
        )
        for item in _object_list(raw.get("finding_closures"), f"{side} finding closures")
    )
    recovery = raw.get("recovery_policy")
    if not isinstance(recovery, dict):
        raise ScenarioRuntimeError(f"{side} recovery_policy must be an object")
    return ExecutionStrategy(
        strategy_id=_text(raw.get("strategy_id"), f"{side} strategy_id"),
        version=_text(raw.get("version"), f"{side} Strategy version"),
        adapter_id=_text(adapter.get("adapter_id"), f"{side} adapter_id"),
        adapter_version=_text(adapter.get("adapter_version"), f"{side} adapter_version"),
        source_commit=_text(adapter.get("source_commit"), f"{side} source_commit"),
        source_plugin_path=adapter.get("source_plugin_path"),
        source_document_hashes=tuple(
            (_text(path, f"{side} source document path"), _text(value, f"{side} source document Hash"))
            for path, value in hashes.items()
        ),
        commands=commands,
        stages=stages,
        completion_states=completion_states,
        artifact_sources=artifact_sources,
        finding_policy=_text(raw.get("finding_policy"), f"{side} finding_policy"),  # type: ignore[arg-type]
        finding_closures=closures,
        recovery_policy=recovery,
        source_ref=(
            _text(adapter.get("source_ref"), f"{side} source_ref")
            if adapter.get("source_ref") is not None
            else None
        ),
    )
def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScenarioRuntimeError(f"{label} must be non-empty text")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ScenarioRuntimeError(f"{label} must be a boolean")
    return value


def _positive_integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ScenarioRuntimeError(f"{label} must be a positive integer")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ScenarioRuntimeError(f"{label} must be a list of non-empty strings")
    return tuple(value)


def _number(value: object, label: str) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ScenarioRuntimeError(f"{label} must be numeric")
    return value


def _validate_runtime_policy(request: ScenarioRunCreateRequest) -> None:
    policy = request.runtime_policy
    if not isinstance(policy, dict):
        raise ScenarioRuntimeError("Runtime Policy must be an object")
    explicit_workload = policy.get("workload_kind")
    if explicit_workload is not None and explicit_workload not in SCENARIO_EXECUTION_WORKLOADS:
        raise ScenarioRuntimeError("Runtime Policy workload kind is invalid")
    execution_scope = policy.get("execution_scope", PAIRED_EXECUTION_SCOPE)
    if execution_scope not in {PAIRED_EXECUTION_SCOPE, BASE_ONLY_EXECUTION_SCOPE}:
        raise ScenarioRuntimeError("Runtime Policy execution scope is invalid")
    max_concurrency = policy.get("max_concurrency")
    if max_concurrency is not None and (
        not isinstance(max_concurrency, int)
        or isinstance(max_concurrency, bool)
        or max_concurrency < 1
        or max_concurrency > MAX_WORKLOAD_CONCURRENCY
    ):
        raise ScenarioRuntimeError(
            f"Runtime Policy max concurrency must be between 1 and {MAX_WORKLOAD_CONCURRENCY}"
        )
    if request.execution_adapter == "MOCK":
        if policy and policy.get("schema_version") != "framework-eval-runtime-policy.v2":
            raise ScenarioRuntimeError("Runtime Policy schema is invalid")
        return
    required_text = (
        "execution_model",
        "judge_model",
        "reasoning_effort",
        "judge_reasoning_effort",
    )
    required_positive = (
        "wall_timeout_seconds",
        "token_budget",
        "tool_call_budget",
        "disk_budget_mb",
    )
    if policy.get("schema_version") != "framework-eval-runtime-policy.v2":
        raise ScenarioRuntimeError("CODEX Runtime Policy schema is invalid")
    if any(not isinstance(policy.get(key), str) or not str(policy[key]).strip() for key in required_text):
        raise ScenarioRuntimeError("CODEX Runtime Policy model and reasoning fields are required")
    if any(
        not isinstance(policy.get(key), int)
        or isinstance(policy.get(key), bool)
        or int(policy[key]) < 1
        for key in required_positive
    ):
        raise ScenarioRuntimeError("CODEX Runtime Policy budgets must be positive integers")


def _frozen_runtime_policy(request: ScenarioRunCreateRequest) -> dict[str, object]:
    """Normalize the current runtime policy before hashing and persistence."""

    policy = dict(request.runtime_policy)
    workload_kind = workload_kind_from_runtime_policy(policy)
    policy["workload_kind"] = workload_kind
    policy.setdefault("max_concurrency", 2 if workload_kind == FORMAL_EVALUATION else 1)
    return policy


def _validate_outcome(outcome: dict[str, object]) -> None:
    if outcome.get("schema_version") != "framework-eval-execution-outcome.v2":
        raise ScenarioRuntimeError("Execution outcome schema is invalid")
    outcome_score = outcome.get("outcome_quality")
    if outcome_score is not None and (
        not isinstance(outcome_score, (int, float)) or not 0 <= float(outcome_score) <= 100
    ):
        raise ScenarioRuntimeError("Execution outcome outcome_quality is invalid")
    workflow_score = outcome.get("framework_workflow_quality")
    if workflow_score is not None and (
        not isinstance(workflow_score, (int, float)) or not 0 <= float(workflow_score) <= 100
    ):
        raise ScenarioRuntimeError("Execution outcome framework_workflow_quality is invalid")
    if outcome_score is None and outcome.get("assessment_compatible") is not False:
        raise ScenarioRuntimeError("Comparable execution outcome requires Outcome Quality")
    if not isinstance(outcome.get("assessment_compatible"), bool):
        raise ScenarioRuntimeError("Execution outcome compatibility is invalid")
    _object_list(outcome.get("evidence"), "Execution evidence")


def _side_evaluation(outcome: dict[str, object], contract: dict[str, object]) -> SideEvaluation:
    resources_value = outcome.get("resources")
    resources = None
    if isinstance(resources_value, dict):
        attributable = resources_value.get("attributable")
        if not isinstance(attributable, dict):
            raise ScenarioRuntimeError("Execution attributable resources are invalid")
        resources = ResourceObservation(
            agent_tokens=float(attributable["agent_tokens"]),
            active_execution_seconds=float(attributable["active_execution_seconds"]),
            workflow_rework_actions=float(attributable["workflow_rework_actions"]),
        )
    workflow_score = _workflow_score(outcome, contract)
    outcome_score = outcome.get("outcome_quality")
    verification = outcome.get("verification")
    verification_status = verification.get("status") if isinstance(verification, dict) else None
    business_status = (
        verification_status if verification_status in {"PASS", "FAIL"} else "INCONCLUSIVE"
    )
    return SideEvaluation(
        outcome_quality=float(outcome_score) if isinstance(outcome_score, (int, float)) else None,
        framework_workflow_quality=float(workflow_score) if workflow_score is not None else None,
        resources=resources,
        case_hard_gates=tuple(str(item) for item in outcome.get("case_hard_gates", [])),
        global_hard_gates=tuple(str(item) for item in outcome.get("global_hard_gates", [])),
        reached_comparable_business_terminal=bool(outcome.get("reached_comparable_business_terminal", True)),
        business_verification_status=business_status,
    )


def _workflow_score(outcome: dict[str, object], contract: dict[str, object]) -> float | None:
    raw_axis_scores = outcome.get("workflow_axis_scores")
    if isinstance(raw_axis_scores, dict):
        profile = _object(contract.get("scoring_profile"), "Scoring Profile")
        axes = _object(profile.get("workflow_axes"), "Workflow axes")
        if all(isinstance(raw_axis_scores.get(axis), (int, float)) for axis in axes):
            return round(
                sum(float(raw_axis_scores[axis]) * float(weight) / 100 for axis, weight in axes.items()),
                2,
            )
    value = outcome.get("framework_workflow_quality")
    return float(value) if isinstance(value, (int, float)) else None


def _assessment_compatibility(
    *,
    base: dict[str, object],
    candidate: dict[str, object],
    contract: dict[str, object],
) -> tuple[bool, tuple[str, ...]]:
    profile = _object(contract.get("scoring_profile"), "Scoring Profile")
    formulas = _object(profile.get("formula_ids"), "Scoring Profile formulas")
    if formulas.get("case_aggregation") == "equal-business-comparable-cases-tolerant-regression.v3":
        reasons: list[str] = []
        for side, outcome in (("BASE", base), ("CANDIDATE", candidate)):
            verification = outcome.get("verification")
            verification_status = verification.get("status") if isinstance(verification, dict) else None
            if verification_status not in {"PASS", "FAIL"} or outcome.get("outcome_quality") is None:
                reasons.append(f"{side}: BUSINESS_VERIFICATION_INCONCLUSIVE")
        return not reasons, tuple(reasons)
    reasons = tuple(
        f"{side}: {reason}"
        for side, outcome in (("BASE", base), ("CANDIDATE", candidate))
        for reason in outcome.get("incompatibility_reasons", [])
        if isinstance(reason, str) and reason
    )
    if not bool(base.get("assessment_compatible")) or not bool(candidate.get("assessment_compatible")):
        return False, reasons or ("Outcome or Workflow evidence is not scoreable",)
    return True, ()


def _scoring_profile(contract: dict[str, object]) -> ScoringProfile:
    raw = _object(contract.get("scoring_profile"), "Scoring Profile")
    weights = _object(raw.get("weights"), "Scoring Profile weights")
    workflow_axes = _object(raw.get("workflow_axes"), "Scoring Profile workflow axes")
    outcome_weights = _object(raw.get("outcome_weights"), "Scoring Profile Outcome weights")
    efficiency_weights = _object(raw.get("efficiency_weights"), "Scoring Profile efficiency weights")
    formulas = _object(raw.get("formula_ids"), "Scoring Profile formulas")
    gates = _object(raw.get("hard_gates"), "Scoring Profile hard gates")
    promotion = _object(raw.get("promotion_policy"), "Scoring Profile promotion policy")
    profile = ScoringProfile(
        profile_id=str(raw["profile_id"]),
        version=str(raw["version"]),
        outcome_weight=int(weights["outcome_quality"]),
        workflow_weight=int(weights["framework_workflow_quality"]),
        efficiency_weight=int(weights["efficiency"]),
        requirement_fulfillment_weight=int(outcome_weights["requirement_fulfillment"]),
        implementation_quality_weight=int(outcome_weights["implementation_quality"]),
        token_efficiency_weight=int(efficiency_weights["agent_tokens"]),
        active_time_efficiency_weight=int(efficiency_weights["active_execution_seconds"]),
        rework_efficiency_weight=int(efficiency_weights["workflow_rework_actions"]),
        workflow_axes=tuple((str(name), int(weight)) for name, weight in workflow_axes.items()),
        global_hard_gates=tuple(str(item) for item in gates["GLOBAL"]),
        case_hard_gates=tuple(str(item) for item in gates["CASE"]),
        unified_formula=str(formulas["unified"]),
        outcome_formula=str(formulas["outcome"]),
        efficiency_formula=str(formulas["efficiency"]),
        efficiency_na_policy=str(formulas["efficiency_na"]),
        case_aggregation_formula=str(formulas["case_aggregation"]),
        recommend_unified_delta=_number(promotion["recommend_unified_delta"], "recommend unified delta"),
        recommend_minimum_outcome_delta=_number(
            promotion["recommend_minimum_outcome_delta"],
            "recommend minimum outcome delta",
        ),
        do_not_recommend_unified_delta=_number(
            promotion["do_not_recommend_unified_delta"],
            "do-not-recommend unified delta",
        ),
        do_not_recommend_outcome_delta=_number(
            promotion["do_not_recommend_outcome_delta"],
            "do-not-recommend outcome delta",
        ),
        candidate_business_regression_tolerance=int(
            promotion.get("candidate_business_regression_tolerance", 0)
        ),
    )
    validate_scoring_profile(profile)
    return profile


def _strategy(contract: dict[str, object], side: ScenarioSide) -> dict[str, object]:
    strategies = _object(contract.get("strategies"), "Strategies")
    return _object(strategies.get(side), f"{side} Strategy")


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioRuntimeError(f"{label} must be an object")
    return value


def _object_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ScenarioRuntimeError(f"{label} must be a list of objects")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _public_outcome(value: object) -> object:
    """Keep immutable Judge inputs in SQLite without exposing source content in reports."""

    if not isinstance(value, dict):
        return value
    public = dict(value)
    public.pop("immutable_judge_evidence", None)
    return public


def _hash_json(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def calculation_from_stored(value: StoredScenarioCalculation) -> ScenarioCalculation:
    """Convert the storage DTO without exposing SQLite details to Web adapters."""

    result = json.loads(value.result_json)
    if not isinstance(result, dict):  # pragma: no cover - protected on write
        raise ScenarioRuntimeError("Stored Scenario calculation is invalid")
    return ScenarioCalculation(
        calculation_id=value.calculation_id,
        run_id=value.run_id,
        sequence_number=value.sequence_number,
        reason=value.reason,
        contract_hash=value.contract_hash,
        result=result,
        created_at=value.created_at,
    )
