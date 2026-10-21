from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import bench.codex_execution as codex_execution
from bench.codex_execution import (
    CodexExecutionError,
    CodexExecutionEvent,
    CodexExecutionRequest,
    CodexExecutionResult,
    CodexTurnExecutionResult,
    FrozenWorkflowClosureResume,
    FrozenWorkflowStep,
    WorkflowCheckpointStore,
    checkpoint_remaining_resources,
    cumulative_checkpoint_turns,
)
from bench.evaluation_contracts import (
    ArtifactSource,
    ExecutionStrategy,
    StrategyStage,
    freeze_evaluation_contract,
)
from bench.report_models import ClaimedJob
from bench.scenario_calibration import ScenarioCalibrationService
from bench.scenario_codex import (
    CodexScenarioExecutionAdapter,
    ScenarioCodexExecutionError,
    _bounded_workflow_failure_reason,
    _business_case_hard_gates,
    _business_failure_outcome_score,
    _completed_stage_ids,
    _diagnostic_turn_evidence,
    _execution_error_detail,
    _execution_terminal_reason,
    _frozen_workflow_semantics,
    _is_operator_recoverable_execution_failure,
    _is_retryable_execution_failure,
    _strategy_workflow_closure,
    verify_initialization_outputs,
)
from bench.scenario_execution_support import (
    FrozenCommandResult,
    ScenarioExecutionSupportError,
    apply_workspace_preconditions,
)
from bench.scenario_judging import (
    build_outcome_judge_request,
    build_workflow_judge_request,
    validate_scenario_judgment,
)
from bench.scenario_runtime import (
    ClaimedScenarioJob,
    GitScenarioStrategyValidator,
    ScenarioRetryableExecutionError,
    ScenarioRunCreateRequest,
    ScenarioRunService,
    ScenarioWorker,
)
from bench.scenario_storage import ScenarioStore
from bench.scenario_workspace import EMPTY_GIT_DIFF_SHA256, scenario_workspace
from bench.scenarios import load_scenario_source
from bench.semantic_judging import JudgeContractError
from bench.tests.test_scenario_calibration import _repository_scenario


@pytest.mark.parametrize(
    ("checks", "expected"),
    (
        (
            [{"kind": "FAIL_TO_PASS", "status": "FAIL"}, {"kind": "PASS_TO_PASS", "status": "PASS"}],
            ["CORE_BUSINESS_BEHAVIOR_FAILURE"],
        ),
        (
            [{"kind": "FAIL_TO_PASS", "status": "PASS"}, {"kind": "PASS_TO_PASS", "status": "FAIL"}],
            ["PROTECTED_REGRESSION_FAILURE"],
        ),
        (
            [{"kind": "FAIL_TO_PASS", "status": "FAIL"}, {"kind": "PASS_TO_PASS", "status": "FAIL"}],
            ["CORE_BUSINESS_BEHAVIOR_FAILURE", "PROTECTED_REGRESSION_FAILURE"],
        ),
    ),
)
def test_business_case_hard_gates_are_independent_by_check_kind(
    checks: list[dict[str, str]],
    expected: list[str],
) -> None:
    assert _business_case_hard_gates({"status": "FAIL", "checks": checks}) == expected


def test_business_failure_has_zero_outcome_but_inconclusive_verification_has_no_score() -> None:
    assert _business_failure_outcome_score({"status": "FAIL"}) == 0
    assert _business_failure_outcome_score({"status": "INCONCLUSIVE"}) is None


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_multi_repository_runtime_materializes_independent_repositories_and_evidence(
    tmp_path: Path,
) -> None:
    source_directory, primary_repository = _repository_scenario(tmp_path)
    secondary_repository = tmp_path / "secondary-repository"
    secondary_repository.mkdir()
    _git(secondary_repository, "init")
    _git(secondary_repository, "config", "user.name", "Scenario Test")
    _git(secondary_repository, "config", "user.email", "scenario@example.test")
    (secondary_repository / "secondary.txt").write_text("context\n", encoding="utf-8")
    _git(secondary_repository, "add", "secondary.txt")
    _git(secondary_repository, "commit", "-m", "context baseline")
    secondary_commit = _git(secondary_repository, "rev-parse", "HEAD")

    manifest_path = source_directory / "case.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    primary = manifest.pop("repository")
    primary.update({"mount_path": "business-repo", "impact": "WRITE"})
    primary_patch = manifest.pop("hidden_test_patch")
    primary_patch["repository_id"] = "business-repo"
    manifest.update(
        {
            "repository_mode": "MULTI_REPOSITORY",
            "workspace_path": "workspace",
            "workspace": {
                "primary_repository_id": "business-repo",
                "repositories": [
                    primary,
                    {
                        "repository_id": "secondary-repo",
                        "mount_path": "secondary-repo",
                        "impact": "READ",
                        "base_commit": secondary_commit,
                        "solution_commit": secondary_commit,
                        "solution_patch_sha256": EMPTY_GIT_DIFF_SHA256,
                    },
                ],
            },
            "hidden_test_patches": [primary_patch],
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    workspace_assets = source_directory / "workspace"
    workspace_assets.mkdir()
    registry = (
        "version: 1\n"
        "workspace:\n  name: runtime-test\n  root: .\n  mode: multi-repo\n"
        "repos:\n"
        "  business-repo:\n    path: ./business-repo\n"
        "  secondary-repo:\n    path: ./secondary-repo\n"
    )
    (workspace_assets / "helixharness.workspace.yaml").write_text(registry, encoding="utf-8")
    gold_path = source_directory / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["source_evidence"][0]["repository_id"] = "business-repo"
    for test in gold["execution_tests"]:
        test["working_directory"] = "business-repo"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    source = load_scenario_source(source_directory)
    snapshot = json.loads(source.snapshot.snapshot_json)
    adapter = CodexScenarioExecutionAdapter(
        helixharness_repository=tmp_path,
        repositories={
            "business-repo": primary_repository,
            "secondary-repo": secondary_repository,
        },
        work_root=tmp_path / "work",
    )
    runtime_workspace = tmp_path / "runtime-workspace"

    adapter._materialize_scenario(snapshot, runtime_workspace)

    assert (runtime_workspace / "business-repo/result.txt").read_text() == "pending\n"
    assert (runtime_workspace / "secondary-repo/secondary.txt").read_text() == "context\n"
    assert (runtime_workspace / "business-repo/.git").is_dir()
    assert (runtime_workspace / "secondary-repo/.git").is_dir()
    assert (runtime_workspace / "helixharness.workspace.yaml").read_text() == registry
    (runtime_workspace / "business-repo/implementation.py").write_text(
        "RESULT = 'complete'\n", encoding="utf-8"
    )
    records, _ = adapter._collect_code_evidence(runtime_workspace, manifest)
    assert any(record.get("locator") == "business-repo/implementation.py" for record in records)
    (runtime_workspace / "secondary-repo/AGENTS.md").write_text("# HelixHarness\n", encoding="utf-8")
    (runtime_workspace / "secondary-repo/docs").mkdir()
    (runtime_workspace / "secondary-repo/docs/.helixharness-init-state.json").write_text(
        "{}\n", encoding="utf-8"
    )
    (runtime_workspace / "secondary-repo/docs/harness").mkdir()
    (runtime_workspace / "secondary-repo/docs/harness/index.md").write_text(
        "# Harness\n", encoding="utf-8"
    )
    (runtime_workspace / "secondary-repo/docs/hooks").mkdir()
    (runtime_workspace / "secondary-repo/docs/hooks/.gitkeep").write_text("", encoding="utf-8")
    (runtime_workspace / "secondary-repo/docs/knowledge").mkdir(parents=True)
    (runtime_workspace / "secondary-repo/docs/knowledge/index.md").write_text(
        "# Knowledge\n", encoding="utf-8"
    )
    (runtime_workspace / "secondary-repo/docs/specs/active/example").mkdir(parents=True)
    (runtime_workspace / "secondary-repo/docs/specs/active/example/design.md").write_text(
        "# Design\n", encoding="utf-8"
    )
    adapter._verify_repository_impacts(runtime_workspace, scenario_workspace(manifest))
    _git(runtime_workspace / "secondary-repo", "add", "AGENTS.md")
    _git(runtime_workspace / "secondary-repo", "commit", "-m", "tracked instructions")
    (runtime_workspace / "secondary-repo/AGENTS.md").write_text("# Modified\n", encoding="utf-8")
    with pytest.raises(ScenarioCodexExecutionError, match="Read-only repository was modified"):
        adapter._verify_repository_impacts(runtime_workspace, scenario_workspace(manifest))
    (runtime_workspace / "secondary-repo/AGENTS.md").write_text("# HelixHarness\n", encoding="utf-8")
    (runtime_workspace / "secondary-repo/forbidden.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(ScenarioCodexExecutionError, match="Read-only repository was modified"):
        adapter._verify_repository_impacts(runtime_workspace, scenario_workspace(manifest))


def test_failed_turn_diagnostics_are_bounded_redacted_and_reason_coded() -> None:
    closure_event = CodexExecutionEvent(
        event_type="workflow/closure",
        category="workflow",
        status="FAILED",
        summary="返工次数耗尽",
        payload={
            "reason": "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED",
            "cycle": 3,
            "progress": {"access_token": "nested-secret"},
        },
        occurred_at_ms=2_000,
    )
    turn = CodexTurnExecutionResult(
        turn_id="turn-1",
        classification="FAILED",
        input_kind="WORKFLOW_STEP_RERUN",
        input_text="/helix:apply evaluation-scenario",
        started_at_ms=1_000,
        completed_at_ms=2_000,
        duration_ms=1_000,
        final_response=(
            "编译失败，api_key=super-secret-value\n"
            "HELIXHARNESS_TURN_STATUS: FAILED"
        ),
        usage={"total_tokens": 100},
        error=None,
        events=(closure_event,),
        workflow_step_id="APPLY",
        milestone_id="IMPLEMENTED",
    )
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id="turn-1",
        status="FAILED",
        started_at_ms=1_000,
        completed_at_ms=2_000,
        duration_ms=1_000,
        final_response=turn.final_response,
        usage=turn.usage,
        error=None,
        events=(closure_event,),
        turns=(turn,),
    )

    evidence = _diagnostic_turn_evidence(result)

    assert _execution_terminal_reason(result) == "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED"
    assert len(evidence) == 1
    assert evidence[0]["workflow_step_id"] == "APPLY"
    assert evidence[0]["usage"] == {"total_tokens": 100}
    assert "super-secret-value" not in str(evidence[0]["final_response"])
    assert "[REDACTED]" in str(evidence[0]["final_response"])
    assert evidence[0]["events"][0]["payload"]["cycle"] == 3
    assert evidence[0]["events"][0]["payload"]["progress"]["access_token"] == "[REDACTED]"


def test_intermediate_review_reason_does_not_override_missing_turn_status() -> None:
    review_event = CodexExecutionEvent(
        event_type="workflow/closure",
        category="workflow",
        status="COMPLETED",
        summary="review required before continuing",
        payload={"decision": "EXECUTE", "reason": "REVIEW_REQUIRED"},
        occurred_at_ms=2_000,
    )
    turn = CodexTurnExecutionResult(
        turn_id="turn-1",
        classification="FAILED",
        input_kind="WORKFLOW_STEP",
        input_text="/helix:apply feature",
        started_at_ms=1_000,
        completed_at_ms=2_000,
        duration_ms=1_000,
        final_response="implementation completed without protocol marker",
        usage={},
        error=None,
        events=(review_event,),
    )
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id="turn-1",
        status="FAILED",
        started_at_ms=1_000,
        completed_at_ms=2_000,
        duration_ms=1_000,
        final_response=turn.final_response,
        usage={},
        error=None,
        events=(review_event,),
        turns=(turn,),
    )

    assert _execution_terminal_reason(result) == "TURN_STATUS_MISSING"


def test_frozen_workflow_semantics_reject_retired_chain() -> None:
    standard = {
        "stages": [
            {"stage_id": "P1", "command": "/helix:propose feature"},
            {"stage_id": "P2", "command": "/helix:propose feature --continue"},
            {"stage_id": "APPLY", "command": "/helix:apply feature"},
            {"stage_id": "REVIEW", "command": "/helix:review feature"},
            {"stage_id": "ARCHIVE", "command": "/helix:archive feature"},
        ]
    }
    workflow = {
        "stages": [
            {"stage_id": "P", "command": "/helix:propose feature"},
            {"stage_id": "A", "command": "/helix:apply feature"},
            {"stage_id": "Z", "command": "/helix:archive feature"},
        ]
    }

    with pytest.raises(ScenarioCodexExecutionError, match="Unsupported"):
        _frozen_workflow_semantics(standard)
    assert _frozen_workflow_semantics(workflow) == {
        "P": "HELIXHARNESS_PROPOSE",
        "A": "HELIXHARNESS_APPLY",
        "Z": "HELIXHARNESS_ARCHIVE",
    }


@pytest.mark.parametrize("command", ["/helix:init --removed", "/helix:apply demo --removed"])
def test_frozen_strategy_cannot_bypass_current_option_validation(command: str) -> None:
    with pytest.raises(ScenarioCodexExecutionError, match="Unsupported"):
        _frozen_workflow_semantics({"stages": [{"stage_id": "P", "command": command}]})


def test_frozen_strategy_keeps_requirement_text_literal() -> None:
    result = _frozen_workflow_semantics({"stages": [
        {"stage_id": "P", "command": "/helix:propose demo -- --loop"},
    ]})
    assert result == {"P": "HELIXHARNESS_PROPOSE"}


def test_workflow_stage_artifacts_require_current_verify_report(tmp_path: Path) -> None:
    active = tmp_path / "docs/specs/active/feature"
    active.mkdir(parents=True)
    (active / "spec.md").write_text("spec\n", encoding="utf-8")
    (active / "tasks.md").write_text("tasks\n", encoding="utf-8")
    propose = FrozenWorkflowStep(
        "P",
        "/helix:propose feature",
        "PROPOSED",
        "HELIXHARNESS_PROPOSE",
        "PROPOSE",
    )
    apply = FrozenWorkflowStep(
        "A",
        "/helix:apply feature",
        "APPLIED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )

    WorkflowCheckpointStore._validate_stage_artifacts(tmp_path, propose)
    with pytest.raises(CodexExecutionError, match="verify-report.md"):
        WorkflowCheckpointStore._validate_stage_artifacts(tmp_path, apply)
    (active / "verify-report.md").write_text("evidence\n", encoding="utf-8")
    WorkflowCheckpointStore._validate_stage_artifacts(tmp_path, apply)


def _helixharness_repository(
    tmp_path: Path, *, combined: bool = False, nested: bool = False,
) -> tuple[Path, str, str, str, str]:
    repository = tmp_path / "helixharness"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "branch", "-M", "master")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    plugin = repository / ("products/tooling/plugin" if nested else "helixharness") if combined else repository
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "commands.md").write_text("base workflow\n", encoding="utf-8")
    (plugin / ".codex-plugin").mkdir()
    (plugin / ".codex-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    (plugin / ".claude-plugin").mkdir()
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"name":"helix"}\n', encoding="utf-8")
    (plugin / "commands").mkdir()
    for command in ("propose", "apply", "archive"):
        (plugin / "commands" / f"{command}.md").write_text(command + "\n")
    if nested:
        # A second valid plugin makes auto-discovery ambiguous. The Worker must
        # use the path frozen in the contract, not whichever copy it sees first.
        decoy = repository / "decoy"
        (decoy / ".claude-plugin").mkdir(parents=True)
        (decoy / ".claude-plugin/plugin.json").write_text('{"name":"helix"}\n')
        (decoy / "commands").mkdir()
        for command in ("propose", "apply", "archive"):
            (decoy / "commands" / f"{command}.md").write_text("different plugin\n")
        (decoy / "commands.md").write_text("wrong snapshot\n")
    if combined:
        (repository / "commands.md").write_text("not a plugin document\n", encoding="utf-8")
        (repository / "helixharness-platform").mkdir()
        (repository / "helixharness-platform" / "not-plugin.txt").write_text("platform source\n")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base workflow")
    base_commit = _git(repository, "rev-parse", "HEAD")
    base_hash = hashlib.sha256(b"base workflow\n").hexdigest()
    (plugin / "commands.md").write_text("candidate workflow\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "candidate workflow")
    candidate_commit = _git(repository, "rev-parse", "HEAD")
    candidate_hash = hashlib.sha256(b"candidate workflow\n").hexdigest()
    _git(repository, "branch", "candidate", candidate_commit)
    _git(repository, "reset", "--hard", base_commit)
    origin = tmp_path / "helixharness-origin.git"
    _git(tmp_path, "clone", "--bare", str(repository), str(origin))
    _git(repository, "remote", "add", "origin", str(origin))
    _git(repository, "fetch", "origin")
    return repository, base_commit, base_hash, candidate_commit, candidate_hash


def _strategy(prefix: str, commit: str, document_hash: str) -> ExecutionStrategy:
    command = f"/{prefix}:implement"
    return ExecutionStrategy(
        strategy_id=f"{prefix}-strategy",
        version="1",
        adapter_id="known-helixharness",
        adapter_version="1",
        source_commit=commit,
        source_document_hashes=(("commands.md", document_hash),),
        commands=(command,),
        stages=(StrategyStage("IMPLEMENT", "IMPLEMENTATION", command, "COMPLETED", True),),
        completion_states=("COMPLETED",),
        artifact_sources=(ArtifactSource("completed-stage-artifacts", "docs/specs/completed/evaluation-scenario"),),
        finding_policy="NONE",
        finding_closures=(),
    )


def _contract(
    base: ExecutionStrategy,
    candidate: ExecutionStrategy,
) -> dict[str, object]:
    return freeze_evaluation_contract(
        base_strategy=base,
        candidate_strategy=candidate,
    )


class _RecordingSdk:
    def __init__(
        self,
        *,
        status: str = "COMPLETED",
        write_evidence: bool = True,
        error: dict[str, object] | None = None,
    ) -> None:
        self.snapshot_documents: list[str] = []
        self.requests: list[CodexExecutionRequest] = []
        self.status = status
        self.write_evidence = write_evidence
        self.error = error

    def execute(self, request: CodexExecutionRequest) -> CodexExecutionResult:
        self.requests.append(request)
        self.snapshot_documents.append(
            (request.helixharness_snapshot_path / "commands.md").read_text(encoding="utf-8").strip()
        )
        (request.workspace_path / "result.txt").write_text("complete\n", encoding="utf-8")
        if self.write_evidence:
            evidence = request.workspace_path / "docs/specs/completed/evaluation-scenario"
            evidence.mkdir(parents=True)
            (evidence / "spec.md").write_text("# Requirements\nComplete the observable result.\n", encoding="utf-8")
            (evidence / "tasks.md").write_text("# Tasks\nImplement and verify the result.\n", encoding="utf-8")
        step = request.workflow_steps[0]
        turn = CodexTurnExecutionResult(
            turn_id="turn-1",
            classification=self.status,
            input_kind="WORKFLOW_STEP",
            input_text=step.command,
            started_at_ms=1_000,
            completed_at_ms=2_000,
            duration_ms=1_000,
            final_response="complete",
            usage={"total_tokens": 500},
            error=self.error,
            events=(),
            workflow_step_id=step.step_id,
            milestone_id=step.milestone_id,
        )
        return CodexExecutionResult(
            thread_id="thread-1",
            turn_id="turn-1",
            status=self.status,
            started_at_ms=1_000,
            completed_at_ms=2_000,
            duration_ms=1_000,
            final_response="complete",
            usage={"total_tokens": 500},
            error=self.error,
            events=(),
            turns=(turn,),
        )


class _CheckpointingSdk:
    """Persist one stage, fail once, then finish only the restored suffix."""

    def __init__(self, *, fail_calls: frozenset[int] = frozenset({1})) -> None:
        self.requests: list[CodexExecutionRequest] = []
        self.fail_calls = fail_calls

    def execute(self, request: CodexExecutionRequest) -> CodexExecutionResult:
        self.requests.append(request)
        evidence = request.workspace_path / "docs/specs/completed/evaluation-scenario"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "spec.md").write_text("# Requirements\nComplete the observable result.\n", encoding="utf-8")
        (evidence / "tasks.md").write_text("# Tasks\nImplement and verify the result.\n", encoding="utf-8")
        step = request.workflow_steps[0]
        turn = CodexTurnExecutionResult(
            turn_id=f"turn-{len(self.requests)}",
            classification="COMPLETED",
            input_kind="WORKFLOW_STEP",
            input_text=step.command,
            started_at_ms=len(self.requests) * 2_000,
            completed_at_ms=len(self.requests) * 2_000 + 1_500,
            duration_ms=1_500,
            final_response="complete",
            usage={"total_tokens": 500},
            error=None,
            events=(
                CodexExecutionEvent(
                    event_type="command/completed",
                    category="command",
                    status="COMPLETED",
                    summary="completed",
                    payload={},
                    occurred_at_ms=len(self.requests) * 2_000 + 1_000,
                ),
            ),
            workflow_step_id=step.step_id,
            milestone_id=step.milestone_id,
        )
        assert request.on_workflow_step_completed is not None
        request.on_workflow_step_completed((turn,))
        if len(self.requests) in self.fail_calls:
            raise ScenarioRetryableExecutionError(
                "transient worker interruption",
                attempt_record={
                    "schema_version": "framework-eval-attempt-resource.v1",
                    "terminal_status": "FAILED",
                    "started_at_ms": turn.started_at_ms,
                    "completed_at_ms": turn.completed_at_ms,
                    "duration_ms": turn.duration_ms,
                    "usage": turn.usage,
                    "evidence_complete": True,
                    "turn_evidence": [],
                },
            )
        return CodexExecutionResult(
            thread_id="thread-restored",
            turn_id=turn.turn_id,
            status="COMPLETED",
            started_at_ms=turn.started_at_ms,
            completed_at_ms=turn.completed_at_ms,
            duration_ms=turn.duration_ms,
            final_response="complete",
            usage=turn.usage,
            error=None,
            events=turn.events,
            turns=(turn,),
        )


class _NoopInstaller:
    def install(self, snapshot_path: Path, codex_home_path: Path) -> None:
        del snapshot_path, codex_home_path


class _NoopTelemetry:
    def disable(self, snapshot_path: Path, workspace_path: Path, codex_home_path: Path) -> Path:
        del snapshot_path, workspace_path
        target = codex_home_path / "helixharness" / "telemetry-consent-cache.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        return target


class _RepositoryRequiredTelemetry:
    """Match the frozen HelixHarness consent script's repository precondition."""

    def __init__(self) -> None:
        self.workspaces: list[Path] = []

    def disable(self, snapshot_path: Path, workspace_path: Path, codex_home_path: Path) -> Path:
        del snapshot_path
        completed = subprocess.run(
            ("git", "-C", str(workspace_path), "rev-parse", "--show-toplevel"),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0 or Path(completed.stdout.strip()).resolve() != workspace_path.resolve():
            raise CodexExecutionError(
                f"HelixHarness telemetry disable failed: not a git repository or HelixHarness workspace: {workspace_path}"
            )
        if not (workspace_path / ".helixharness_case" / "initial-request.md").is_file():
            raise CodexExecutionError("HelixHarness telemetry disable ran before Scenario materialization")
        self.workspaces.append(workspace_path)
        target = codex_home_path / "helixharness" / "telemetry-consent-cache.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        return target


class _PerfectJudge:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def judge(self, request: dict[str, Any], *, model: str, reasoning_effort: str) -> dict[str, Any]:
        del model, reasoning_effort
        self.requests.append(request)
        scopes = {item["axis"]: item["allowed_evidence_ids"] for item in request["evidence_scopes"]}
        citation_requirements = {
            item["axis"]: item
            for item in request.get("citation_requirements", [])
        }
        requirement_ids = [item["requirement_id"] for item in request["traceability"]["requirements"]]
        gold_refs = [item["ref_id"] for item in request["traceability"]["gold_evidence_refs"]]
        return {
            "schema_version": "framework-eval-judge-output.v2",
            "status": "COMPLETED",
            "axis_scores": [
                {
                    "axis": axis,
                    "criterion_id": axis,
                    "criterion": "All anchored requirements are satisfied.",
                    "score": 100,
                    "rationale": "The retained scoped evidence is complete.",
                    "evidence_ids": list(
                        dict.fromkeys(
                            [
                                *citation_requirements.get(axis, {}).get("required_evidence_ids", []),
                                *citation_requirements.get(axis, {}).get("at_least_one_evidence_id_from", [])[:1],
                                scopes[axis][0],
                            ]
                        )
                    ),
                    "requirement_ids": citation_requirements.get(axis, {}).get(
                        "required_requirement_ids",
                        requirement_ids,
                    ),
                    "gold_evidence_refs": gold_refs,
                    "deductions": [],
                    "limitations": [],
                }
                for axis in request["axes"]
            ],
        }


class _InvalidThenPerfectJudge:
    def __init__(self, *, always_invalid: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self.always_invalid = always_invalid

    def judge(self, request: dict[str, Any], *, model: str, reasoning_effort: str) -> dict[str, Any]:
        self.requests.append(request)
        output = _PerfectJudge().judge(request, model=model, reasoning_effort=reasoning_effort)
        if self.always_invalid or len(self.requests) == 1:
            output["axis_scores"][0]["gold_evidence_refs"] = ["GOLD-UNKNOWN"]
        return output


class _InvalidEvidenceThenPerfectJudge(_PerfectJudge):
    def judge(self, request: dict[str, Any], *, model: str, reasoning_effort: str) -> dict[str, Any]:
        output = super().judge(request, model=model, reasoning_effort=reasoning_effort)
        if len(self.requests) == 1:
            output["axis_scores"][0]["evidence_ids"] = ["invented-evidence-id"]
        return output


def test_scenario_judge_repairs_invalid_evidence_references_once(tmp_path: Path) -> None:
    contract = _contract(
        _strategy("base", "a" * 40, "c" * 64),
        _strategy("candidate", "b" * 40, "d" * 64),
    )
    request = build_workflow_judge_request(
        contract,
        gold={
            "requirements": [{"requirement_id": "REQ-1"}],
            "source_evidence": [{"evidence_id": "E-1"}],
        },
        evidence=[
            {
                "evidence_id": "artifact-spec",
                "kind": "STAGE_ARTIFACT",
                "path": "docs/specs/completed/feature/spec.md",
                "content": "# Requirements",
            }
        ],
    )
    judge = _InvalidEvidenceThenPerfectJudge()
    adapter = CodexScenarioExecutionAdapter(
        helixharness_repository=tmp_path / "helixharness",
        repositories={},
        work_root=tmp_path / "work",
        sdk_adapter=_RecordingSdk(),
        snapshot_installer=_NoopInstaller(),
        telemetry_configurator=_NoopTelemetry(),
        judge_adapter=judge,
    )
    job = ClaimedScenarioJob(
        job_id="job-1",
        run_id="run-1",
        case_id="case-1",
        side="BASE",
        attempt_number=1,
        lease_token="lease-1",
        contract=contract,
        runtime_policy={
            "judge_model": "judge-test",
            "judge_reasoning_effort": "high",
            "judge_max_retries": 1,
        },
        snapshot={},
    )

    _, scores, diagnostics = adapter._execute_judge(job, request)

    assert scores
    assert len(judge.requests) == 2
    assert judge.requests[1]["validation_retry"]["previous_error"] == "Judge cited unknown evidence id"
    assert diagnostics[0]["output"]["axis_scores"][0]["evidence_ids"] == ["invented-evidence-id"]


def test_completed_stage_ids_are_ordered_and_unique_across_recovery_turns() -> None:
    turns = tuple(
        CodexTurnExecutionResult(
            turn_id=f"turn-{index}",
            classification=classification,
            input_kind="WORKFLOW_STEP",
            input_text="command",
            started_at_ms=index,
            completed_at_ms=index + 1,
            duration_ms=1,
            final_response="complete",
            usage={"total_tokens": 1},
            error=None,
            events=(),
            workflow_step_id=stage_id,
            milestone_id="COMPLETED",
        )
        for index, (stage_id, classification) in enumerate(
            (
                ("STEP_01", "COMPLETED"),
                ("STEP_05", "COMPLETED"),
                ("STEP_05", "COMPLETED"),
                ("STEP_04", "FAILED"),
                (None, "COMPLETED"),
            ),
            start=1,
        )
    )

    assert _completed_stage_ids(turns) == ["STEP_01", "STEP_05"]


def test_checkpoint_tree_clone_prefers_linux_reflink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(codex_execution.sys, "platform", "linux")
    monkeypatch.setattr(codex_execution.shutil, "which", lambda name: "/custom/bin/cp")
    monkeypatch.setattr(
        codex_execution.subprocess,
        "run",
        lambda command, **kwargs: commands.append(tuple(command)) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        codex_execution.shutil,
        "copytree",
        lambda *args, **kwargs: pytest.fail("portable copy fallback should not run"),
    )

    codex_execution._clone_tree_copy_on_write(source, target)

    assert commands == [
        ("/custom/bin/cp", "--archive", "--reflink=auto", str(source), str(target))
    ]


def test_checkpoint_tree_clone_falls_back_when_native_clone_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "evidence.txt").write_text("immutable\n", encoding="utf-8")
    target = tmp_path / "target"

    monkeypatch.setattr(codex_execution.sys, "platform", "linux")
    monkeypatch.setattr(codex_execution.shutil, "which", lambda name: "/custom/bin/cp")
    def partial_native_clone(*args: object, **kwargs: object) -> SimpleNamespace:
        del args, kwargs
        target.mkdir()
        (target / "partial.txt").write_text("partial\n", encoding="utf-8")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(codex_execution.subprocess, "run", partial_native_clone)

    codex_execution._clone_tree_copy_on_write(source, target)

    assert (target / "evidence.txt").read_text(encoding="utf-8") == "immutable\n"
    assert not (target / "partial.txt").exists()


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("entrypoint", ["checkpoint", "gradle"])
@pytest.mark.parametrize("failure", ["missing", "disappeared", "denied", "partial"])
def test_cache_copy_preserves_source_and_falls_back_without_usable_cp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, entrypoint: str, failure: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    tree = source / "caches" if entrypoint == "gradle" else source
    tree.mkdir(exist_ok=True)
    (tree / "payload.txt").write_text("immutable cache")
    (tree / "payload.link").symlink_to("payload.txt")
    (tree / "stale.lock").write_text("lock")
    (source / "gradle.properties").write_text("private source config")
    monkeypatch.setattr(codex_execution.sys, "platform", platform)
    monkeypatch.setattr(codex_execution.shutil, "which",
                        lambda name: None if failure == "missing" else "/custom/bin/cp")

    def unavailable_cp(command: tuple[str, ...], **kwargs: object) -> SimpleNamespace:
        assert failure != "missing", "Do not launch a missing executable"
        if failure == "disappeared":
            raise FileNotFoundError(command[0])
        if failure == "denied":
            raise PermissionError(command[0])
        partial = Path(command[-1])
        partial.mkdir()
        (partial / "partial.txt").write_text("incomplete native copy")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(codex_execution.subprocess, "run", unavailable_cp)
    if entrypoint == "gradle":
        home = codex_execution.seed_isolated_gradle_home(tmp_path / "codex", source=source)
        target = home / "caches"
        assert "private source config" not in (home / "gradle.properties").read_text()
        assert not (target / "stale.lock").exists()
    else:
        target = tmp_path / "target"
        codex_execution._clone_tree_copy_on_write(source, target)
        assert (target / "stale.lock").read_text() == "lock"
    assert (target / "payload.txt").read_text() == "immutable cache"
    assert (target / "payload.link").is_symlink()
    assert not (target / "partial.txt").exists()
    assert (tree / "payload.txt").read_text() == "immutable cache"
    assert (tree / "stale.lock").read_text() == "lock"
    assert (source / "gradle.properties").read_text() == "private source config"


def test_cache_copy_does_not_remove_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep.txt").write_text("existing data")
    with pytest.raises(FileExistsError):
        codex_execution._clone_tree_copy_on_write(source, target)
    assert (target / "keep.txt").read_text() == "existing data"


def test_codex_adapter_cleans_only_exact_terminal_runtime_trees(tmp_path: Path) -> None:
    helixharness, _, _, _, _ = _helixharness_repository(tmp_path)
    work_root = tmp_path / "work"
    adapter = CodexScenarioExecutionAdapter(
        helixharness_repository=helixharness,
        repositories={"business-repo": tmp_path},
        work_root=work_root,
        sdk_adapter=_RecordingSdk(),
        snapshot_installer=_NoopInstaller(),
        telemetry_configurator=_NoopTelemetry(),
        judge_adapter=_PerfectJudge(),
    )
    run_id = "sv2_" + "a" * 32
    job_id = "sj_" + "b" * 32
    run_tree = work_root / run_id
    checkpoint_tree = work_root / "_workflow-checkpoints" / job_id
    diagnostic_tree = work_root / "_diagnostics" / run_id
    sibling = work_root / ("sv2_" + "c" * 32)
    for path in (run_tree, checkpoint_tree, diagnostic_tree, sibling):
        path.mkdir(parents=True)
        (path / "state.txt").write_text("state\n", encoding="utf-8")

    adapter.cleanup_run_workspace(run_id, (job_id,))
    adapter.cleanup_run_workspace(run_id, (job_id,))

    assert not run_tree.exists()
    assert not checkpoint_tree.exists()
    assert (diagnostic_tree / "state.txt").is_file()
    assert (diagnostic_tree / ".terminal.json").is_file()
    assert (sibling / "state.txt").is_file()
    with pytest.raises(OSError, match="cleanup id is invalid"):
        adapter.cleanup_run_workspace("../outside", (job_id,))


def test_absent_helixharness_precondition_preserves_business_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "docs" / "harness").mkdir(parents=True)
    (workspace / "docs" / "harness" / "index.md").write_text("old\n", encoding="utf-8")
    (workspace / "docs" / "knowledge").mkdir()
    (workspace / "docs" / "knowledge" / "index.md").write_text("old\n", encoding="utf-8")
    (workspace / "AGENTS.md").write_text("old\n", encoding="utf-8")

    removed = apply_workspace_preconditions(
        workspace,
        {
            "workspace_preconditions": {
                "schema_version": "framework-eval-workspace-preconditions.v1",
                "helixharness_initialization": "ABSENT",
                "init_mode": "SERVICE",
            }
        },
    )

    assert removed == ("AGENTS.md", "docs/harness", "docs/knowledge")
    assert (workspace / "src" / "service.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (workspace / "AGENTS.md").exists()
    assert not (workspace / "docs" / "harness").exists()
    assert not (workspace / "docs" / "knowledge").exists()


def test_init_stage_requires_core_outputs_and_side_native_validator(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    snapshot = tmp_path / "snapshot"
    runtime = tmp_path / "runtime"
    for path in (
        workspace / ".codex",
        workspace / "docs" / "harness",
        workspace / "docs" / "knowledge",
        workspace / "docs" / "specs",
        workspace / "docs" / "hooks",
        snapshot / "scripts",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (workspace / "AGENTS.md").write_text("entry\n", encoding="utf-8")
    (workspace / ".codex" / "hooks.json").write_text("{}\n", encoding="utf-8")
    (workspace / "docs" / "harness" / "index.md").write_text("index\n", encoding="utf-8")
    (workspace / "docs" / "knowledge" / "index.md").write_text("index\n", encoding="utf-8")
    (workspace / "docs" / "knowledge" / "service-meta.yaml").write_text("name: demo\n", encoding="utf-8")
    (workspace / "docs" / "knowledge" / "project-context.md").write_text("project-mode: brownfield\n", encoding="utf-8")
    (snapshot / "scripts" / "validate_init_outputs.py").write_text(
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    strategy = {"stages": [{"semantic_role": "INIT"}]}
    manifest = {
        "workspace_preconditions": {
            "schema_version": "framework-eval-workspace-preconditions.v1",
            "helixharness_initialization": "ABSENT",
            "init_mode": "SERVICE",
        }
    }

    result = verify_initialization_outputs(
        strategy,
        manifest,
        workspace=workspace,
        snapshot_path=snapshot,
        runtime_root=runtime,
    )
    assert result["status"] == "PASS"
    assert result["missing_paths"] == []
    assert result["native_validator"]["status"] == "PASS"

    (workspace / "docs" / "knowledge" / "service-meta.yaml").unlink()
    failed = verify_initialization_outputs(
        strategy,
        manifest,
        workspace=workspace,
        snapshot_path=snapshot,
        runtime_root=tmp_path / "runtime-missing",
    )
    assert failed["status"] == "FAIL"
    assert failed["missing_paths"] == ["docs/knowledge/service-meta.yaml"]

    (workspace / "docs" / "knowledge" / "project-context.md").unlink()
    frontend = verify_initialization_outputs(
        strategy,
        {"workspace_preconditions": {"init_mode": "FRONTEND"}},
        workspace=workspace,
        snapshot_path=snapshot,
        runtime_root=tmp_path / "runtime-no-context",
    )
    assert frontend["status"] == "FAIL"
    assert frontend["missing_paths"] == ["docs/knowledge/project-context.md"]



def test_codex_adapter_rejects_cleanup_through_replaced_work_root(tmp_path: Path) -> None:
    helixharness, _, _, _, _ = _helixharness_repository(tmp_path)
    work_root = tmp_path / "replaceable-work"
    adapter = CodexScenarioExecutionAdapter(
        helixharness_repository=helixharness,
        repositories={"business-repo": tmp_path},
        work_root=work_root,
        sdk_adapter=_RecordingSdk(),
        snapshot_installer=_NoopInstaller(),
        telemetry_configurator=_NoopTelemetry(),
        judge_adapter=_PerfectJudge(),
    )
    run_id = "sv2_" + "d" * 32
    outside = tmp_path / "outside"
    outside_run = outside / run_id
    outside_run.mkdir(parents=True)
    (outside_run / "keep.txt").write_text("keep\n", encoding="utf-8")
    work_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="cleanup target is unsafe"):
        adapter.cleanup_run_workspace(run_id, ())
    assert (outside_run / "keep.txt").is_file()


def test_codex_adapter_rejects_cleanup_through_linked_checkpoint_root(tmp_path: Path) -> None:
    helixharness, _, _, _, _ = _helixharness_repository(tmp_path)
    work_root = tmp_path / "work"
    adapter = CodexScenarioExecutionAdapter(
        helixharness_repository=helixharness,
        repositories={"business-repo": tmp_path},
        work_root=work_root,
        sdk_adapter=_RecordingSdk(),
        snapshot_installer=_NoopInstaller(),
        telemetry_configurator=_NoopTelemetry(),
        judge_adapter=_PerfectJudge(),
    )
    job_id = "sj_" + "e" * 32
    outside = tmp_path / "outside-checkpoints"
    outside_job = outside / job_id
    outside_job.mkdir(parents=True)
    (outside_job / "keep.txt").write_text("keep\n", encoding="utf-8")
    work_root.mkdir()
    (work_root / "_workflow-checkpoints").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="cleanup target is unsafe"):
        adapter.cleanup_run_workspace("sv2_" + "f" * 32, (job_id,))
    assert (outside_job / "keep.txt").is_file()


def _strict_checkpoint_fixture(
    tmp_path: Path,
) -> tuple[WorkflowCheckpointStore, ClaimedJob, tuple[FrozenWorkflowStep, ...]]:
    store = WorkflowCheckpointStore(tmp_path / "checkpoints")
    job = ClaimedJob(
        job_id="scenario-job",
        run_id="scenario-run",
        case_id="scenario-case",
        side="BASE",
        sequence_number=0,
        run_config={},
        episode_id="scenario-job",
        attempt_id="scenario-job-attempt-1",
        attempt_number=1,
        lease_token="lease-1",
    )
    steps = (
        FrozenWorkflowStep(
            "STEP_01",
            "/helix:apply evaluation-scenario",
            "IMPLEMENTED",
            "GENERIC_STAGE_EVIDENCE",
            "APPLY",
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "evidence.txt").write_text("complete\n", encoding="utf-8")
    turn = CodexTurnExecutionResult(
        turn_id="turn-1",
        classification="COMPLETED",
        input_kind="WORKFLOW_STEP",
        input_text=steps[0].command,
        started_at_ms=1_000,
        completed_at_ms=2_000,
        duration_ms=1_000,
        final_response="complete",
        usage={"total_tokens": 100},
        error=None,
        events=(),
        workflow_step_id=steps[0].step_id,
        milestone_id=steps[0].milestone_id,
    )
    store.save(
        job,
        workspace_path=workspace,
        initial_tree_hash="initial-tree",
        workflow_steps=steps,
        turns=(turn,),
    )
    return store, job, steps


def test_scenario_checkpoint_binds_complete_frozen_step_contract(tmp_path: Path) -> None:
    store, job, steps = _strict_checkpoint_fixture(tmp_path)
    changed_steps = (replace(steps[0], command="/helix:verify evaluation-scenario"),)

    with pytest.raises(CodexExecutionError, match="does not match the frozen Episode"):
        store.restore_latest(
            replace(job, attempt_id="scenario-job-attempt-2", attempt_number=2),
            workflow_steps=changed_steps,
            target_workspace=tmp_path / "restored",
        )


def test_scenario_checkpoint_restores_stage_rework_cycle_count(tmp_path: Path) -> None:
    store, job, steps = _strict_checkpoint_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    repaired_artifact = (
        workspace
        / "docs/specs/active/evaluation-scenario/evidence.md"
    )
    repaired_artifact.parent.mkdir(parents=True)
    repaired_artifact.write_text("repaired evidence\n", encoding="utf-8")
    turn = store.restore_latest(
        job,
        workflow_steps=steps,
        target_workspace=tmp_path / "initial-restore",
    ).prior_turns[0]
    rework_job = replace(
        job,
        job_id="stage-rework-job",
        episode_id="stage-rework-job",
        attempt_id="stage-rework-job-attempt-1",
    )
    restored = store.save_closure(
        rework_job,
        workspace_path=workspace,
        initial_tree_hash="initial-tree",
        workflow_steps=steps,
        turns=(replace(turn, classification="FAILED"),),
        closure_resume=FrozenWorkflowClosureResume(
            origin_step=steps[0],
            next_input_kind="HANDLE_STAGE_FINDINGS",
            stage_rework_count=2,
        ),
        remaining_steps=(),
    )

    assert restored.closure_resume is not None
    assert restored.closure_resume.next_input_kind == "HANDLE_STAGE_FINDINGS"
    assert restored.closure_resume.stage_rework_count == 2

    resumed = store.restore_latest(
        replace(rework_job, attempt_id="stage-rework-job-attempt-2", attempt_number=2),
        workflow_steps=steps,
        target_workspace=tmp_path / "resumed-stage-rework",
    )
    assert resumed.closure_resume is not None
    assert resumed.closure_resume.next_input_kind == "HANDLE_STAGE_FINDINGS"
    assert resumed.closure_resume.stage_rework_count == 2
    assert (
        tmp_path
        / "resumed-stage-rework/docs/specs/active/evaluation-scenario/evidence.md"
    ).read_text(encoding="utf-8") == "repaired evidence\n"


def test_scenario_checkpoint_excludes_rebuildable_workspace_outputs(tmp_path: Path) -> None:
    store, job, steps = _strict_checkpoint_fixture(tmp_path)
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir()
    (workspace / "src" / "Service.java").write_text("final class Service {}\n", encoding="utf-8")
    (workspace / "module" / "build" / "libs").mkdir(parents=True)
    (workspace / "module" / "build" / "libs" / "module.jar").write_bytes(b"x" * 1_000_000)
    (workspace / "module" / ".gradle" / "cache").mkdir(parents=True)
    (workspace / "module" / ".gradle" / "cache" / "state.bin").write_bytes(b"y" * 1_000_000)

    generated_job = replace(job, job_id="generated-output-job", episode_id="generated-output-job")
    turn = replace(
        store.restore_latest(
            job,
            workflow_steps=steps,
            target_workspace=tmp_path / "first-restore",
        ).prior_turns[0],
        turn_id="generated-output-turn",
    )
    store.save(
        generated_job,
        workspace_path=workspace,
        initial_tree_hash="initial-tree",
        workflow_steps=steps,
        turns=(turn,),
    )

    checkpoint_workspace = next(
        (store.root / generated_job.episode_id).glob("01-*/workspace")
    )
    assert (checkpoint_workspace / "src" / "Service.java").is_file()
    assert not (checkpoint_workspace / "module" / "build").exists()
    assert not (checkpoint_workspace / "module" / ".gradle").exists()
    restored_workspace = tmp_path / "generated-output-restore"
    store.restore_latest(
        replace(generated_job, attempt_id="generated-output-attempt-2", attempt_number=2),
        workflow_steps=steps,
        target_workspace=restored_workspace,
    )
    assert (restored_workspace / "src" / "Service.java").is_file()
    assert not (restored_workspace / "module" / "build").exists()
    assert not (restored_workspace / "module" / ".gradle").exists()


def test_scenario_checkpoint_accepts_completed_closure_after_later_stage(tmp_path: Path) -> None:
    store = WorkflowCheckpointStore(tmp_path / "checkpoints")
    job = ClaimedJob(
        job_id="scenario-job",
        run_id="scenario-run",
        case_id="scenario-case",
        side="BASE",
        sequence_number=0,
        run_config={},
        episode_id="scenario-job",
        attempt_id="scenario-job-attempt-1",
        attempt_number=1,
        lease_token="lease-1",
    )
    steps = tuple(
        FrozenWorkflowStep(
            f"STEP_{index:02d}",
            f"/helix:step-{index} evaluation-scenario",
            f"STEP_{index:02d}_COMPLETED",
            "GENERIC_STAGE_EVIDENCE",
            "APPLY",
        )
        for index in range(1, 4)
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "evidence.txt").write_text("complete\n", encoding="utf-8")

    def turn(
        turn_id: str,
        *,
        input_kind: str,
        step: FrozenWorkflowStep,
    ) -> CodexTurnExecutionResult:
        return CodexTurnExecutionResult(
            turn_id=turn_id,
            classification="COMPLETED",
            input_kind=input_kind,
            input_text=step.command,
            started_at_ms=1_000,
            completed_at_ms=2_000,
            duration_ms=1_000,
            final_response="complete",
            usage={"total_tokens": 100},
            error=None,
            events=(),
            workflow_step_id=step.step_id,
            milestone_id=step.milestone_id,
        )

    restored = store.save(
        job,
        workspace_path=workspace,
        initial_tree_hash="initial-tree",
        workflow_steps=steps,
        turns=(
            turn("turn-1", input_kind="WORKFLOW_STEP", step=steps[0]),
            turn("turn-2", input_kind="WORKFLOW_STEP", step=steps[1]),
            turn("turn-3", input_kind="WORKFLOW_STEP", step=steps[2]),
            turn("turn-4", input_kind="HANDLE_STAGE_FINDINGS", step=steps[1]),
        ),
    )

    assert restored.last_completed_step_id == "STEP_03"
    with pytest.raises(CodexExecutionError, match="not a completed stage prefix"):
        store.save(
            replace(job, job_id="out-of-order-job", episode_id="out-of-order-job"),
            workspace_path=workspace,
            initial_tree_hash="initial-tree",
            workflow_steps=steps,
            turns=(
                turn("turn-1", input_kind="WORKFLOW_STEP", step=steps[0]),
                turn("turn-3", input_kind="WORKFLOW_STEP", step=steps[2]),
            ),
        )


@pytest.mark.parametrize(
    "recovery_input_kind",
    ["WORKFLOW_STEP_RERUN", "UNIVERSAL_USER_RESPONSE"],
)
def test_scenario_checkpoint_promotes_failed_stage_after_successful_stage_recovery(
    tmp_path: Path,
    recovery_input_kind: str,
) -> None:
    store = WorkflowCheckpointStore(tmp_path / "checkpoints")
    job = ClaimedJob(
        job_id="scenario-job",
        run_id="scenario-run",
        case_id="scenario-case",
        side="BASE",
        sequence_number=0,
        run_config={},
        episode_id="scenario-job",
        attempt_id="scenario-job-attempt-1",
        attempt_number=1,
        lease_token="lease-1",
    )
    steps = tuple(
        FrozenWorkflowStep(
            f"STEP_{index:02d}",
            f"/helix:step-{index} evaluation-scenario",
            f"STEP_{index:02d}_COMPLETED",
            "GENERIC_STAGE_EVIDENCE",
            "VERIFY" if index == 4 else "APPLY",
        )
        for index in range(1, 5)
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "evidence.txt").write_text("complete\n", encoding="utf-8")

    def turn(
        turn_id: str,
        *,
        input_kind: str,
        step: FrozenWorkflowStep,
        classification: str = "COMPLETED",
    ) -> CodexTurnExecutionResult:
        return CodexTurnExecutionResult(
            turn_id=turn_id,
            classification=classification,
            input_kind=input_kind,
            input_text=step.command,
            started_at_ms=1_000,
            completed_at_ms=2_000,
            duration_ms=1_000,
            final_response="complete",
            usage={"total_tokens": 100},
            error=None,
            events=(),
            workflow_step_id=step.step_id,
            milestone_id=step.milestone_id,
        )

    restored = store.save(
        job,
        workspace_path=workspace,
        initial_tree_hash="initial-tree",
        workflow_steps=steps,
        turns=(
            turn("turn-1", input_kind="WORKFLOW_STEP", step=steps[0]),
            turn("turn-2", input_kind="WORKFLOW_STEP", step=steps[1]),
            turn("turn-3", input_kind="WORKFLOW_STEP", step=steps[2]),
            turn(
                "turn-4",
                input_kind="WORKFLOW_STEP",
                step=steps[3],
                classification="FAILED",
            ),
            turn("turn-5", input_kind=recovery_input_kind, step=steps[3]),
        ),
    )

    assert restored.last_completed_step_id == "STEP_04"


def test_scenario_checkpoint_rejects_workspace_links_before_restore(tmp_path: Path) -> None:
    store, job, steps = _strict_checkpoint_fixture(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    checkpoint_workspace = next((tmp_path / "checkpoints/scenario-job").glob("01-*/workspace"))
    (checkpoint_workspace / "linked.txt").symlink_to(outside)

    with pytest.raises(CodexExecutionError, match="link or special file"):
        store.restore_latest(
            replace(job, attempt_id="scenario-job-attempt-2", attempt_number=2),
            workflow_steps=steps,
            target_workspace=tmp_path / "restored",
        )


def test_scenario_checkpoint_rejects_workspace_links_before_save(tmp_path: Path) -> None:
    store, job, steps = _strict_checkpoint_fixture(tmp_path)
    unsafe_workspace = tmp_path / "unsafe-workspace"
    unsafe_workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (unsafe_workspace / "linked.txt").symlink_to(outside)
    turn = CodexTurnExecutionResult(
        turn_id="turn-unsafe",
        classification="COMPLETED",
        input_kind="WORKFLOW_STEP",
        input_text=steps[0].command,
        started_at_ms=1_000,
        completed_at_ms=2_000,
        duration_ms=1_000,
        final_response="complete",
        usage={"total_tokens": 100},
        error=None,
        events=(),
        workflow_step_id=steps[0].step_id,
        milestone_id=steps[0].milestone_id,
    )

    with pytest.raises(CodexExecutionError, match="link or special file"):
        store.save(
            replace(job, episode_id="unsafe-job", job_id="unsafe-job"),
            workspace_path=unsafe_workspace,
            initial_tree_hash="initial-tree",
            workflow_steps=steps,
            turns=(turn,),
        )
    assert not (store.root / "unsafe-job").exists()


def test_scenario_checkpoint_rejects_linked_episode_root(tmp_path: Path) -> None:
    store, job, steps = _strict_checkpoint_fixture(tmp_path)
    linked_job = replace(job, episode_id="linked-episode", job_id="linked-episode")
    (store.root / linked_job.episode_id).symlink_to(
        store.root / job.episode_id,
        target_is_directory=True,
    )

    with pytest.raises(CodexExecutionError, match="Episode directory is invalid"):
        store.restore_latest(
            linked_job,
            workflow_steps=steps,
            target_workspace=tmp_path / "restored",
        )


def test_checkpoint_remaining_resources_stops_enforced_exhaustion() -> None:
    turn = CodexTurnExecutionResult(
        turn_id="turn-1",
        classification="COMPLETED",
        input_kind="WORKFLOW_STEP",
        input_text="command",
        started_at_ms=0,
        completed_at_ms=2_000,
        duration_ms=2_000,
        final_response="complete",
        usage={"total_tokens": 500},
        error=None,
        events=(
            CodexExecutionEvent(
                event_type="command/completed",
                category="command",
                status="COMPLETED",
                summary="complete",
                payload={},
                occurred_at_ms=1_000,
            ),
        ),
    )
    runtime = {
        "wall_timeout_seconds": 2,
        "token_budget": 100,
        "token_budget_enforced": False,
        "tool_call_budget": 1,
    }

    assert checkpoint_remaining_resources(runtime, (turn,)) == (
        0,
        -400,
        0,
        ("wall_timeout_seconds", 0),
    )


def test_checkpoint_usage_remains_cumulative_across_three_attempts() -> None:
    def turn(turn_id: str, tokens: int) -> CodexTurnExecutionResult:
        return CodexTurnExecutionResult(
            turn_id=turn_id,
            classification="COMPLETED",
            input_kind="WORKFLOW_STEP",
            input_text="command",
            started_at_ms=0,
            completed_at_ms=1_000,
            duration_ms=1_000,
            final_response="complete",
            usage={"input_tokens": tokens - 10, "output_tokens": 10, "total_tokens": tokens},
            error=None,
            events=(),
        )

    first_attempt = (turn("attempt-1", 500),)
    second_checkpoint = cumulative_checkpoint_turns(first_attempt, (turn("attempt-2", 700),))
    third_checkpoint = cumulative_checkpoint_turns(second_checkpoint, (turn("attempt-3", 300),))

    assert [item.usage["total_tokens"] for item in third_checkpoint] == [500, 1_200, 1_500]
    assert checkpoint_remaining_resources(
        {
            "wall_timeout_seconds": 10,
            "token_budget": 2_000,
            "token_budget_enforced": True,
            "tool_call_budget": 10,
        },
        third_checkpoint,
    ) == (7, 500, 10, None)


def test_strategy_recovery_policy_wires_bounded_goal_control_into_codex() -> None:
    from bench.tests.test_workflow_goal_control import _policy

    closure = _strategy_workflow_closure({"recovery_policy": {
        "schema_version": "framework-eval-strategy-recovery.v1",
        "feature": "demo",
        "workflow_goal_control": _policy(),
    }})
    assert closure is not None and closure.goal_policy is not None
    assert closure.goal_policy.max_archive_attempts == 2
    assert closure.max_stage_rework_cycles == 2
    assert closure.stage_findings_prompt == "handle stage findings"


def test_failed_archive_retries_only_after_stage_recovery() -> None:
    repeat = codex_execution.WorkflowGoalDecision(
        status="EXECUTE",
        reason="ARCHIVE_REQUIRED",
        actions=("ARCHIVE",),
        progress=codex_execution.WorkflowGoalProgress(archive_attempts=1, inferred_actions=1),
    )
    recover = codex_execution.WorkflowGoalDecision(
        status="EXECUTE",
        reason="STAGE_FAILED",
        actions=("HANDLE_STAGE_FINDINGS",),
        progress=codex_execution.WorkflowGoalProgress(inferred_actions=1),
    )

    assert codex_execution._repeats_failed_archive_without_recovery(repeat) is True
    assert codex_execution._repeats_failed_archive_without_recovery(recover) is False


def test_workflow_stage_recovery_honors_only_exact_routed_frozen_commands() -> None:
    propose = FrozenWorkflowStep(
        "STEP_01",
        "/helix:propose evaluation-scenario",
        "STEP_01_COMPLETED",
        "HELIXHARNESS_PROPOSE",
        "PROPOSE",
    )
    apply = FrozenWorkflowStep(
        "STEP_02",
        "/helix:apply evaluation-scenario",
        "STEP_02_COMPLETED",
        "HELIXHARNESS_APPLY",
        "APPLY",
    )
    archive = FrozenWorkflowStep(
        "STEP_03",
        "/helix:archive evaluation-scenario",
        "STEP_03_COMPLETED",
        "HELIXHARNESS_ARCHIVE",
        "ARCHIVE",
    )
    steps = (propose, apply, archive)

    assert (
        codex_execution._recommended_frozen_stage_recovery(
            steps,
            archive,
            "recommendation=/helix:apply evaluation-scenario",
        )
        == apply
    )
    assert (
        codex_execution._recommended_frozen_stage_recovery(
            steps,
            archive,
            "recommendation: /helix:apply evaluation-scenario",
        )
        == apply
    )
    assert (
        codex_execution._recommended_frozen_stage_recovery(
            steps,
            archive,
            (
                "reason=workflow_stage_already_advanced\n"
                "请先继续执行：`/helix:apply evaluation-scenario`"
            ),
        )
        == archive
    )
    assert (
        codex_execution._recommended_frozen_stage_recovery(
            steps,
            archive,
            "Run an unrelated command before Archive.",
        )
        == archive
    )
    assert (
        codex_execution._recommended_frozen_stage_recovery(
            steps,
            apply,
            "recommendation=/helix:propose evaluation-scenario",
        )
        == propose
    )
    assert (
        codex_execution._recommended_frozen_stage_recovery(
            steps,
            archive,
            "Propose already ran as `/helix:propose evaluation-scenario`.",
        )
        == archive
    )
    assert (
        codex_execution._recommended_frozen_stage_recovery(
            steps,
            archive,
            "Try `/helix:apply evaluation-scenario --force`.",
        )
        == archive
    )
    assert (
        codex_execution._recommended_frozen_stage_recovery(
            steps,
            archive,
            "Archive mentions `/helix:apply evaluation-scenario` as prior work.",
        )
        == archive
    )
    assert (
        codex_execution._recommended_frozen_stage_recovery(
            steps,
            archive,
            (
                "reason=workflow_stage_already_advanced\n"
                "Try `/helix:apply evaluation-scenario --force`."
            ),
        )
        == archive
    )
    assert (
        codex_execution._deterministic_frozen_stage_recommendation(
            steps,
            apply,
            "recommendation=/helix:apply evaluation-scenario",
        )
        == apply
    )
    assert (
        codex_execution._deterministic_frozen_stage_recommendation(
            steps,
            archive,
            "\n".join(
                (
                    "recommendation=/helix:propose evaluation-scenario",
                    "recommendation=/helix:apply evaluation-scenario",
                )
            ),
        )
        is None
    )
    assert (
        codex_execution._deterministic_frozen_stage_recommendation(
            steps,
            apply,
            "recommendation=/helix:archive evaluation-scenario",
        )
        is None
    )
    assert (
        codex_execution._deterministic_frozen_stage_recommendation(
            steps,
            apply,
            "recommendation=/helix:propose other-feature",
        )
        is None
    )
    assert (
        codex_execution._deterministic_frozen_stage_recommendation(
            steps,
            apply,
            "recommendation =/helix:propose evaluation-scenario",
        )
        is None
    )

    assert codex_execution._workflow_recovery_suffix_index(steps, archive, len(steps)) == 3


def test_unrecognized_artifact_contract_does_not_infer_recovery() -> None:
    apply = FrozenWorkflowStep(
        "STEP_03",
        "/helix:apply evaluation-scenario",
        "STEP_03_COMPLETED",
        "APPLY",
        "APPLY",
    )
    archive = FrozenWorkflowStep(
        "STEP_05",
        "/helix:archive evaluation-scenario",
        "STEP_05_COMPLETED",
        "ARCHIVE",
        "ARCHIVE",
    )

    assert (
        codex_execution._recommended_frozen_stage_recovery(
            (apply, archive),
            archive,
            "Router requires `/helix:apply evaluation-scenario` before Archive.",
        )
        == archive
    )


def test_stage_rework_budget_survives_a_successful_backrouted_rerun() -> None:
    apply = FrozenWorkflowStep(
        "STEP_02",
        "/helix:apply evaluation-scenario",
        "STEP_02_COMPLETED",
        "APPLY",
        "APPLY",
    )
    archive = FrozenWorkflowStep(
        "STEP_03",
        "/helix:archive evaluation-scenario",
        "STEP_03_COMPLETED",
        "ARCHIVE",
        "ARCHIVE",
    )

    assert codex_execution._stage_rework_count_after_rerun(2, (archive,), 0) == 2
    assert codex_execution._stage_rework_count_after_rerun(2, (apply, archive), 2) == 0


def test_stage_findings_prompt_forbids_cross_stage_or_directory_reversal() -> None:
    validation = CodexExecutionEvent(
        event_type="stage/validation",
        category="validation",
        status="FAILED",
        summary="Review state has invalid review_scope_sha256",
        payload={"error": "Review state has invalid review_scope_sha256"},
        occurred_at_ms=2,
    )
    turn = CodexTurnExecutionResult(
        turn_id="turn-archive",
        classification="FAILED",
        input_kind="WORKFLOW_STEP",
        input_text="/helix:archive evaluation-scenario",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response="blocked",
        usage={"total_tokens": 1},
        error=None,
        events=(validation,),
    )

    prompt = codex_execution.CodexPythonSdkAdapter._stage_findings_input(
        "修复当前阶段。",
        [turn],
    )

    assert "Review state has invalid review_scope_sha256" in prompt
    assert "不得执行其他 /helix:* 阶段命令" in prompt
    assert "不得在 docs/specs/active 与 docs/specs/completed 之间移动需求目录" in prompt


def test_stage_findings_prompt_explains_review_state_identity_repair() -> None:
    validation = CodexExecutionEvent(
        event_type="stage/validation",
        category="validation",
        status="FAILED",
        summary="Review state has invalid identity",
        payload={"error": "Review state has invalid identity"},
        occurred_at_ms=2,
    )
    turn = CodexTurnExecutionResult(
        turn_id="turn-archive",
        classification="FAILED",
        input_kind="WORKFLOW_STEP_RERUN",
        input_text="/helix:archive evaluation-scenario",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response="blocked",
        usage={"total_tokens": 1},
        error=None,
        events=(validation,),
    )

    prompt = codex_execution.CodexPythonSdkAdapter._stage_findings_input(
        "修复当前阶段。",
        [turn],
    )

    assert "[平台定向修复提示]" in prompt
    assert "schema_version 必须是 JSON 数字 1（不是字符串）" in prompt
    assert "feature 必须与当前冻结命令中的 feature 名完全一致" in prompt
    assert "不得改写其他已通过的业务实现" in prompt


def test_stage_findings_prompt_does_not_add_identity_hint_to_other_failures() -> None:
    validation = CodexExecutionEvent(
        event_type="stage/validation",
        category="validation",
        status="FAILED",
        summary="Workflow checkpoint archive move is incomplete",
        payload={"error": "Workflow checkpoint archive move is incomplete"},
        occurred_at_ms=2,
    )
    turn = CodexTurnExecutionResult(
        turn_id="turn-archive",
        classification="FAILED",
        input_kind="WORKFLOW_STEP",
        input_text="/helix:archive evaluation-scenario",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response="blocked",
        usage={"total_tokens": 1},
        error=None,
        events=(validation,),
    )

    prompt = codex_execution.CodexPythonSdkAdapter._stage_findings_input(
        "修复当前阶段。",
        [turn],
    )

    assert "[平台定向修复提示]" not in prompt


@pytest.mark.parametrize("layout", ("standalone", "combined", "nested"))
def test_formal_codex_worker_uses_exact_strategy_commits_and_produces_report(tmp_path: Path, layout: str) -> None:
    scenario_source, business_repository = _repository_scenario(tmp_path)
    helixharness, base_commit, base_hash, candidate_commit, candidate_hash = _helixharness_repository(
        tmp_path, combined=layout != "standalone", nested=layout == "nested",
    )
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    scenario = store.publish(load_scenario_source(scenario_source))
    calibration = ScenarioCalibrationService(
        store,
        repositories={"business-repo": business_repository},
    ).calibrate(scenario.case_id)
    assert calibration.status == "CALIBRATED"
    base_strategy = _strategy("base", base_commit, base_hash)
    candidate_strategy = _strategy("candidate", candidate_commit, candidate_hash)
    if layout == "nested":
        base_strategy = replace(base_strategy, source_plugin_path="products/tooling/plugin")
        candidate_strategy = replace(candidate_strategy, source_plugin_path="products/tooling/plugin")
        helixharness = helixharness / "products"
    contract = _contract(base_strategy, candidate_strategy)
    request = ScenarioRunCreateRequest(
        scenario_ids=(scenario.case_id,),
        run_kind="FORMAL",
        execution_adapter="CODEX",
        evaluation_contract=contract,
        created_by="operator",
        runtime_policy={
            "schema_version": "framework-eval-runtime-policy.v2",
            "execution_model": "codex-test",
            "judge_model": "judge-test",
            "reasoning_effort": "high",
            "judge_reasoning_effort": "high",
            "wall_timeout_seconds": 600,
            "token_budget": 100_000,
            "tool_call_budget": 1_000,
            "disk_budget_mb": 1_024,
        },
    )
    judge = _PerfectJudge()
    service = ScenarioRunService(
        store,
        strategy_validator=GitScenarioStrategyValidator(helixharness),
    )
    preview = service.preview(request)
    run = service.create(request, expected_preview_hash=preview.preview_hash)
    sdk = _RecordingSdk()
    work_root = tmp_path / "work"
    telemetry = _RepositoryRequiredTelemetry()
    adapter = CodexScenarioExecutionAdapter(
        helixharness_repository=helixharness,
        repositories={"business-repo": business_repository},
        work_root=work_root,
        sdk_adapter=sdk,
        snapshot_installer=_NoopInstaller(),
        telemetry_configurator=telemetry,
        judge_adapter=judge,
    )
    worker = ScenarioWorker(store, adapter)

    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert len(tuple(work_root.rglob("verifier-gradle-home/gradle.properties"))) == 2
    assert not tuple(work_root.rglob("commands/*/gradle-user-home"))
    assert worker.run_once(run_id=run.run_id).status == "FINALIZED"

    report = service.get_report(run.run_id)
    assert not (work_root / run.run_id).exists()
    assert not (work_root / "_workflow-checkpoints").exists()
    assert len(telemetry.workspaces) == 2
    assert sdk.snapshot_documents == ["base workflow", "candidate workflow"]
    assert {item.max_business_clarifications for item in sdk.requests} == {3}
    assert all("Complete is the target." in item.universal_user_response for item in sdk.requests)
    assert all("无需等待外部确认" in item.universal_user_response for item in sdk.requests)
    assert report["calculation"]["status"] == "FULLY_COMPARABLE"
    assert report["execution_outcomes"][0]["outcome"]["verification"]["status"] == "PASS"
    assert {
        item["outcome"]["verification"]["hidden_test_injection"]
        for item in report["execution_outcomes"]
    } == {"DIRECT"}
    assert "immutable_judge_evidence" not in report["execution_outcomes"][0]["outcome"]
    workflow_request = judge.requests[0]
    assert set(workflow_request["axes"]) == {
        "goal_requirement_decomposition",
        "decision_planning_quality",
        "implementation_traceability",
        "verification_completion_quality",
    }
    assert {item["kind"] for item in workflow_request["evidence"]} == {"STAGE_ARTIFACT"}
    assert "不得判断业务是否实际成功" in workflow_request["score_contract"]["execution_fact_policy"]
    assert set(workflow_request["score_contract"]["axis_anchors"]) == set(workflow_request["axes"])
    outcome_request = judge.requests[1]
    assert outcome_request["axes"] == ["requirement_fulfillment", "implementation_quality"]
    assert "business_verification" in outcome_request["evidence_scopes"][0]["allowed_evidence_ids"]
    assert outcome_request["score_contract"]["weights"] == {
        "requirement_fulfillment": 90,
        "implementation_quality": 10,
    }
    assert "不得被源码推测推翻" in outcome_request["score_contract"]["business_pass_policy"]
    assert "不得在 requirement_fulfillment 和 implementation_quality 重复扣分" in (
        outcome_request["score_contract"]["cross_axis_deduction_policy"]
    )
    recalculated = service.recalculate(run.run_id, reason="rebuild deterministic aggregate")
    corrected_report = service.get_report(run.run_id, calculation_id=recalculated.calculation_id)
    assert corrected_report["calculation"]["status"] == "FULLY_COMPARABLE"
    assert len(judge.requests) == 4
    assert sdk.snapshot_documents == ["base workflow", "candidate workflow"]


@pytest.mark.parametrize("fail_after_final_stage", (False, True))
def test_scenario_retry_restores_latest_completed_stage_checkpoint(
    tmp_path: Path,
    fail_after_final_stage: bool,
) -> None:
    scenario_source, business_repository = _repository_scenario(tmp_path)
    helixharness, base_commit, base_hash, candidate_commit, candidate_hash = _helixharness_repository(tmp_path)
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    scenario = store.publish(load_scenario_source(scenario_source))
    assert ScenarioCalibrationService(
        store,
        repositories={"business-repo": business_repository},
    ).calibrate(scenario.case_id).status == "CALIBRATED"

    def two_stage(strategy: ExecutionStrategy) -> ExecutionStrategy:
        commands = (strategy.commands[0], f"{strategy.commands[0]} --verify")
        return replace(
            strategy,
            commands=commands,
            stages=(
                StrategyStage("STEP_01", "IMPLEMENTATION", commands[0], "IMPLEMENTED", True),
                StrategyStage("STEP_02", "VERIFICATION", commands[1], "VERIFIED", True),
            ),
            completion_states=("IMPLEMENTED", "VERIFIED"),
        )

    contract = _contract(
        two_stage(_strategy("base", base_commit, base_hash)),
        two_stage(_strategy("candidate", candidate_commit, candidate_hash)),
    )
    request = ScenarioRunCreateRequest(
        scenario_ids=(scenario.case_id,),
        run_kind="FORMAL",
        execution_adapter="CODEX",
        evaluation_contract=contract,
        created_by="checkpoint-test",
        runtime_policy={
            "schema_version": "framework-eval-runtime-policy.v2",
            "execution_model": "codex-test",
            "judge_model": "judge-test",
            "reasoning_effort": "high",
            "judge_reasoning_effort": "high",
            "wall_timeout_seconds": 600,
            "token_budget": 100_000,
            "tool_call_budget": 1_000,
            "disk_budget_mb": 1_024,
        },
    )
    judge = _PerfectJudge()
    service = ScenarioRunService(
        store,
        strategy_validator=GitScenarioStrategyValidator(helixharness),
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)
    sdk = _CheckpointingSdk(
        fail_calls=frozenset({1, 2}) if fail_after_final_stage else frozenset({1})
    )
    work_root = tmp_path / "checkpoint-work"
    worker = ScenarioWorker(
        store,
        CodexScenarioExecutionAdapter(
            helixharness_repository=helixharness,
            repositories={"business-repo": business_repository},
            work_root=work_root,
            sdk_adapter=sdk,
            snapshot_installer=_NoopInstaller(),
            telemetry_configurator=_NoopTelemetry(),
            judge_adapter=judge,
        ),
    )

    assert worker.run_once(run_id=run.run_id).status == "RETRY_PENDING"
    second_status = worker.run_once(run_id=run.run_id).status
    if fail_after_final_stage:
        assert second_status == "RETRY_PENDING"
        assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    else:
        assert second_status == "PROCESSED"

    assert [tuple(step.step_id for step in item.workflow_steps) for item in sdk.requests] == [
        ("STEP_01", "STEP_02"),
        ("STEP_02",),
    ]
    assert sdk.requests[1].resumed_after_step_id == "STEP_01"
    assert sdk.requests[1].wall_timeout_seconds == 599
    assert sdk.requests[1].token_budget == 99_500
    assert sdk.requests[1].tool_call_budget == 999
    assert (
        work_root
        / run.run_id
        / scenario.case_id
        / "base"
        / "attempt-2"
        / "workspace/docs/specs/completed/evaluation-scenario/spec.md"
    ).is_file()
    checkpoint_manifests = tuple((work_root / "_workflow-checkpoints").rglob("checkpoint.json"))
    assert checkpoint_manifests
    base_job = next(item for item in store.list_run_jobs(run.run_id) if item.side == "BASE")
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute(
            "SELECT outcome_json FROM scenario_jobs WHERE job_id = ?",
            (base_job.job_id,),
        ).fetchone()
    assert row is not None
    outcome = json.loads(str(row[0]))
    resources = outcome["resources"]
    assert resources["accounting_status"] == "COMPLETE"
    assert resources["attributable"]["agent_tokens"] == 1_000
    assert resources["attributable"]["active_execution_seconds"] == 3.0
    assert resources["actual"]["agent_tokens"] == 1_000
    assert resources["actual"]["execution_seconds"] == 3.0
    assert resources["actual"]["observed_attempts"] == (3 if fail_after_final_stage else 2)


def test_semantic_workflow_failure_is_scored_with_hard_gate(tmp_path: Path) -> None:
    scenario_source, business_repository = _repository_scenario(tmp_path)
    helixharness, base_commit, base_hash, candidate_commit, candidate_hash = _helixharness_repository(tmp_path)
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    scenario = store.publish(load_scenario_source(scenario_source))
    calibration = ScenarioCalibrationService(
        store,
        repositories={"business-repo": business_repository},
    ).calibrate(scenario.case_id)
    assert calibration.status == "CALIBRATED"
    contract = _contract(
        _strategy("base", base_commit, base_hash),
        _strategy("candidate", candidate_commit, candidate_hash),
    )
    request = ScenarioRunCreateRequest(
        scenario_ids=(scenario.case_id,),
        run_kind="FORMAL",
        execution_adapter="CODEX",
        evaluation_contract=contract,
        created_by="semantic-terminal-test",
        runtime_policy={
            "schema_version": "framework-eval-runtime-policy.v2",
            "execution_model": "codex-test",
            "judge_model": "judge-test",
            "reasoning_effort": "high",
            "judge_reasoning_effort": "high",
            "wall_timeout_seconds": 600,
            "token_budget": 100_000,
            "tool_call_budget": 1_000,
            "disk_budget_mb": 1_024,
        },
    )
    judge = _PerfectJudge()
    service = ScenarioRunService(
        store,
        strategy_validator=GitScenarioStrategyValidator(helixharness),
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)
    work_root = tmp_path / "semantic-failure-work"
    adapter = CodexScenarioExecutionAdapter(
        helixharness_repository=helixharness,
        repositories={"business-repo": business_repository},
        work_root=work_root,
        sdk_adapter=_RecordingSdk(
            status="FAILED",
            write_evidence=False,
            error={"type": "WorkflowError", "message": "Archive verification is blocked"},
        ),
        snapshot_installer=_NoopInstaller(),
        telemetry_configurator=_NoopTelemetry(),
        judge_adapter=judge,
    )
    worker = ScenarioWorker(store, adapter)

    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "PROCESSED"
    assert worker.run_once(run_id=run.run_id).status == "FINALIZED"

    report = service.get_report(run.run_id)
    assert service.get_run(run.run_id).status == "COMPLETED"
    assert report["calculation"]["status"] == "FULLY_COMPARABLE"
    assert report["calculation"]["recommendation"] == "NO_CLEAR_GAIN"
    assert report["calculation"]["base_unified_score"] == 100
    assert report["calculation"]["candidate_unified_score"] == 100
    assert report["calculation"]["base_case_hard_gates"] == ["WORKFLOW_TERMINAL_FAILURE"]
    assert report["calculation"]["candidate_case_hard_gates"] == ["WORKFLOW_TERMINAL_FAILURE"]
    assert {item["outcome"]["outcome_quality"] for item in report["execution_outcomes"]} == {100.0}
    assert {item["outcome"]["framework_workflow_quality"] for item in report["execution_outcomes"]} == {None}
    assert len(judge.requests) == 2
    assert all(request["axes"] == ["requirement_fulfillment", "implementation_quality"] for request in judge.requests)
    assert {
        item["outcome"]["terminal_status"]
        for item in report["execution_outcomes"]
    } == {"FAILED"}
    diagnostic_root = work_root / "_diagnostics" / run.run_id
    assert len(tuple(diagnostic_root.glob("sj_*-attempt-1.json"))) == 2
    assert (diagnostic_root / ".terminal.json").is_file()
    assert not (work_root / run.run_id).exists()


def test_evidence_reader_rejects_parent_directory_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "problem.md").write_text("host secret\n", encoding="utf-8")
    (workspace / "evidence").symlink_to(outside, target_is_directory=True)
    strategy = {
        "artifact_sources": [
            {
                "source_id": "completed-stage-artifacts",
                "root": "evidence",
                "include_glob": "**/*.md",
                "artifact_schema": "helixharness-completed-markdown.v1",
                "required": True,
                "max_files": 50,
                "max_total_bytes": 500_000,
            }
        ]
    }

    with pytest.raises(ScenarioExecutionSupportError, match="symbolic link"):
        CodexScenarioExecutionAdapter._collect_artifact_evidence(strategy, workspace)


def test_code_evidence_separates_production_tests_and_collects_bounded_context(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "Scenario Test")
    _git(workspace, "config", "user.email", "scenario@example.test")
    source = workspace / "src"
    tests = workspace / "tests"
    source.mkdir()
    tests.mkdir()
    (source / "FooService.java").write_text(
        "class FooService { FooRequest map() { return new FooRequest(); } }\n",
        encoding="utf-8",
    )
    (source / "FooController.java").write_text(
        "class FooController { FooService service; }\n",
        encoding="utf-8",
    )
    (source / "FooRequest.java").write_text("record FooRequest(String id) {}\n", encoding="utf-8")
    (source / "Unrelated.java").write_text(
        "class Unrelated { enum Column { ID } }\n",
        encoding="utf-8",
    )
    (tests / "FooServiceTest.java").write_text("class FooServiceTest {}\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "initial")
    (source / "FooService.java").write_text(
        "class FooService { enum Column { ID } FooRequest map() { return new FooRequest(\"changed\"); } }\n",
        encoding="utf-8",
    )
    (tests / "FooServiceTest.java").write_text(
        "class FooServiceTest { void changed() {} }\n",
        encoding="utf-8",
    )
    (source / "NewHelper.java").write_text("class NewHelper {}\n", encoding="utf-8")

    records, judge_records = CodexScenarioExecutionAdapter._collect_code_evidence(workspace)

    production_patch = next(item for item in judge_records if item["kind"] == "CODE_CHANGESET")
    test_patch = next(item for item in judge_records if item["kind"] == "TEST_CHANGESET")
    assert "FooService.java" in production_patch["content"]
    assert "NewHelper.java" in production_patch["content"]
    assert "FooServiceTest.java" not in production_patch["content"]
    assert "FooServiceTest.java" in test_patch["content"]
    assert "FooService.java" not in test_patch["content"]
    context_paths = {
        item["path"]
        for item in judge_records
        if item["kind"] == "CODE_CONTEXT"
    }
    assert context_paths == {"src/FooController.java", "src/FooRequest.java"}
    assert "src/Unrelated.java" not in context_paths
    full_file_evidence = [
        item
        for item in judge_records
        if item["kind"] in {"CODE_IMPLEMENTATION", "TEST_CODE", "CODE_CONTEXT"}
    ]
    assert all(item["start_line"] == 1 and item["end_line"] >= 1 for item in full_file_evidence)
    context_index = next(item for item in records if item["kind"] == "CODE_CONTEXT_INDEX")
    assert context_index["selection_policy"] == "declared-symbol-callers-and-referenced-types.v1"


def test_code_evidence_keeps_patch_when_changed_full_file_exceeds_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.name", "Scenario Test")
    _git(workspace, "config", "user.email", "scenario@example.test")
    source = workspace / "LargeService.java"
    source.write_text("class LargeService {\n" + "  int value = 1;\n" * 10 + "}\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "initial")
    source.write_text("class LargeService {\n" + "  int value = 2;\n" * 10 + "}\n", encoding="utf-8")
    monkeypatch.setattr("bench.scenario_codex._MAX_CODE_FILE_BYTES", 32)

    records, judge_records = CodexScenarioExecutionAdapter._collect_code_evidence(workspace)

    patch = next(item for item in judge_records if item["kind"] == "CODE_CHANGESET")
    assert "LargeService.java" in patch["content"]
    omitted = next(item for item in records if item.get("locator") == "LargeService.java")
    assert omitted["status"] == "OMITTED_SIZE_LIMIT"
    assert omitted["reason"] == "FULL_FILE_CONTENT_EXCEEDS_FROZEN_EVIDENCE_LIMIT"
    assert not any(item.get("path") == "LargeService.java" for item in judge_records)


def test_business_verification_timeout_is_inconclusive_not_business_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("fixture\n", encoding="utf-8")
    monkeypatch.setattr(
        "bench.scenario_codex.run_frozen_command",
        lambda *args, **kwargs: FrozenCommandResult(returncode=None, timed_out=True),
    )
    adapter = object.__new__(CodexScenarioExecutionAdapter)
    adapter.repositories = {}

    verification = adapter._verify_business(
        {
            "execution_tests": [
                {
                    "check_id": "F2P-1",
                    "kind": "FAIL_TO_PASS",
                    "argv": ["python3", "-c", "raise SystemExit(0)"],
                    "timeout_seconds": 1,
                }
            ]
        },
        manifest={},
        snapshot={},
        workspace=workspace,
        episode_root=tmp_path / "episode",
    )

    assert verification["status"] == "INCONCLUSIVE"
    assert verification["checks"] == [
        {
            "check_id": "F2P-1",
            "kind": "FAIL_TO_PASS",
            "status": "ERROR",
            "timed_out": True,
            "duration_ms": verification["checks"][0]["duration_ms"],
        }
    ]


def test_strategy_validator_requires_base_latest_remote_master_and_exact_documents(tmp_path: Path) -> None:
    helixharness, base_commit, base_hash, candidate_commit, candidate_hash = _helixharness_repository(tmp_path)
    validator = GitScenarioStrategyValidator(helixharness)
    validator.validate(
        _contract(
            _strategy("base", base_commit, base_hash),
            _strategy("candidate", candidate_commit, candidate_hash),
        )
    )

    with pytest.raises(ValueError, match="latest configured baseline"):
        validator.validate(
            _contract(
                _strategy("base", candidate_commit, candidate_hash),
                _strategy("candidate", base_commit, base_hash),
            )
        )


def test_strategy_validator_refreshes_remote_master_before_create(tmp_path: Path) -> None:
    helixharness, base_commit, base_hash, candidate_commit, candidate_hash = _helixharness_repository(tmp_path)
    validator = GitScenarioStrategyValidator(helixharness)
    contract = _contract(
        _strategy("base", base_commit, base_hash),
        _strategy("candidate", candidate_commit, candidate_hash),
    )
    publisher = tmp_path / "publisher"
    _git(tmp_path, "clone", str(tmp_path / "helixharness-origin.git"), str(publisher))
    _git(publisher, "config", "user.name", "Scenario Publisher")
    _git(publisher, "config", "user.email", "publisher@example.test")
    (publisher / "commands.md").write_text("new remote master workflow\n", encoding="utf-8")
    _git(publisher, "add", "commands.md")
    _git(publisher, "commit", "-m", "advance remote master")
    _git(publisher, "push", "origin", "master")

    with pytest.raises(ValueError, match="latest configured baseline"):
        validator.validate(contract)


def test_scenario_judge_rejects_citation_outside_artifact_scope() -> None:
    contract = _contract(
        _strategy("base", "a" * 40, "c" * 64),
        _strategy("candidate", "b" * 40, "d" * 64),
    )
    evidence = [
        {
            "evidence_id": "artifact-spec",
            "kind": "STAGE_ARTIFACT",
            "path": "docs/specs/completed/feature/spec.md",
            "content": "# Requirements",
        }
    ]
    request = build_workflow_judge_request(
        contract,
        gold={
            "requirements": [{"requirement_id": "REQ-1"}],
            "source_evidence": [{"evidence_id": "E-1"}],
        },
        evidence=evidence,
    )
    output = _PerfectJudge().judge(request, model="judge-test", reasoning_effort="high")
    output["axis_scores"][0]["evidence_ids"] = ["not-in-scope"]

    with pytest.raises(JudgeContractError, match="unknown evidence|outside axis scope"):
        validate_scenario_judgment(output, request)


def test_scenario_judge_repairs_invalid_citations_without_reexecuting_workflow() -> None:
    contract = _contract(
        _strategy("base", "a" * 40, "c" * 64),
        _strategy("candidate", "b" * 40, "d" * 64),
    )
    request = build_workflow_judge_request(
        contract,
        gold={
            "requirements": [{"requirement_id": "REQ-1"}],
            "source_evidence": [{"evidence_id": "GOLD-1"}],
        },
        evidence=[
            {
                "evidence_id": "artifact-spec",
                "kind": "STAGE_ARTIFACT",
                "path": "docs/specs/completed/feature/spec.md",
                "content": "# Requirements",
            }
        ],
    )
    judge = _InvalidThenPerfectJudge()
    adapter = object.__new__(CodexScenarioExecutionAdapter)
    adapter.judge_adapter = judge
    job = SimpleNamespace(
        contract=contract,
        side="BASE",
        runtime_policy={
            "judge_model": "judge-test",
            "judge_reasoning_effort": "high",
            "judge_max_retries": 1,
        },
    )

    output, scores, diagnostics = adapter._execute_judge(job, request)

    assert output["status"] == "COMPLETED"
    assert set(scores) == set(request["axes"])
    assert len(judge.requests) == 2
    retry = judge.requests[1]["validation_retry"]
    assert retry["previous_error"] == "Judge cited unknown Gold evidence reference"
    assert retry["legal_gold_evidence_refs"] == ["GOLD-1"]
    assert retry["legal_evidence_ids_by_axis"]
    assert diagnostics[0]["output"]["axis_scores"][0]["gold_evidence_refs"] == ["GOLD-UNKNOWN"]


def test_scenario_judge_retains_bounded_invalid_output_after_retry_exhaustion() -> None:
    contract = _contract(
        _strategy("base", "a" * 40, "c" * 64),
        _strategy("candidate", "b" * 40, "d" * 64),
    )
    request = build_workflow_judge_request(
        contract,
        gold={
            "requirements": [{"requirement_id": "REQ-1"}],
            "source_evidence": [{"evidence_id": "GOLD-1"}],
        },
        evidence=[
            {
                "evidence_id": "artifact-spec",
                "kind": "STAGE_ARTIFACT",
                "path": "docs/specs/completed/feature/spec.md",
                "content": "# Requirements",
            }
        ],
    )
    judge = _InvalidThenPerfectJudge(always_invalid=True)
    adapter = object.__new__(CodexScenarioExecutionAdapter)
    adapter.judge_adapter = judge
    job = SimpleNamespace(
        contract=contract,
        side="BASE",
        runtime_policy={
            "judge_model": "judge-test",
            "judge_reasoning_effort": "high",
            "judge_max_retries": 1,
        },
    )
    attempt_record = {"terminal_status": "COMPLETED", "usage": {"total_tokens": 10}}

    with pytest.raises(ScenarioCodexExecutionError, match="unknown Gold evidence reference") as caught:
        adapter._execute_judge(job, request, attempt_record=attempt_record)

    assert len(judge.requests) == 2
    assert caught.value.attempt_record is not None
    error = caught.value.attempt_record["error"]
    assert error["type"] == "JudgeContractError"
    assert len(error["validation_attempts"]) == 2
    assert error["validation_attempts"][0]["output"]["axis_scores"][0]["gold_evidence_refs"] == [
        "GOLD-UNKNOWN"
    ]


def test_outcome_judge_requires_business_code_and_every_requirement_citation() -> None:
    contract = _contract(
        _strategy("base", "a" * 40, "c" * 64),
        _strategy("candidate", "b" * 40, "d" * 64),
    )
    evidence = [
        {
            "evidence_id": "production-code-changeset",
            "kind": "CODE_CHANGESET",
            "path": "production.git.patch",
            "content": "diff --git a/src/A.java b/src/A.java",
        },
        {
            "evidence_id": "artifact-spec",
            "kind": "STAGE_ARTIFACT",
            "path": "docs/specs/completed/feature/spec.md",
            "content": "# Requirements",
        },
        {
            "evidence_id": "test-code-changeset",
            "kind": "TEST_CHANGESET",
            "path": "tests.git.patch",
            "content": "diff --git a/src/test/A.java b/src/test/A.java",
        },
    ]
    request = build_outcome_judge_request(
        contract,
        gold={
            "requirements": [
                {"requirement_id": "REQ-1", "statement": "交付生产实现"},
                {"requirement_id": "REQ-2", "statement": "保持既有行为"},
            ],
            "source_evidence": [{"evidence_id": "E-1"}],
        },
        verification={"status": "PASS", "checks": []},
        evidence=evidence,
    )
    output = _PerfectJudge().judge(request, model="judge-test", reasoning_effort="high")

    assert validate_scenario_judgment(output, request) == {
        "requirement_fulfillment": 100.0,
        "implementation_quality": 100.0,
    }
    evidence_policy = request["score_contract"]["implementation_evidence_policy"]
    assert evidence_policy["production_code_evidence_ids"] == ["production-code-changeset"]
    assert evidence_policy["test_code_evidence_ids"] == []
    assert evidence_policy["test_deliverable_requirement_ids"] == []
    assert "test-code-changeset" not in request["evidence_scopes"][1]["allowed_evidence_ids"]
    assert "仅当冻结 Requirement 明确要求" in evidence_policy["test_code_scoring_rule"]
    assert set(request["score_contract"]["axis_anchors"]) == {
        "requirement_fulfillment",
        "implementation_quality",
    }
    implementation_axis = next(
        item for item in output["axis_scores"] if item["axis"] == "implementation_quality"
    )
    implementation_axis["requirement_ids"] = []
    implementation_axis["gold_evidence_refs"] = []
    assert validate_scenario_judgment(output, request)["implementation_quality"] == 100.0
    requirement_axis = next(
        item for item in output["axis_scores"] if item["axis"] == "requirement_fulfillment"
    )
    requirement_axis["evidence_ids"] = ["business_verification"]
    with pytest.raises(JudgeContractError, match="implementation evidence"):
        validate_scenario_judgment(output, request)

    requirement_axis["evidence_ids"] = ["business_verification", "production-code-changeset"]
    requirement_axis["requirement_ids"] = ["REQ-1"]
    with pytest.raises(JudgeContractError, match="Requirement citation"):
        validate_scenario_judgment(output, request)


def test_outcome_judge_only_scores_test_code_for_explicit_test_deliverable() -> None:
    contract = _contract(
        _strategy("base", "a" * 40, "c" * 64),
        _strategy("candidate", "b" * 40, "d" * 64),
    )
    evidence = [
        {
            "evidence_id": "production-code-changeset",
            "kind": "CODE_CHANGESET",
            "path": "production.git.patch",
            "content": "diff --git a/src/A.java b/src/A.java",
        },
        {
            "evidence_id": "test-code-changeset",
            "kind": "TEST_CHANGESET",
            "path": "tests.git.patch",
            "content": "diff --git a/src/test/A.java b/src/test/A.java",
        },
    ]
    request = build_outcome_judge_request(
        contract,
        gold={
            "requirements": [
                {
                    "requirement_id": "REQ-TEST",
                    "statement": "必须交付完整且可执行的测试套件",
                }
            ],
            "source_evidence": [{"evidence_id": "E-1"}],
        },
        verification={"status": "PASS", "checks": []},
        evidence=evidence,
    )
    policy = request["score_contract"]["implementation_evidence_policy"]
    assert policy["test_code_evidence_ids"] == ["test-code-changeset"]
    assert policy["test_deliverable_requirement_ids"] == ["REQ-TEST"]
    assert "test-code-changeset" in request["evidence_scopes"][1]["allowed_evidence_ids"]

    output = _PerfectJudge().judge(request, model="judge-test", reasoning_effort="high")
    implementation_axis = next(
        item for item in output["axis_scores"] if item["axis"] == "implementation_quality"
    )
    implementation_axis["evidence_ids"] = ["test-code-changeset"]
    implementation_axis["requirement_ids"] = []
    with pytest.raises(JudgeContractError, match="test-deliverable Requirement"):
        validate_scenario_judgment(output, request)

    implementation_axis["requirement_ids"] = ["REQ-TEST"]
    assert validate_scenario_judgment(output, request)["implementation_quality"] == 100.0


def test_codex_transport_error_is_safely_classified_for_retry() -> None:
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id="turn-1",
        status="FAILED",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response=None,
        usage={},
        error={
            "type": "APIConnectionError",
            "message": (
                "stream disconnected from https://api.example.test/v1?api_key=secret "
                "sk-example-secret access_token=another-secret"
            ),
        },
        events=(),
        turns=(),
    )

    detail = _execution_error_detail(result)

    assert "APIConnectionError" in detail
    assert "stream disconnected" in detail
    assert "api.example.test" not in detail
    assert "example-secret" not in detail
    assert "another-secret" not in detail
    assert _is_retryable_execution_failure(result.status, detail) is True


def test_codex_sdk_reconnect_status_is_safely_classified_for_retry() -> None:
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id="turn-1",
        status="FAILED",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response=None,
        usage={},
        error={"message": "Reconnecting... 1/5"},
        events=(),
        turns=(),
    )

    assert _is_retryable_execution_failure(result.status, _execution_error_detail(result)) is True


def test_checkpoint_history_reconnect_does_not_override_a_new_semantic_terminal() -> None:
    reconnect_error = {"message": "Reconnecting... 1/5"}
    prior_turn = CodexTurnExecutionResult(
        turn_id="turn-prior",
        classification="FAILED",
        input_kind="WORKFLOW_STEP",
        input_text="/helix:archive demo",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response=None,
        usage={"total_tokens": 10},
        error=reconnect_error,
        events=(),
    )
    terminal_turn = CodexTurnExecutionResult(
        turn_id="turn-current",
        classification="FAILED",
        input_kind="WORKFLOW_STEP_RERUN",
        input_text="/helix:archive demo",
        started_at_ms=3,
        completed_at_ms=4,
        duration_ms=1,
        final_response=(
            "归档重试次数已耗尽。\n"
            "HELIXHARNESS_TURN_STATUS: COMPLETED"
        ),
        usage={"total_tokens": 20},
        error=None,
        events=(
            CodexExecutionEvent(
                event_type="workflow/goal-control",
                category="workflow",
                status="STOP",
                summary="目标驱动工作流：ARCHIVE_ATTEMPT_LIMIT_EXHAUSTED",
                payload={"reason": "ARCHIVE_ATTEMPT_LIMIT_EXHAUSTED"},
                occurred_at_ms=4,
            ),
        ),
    )
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id=terminal_turn.turn_id,
        status="FAILED",
        started_at_ms=1,
        completed_at_ms=4,
        duration_ms=3,
        final_response=terminal_turn.final_response,
        usage=terminal_turn.usage,
        error=None,
        events=(*prior_turn.events, *terminal_turn.events),
        turns=(prior_turn, terminal_turn),
    )

    detail = _execution_error_detail(result)

    assert detail == ""
    assert _is_retryable_execution_failure(result.status, detail) is False


def test_terminal_turn_reconnect_remains_retryable_without_a_business_terminal() -> None:
    terminal_error = {"message": "Reconnecting... 1/5"}
    terminal_turn = CodexTurnExecutionResult(
        turn_id="turn-current",
        classification="FAILED",
        input_kind="WORKFLOW_STEP",
        input_text="/helix:apply demo",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response=None,
        usage={"total_tokens": 10},
        error=terminal_error,
        events=(),
    )
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id=terminal_turn.turn_id,
        status="FAILED",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response=None,
        usage=terminal_turn.usage,
        error=None,
        events=(),
        turns=(terminal_turn,),
    )

    detail = _execution_error_detail(result)

    assert "Reconnecting... 1/5" in detail
    assert _is_retryable_execution_failure(result.status, detail) is True


def test_retry_noise_after_bounded_deterministic_archive_failure_becomes_semantic_terminal() -> None:
    goal_event = CodexExecutionEvent(
        event_type="workflow/goal-control",
        category="workflow",
        status="TRANSITIONED",
        summary="目标驱动工作流：ARCHIVE_REQUIRED",
        payload={
            "decision": "EXECUTE",
            "reason": "ARCHIVE_REQUIRED",
            "actions": ["ARCHIVE"],
            "progress": {
                "archive_attempts": 5,
                "inferred_actions": 5,
            },
        },
        occurred_at_ms=2,
    )
    validation_event = CodexExecutionEvent(
        event_type="stage/validation",
        category="validation",
        status="FAILED",
        summary="Workflow checkpoint archive move is incomplete",
        payload={"error": "Workflow checkpoint archive move is incomplete"},
        occurred_at_ms=3,
    )
    failed_archive_turn = CodexTurnExecutionResult(
        turn_id="turn-archive",
        classification="FAILED",
        input_kind="WORKFLOW_STEP_RERUN",
        input_text="/helix:archive demo",
        started_at_ms=1,
        completed_at_ms=3,
        duration_ms=2,
        final_response="归档已完成\nHELIXHARNESS_TURN_STATUS: COMPLETED",
        usage={"total_tokens": 10},
        error=None,
        events=(goal_event, validation_event),
    )
    reconnect_turn = CodexTurnExecutionResult(
        turn_id="turn-reconnect",
        classification="FAILED",
        input_kind="WORKFLOW_STEP_RERUN",
        input_text="/helix:archive demo",
        started_at_ms=4,
        completed_at_ms=5,
        duration_ms=1,
        final_response=None,
        usage={"total_tokens": 5},
        error={"message": "Reconnecting... 1/5"},
        events=(),
    )
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id=reconnect_turn.turn_id,
        status="FAILED",
        started_at_ms=1,
        completed_at_ms=5,
        duration_ms=4,
        final_response=None,
        usage=reconnect_turn.usage,
        error=reconnect_turn.error,
        events=(goal_event, validation_event),
        turns=(failed_archive_turn, reconnect_turn),
    )
    closure = SimpleNamespace(
        goal_policy=SimpleNamespace(
            max_archive_attempts=5,
            max_inferred_actions=12,
        )
    )

    assert _bounded_workflow_failure_reason(result, closure) == "ARCHIVE_ATTEMPT_LIMIT_EXHAUSTED"


def test_retry_noise_without_deterministic_failure_does_not_consume_closure_budget() -> None:
    reconnect_turn = CodexTurnExecutionResult(
        turn_id="turn-reconnect",
        classification="FAILED",
        input_kind="WORKFLOW_STEP_RERUN",
        input_text="/helix:archive demo",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response=None,
        usage={"total_tokens": 5},
        error={"message": "Reconnecting... 1/5"},
        events=(),
    )
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id=reconnect_turn.turn_id,
        status="FAILED",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response=None,
        usage=reconnect_turn.usage,
        error=reconnect_turn.error,
        events=(),
        turns=(reconnect_turn,),
    )
    closure = SimpleNamespace(
        goal_policy=SimpleNamespace(
            max_archive_attempts=5,
            max_inferred_actions=12,
        )
    )

    assert _bounded_workflow_failure_reason(result, closure) is None


@pytest.mark.parametrize(
    "message",
    (
        "[Errno 28] No space left on device",
        "[Errno 122] Disk quota exceeded",
    ),
)
def test_codex_checkpoint_capacity_error_requires_operator_recovery(message: str) -> None:
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id="turn-1",
        status="FAILED",
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response=None,
        usage={},
        error={"type": "OSError", "message": message},
        events=(),
        turns=(),
    )

    detail = _execution_error_detail(result)
    assert _is_operator_recoverable_execution_failure(result.status, detail) is True
    assert _is_retryable_execution_failure(result.status, detail) is False


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    (
        ("BUDGET_EXHAUSTED", {"type": "APITimeoutError", "message": "timeout"}, False),
        ("FAILED", {"type": "ValidationError", "message": "required evidence missing"}, False),
        ("FAILED", {"message": "Requirement says reconnecting clients are supported"}, False),
        ("COMPLETED", {"message": "Reconnecting... 1/5"}, False),
        ("FAILED", {"type": "RateLimitError", "code": 429, "message": "rate limited"}, True),
    ),
)
def test_codex_retry_classification_excludes_semantic_and_budget_failures(
    status: str,
    error: dict[str, object],
    expected: bool,
) -> None:
    result = CodexExecutionResult(
        thread_id="thread-1",
        turn_id="turn-1",
        status=status,
        started_at_ms=1,
        completed_at_ms=2,
        duration_ms=1,
        final_response=None,
        usage={},
        error=error,
        events=(),
        turns=(),
    )
    assert _is_retryable_execution_failure(status, _execution_error_detail(result)) is expected


def test_strategy_validator_accepts_configured_local_baseline_without_origin(tmp_path: Path) -> None:
    repository, base, base_hash, candidate, candidate_hash = _helixharness_repository(tmp_path)
    _git(repository, "remote", "remove", "origin")
    _git(repository, "branch", "main", base)
    contract = _contract(_strategy("base", base, base_hash), _strategy("candidate", candidate, candidate_hash))
    GitScenarioStrategyValidator(repository, reference_ref="main").validate(contract)
    _git(repository, "branch", "-f", "main", candidate)
    with pytest.raises(ValueError, match="latest configured baseline"):
        GitScenarioStrategyValidator(repository, reference_ref="main").validate(contract)


@pytest.mark.parametrize("stage,next_stage", [("PROPOSE", "apply"), ("APPLY", "archive")])
@pytest.mark.parametrize("ready", [False, True])
def test_native_router_must_admit_next_stage_before_completion(tmp_path: Path, stage: str, next_stage: str, ready: bool) -> None:
    from bench.scenario_codex import verify_native_workflow_transition

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = tmp_path / "frozen-side"
    (snapshot / "scripts").mkdir(parents=True)
    # Exit 0 alone is not completion: the native Router may recommend an earlier stage.
    payload = {"action": "load_workflow" if ready else "recommend_command", "stage": next_stage,
               "recommendation": None if ready else "/helix:apply feature"}
    (snapshot / "scripts/workflow_router.py").write_text(
        "import json,sys\n"
        "assert sys.argv[-2:] == ['--feature','feature']\n"
        f"print({json.dumps(json.dumps(payload))})\n",
        encoding="utf-8",
    )
    step = FrozenWorkflowStep("STEP", f"/helix:{stage.lower()} feature", "DONE", f"HELIXHARNESS_{stage}", stage)
    arguments = {"workspace": workspace, "snapshot_path": snapshot, "runtime_root": tmp_path / "runtime"}
    if ready:
        verify_native_workflow_transition(step, **arguments)
    else:
        with pytest.raises(CodexExecutionError, match="current stage is not complete"):
            verify_native_workflow_transition(step, **arguments)


@pytest.mark.parametrize("output", ['print("not json")', 'print("[]")', 'raise SystemExit(2)', 'print(\'{"action":"load_workflow","stage":"apply"}\')'])
def test_native_router_invalid_or_wrong_stage_result_is_not_completion(tmp_path: Path, output: str) -> None:
    from bench.scenario_codex import verify_native_workflow_transition

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = tmp_path / "snapshot"
    (snapshot / "scripts").mkdir(parents=True)
    (snapshot / "scripts/workflow_router.py").write_text(output + "\n", encoding="utf-8")
    step = FrozenWorkflowStep("A", "/helix:apply feature", "DONE", "HELIXHARNESS_APPLY", "APPLY")
    with pytest.raises(CodexExecutionError, match="does not admit archive"):
        verify_native_workflow_transition(step, workspace=workspace, snapshot_path=snapshot, runtime_root=tmp_path / "runtime")


def test_unfinished_native_apply_does_not_save_checkpoint_or_progress(tmp_path: Path) -> None:
    source, business = _repository_scenario(tmp_path)
    repository, _, document_hash, _, _ = _helixharness_repository(tmp_path)
    (repository / "scripts").mkdir()
    (repository / "scripts/workflow_router.py").write_text(
        'print(\'{"action":"recommend_command","recommendation":"/helix:apply evaluation-scenario"}\')\n',
        encoding="utf-8",
    )
    _git(repository, "add", "scripts")
    _git(repository, "commit", "-m", "freeze incomplete native Apply router")
    _git(repository, "push", "origin", "master")
    commit = _git(repository, "rev-parse", "HEAD")
    command = "/helix:apply evaluation-scenario"
    strategy = replace(
        _strategy("base", commit, document_hash), commands=(command,),
        stages=(StrategyStage("A", "APPLY", command, "APPLIED", True),), completion_states=("APPLIED",),
    )
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    case = store.publish(load_scenario_source(source))
    assert ScenarioCalibrationService(store, repositories={"business-repo": business}).calibrate(case.case_id).status == "CALIBRATED"
    service = ScenarioRunService(store, strategy_validator=GitScenarioStrategyValidator(repository))
    request = ScenarioRunCreateRequest(
        scenario_ids=(case.case_id,), run_kind="FORMAL", execution_adapter="CODEX",
        evaluation_contract=_contract(strategy, replace(strategy, strategy_id="candidate-strategy")),
        created_by="test", runtime_policy={
            "schema_version": "framework-eval-runtime-policy.v2", "execution_model": "test", "judge_model": "test",
            "reasoning_effort": "low", "judge_reasoning_effort": "low", "wall_timeout_seconds": 600,
            "token_budget": 100_000, "tool_call_budget": 1_000, "disk_budget_mb": 1_024,
        },
    )
    run = service.create(request, expected_preview_hash=service.preview(request).preview_hash)

    class ReportOnlySdk(_RecordingSdk):
        def execute(self, request: CodexExecutionRequest) -> CodexExecutionResult:
            result = super().execute(request)
            active = request.workspace_path / "docs/specs/active/evaluation-scenario"
            active.mkdir(parents=True)
            for name in ("spec.md", "tasks.md", "verify-report.md"):
                (active / name).write_text("A report alone is not native completion.\n", encoding="utf-8")
            assert request.on_workflow_step_completed is not None
            with pytest.raises(CodexExecutionError, match="does not admit archive"):
                request.on_workflow_step_completed(result.turns)
            return replace(result, status="FAILED", turns=(replace(result.turns[0], classification="FAILED"),))

    recorded_progress = []
    adapter = CodexScenarioExecutionAdapter(
        helixharness_repository=repository, repositories={"business-repo": business}, work_root=tmp_path / "work",
        sdk_adapter=ReportOnlySdk(), snapshot_installer=_NoopInstaller(), telemetry_configurator=_NoopTelemetry(),
        judge_adapter=_PerfectJudge(), workflow_progress_recorder=lambda *args: recorded_progress.append(args),
    )
    assert ScenarioWorker(store, adapter).run_once(run_id=run.run_id).status == "PROCESSED"
    assert recorded_progress == []
    assert not list((tmp_path / "work/_workflow-checkpoints").glob("*/??-*/checkpoint.json"))
