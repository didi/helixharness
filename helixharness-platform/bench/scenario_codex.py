"""Real Codex execution adapter for isolated workflow-neutral Scenario v2 jobs."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from bench.codex_execution import (
    CodexExecutionError,
    CodexExecutionRequest,
    CodexPythonSdkAdapter,
    CodexSdkAdapter,
    CodexTurnExecutionResult,
    FrozenWorkflowClosurePolicy,
    FrozenWorkflowClosureResume,
    FrozenWorkflowStep,
    PinnedCodexPluginInstaller,
    RestoredWorkflowCheckpoint,
    SnapshotInstaller,
    SnapshotTelemetryConfigurator,
    TelemetryConfigurator,
    WorkflowCheckpointStore,
    checkpoint_budget_exhausted_result,
    checkpoint_remaining_resources,
    completed_checkpoint_execution,
    cumulative_checkpoint_turns,
    merge_checkpoint_execution,
    workspace_tree_hash,
    write_isolated_codex_config,
)
from bench.helixharness_source import frozen_plugin_tree, git_repository_root
from bench.report_models import ClaimedJob
from bench.resource_accounting import attributable_resource_observation
from bench.runtime_secrets import evaluation_provider_config
from bench.scenario_diagnostics import ScenarioDiagnosticRetentionService
from bench.scenario_execution_support import (
    ScenarioExecutionSupportError,
    apply_hidden_test_patch,
    apply_workspace_fixture_patch,
    apply_workspace_preconditions,
    assert_safe_workspace_tree,
    export_git_tree,
    frozen_command_environment,
    json_object,
    run_frozen_command,
    safe_relative_path,
    safe_workspace_path,
    snapshot_file,
)
from bench.scenario_judging import (
    build_outcome_judge_request,
    build_workflow_judge_request,
    validate_scenario_judgment,
)
from bench.scenario_runtime import (
    ClaimedScenarioJob,
    ScenarioExecutionInterruption,
    ScenarioPauseExecutionError,
    ScenarioRetryableExecutionError,
)
from bench.scenario_workspace import scenario_workspace
from bench.semantic_judging import (
    JudgeContractError,
    OpenAISemanticJudgeAdapter,
    SemanticJudgeAdapter,
    anonymize_value,
    evidence_ids_by_axis,
    traceability_ids,
)
from bench.turn_control import (
    DEFAULT_MAX_BUSINESS_CLARIFICATIONS,
    EVIDENCE_BOUND_USER_RESPONSE,
    TURN_STATUS_PREFIX,
)
from bench.workflow_contracts import WorkflowContractError, parse_helixharness_command
from bench.workflow_goal_control import (
    WorkflowGoalContractError,
    parse_strategy_workflow_recovery,
)


class ScenarioCodexExecutionError(ScenarioExecutionInterruption):
    """Raised when a real v2 Episode cannot produce trusted immutable evidence."""


_ERROR_DETAIL_KEYS = ("type", "code", "message", "detail")
_RETRYABLE_ERROR_TYPES = {
    "apiconnectionerror",
    "apitimeouterror",
    "ratelimiterror",
    "connectionerror",
    "timeouterror",
}
_RETRYABLE_HTTP_CODES = {"429", "502", "503", "504"}
_RETRYABLE_MESSAGE_PATTERNS = (
    r"\bstream disconnected\b",
    r"\bconnection (?:reset|refused|closed|aborted)\b",
    r"\btemporarily unavailable\b",
    r"\brate limit(?:ed)?\b",
    r"\btimed out\b",
    r"\btimeout\b",
    r"\b(?:http\s*)?(?:429|502|503|504)\b",
)
_SDK_RECONNECT_MESSAGE_PATTERN = re.compile(
    r'"message":"reconnecting\.\.\.\s+[1-9][0-9]*/[1-9][0-9]*"',
    re.IGNORECASE,
)
_SCORABLE_SEMANTIC_TERMINALS = {"FAILED", "STUCK_CLARIFICATION"}
_MAX_RETAINED_JUDGE_OUTPUT_BYTES = 64_000
_CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".h", ".hpp", ".java", ".js", ".jsx",
    ".kt", ".kts", ".php", ".py", ".rb", ".rs", ".scala", ".sql", ".swift",
    ".ts", ".tsx", ".vue", ".xml", ".yaml", ".yml",
}
_CODE_FILENAMES = {
    "build.gradle", "build.gradle.kts", "gradle.properties", "package.json", "pom.xml",
    "pyproject.toml", "requirements.txt", "settings.gradle", "settings.gradle.kts",
}
_MAX_CODE_FILE_BYTES = 200_000
_MAX_CODE_TOTAL_BYTES = 1_000_000
_MAX_CODE_CONTEXT_FILES = 30
_MAX_CODE_CONTEXT_TOTAL_BYTES = 750_000
_TOP_LEVEL_DECLARED_SYMBOL_PATTERN = re.compile(
    r"(?m)^(?:export\s+)?(?:(?:public|protected|private|abstract|final|sealed|static|data|pub)\s+)*"
    r"(?:class|interface|enum|record|struct|trait|type|def|function)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_IDENTIFIER_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9_]{3,}\b")
_CONTEXT_SYMBOL_STOPWORDS = frozenset(
    {
        "Builder",
        "Exception",
        "Override",
        "Request",
        "Response",
        "Result",
        "String",
    }
)

_INIT_REQUIRED_PATHS = {
    "SERVICE": (
        "AGENTS.md",
        ".codex/hooks.json",
        "docs/harness/index.md",
        "docs/knowledge/index.md",
        "docs/knowledge/project-context.md",
        "docs/knowledge/service-meta.yaml",
        "docs/specs",
        "docs/hooks",
    ),
    "FRONTEND": (
        "AGENTS.md",
        ".codex/hooks.json",
        "docs/harness/index.md",
        "docs/knowledge/index.md",
        "docs/knowledge/project-context.md",
        "docs/specs",
        "docs/hooks",
    ),
    "WORKSPACE": (
        "AGENTS.md",
        "helixharness.workspace.yaml",
        "docs/knowledge/index.md",
        "docs/knowledge/service-catalog.yaml",
        "docs/knowledge/service-graph.md",
        "docs/knowledge/integration-boundaries.md",
        "docs/specs/active",
        "docs/specs/completed",
    ),
}


def _completed_stage_ids(turns: tuple[Any, ...]) -> list[str]:
    """Return semantic stage completions once, preserving first-completion order."""

    return list(
        dict.fromkeys(
            str(turn.workflow_step_id)
            for turn in turns
            if turn.workflow_step_id and turn.classification == "COMPLETED"
        )
    )


def verify_initialization_outputs(
    strategy: dict[str, object],
    manifest: dict[str, Any],
    *,
    workspace: Path,
    snapshot_path: Path,
    runtime_root: Path,
) -> dict[str, object]:
    """Verify a frozen INIT stage with platform checks plus the side-native validator."""

    stages = strategy.get("stages")
    if not isinstance(stages, list) or not any(
        isinstance(stage, dict) and stage.get("semantic_role") == "INIT" for stage in stages
    ):
        return {"status": "NOT_APPLICABLE"}
    preconditions = manifest.get("workspace_preconditions")
    mode = (
        str(preconditions.get("init_mode"))
        if isinstance(preconditions, dict) and preconditions.get("init_mode") in _INIT_REQUIRED_PATHS
        else "SERVICE"
    )
    missing: list[str] = []
    present: list[str] = []
    for value in _INIT_REQUIRED_PATHS[mode]:
        relative = safe_relative_path(value, label="Initialization evidence path")
        target = safe_workspace_path(workspace, relative, label="Initialization evidence path")
        if target.exists() and not target.is_symlink():
            present.append(value)
        else:
            missing.append(value)

    validator = snapshot_path / "scripts" / "validate_init_outputs.py"
    validator_status = "MISSING"
    timed_out = False
    if validator.is_file() and not validator.is_symlink():
        command = (
            sys.executable,
            str(validator),
            "--repo-root",
            str(workspace),
            "--mode",
            mode.lower(),
            "--platform",
            "codex",
            "--telemetry-mode",
            "disabled",
        )
        completed = run_frozen_command(
            command,
            cwd=workspace,
            environment=frozen_command_environment(runtime_root, workspace=workspace),
            timeout_seconds=120,
        )
        timed_out = completed.timed_out
        validator_status = "PASS" if completed.returncode == 0 else "FAIL"
    status = "PASS" if not missing and validator_status == "PASS" else "FAIL"
    return {
        "status": status,
        "mode": mode,
        "required_paths_present": present,
        "missing_paths": missing,
        "native_validator": {
            "path": "scripts/validate_init_outputs.py",
            "status": validator_status,
            "timed_out": timed_out,
        },
    }



def verify_native_workflow_transition(
    step: FrozenWorkflowStep,
    *,
    workspace: Path,
    snapshot_path: Path,
    runtime_root: Path,
) -> None:
    """Require the frozen Router to admit the next stage before saving progress.

    Older snapshots without this Router retain their declared artifact contract.
    No private Apply/Review state fields are interpreted by the platform.
    """

    next_stage = {"HELIXHARNESS_PROPOSE": "apply", "HELIXHARNESS_APPLY": "archive"}.get(step.artifact_contract)
    router = snapshot_path / "scripts" / "workflow_router.py"
    if next_stage is None or not router.exists():
        return
    if not router.is_file() or router.is_symlink():
        raise CodexExecutionError("Frozen native workflow Router is not a regular file")
    tokens = shlex.split(step.command)
    if len(tokens) < 2 or re.fullmatch(r"[A-Za-z0-9_.-]+", tokens[1]) is None:
        raise CodexExecutionError("Frozen native workflow feature is invalid")
    # Execute the Side's public CLI in a separate, credential-free process. Only
    # its exit status crosses back; arbitrary plugin stdout is not persisted.
    verifier = """import contextlib,io,json,os,runpy,sys
script,expected,*args=sys.argv[1:]
sys.path.insert(0,os.path.dirname(script));sys.argv=[script,*args]
output=io.StringIO();code=0
with contextlib.redirect_stdout(output):
    try: runpy.run_path(script,run_name='__main__')
    except SystemExit as exc: code=exc.code
if code not in (None,0): raise SystemExit(1)
try: result=json.loads(output.getvalue())
except (ValueError,TypeError): raise SystemExit(1)
raise SystemExit(0 if isinstance(result,dict) and result.get('action')=='load_workflow' and result.get('stage')==expected else 1)
"""
    environment = frozen_command_environment(runtime_root, workspace=workspace)
    result = run_frozen_command(
        (sys.executable, "-I", "-B", "-c", verifier, str(router), next_stage,
         "--repo-root", str(workspace), "route", "--command", next_stage, "--feature", tokens[1]),
        cwd=workspace,
        environment=environment,
        timeout_seconds=120,
    )
    if result.returncode != 0 or result.timed_out:
        raise CodexExecutionError(
            f"Frozen native Router does not admit {next_stage} for {tokens[1]}; "
            "the current stage is not complete. Run the current native workflow to finish its prerequisites; "
            "do not manufacture state or only write a completion report."
        )


def _business_case_hard_gates(verification: dict[str, object]) -> list[str]:
    """Map failed business checks to independent F2P and P2P gates."""

    if verification.get("status") != "FAIL":
        return []
    checks = verification.get("checks")
    if not isinstance(checks, list):
        return []
    failed_kinds = {
        str(item.get("kind"))
        for item in checks
        if isinstance(item, dict) and item.get("status") == "FAIL"
    }
    gates: list[str] = []
    if "FAIL_TO_PASS" in failed_kinds:
        gates.append("CORE_BUSINESS_BEHAVIOR_FAILURE")
    if "PASS_TO_PASS" in failed_kinds:
        gates.append("PROTECTED_REGRESSION_FAILURE")
    return gates


def _business_failure_outcome_score(verification: dict[str, object]) -> float | None:
    """Return the deterministic Outcome score for a trusted business failure."""

    return 0.0 if verification.get("status") == "FAIL" else None


def _checkpoint_job(job: ClaimedScenarioJob) -> ClaimedJob:
    """Adapt one Scenario lease to the shared durable checkpoint identity."""

    return ClaimedJob(
        job_id=job.job_id,
        run_id=job.run_id,
        case_id=job.case_id,
        side=job.side,
        sequence_number=0,
        run_config={},
        episode_id=job.job_id,
        attempt_id=f"{job.job_id}-attempt-{job.attempt_number}",
        attempt_number=job.attempt_number,
        lease_token=job.lease_token,
    )


def _execution_error_detail(result: Any) -> str:
    """Return the current terminal SDK error without reviving checkpoint history."""

    candidates: list[dict[str, Any]] = []
    if isinstance(result.error, dict):
        candidates.append(result.error)
    terminal_turn = result.turns[-1] if result.turns else None
    if terminal_turn is not None:
        if isinstance(terminal_turn.error, dict):
            candidates.append(terminal_turn.error)
        candidates.extend(
            event.payload
            for event in reversed(terminal_turn.events)
            if event.category == "error" and isinstance(event.payload, dict)
        )
    elif not candidates:
        candidates.extend(
            event.payload
            for event in reversed(result.events)
            if event.category == "error" and isinstance(event.payload, dict)
        )
    for candidate in candidates:
        selected = {
            key: _sanitize_error_value(candidate[key])
            for key in _ERROR_DETAIL_KEYS
            if key in candidate and isinstance(candidate[key], (str, int, float))
        }
        selected = {key: value for key, value in selected.items() if value}
        if selected:
            return json.dumps(selected, ensure_ascii=False, separators=(",", ":"))[:500]
    return ""


def _sanitize_error_value(value: str | int | float) -> str:
    text = str(value)
    text = re.sub(r"https?://[^\s\"'<>]+", "[REDACTED_URL]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
    text = re.sub(
        r"\b(api[-_ ]?key|authorization|password|secret|access[-_ ]?token)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(text.split())[:400]


_DIAGNOSTIC_EVENT_PAYLOAD_KEYS = frozenset(
    {
        "actions",
        "classification",
        "cycle",
        "decision",
        "error",
        "error_type",
        "milestone_id",
        "progress",
        "reason",
        "transition",
        "workflow_step_id",
    }
)


def _sanitize_diagnostic_text(value: str, *, limit: int) -> str:
    """Bound and redact persisted failure text without retaining provider payloads."""

    text = re.sub(r"https?://[^\s\"'<>]+", "[REDACTED_URL]", value, flags=re.IGNORECASE)
    text = re.sub(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED_KEY]", text)
    text = re.sub(
        r"\b(api[-_ ]?key|authorization|password|secret|access[-_ ]?token)\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
        flags=re.IGNORECASE,
    )
    return text[:limit]


def _safe_diagnostic_payload(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_diagnostic_text(value, limit=1_000)
    if isinstance(value, list):
        return [_safe_diagnostic_payload(item) for item in value[:20]]
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, item in list(value.items())[:30]:
            safe_key = str(key)[:128]
            sanitized[safe_key] = (
                "[REDACTED]"
                if re.search(
                    r"api[-_ ]?key|authorization|password|secret|access[-_ ]?token|bearer",
                    safe_key,
                    flags=re.IGNORECASE,
                )
                else _safe_diagnostic_payload(item)
            )
        return sanitized
    return _sanitize_diagnostic_text(str(value), limit=1_000)


def _diagnostic_turn_evidence(result: Any) -> list[dict[str, object]]:
    """Project bounded immutable Turn diagnostics before runtime cleanup."""

    return _diagnostic_turns(tuple(result.turns))


def _diagnostic_turns(
    turns: tuple[CodexTurnExecutionResult, ...],
) -> list[dict[str, object]]:
    """Project a bounded immutable diagnostic list from exact physical Turns."""

    evidence: list[dict[str, object]] = []
    for position, turn in enumerate(turns, start=1):
        diagnostic_events = []
        for event in turn.events:
            if event.category not in {"classification", "error", "validation", "workflow"}:
                continue
            diagnostic_events.append(
                {
                    "event_type": event.event_type[:128],
                    "category": event.category[:64],
                    "status": event.status[:64],
                    "summary": _sanitize_diagnostic_text(event.summary, limit=1_000),
                    "payload": {
                        key: _safe_diagnostic_payload(value)
                        for key, value in event.payload.items()
                        if key in _DIAGNOSTIC_EVENT_PAYLOAD_KEYS
                    },
                    "occurred_at_ms": event.occurred_at_ms,
                }
            )
        error = None
        if isinstance(turn.error, dict):
            error = {
                key: _sanitize_diagnostic_text(str(value), limit=1_000)
                for key, value in turn.error.items()
                if key in _ERROR_DETAIL_KEYS and isinstance(value, (str, int, float))
            }
        evidence.append(
            {
                "turn_sequence": position,
                "turn_id": turn.turn_id,
                "input_kind": turn.input_kind,
                "input_text": (
                    None
                    if turn.input_kind == "INITIAL_REQUEST"
                    else _sanitize_diagnostic_text(turn.input_text, limit=4_000)
                ),
                "workflow_step_id": turn.workflow_step_id,
                "milestone_id": turn.milestone_id,
                "classification": turn.classification,
                "started_at_ms": turn.started_at_ms,
                "completed_at_ms": turn.completed_at_ms,
                "duration_ms": turn.duration_ms,
                "usage": {
                    str(key): int(value)
                    for key, value in turn.usage.items()
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                },
                "final_response": (
                    _sanitize_diagnostic_text(turn.final_response, limit=16_000)
                    if isinstance(turn.final_response, str)
                    else None
                ),
                "error": error,
                "events": diagnostic_events,
            }
        )
    return evidence


def _attempt_resource_record(
    result: Any,
    *,
    turn_evidence: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Freeze bounded resource evidence for exactly one physical Attempt."""

    turns = tuple(result.turns)
    usage = result.usage if isinstance(result.usage, dict) else {}
    if not usage and turns and isinstance(turns[-1].usage, dict):
        usage = turns[-1].usage
    sanitized_usage = {
        str(key): int(value)
        for key, value in usage.items()
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }
    started_at_ms = result.started_at_ms
    completed_at_ms = result.completed_at_ms
    duration_ms = result.duration_ms
    if duration_ms is None and started_at_ms is not None and completed_at_ms is not None:
        duration_ms = max(0, completed_at_ms - started_at_ms)
    error = None
    if isinstance(result.error, dict):
        error = {
            key: _sanitize_diagnostic_text(str(value), limit=1_000)
            for key, value in result.error.items()
            if key in _ERROR_DETAIL_KEYS and isinstance(value, (str, int, float))
        }
    return {
        "schema_version": "framework-eval-attempt-resource.v1",
        "terminal_status": str(result.status),
        "started_at_ms": started_at_ms,
        "completed_at_ms": completed_at_ms,
        "duration_ms": duration_ms,
        "usage": sanitized_usage,
        "evidence_complete": (
            isinstance(started_at_ms, int)
            and isinstance(completed_at_ms, int)
            and isinstance(duration_ms, int)
            and "total_tokens" in sanitized_usage
        ),
        "error": error,
        "turn_evidence": turn_evidence if turn_evidence is not None else _diagnostic_turn_evidence(result),
    }


def _empty_attempt_resource_record(status: str) -> dict[str, object]:
    """Record a trusted zero-cost resume that did not start another model Turn."""

    now_ms = int(time.time() * 1000)
    return {
        "schema_version": "framework-eval-attempt-resource.v1",
        "terminal_status": status,
        "started_at_ms": now_ms,
        "completed_at_ms": now_ms,
        "duration_ms": 0,
        "usage": {"total_tokens": 0},
        "evidence_complete": True,
        "error": None,
        "turn_evidence": [],
    }


def _interrupted_attempt_resource_record(
    turns: tuple[CodexTurnExecutionResult, ...],
    *,
    status: str,
) -> dict[str, object]:
    """Retain known checkpoint work while marking an uncaptured failure tail partial."""

    started_at_ms = turns[0].started_at_ms if turns else None
    completed_at_ms = turns[-1].completed_at_ms if turns else None
    duration_ms = (
        max(0, completed_at_ms - started_at_ms)
        if isinstance(started_at_ms, int) and isinstance(completed_at_ms, int)
        else None
    )
    usage = dict(turns[-1].usage) if turns else {}
    return {
        "schema_version": "framework-eval-attempt-resource.v1",
        "terminal_status": status,
        "started_at_ms": started_at_ms,
        "completed_at_ms": completed_at_ms,
        "duration_ms": duration_ms,
        "usage": usage,
        "evidence_complete": False,
        "error": None,
        "turn_evidence": _diagnostic_turns(turns),
    }


def _execution_terminal_reason(result: Any) -> str:
    """Return one stable reason code for UI and later diagnosis."""

    if result.status == "COMPLETED":
        return "COMPLETED"
    for turn in reversed(result.turns):
        for event in reversed(turn.events):
            reason = event.payload.get("reason") if isinstance(event.payload, dict) else None
            decision = event.payload.get("decision") if isinstance(event.payload, dict) else None
            if (
                isinstance(reason, str)
                and reason
                and (decision == "STOP" or event.status in {"FAILED", "STOP"})
            ):
                return reason[:128]
    if isinstance(result.error, dict):
        error_type = result.error.get("type")
        if isinstance(error_type, str) and error_type:
            return error_type.upper()[:128]
        return "SDK_ERROR"
    if result.status == "BUDGET_EXHAUSTED":
        return "BUDGET_EXHAUSTED"
    final_response = result.final_response
    if not isinstance(final_response, str):
        return "TURN_RESPONSE_MISSING"
    final_line = final_response.rstrip().rsplit("\n", maxsplit=1)[-1]
    if not final_line.startswith(TURN_STATUS_PREFIX):
        return "TURN_STATUS_MISSING"
    if any(line.strip().startswith("BLOCKED:") for line in final_response.splitlines()):
        return "WORKFLOW_BLOCKED"
    if final_line == f"{TURN_STATUS_PREFIX}FAILED":
        return "TURN_EXPLICIT_FAILED"
    return str(result.status)[:128]


def _is_retryable_execution_failure(status: str, detail: str) -> bool:
    """Classify only explicit upstream transport/capacity failures as retryable."""

    if status != "FAILED" or not detail:
        return False
    lowered = detail.lower()
    if any(marker in lowered for marker in ("budget_exhausted", "budget exceeded", "wall timeout")):
        return False
    type_match = re.search(r'"type":"([^\"]+)"', lowered)
    if type_match and type_match.group(1).replace("_", "") in _RETRYABLE_ERROR_TYPES:
        return True
    code_match = re.search(r'"code":"?([0-9]+)"?', lowered)
    if code_match and code_match.group(1) in _RETRYABLE_HTTP_CODES:
        return True
    if _SDK_RECONNECT_MESSAGE_PATTERN.search(detail):
        return True
    return any(re.search(pattern, lowered) for pattern in _RETRYABLE_MESSAGE_PATTERNS)


def _bounded_workflow_failure_reason(
    result: Any,
    workflow_closure: FrozenWorkflowClosurePolicy | None,
) -> str | None:
    """Recover the semantic terminal hidden by retry noise after a bounded closure failure."""

    if workflow_closure is None:
        return None
    detail = _execution_error_detail(result)
    if not _is_retryable_execution_failure(str(result.status), detail):
        return None
    policy = workflow_closure.goal_policy
    events = tuple(getattr(result, "events", ()) or ())
    for decision_event in reversed(events):
        if decision_event.event_type != "workflow/goal-control":
            continue
        payload = decision_event.payload if isinstance(decision_event.payload, dict) else {}
        if payload.get("decision") != "EXECUTE":
            continue
        actions = payload.get("actions")
        progress = payload.get("progress")
        if not isinstance(actions, list) or not isinstance(progress, dict):
            continue
        terminal_reason: str | None = None
        if "ARCHIVE" in actions and int(progress.get("archive_attempts", 0)) >= policy.max_archive_attempts:
            terminal_reason = "ARCHIVE_ATTEMPT_LIMIT_EXHAUSTED"
        elif int(progress.get("inferred_actions", 0)) >= policy.max_inferred_actions:
            terminal_reason = "INFERRED_ACTION_LIMIT_EXHAUSTED"
        if terminal_reason is None:
            continue
        deterministic_failure = any(
            event.status == "FAILED"
            and event.event_type != "workflow/goal-control"
            and event.category in {"validation", "workflow"}
            and event.occurred_at_ms >= decision_event.occurred_at_ms
            for event in events
        )
        if deterministic_failure:
            return terminal_reason
    return None


def _bounded_judge_output(output: dict[str, Any]) -> dict[str, Any]:
    """Retain an anonymous failed Judge response without unbounded database growth."""

    encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) <= _MAX_RETAINED_JUDGE_OUTPUT_BYTES:
        return output
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "preview": encoded[:8_000],
    }


def _judge_validation_retry_request(
    request: dict[str, Any],
    *,
    error: JudgeContractError,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Return the same anonymous Judge request with exact legal citation feedback."""

    retry = dict(request)
    scoped_ids = evidence_ids_by_axis(request)
    requirement_ids, gold_refs = traceability_ids(request)
    retry["validation_retry"] = {
        "instruction": (
            "重新生成完整评分 JSON；保持基于原证据独立评分，只修正违反可信契约的字段，"
            "且只能使用下列合法动态 ID。"
        ),
        "previous_error": str(error)[:1_000],
        "legal_evidence_ids_by_axis": {
            axis: sorted(evidence_ids) for axis, evidence_ids in sorted(scoped_ids.items())
        },
        "legal_requirement_ids": sorted(requirement_ids),
        "legal_gold_evidence_refs": sorted(gold_refs),
        "previous_output": _bounded_judge_output(output),
    }
    return retry


def _attempt_record_with_judge_error(
    attempt_record: dict[str, object] | None,
    *,
    error_type: str,
    message: str,
    diagnostics: list[dict[str, Any]],
) -> dict[str, object]:
    record = dict(attempt_record or {})
    record["error"] = {
        "type": error_type,
        "message": message[:4_000],
        "validation_attempts": diagnostics,
    }
    return record


def _is_operator_recoverable_execution_failure(status: str, detail: str) -> bool:
    """Recognize capacity faults that require cleanup or quota expansion."""

    return status == "FAILED" and bool(re.search(r"\[errno\s+(?:28|122)\]", detail, re.IGNORECASE))


def _clarification_response(
    runtime: dict[str, object],
    policy: dict[str, Any],
) -> str:
    """Render one evidence-bound answer from the frozen Scenario policy."""

    universal = str(runtime.get("universal_user_response") or "").strip()
    lines = [universal or EVIDENCE_BOUND_USER_RESPONSE]
    if policy.get("external_confirmation_required") is False:
        lines.append("无需等待外部确认；本次评测只能使用已冻结事实继续。")
    if policy.get("repository_fact_policy") == "AGENT_INSPECTS":
        lines.append("仓库内能够查证的事实由 Agent 自行检查。")
    if policy.get("workflow_choice_policy") == "ACCEPT_RECOMMENDED_NON_BUSINESS":
        lines.append("不改变业务契约的流程性选项直接采用命令或工具给出的推荐项。")
    facts = policy.get("facts")
    if isinstance(facts, list):
        for item in facts:
            if not isinstance(item, dict):
                continue
            statement = str(item.get("statement") or "").strip()
            if statement:
                lines.append(f"冻结事实：{statement}")
    if policy.get("unknown_business_fact_policy") == "CASE_INVALID":
        lines.append("若确有冻结材料无法回答的业务事实，不得编造；应明确指出 Case 无效。")
    lines.append("请继续当前原生工作流，不要重复已完成阶段。")
    return "\n".join(lines)


def _strategy_workflow_closure(strategy: dict[str, object]) -> FrozenWorkflowClosurePolicy | None:
    """Translate one frozen Strategy recovery contract into the shared bounded driver."""

    try:
        recovery = parse_strategy_workflow_recovery(strategy.get("recovery_policy"))
    except WorkflowGoalContractError as exc:
        raise ScenarioCodexExecutionError(f"Strategy recovery policy is invalid: {exc}") from exc
    if recovery is None:
        return None
    goal_policy = recovery.goal_policy
    stage_prompt = (
        goal_policy.stage_findings_prompt
        if goal_policy.max_stage_rework_cycles > 0
        else ""
    )
    return FrozenWorkflowClosurePolicy(
        stage_findings_prompt=stage_prompt,
        max_stage_rework_cycles=goal_policy.max_stage_rework_cycles,
        goal_policy=goal_policy,
    )


def _frozen_workflow_semantics(
    strategy: dict[str, object],
) -> dict[str, str]:
    """Derive artifact contracts from immutable Strategy commands."""

    stages = _object_list(strategy.get("stages"), "Strategy stages")
    contracts = {}
    mapping = {"init": "GENERIC_STAGE_EVIDENCE", "propose": "HELIXHARNESS_PROPOSE",
               "apply": "HELIXHARNESS_APPLY", "bug-fix": "HELIXHARNESS_APPLY", "archive": "HELIXHARNESS_ARCHIVE", "ut": "GENERIC_STAGE_EVIDENCE"}
    for stage in stages:
        command = str(stage.get("command", "")).strip()
        if not command:
            raise ScenarioCodexExecutionError("Scenario Strategy stage is incomplete")
        if not command.startswith("/helix:"):
            contract = "GENERIC_STAGE_EVIDENCE"
        else:
            try:
                name, _, options = parse_helixharness_command(command)
            except WorkflowContractError as exc:
                raise ScenarioCodexExecutionError(f"Unsupported HelixHarness command in current Strategy: {exc}") from exc
            contract = "HELIXHARNESS_ARCHIVE" if name == "propose" and "--loop" in options else mapping[name]
        contracts[str(stage.get("stage_id", ""))] = contract
    return contracts


class CodexScenarioExecutionAdapter:
    """Materialize one frozen Strategy/Scenario pair and assess its evidence."""

    name = "CODEX"

    def __init__(
        self,
        *,
        helixharness_repository: Path,
        repositories: dict[str, Path],
        work_root: Path,
        sdk_adapter: CodexSdkAdapter | None = None,
        snapshot_installer: SnapshotInstaller | None = None,
        telemetry_configurator: TelemetryConfigurator | None = None,
        judge_adapter: SemanticJudgeAdapter | None = None,
        workflow_progress_recorder: Callable[[str, str, tuple[str, ...]], None] | None = None,
        diagnostic_retention: ScenarioDiagnosticRetentionService | None = None,
    ) -> None:
        self.helixharness_repository = helixharness_repository.resolve()
        self.repositories = {name: path.resolve() for name, path in repositories.items()}
        self.work_root = work_root.resolve()
        provider = evaluation_provider_config("codex")
        self.sdk_adapter = sdk_adapter or CodexPythonSdkAdapter(provider_config=provider)
        self.snapshot_installer = snapshot_installer or PinnedCodexPluginInstaller()
        self.telemetry_configurator = telemetry_configurator or SnapshotTelemetryConfigurator()
        self.judge_adapter = judge_adapter or OpenAISemanticJudgeAdapter(provider_config=provider)
        self.checkpoint_store = WorkflowCheckpointStore(self.work_root / "_workflow-checkpoints")
        self.workflow_progress_recorder = workflow_progress_recorder
        self.diagnostic_retention = diagnostic_retention or ScenarioDiagnosticRetentionService(
            self.work_root / "_diagnostics"
        )

    def execute(self, job: ClaimedScenarioJob) -> dict[str, object]:
        """Execute one side at an exact plugin Commit, then Judge anonymous evidence."""

        try:
            outcome = self._execute(job)
        except ScenarioExecutionSupportError as exc:
            raise ScenarioCodexExecutionError(str(exc)) from exc
        try:
            self.diagnostic_retention.capture(
                run_id=job.run_id,
                job_id=job.job_id,
                case_id=job.case_id,
                side=job.side,
                attempt_number=job.attempt_number,
                outcome=outcome,
            )
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning(
                "failed to retain Scenario diagnostic bundle for %s: %s: %s",
                job.job_id,
                type(exc).__name__,
                exc,
            )
        return outcome

    def cleanup_run_workspace(self, run_id: str, job_ids: tuple[str, ...]) -> None:
        """Remove heavy runtime trees while retaining bounded diagnostics by policy."""

        if re.fullmatch(r"sv2_[0-9a-f]{32}", run_id) is None:
            raise OSError("Scenario Run cleanup id is invalid")
        run_root = self.work_root / run_id
        self._remove_runtime_tree(run_root, expected_parent=self.work_root)
        checkpoint_root = self.checkpoint_store.root
        for job_id in job_ids:
            if re.fullmatch(r"sj_[0-9a-f]{32}", job_id) is None:
                raise OSError("Scenario checkpoint cleanup id is invalid")
            self._remove_runtime_tree(
                checkpoint_root / job_id,
                expected_parent=checkpoint_root,
            )
        self.diagnostic_retention.mark_terminal(run_id)
        cleanup = self.diagnostic_retention.sweep_if_due()
        if cleanup is not None and cleanup.removed_run_ids:
            logging.getLogger(__name__).info(
                "removed %s expired Scenario diagnostic workspace(s)",
                len(cleanup.removed_run_ids),
            )
        if cleanup is not None and cleanup.errors:
            logging.getLogger(__name__).warning(
                "Scenario diagnostic cleanup had %s error(s): %s",
                len(cleanup.errors),
                "; ".join(cleanup.errors),
            )

    @staticmethod
    def _remove_runtime_tree(path: Path, *, expected_parent: Path) -> None:
        if (
            expected_parent.is_symlink()
            or expected_parent.resolve() != expected_parent
            or path.is_symlink()
            or path.parent != expected_parent
        ):
            raise OSError("Scenario runtime cleanup target is unsafe")
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return

    def _execute(self, job: ClaimedScenarioJob) -> dict[str, object]:
        strategy = _strategy(job.contract, job.side)
        runtime = job.runtime_policy
        episode_root = self.work_root / job.run_id / job.case_id / job.side.lower() / f"attempt-{job.attempt_number}"
        if episode_root.exists():
            raise ScenarioCodexExecutionError(f"Fresh Scenario Episode already exists: {episode_root}")
        workspace = episode_root / "workspace"
        codex_home = episode_root / "codex-home"
        snapshot_path = episode_root / "helixharness-snapshot"
        codex_home.mkdir(parents=True)
        adapter = _object(strategy.get("adapter"), "Strategy Adapter")
        if adapter.get("adapter_id") != "known-helixharness" or adapter.get("adapter_version") != "1":
            raise ScenarioCodexExecutionError("Scenario v2 pilot Strategy Adapter is unsupported")
        if strategy.get("finding_policy") != "NONE" or strategy.get("finding_closures") != []:
            raise ScenarioCodexExecutionError("Scenario v2 pilot cannot skip declared Finding closures")
        workflow_closure = _strategy_workflow_closure(strategy)
        source_commit = str(adapter.get("source_commit", ""))
        source_repository = git_repository_root(self.helixharness_repository)
        export_git_tree(
            source_repository,
            frozen_plugin_tree(
                source_repository, source_commit,
                plugin_path=adapter.get("source_plugin_path"),
            ),
            snapshot_path,
            materialize_safe_symlinks=True,
        )
        self._verify_strategy_documents(snapshot_path, adapter)
        write_isolated_codex_config(codex_home, workspace, snapshot_path)
        self.snapshot_installer.install(snapshot_path, codex_home)
        artifact_contracts = _frozen_workflow_semantics(strategy)
        steps = tuple(
            FrozenWorkflowStep(
                step_id=str(stage["stage_id"]),
                command=str(stage["command"]),
                milestone_id=str(stage["completion_state"]),
                artifact_contract=artifact_contracts[str(stage["stage_id"])],
                stage_semantics=str(stage.get("semantic_role", "")) or None,
            )
            for stage in _object_list(strategy.get("stages"), "Strategy stages")
        )
        checkpoint_job = _checkpoint_job(job)
        restored = None
        if job.attempt_number > 1:
            restored = self.checkpoint_store.restore_latest(
                checkpoint_job,
                workflow_steps=steps,
                target_workspace=workspace,
            )
        manifest, gold, prompt, clarification_policy = self._materialize_scenario(
            job.snapshot,
            workspace,
            materialize_workspace=restored is None,
        )
        self.telemetry_configurator.disable(snapshot_path, workspace, codex_home)
        if restored is None:
            initial_tree_hash = workspace_tree_hash(workspace)
            workflow_steps = steps
            prior_turns: tuple[CodexTurnExecutionResult, ...] = ()
            resumed_after_step_id = None
            workflow_closure_resume = None
        else:
            initial_tree_hash = restored.initial_tree_hash
            remaining_ids = {step.step_id for step in restored.remaining_steps}
            workflow_steps = tuple(step for step in steps if step.step_id in remaining_ids)
            prior_turns = restored.prior_turns
            resumed_after_step_id = restored.last_completed_step_id
            workflow_closure_resume = restored.closure_resume
            assert_safe_workspace_tree(workspace)
        clarification_response = _clarification_response(runtime, clarification_policy)
        workflow_contract_response = "本次 Run 使用当前 HelixHarness 工作流；严格执行冻结命令与工件合同。"
        clarification_response = f"{workflow_contract_response}\n{clarification_response}"
        execution_prompt = (
            f"{prompt}\n\n[冻结 Scenario v2 执行契约]\n"
            "业务事实来自已冻结 Scenario；平台会依次发送已确认的原生工作流命令。"
            "不得替换、跳过或猜测其他工作流。只在当前隔离工作区实现并留下声明的证据。\n\n"
            f"[冻结 Case 澄清口径]\n{clarification_response}\n"
        )
        current_attempt_turns: tuple[CodexTurnExecutionResult, ...] = ()

        def save_stage_checkpoint(
            turns: tuple[CodexTurnExecutionResult, ...],
        ) -> RestoredWorkflowCheckpoint:
            nonlocal current_attempt_turns
            current_attempt_turns = turns
            assert_safe_workspace_tree(workspace)
            cumulative_turns = cumulative_checkpoint_turns(prior_turns, turns)
            completed_step = next(
                (item for item in steps if item.step_id == turns[-1].workflow_step_id), None,
            )
            if completed_step is not None:
                if completed_step.stage_semantics == "INIT":
                    initialization = verify_initialization_outputs(
                        strategy, manifest, workspace=workspace, snapshot_path=snapshot_path,
                        runtime_root=episode_root / "stage-validator-runtime",
                    )
                    if initialization["status"] != "PASS":
                        raise CodexExecutionError(
                            "Frozen Init completion check failed: "
                            + json.dumps(initialization, ensure_ascii=False, sort_keys=True)
                        )
                verify_native_workflow_transition(
                    completed_step, workspace=workspace, snapshot_path=snapshot_path,
                    runtime_root=episode_root / "stage-validator-runtime",
                )
            checkpoint = self.checkpoint_store.save(
                checkpoint_job,
                workspace_path=workspace,
                initial_tree_hash=initial_tree_hash,
                workflow_steps=steps,
                turns=cumulative_turns,
            )
            if self.workflow_progress_recorder is not None:
                self.workflow_progress_recorder(
                    job.job_id,
                    job.lease_token,
                    tuple(_completed_stage_ids(cumulative_turns)),
                )
            return checkpoint

        def save_closure_checkpoint(
            turns: tuple[CodexTurnExecutionResult, ...],
            closure_resume: FrozenWorkflowClosureResume,
            remaining_steps: tuple[FrozenWorkflowStep, ...],
        ) -> RestoredWorkflowCheckpoint:
            nonlocal current_attempt_turns
            current_attempt_turns = turns
            assert_safe_workspace_tree(workspace)
            return self.checkpoint_store.save_closure(
                checkpoint_job,
                workspace_path=workspace,
                initial_tree_hash=initial_tree_hash,
                workflow_steps=steps,
                turns=cumulative_checkpoint_turns(prior_turns, turns),
                closure_resume=closure_resume,
                remaining_steps=remaining_steps,
            )

        (
            remaining_wall_seconds,
            remaining_tokens,
            remaining_tool_calls,
            exhausted_budget,
        ) = checkpoint_remaining_resources(runtime, prior_turns)
        request = CodexExecutionRequest(
            prompt=execution_prompt,
            workspace_path=workspace,
            codex_home_path=codex_home,
            helixharness_snapshot_path=snapshot_path,
            model=str(runtime["execution_model"]),
            reasoning_effort=str(runtime["reasoning_effort"]),
            permission_mode="workspace-write",
            wall_timeout_seconds=remaining_wall_seconds,
            token_budget=remaining_tokens,
            tool_call_budget=remaining_tool_calls,
            disk_budget_mb=int(runtime["disk_budget_mb"]),
            token_budget_enforced=bool(runtime.get("token_budget_enforced", False)),
            universal_user_response=clarification_response,
            max_business_clarifications=int(
                runtime.get("max_business_clarifications", DEFAULT_MAX_BUSINESS_CLARIFICATIONS)
            ),
            max_api_step_retries=int(runtime.get("max_api_step_retries", 5)),
            workflow_steps=workflow_steps,
            workflow_driver_steps=steps,
            workflow_closure=workflow_closure,
            resumed_after_step_id=resumed_after_step_id,
            workflow_closure_resume=workflow_closure_resume,
            on_workflow_step_completed=save_stage_checkpoint,
            on_workflow_closure_checkpoint=save_closure_checkpoint,
        )
        completed_from_checkpoint = (
            restored is not None and not workflow_steps and workflow_closure_resume is None
        )
        if completed_from_checkpoint:
            fresh_result = completed_checkpoint_execution(prior_turns)
            attempt_record = _empty_attempt_resource_record("COMPLETED")
        else:
            try:
                fresh_result = (
                    checkpoint_budget_exhausted_result(*exhausted_budget)
                    if exhausted_budget is not None
                    else self.sdk_adapter.execute(request)
                )
            except ScenarioExecutionInterruption as exc:
                if exc.attempt_record is not None:
                    raise
                record = _interrupted_attempt_resource_record(
                    current_attempt_turns,
                    status="FAILED",
                )
                raise type(exc)(str(exc), attempt_record=record) from exc
            except Exception as exc:
                record = _interrupted_attempt_resource_record(
                    current_attempt_turns,
                    status="FAILED",
                )
                raise ScenarioCodexExecutionError(
                    str(exc),
                    attempt_record=record,
                ) from exc
            attempt_record = _attempt_resource_record(fresh_result)
        result = fresh_result
        if prior_turns and not completed_from_checkpoint:
            result = merge_checkpoint_execution(prior_turns, result)
        bounded_workflow_failure = _bounded_workflow_failure_reason(result, workflow_closure)
        if result.status != "COMPLETED":
            detail = _execution_error_detail(result)
            message = f"Codex Episode ended without trusted completion: {result.status}"
            if detail:
                message = f"{message}; {detail}"
            if _is_operator_recoverable_execution_failure(result.status, detail):
                raise ScenarioPauseExecutionError(
                    message,
                    attempt_record=attempt_record,
                )
            if _is_retryable_execution_failure(result.status, detail) and bounded_workflow_failure is None:
                raise ScenarioRetryableExecutionError(
                    message,
                    attempt_record=attempt_record,
                )
            if result.status not in _SCORABLE_SEMANTIC_TERMINALS:
                raise ScenarioCodexExecutionError(
                    message,
                    attempt_record=attempt_record,
                )
        assert_safe_workspace_tree(workspace)
        initialization = verify_initialization_outputs(
            strategy,
            manifest,
            workspace=workspace,
            snapshot_path=snapshot_path,
            runtime_root=episode_root / "initialization-validator-runtime",
        )
        verification = self._verify_business(
            gold,
            manifest=manifest,
            snapshot=job.snapshot,
            workspace=workspace,
            episode_root=episode_root,
        )
        artifact_records, artifact_judge_evidence = self._collect_artifact_evidence(strategy, workspace)
        code_records, code_judge_evidence = self._collect_code_evidence(workspace, manifest)
        evidence = [*artifact_records, *code_records]
        judge_evidence = [*artifact_judge_evidence, *code_judge_evidence]
        missing_required = [
            str(item["evidence_id"])
            for item in artifact_records
            if item.get("required") is True and item.get("status") != "PRESENT"
        ]
        case_gates: list[str] = []
        if result.status != "COMPLETED" or missing_required:
            case_gates.append("WORKFLOW_TERMINAL_FAILURE")
        if initialization["status"] == "FAIL":
            case_gates.append("INIT_OUTPUT_INVALID")
        case_gates.extend(_business_case_hard_gates(verification))
        workflow_judgment: dict[str, Any] | None = None
        workflow_axis_scores: dict[str, float] = {}
        workflow_judge_diagnostics: list[dict[str, Any]] = []
        workflow_quality: float | None = None
        if artifact_judge_evidence:
            workflow_judgment, workflow_axis_scores, workflow_judge_diagnostics = self._judge_workflow(
                job,
                gold=gold,
                evidence=artifact_judge_evidence,
                attempt_record=attempt_record,
            )
            workflow_quality = self._workflow_score(job, workflow_axis_scores)
        outcome_judgment: dict[str, Any] | None = None
        outcome_axis_scores: dict[str, float] = {}
        outcome_judge_diagnostics: list[dict[str, Any]] = []
        outcome_quality = _business_failure_outcome_score(verification)
        if verification["status"] == "PASS":
            outcome_judgment, outcome_axis_scores, outcome_judge_diagnostics = self._judge_outcome(
                job,
                gold=gold,
                verification=verification,
                evidence=judge_evidence,
                attempt_record=attempt_record,
            )
            outcome_quality = self._outcome_score(job, outcome_axis_scores)
        resources = attributable_resource_observation(
            result.turns,
            cumulative_usage=result.usage if isinstance(result.usage, dict) else {},
        )
        return {
            "schema_version": "framework-eval-execution-outcome.v2",
            "adapter": self.name,
            "terminal_status": result.status,
            "outcome_quality": outcome_quality,
            "outcome_axis_scores": outcome_axis_scores,
            "framework_workflow_quality": workflow_quality,
            "workflow_axis_scores": workflow_axis_scores,
            "resources": resources,
            "case_hard_gates": case_gates,
            "global_hard_gates": [],
            "reached_comparable_business_terminal": (
                result.status == "COMPLETED"
                and verification["status"] == "PASS"
                and initialization["status"] in {"PASS", "NOT_APPLICABLE"}
            ),
            "assessment_compatible": outcome_quality is not None,
            "incompatibility_reasons": (
                []
                if outcome_quality is not None
                else [
                    "BUSINESS_VERIFICATION_INCONCLUSIVE",
                ]
            ),
            "evidence": evidence,
            "verification": verification,
            "initialization": initialization,
            "judge": {
                "outcome": outcome_judgment,
                "workflow": workflow_judgment,
                "retry_diagnostics": {
                    "outcome": outcome_judge_diagnostics,
                    "workflow": workflow_judge_diagnostics,
                },
            },
            "judge_basis": {
                "schema_version": "framework-eval-side-score-basis.v2",
                "semantics": "BUSINESS_GATED_CODE_AND_ARTIFACT_OUTCOME_PLUS_UNIFIED_ARTIFACT_WORKFLOW",
                "immutable_evidence_hash": hashlib.sha256(
                    json.dumps(judge_evidence, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
                        "utf-8"
                    )
                ).hexdigest(),
            },
            "immutable_judge_evidence": judge_evidence,
            "execution": {
                "thread_id": result.thread_id,
                "turn_count": len(result.turns),
                "completed_stage_ids": _completed_stage_ids(result.turns),
                "terminal_reason": bounded_workflow_failure or _execution_terminal_reason(result),
                "turn_evidence": attempt_record["turn_evidence"],
                "attempt_record": attempt_record,
            },
        }

    def _materialize_scenario(
        self,
        snapshot: dict[str, object],
        workspace: Path,
        *,
        materialize_workspace: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, Any]]:
        manifest = json_object(snapshot_file(snapshot, "case.json"), label="Scenario manifest")
        initial_request_path = str(manifest.get("initial_request_path", ""))
        gold_path = str(manifest.get("gold_path", ""))
        clarification_path = str(manifest.get("clarification_policy_path", ""))
        prompt = snapshot_file(snapshot, initial_request_path).decode("utf-8")
        gold = json_object(snapshot_file(snapshot, gold_path), label="Scenario Gold")
        clarification_bytes = snapshot_file(snapshot, clarification_path)
        clarification = json_object(clarification_bytes, label="Scenario clarification policy")
        if not materialize_workspace:
            return manifest, gold, prompt, clarification
        repository: Path | None = None
        if manifest.get("case_kind") == "REPOSITORY_CASE":
            case_workspace = scenario_workspace(manifest)
            if case_workspace.is_multi_repository:
                workspace.mkdir(parents=True)
            for member in case_workspace.repositories:
                source = self.repositories.get(member.repository_id)
                if source is None:
                    raise ScenarioCodexExecutionError(
                        f"Repository resolver is missing: {member.repository_id}"
                    )
                target = workspace if member.mount_path == "." else workspace / member.mount_path
                export_git_tree(source, member.base_commit, target, materialize_safe_symlinks=True)
                self._initialize_git(target)
                if member.primary:
                    repository = source
            if case_workspace.is_multi_repository:
                self._materialize_workspace_files(snapshot, manifest, workspace)
                self._write_workspace_registry(workspace, case_workspace)
        else:
            workspace.mkdir(parents=True)
            prefix_value = manifest.get("workspace_path")
            prefix = safe_relative_path(prefix_value, label="Scenario workspace_path")
            files = snapshot.get("files")
            if not isinstance(files, dict):
                raise ScenarioCodexExecutionError("Scenario files are invalid")
            for name, item in sorted(files.items()):
                if not isinstance(item, dict) or item.get("visibility") != "EXECUTION":
                    continue
                path = safe_relative_path(name, label="Scenario execution path")
                try:
                    target_relative = path.relative_to(prefix)
                except ValueError:
                    continue
                target = workspace.joinpath(*target_relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(snapshot_file(snapshot, name))
        apply_workspace_preconditions(workspace, manifest)
        if repository is not None:
            fixture_merge_root = workspace.parent / "workspace-fixture-merge"
            try:
                apply_workspace_fixture_patch(
                    workspace,
                    manifest,
                    snapshot,
                    repository=repository,
                    merge_root=fixture_merge_root,
                )
            finally:
                shutil.rmtree(fixture_merge_root, ignore_errors=True)
        case_directory = workspace / ".helixharness_case"
        case_directory.mkdir(parents=True, exist_ok=True)
        (case_directory / "initial-request.md").write_text(prompt, encoding="utf-8")
        (case_directory / "clarification-policy.json").write_bytes(clarification_bytes)
        if manifest.get("case_kind") != "REPOSITORY_CASE":
            self._initialize_git(workspace)
        return manifest, gold, prompt, clarification

    @staticmethod
    def _materialize_workspace_files(
        snapshot: dict[str, object],
        manifest: dict[str, Any],
        workspace: Path,
    ) -> None:
        prefix_value = manifest.get("workspace_path")
        if not isinstance(prefix_value, str) or not prefix_value:
            return
        prefix = safe_relative_path(prefix_value, label="Scenario workspace_path")
        files = snapshot.get("files")
        if not isinstance(files, dict):
            raise ScenarioCodexExecutionError("Scenario files are invalid")
        for name, item in sorted(files.items()):
            if not isinstance(item, dict) or item.get("visibility") != "EXECUTION":
                continue
            path = safe_relative_path(name, label="Scenario execution path")
            try:
                relative = path.relative_to(prefix)
            except ValueError:
                continue
            target = safe_workspace_path(workspace, relative, label="Scenario execution path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(snapshot_file(snapshot, name))

    @staticmethod
    def _write_workspace_registry(workspace: Path, case_workspace: Any) -> None:
        registry = workspace / "helixharness.workspace.yaml"
        if registry.is_file():
            return
        lines = ["version: 1", "", "workspace:", "  name: framework-evaluation", "  root: .", "  mode: multi-repo", "", "repos:"]
        for member in case_workspace.repositories:
            key = json.dumps(member.repository_id, ensure_ascii=False)
            path = json.dumps(f"./{member.mount_path}", ensure_ascii=False)
            lines.extend(
                (
                    f"  {key}:",
                    f"    path: {path}",
                    f"    role: {member.impact.lower()}",
                    "    helix:",
                    "      initialized: true",
                    "      service_meta: null",
                )
            )
        registry.write_text("\n".join(lines) + "\n", encoding="utf-8")

    @staticmethod
    def _initialize_git(workspace: Path) -> None:
        commands = (
            ("git", "init"),
            ("git", "config", "user.name", "HelixHarness Evaluation"),
            ("git", "config", "user.email", "evaluation@localhost"),
            ("git", "add", "-A"),
            ("git", "commit", "-m", "frozen scenario baseline"),
        )
        for command in commands:
            completed = subprocess.run(
                command,
                cwd=workspace,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if completed.returncode != 0:
                raise ScenarioCodexExecutionError(f"Scenario workspace Git initialization failed: {' '.join(command)}")

    @staticmethod
    def _verify_strategy_documents(snapshot_path: Path, adapter: dict[str, Any]) -> None:
        hashes = _object(adapter.get("source_document_hashes"), "Strategy source document Hashes")
        for name, expected in hashes.items():
            relative = safe_relative_path(name, label="Strategy source document")
            target = snapshot_path.joinpath(*relative.parts)
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ScenarioCodexExecutionError(f"Strategy source document Hash mismatch: {name}")

    def _verify_business(
        self,
        gold: dict[str, Any],
        *,
        manifest: dict[str, Any],
        snapshot: dict[str, object],
        workspace: Path,
        episode_root: Path,
    ) -> dict[str, object]:
        case_workspace = (
            scenario_workspace(manifest)
            if manifest.get("case_kind") == "REPOSITORY_CASE"
            else None
        )
        if case_workspace is not None:
            self._verify_repository_impacts(workspace, case_workspace)
        verifier = episode_root / "verifier"
        shutil.copytree(
            workspace,
            verifier,
            symlinks=False,
            ignore=shutil.ignore_patterns(".git", ".env", ".env.*"),
        )
        hidden_patch = manifest.get("hidden_test_patch")
        hidden_test_injection = "NONE"
        patch_contracts = (
            manifest.get("hidden_test_patches")
            if case_workspace is not None and case_workspace.is_multi_repository
            else [hidden_patch] if isinstance(hidden_patch, dict) else []
        )
        injections: list[str] = []
        members = {
            member.repository_id: member
            for member in (case_workspace.repositories if case_workspace is not None else ())
        }
        for position, patch_contract in enumerate(patch_contracts, start=1):
            if not isinstance(patch_contract, dict) or not isinstance(patch_contract.get("path"), str):
                raise ScenarioCodexExecutionError("Frozen hidden test merge contract is invalid")
            repository_id = str(
                patch_contract.get("repository_id")
                or (case_workspace.primary_repository_id if case_workspace is not None else "")
            )
            member = members.get(repository_id)
            protected_paths = patch_contract.get("protected_paths")
            if member is None or not isinstance(protected_paths, list) or any(
                not isinstance(item, str) for item in protected_paths
            ):
                raise ScenarioCodexExecutionError("Frozen hidden test merge contract is invalid")
            repository = self.repositories.get(repository_id)
            if repository is None:
                raise ScenarioCodexExecutionError("Frozen hidden test repository is unavailable")
            target = verifier if member.mount_path == "." else verifier / member.mount_path
            injection = apply_hidden_test_patch(
                target,
                snapshot_file(snapshot, str(patch_contract["path"])),
                protected_paths=protected_paths,
                repository=repository,
                base_commit=member.base_commit,
                merge_root=episode_root / f"hidden-test-merge-{position}",
            )
            injections.append(
                f"{repository_id}:{injection}"
                if case_workspace is not None and case_workspace.is_multi_repository
                else injection
            )
        if injections:
            hidden_test_injection = ",".join(injections)
        assert_safe_workspace_tree(verifier)
        results: list[dict[str, object]] = []
        for position, test in enumerate(_object_list(gold.get("execution_tests", []), "Execution tests"), start=1):
            command_workspace = episode_root / "commands" / f"{position:03d}" / "workspace"
            command_workspace.parent.mkdir(parents=True)
            shutil.copytree(verifier, command_workspace)
            argv = test.get("argv")
            timeout = test.get("timeout_seconds")
            working_directory = test.get("working_directory", ".")
            if (
                not isinstance(argv, list)
                or not argv
                or any(not isinstance(argument, str) or not argument for argument in argv)
                or not isinstance(timeout, int)
                or not isinstance(working_directory, str)
            ):
                raise ScenarioCodexExecutionError("Frozen execution test is invalid")
            command_cwd = safe_workspace_path(
                command_workspace,
                safe_relative_path(working_directory, label="Execution test working_directory"),
                label="Execution test working_directory",
            )
            started = time.monotonic()
            completed = run_frozen_command(
                argv,
                cwd=command_cwd,
                environment=frozen_command_environment(
                    command_workspace.parent,
                    workspace=command_workspace,
                    gradle_home=episode_root / "verifier-gradle-home",
                ),
                timeout_seconds=timeout,
            )
            status = "ERROR" if completed.returncode is None else ("PASS" if completed.returncode == 0 else "FAIL")
            results.append(
                {
                    "check_id": str(test.get("check_id", "")),
                    "kind": str(test.get("kind", "")),
                    "status": status,
                    "timed_out": completed.timed_out,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                }
            )
        overall_status = "PASS"
        if not results or any(item["status"] == "ERROR" for item in results):
            overall_status = "INCONCLUSIVE"
        elif any(item["status"] == "FAIL" for item in results):
            overall_status = "FAIL"
        return {
            "status": overall_status,
            "checks": results,
            "hidden_test_injection": hidden_test_injection,
        }

    @staticmethod
    def _verify_repository_impacts(workspace: Path, case_workspace: Any) -> None:
        """Reject implementation changes in repositories frozen as read-only context."""

        for member in case_workspace.repositories:
            if member.impact != "READ":
                continue
            target = workspace / member.mount_path
            completed = subprocess.run(
                ("git", "status", "--porcelain", "--untracked-files=all"),
                cwd=target,
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                raise ScenarioCodexExecutionError(
                    f"Unable to inspect read-only repository: {member.repository_id}"
                )
            changes = [(line[:2], line[3:]) for line in completed.stdout.splitlines() if len(line) > 3]
            if any(not _is_allowed_read_repository_change(status, path) for status, path in changes):
                raise ScenarioCodexExecutionError(
                    f"Read-only repository was modified: {member.repository_id}"
                )

    @staticmethod
    def _collect_artifact_evidence(
        strategy: dict[str, Any],
        workspace: Path,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Collect every Markdown file from each frozen completed-feature package."""

        records: list[dict[str, object]] = []
        judge_records: list[dict[str, object]] = []
        for source in _object_list(strategy.get("artifact_sources"), "Strategy artifact sources"):
            source_id = str(source.get("source_id", ""))
            relative_root = safe_relative_path(source.get("root"), label="Artifact source root")
            root = safe_workspace_path(workspace, relative_root, label="Artifact source root")
            include_glob = str(source.get("include_glob", "**/*.md"))
            max_files = int(source.get("max_files", 50))
            max_total_bytes = int(source.get("max_total_bytes", 500_000))
            files = sorted(path for path in root.glob(include_glob) if path.is_file() and not path.is_symlink()) if root.is_dir() else []
            contents = [(path, path.read_bytes()) for path in files]
            total_bytes = sum(len(content) for _, content in contents)
            source_status = "PRESENT"
            if not contents:
                source_status = "MISSING"
            elif len(contents) > max_files or total_bytes > max_total_bytes:
                source_status = "LIMIT_EXCEEDED"
            source_record = {
                "evidence_id": source_id,
                "kind": "STAGE_ARTIFACT_SOURCE",
                "locator": relative_root.as_posix(),
                "include_glob": include_glob,
                "artifact_schema": str(source.get("artifact_schema", "")),
                "required": bool(source.get("required", True)),
                "status": source_status,
                "file_count": len(contents),
                "size_bytes": total_bytes,
            }
            records.append(source_record)
            if source_status != "PRESENT":
                continue
            for path, content in contents:
                relative = path.relative_to(workspace).as_posix()
                evidence_id = f"artifact-{hashlib.sha256(relative.encode('utf-8')).hexdigest()[:20]}"
                record = {
                    "evidence_id": evidence_id,
                    "kind": "STAGE_ARTIFACT",
                    "source_id": source_id,
                    "locator": relative,
                    "required": False,
                    "status": "PRESENT",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                }
                records.append(record)
                judge_records.append(
                    {
                        "evidence_id": evidence_id,
                        "kind": "STAGE_ARTIFACT",
                        "path": relative,
                        "content": content.decode("utf-8", errors="replace"),
                    }
                )
        return records, judge_records

    @staticmethod
    def _collect_code_evidence(
        workspace: Path,
        manifest: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Freeze production/test changes and bounded implementation context."""

        if not isinstance(manifest, dict) or manifest.get("case_kind") != "REPOSITORY_CASE":
            return CodexScenarioExecutionAdapter._collect_repository_code_evidence(workspace)
        case_workspace = scenario_workspace(manifest)
        if case_workspace.is_multi_repository:
            records: list[dict[str, object]] = []
            judge_records: list[dict[str, object]] = []
            for member in case_workspace.repositories:
                repository_root = workspace / member.mount_path
                member_records, member_judge_records = CodexScenarioExecutionAdapter._collect_repository_code_evidence(
                    repository_root,
                    path_prefix=member.mount_path,
                )
                records.extend(member_records)
                judge_records.extend(member_judge_records)
            return records, judge_records
        return CodexScenarioExecutionAdapter._collect_repository_code_evidence(workspace)

    @staticmethod
    def _collect_repository_code_evidence(
        workspace: Path,
        *,
        path_prefix: str = "",
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Collect one repository while preserving its workspace-relative identity."""

        tracked = subprocess.run(
            ("git", "diff", "--name-only", "-z", "HEAD"),
            cwd=workspace,
            check=False,
            capture_output=True,
        )
        untracked = subprocess.run(
            ("git", "ls-files", "--others", "--exclude-standard", "-z"),
            cwd=workspace,
            check=False,
            capture_output=True,
        )
        if tracked.returncode != 0 or untracked.returncode != 0:
            raise ScenarioCodexExecutionError("Unable to collect final implementation evidence")
        tracked_paths = {
            path
            for path in tracked.stdout.decode("utf-8", errors="replace").split("\0")
            if path
        }
        untracked_paths = {
            path
            for path in untracked.stdout.decode("utf-8", errors="replace").split("\0")
            if path
        }
        changed_paths: list[str] = []
        raw_paths = [
            *tracked_paths,
            *untracked_paths,
        ]
        for path in raw_paths:
            if not path:
                continue
            relative = safe_relative_path(path, label="Changed implementation path")
            if _is_code_path(relative):
                changed_paths.append(relative.as_posix())
        changed_paths = list(dict.fromkeys(sorted(changed_paths)))
        production_paths = [path for path in changed_paths if not _is_test_path(Path(path))]
        test_paths = [path for path in changed_paths if _is_test_path(Path(path))]
        records: list[dict[str, object]] = []
        judge_records: list[dict[str, object]] = []
        for evidence_id, kind, evidence_path, paths in (
            ("production-code-changeset", "CODE_CHANGESET", "production.git.patch", production_paths),
            ("test-code-changeset", "TEST_CHANGESET", "tests.git.patch", test_paths),
        ):
            patch = _build_changed_paths_patch(
                workspace,
                paths,
                tracked_paths=tracked_paths,
                untracked_paths=untracked_paths,
            )
            records.append(
                {
                    "evidence_id": f"{evidence_id}-{path_prefix}" if path_prefix else evidence_id,
                    "kind": kind,
                    "status": "PRESENT",
                    "sha256": hashlib.sha256(patch).hexdigest(),
                    "size_bytes": len(patch),
                    "changed_paths": [f"{path_prefix}/{path}" if path_prefix else path for path in paths],
                }
            )
            judge_records.append(
                {
                    "evidence_id": f"{evidence_id}-{path_prefix}" if path_prefix else evidence_id,
                    "kind": kind,
                    "path": f"{path_prefix}/{evidence_path}" if path_prefix else evidence_path,
                    "content": patch.decode("utf-8", errors="replace") or "No changes were produced for this evidence class.",
                }
            )
        total_bytes = 0
        for relative_text in changed_paths:
            relative = safe_relative_path(relative_text, label="Changed implementation path")
            target = safe_workspace_path(workspace, relative, label="Changed implementation path")
            if not target.is_file() or target.is_symlink():
                continue
            content = target.read_bytes()
            is_test = _is_test_path(relative)
            kind = "TEST_CODE" if is_test else "CODE_IMPLEMENTATION"
            displayed_path = f"{path_prefix}/{relative_text}" if path_prefix else relative_text
            evidence_id = f"code-{hashlib.sha256(displayed_path.encode('utf-8')).hexdigest()[:20]}"
            if len(content) > _MAX_CODE_FILE_BYTES or total_bytes + len(content) > _MAX_CODE_TOTAL_BYTES:
                records.append(
                    {
                        "evidence_id": evidence_id,
                        "kind": kind,
                        "locator": displayed_path,
                        "status": "OMITTED_SIZE_LIMIT",
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content),
                        "reason": "FULL_FILE_CONTENT_EXCEEDS_FROZEN_EVIDENCE_LIMIT",
                    }
                )
                continue
            total_bytes += len(content)
            line_count = _line_count(content)
            records.append(
                {
                    "evidence_id": evidence_id,
                    "kind": kind,
                    "locator": displayed_path,
                    "status": "PRESENT",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "start_line": 1,
                    "end_line": line_count,
                }
            )
            judge_records.append(
                {
                    "evidence_id": evidence_id,
                    "kind": kind,
                    "path": displayed_path,
                    "start_line": 1,
                    "end_line": line_count,
                    "content": content.decode("utf-8", errors="replace"),
                }
            )
        context_paths = _discover_code_context_paths(
            workspace,
            production_paths,
            excluded_paths=set(changed_paths),
        )
        context_total_bytes = 0
        selected_context_paths: list[str] = []
        for relative_text in context_paths:
            relative = safe_relative_path(relative_text, label="Implementation context path")
            target = safe_workspace_path(workspace, relative, label="Implementation context path")
            if not target.is_file() or target.is_symlink():
                continue
            content = target.read_bytes()
            if (
                len(content) > _MAX_CODE_FILE_BYTES
                or context_total_bytes + len(content) > _MAX_CODE_CONTEXT_TOTAL_BYTES
            ):
                continue
            context_total_bytes += len(content)
            selected_context_paths.append(relative_text)
            displayed_path = f"{path_prefix}/{relative_text}" if path_prefix else relative_text
            evidence_id = f"context-{hashlib.sha256(displayed_path.encode('utf-8')).hexdigest()[:20]}"
            line_count = _line_count(content)
            records.append(
                {
                    "evidence_id": evidence_id,
                    "kind": "CODE_CONTEXT",
                    "locator": displayed_path,
                    "status": "PRESENT",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size_bytes": len(content),
                    "start_line": 1,
                    "end_line": line_count,
                }
            )
            judge_records.append(
                {
                    "evidence_id": evidence_id,
                    "kind": "CODE_CONTEXT",
                    "path": displayed_path,
                    "start_line": 1,
                    "end_line": line_count,
                    "content": content.decode("utf-8", errors="replace"),
                }
            )
        records.append(
            {
                "evidence_id": f"code-context-index-{path_prefix}" if path_prefix else "code-context-index",
                "kind": "CODE_CONTEXT_INDEX",
                "status": "PRESENT",
                "selection_policy": "declared-symbol-callers-and-referenced-types.v1",
                "selected_paths": [
                    f"{path_prefix}/{path}" if path_prefix else path
                    for path in selected_context_paths
                ],
                "max_files": _MAX_CODE_CONTEXT_FILES,
                "max_total_bytes": _MAX_CODE_CONTEXT_TOTAL_BYTES,
            }
        )
        return records, judge_records

    def _judge_outcome(
        self,
        job: ClaimedScenarioJob,
        *,
        gold: dict[str, Any],
        verification: dict[str, object],
        evidence: list[dict[str, object]],
        attempt_record: dict[str, object] | None = None,
    ) -> tuple[dict[str, Any], dict[str, float], list[dict[str, Any]]]:
        request = build_outcome_judge_request(
            job.contract,
            gold=gold,
            verification=verification,
            evidence=evidence,
        )
        return self._execute_judge(job, request, attempt_record=attempt_record)

    def _judge_workflow(
        self,
        job: ClaimedScenarioJob,
        *,
        gold: dict[str, Any],
        evidence: list[dict[str, object]],
        attempt_record: dict[str, object] | None = None,
    ) -> tuple[dict[str, Any], dict[str, float], list[dict[str, Any]]]:
        request = build_workflow_judge_request(job.contract, gold=gold, evidence=evidence)
        return self._execute_judge(job, request, attempt_record=attempt_record)

    def _execute_judge(
        self,
        job: ClaimedScenarioJob,
        request: dict[str, Any],
        *,
        attempt_record: dict[str, object] | None = None,
    ) -> tuple[dict[str, Any], dict[str, float], list[dict[str, Any]]]:
        adapter = _object(_strategy(job.contract, job.side).get("adapter"), "Strategy Adapter")
        anonymous = anonymize_value(request, (str(adapter.get("source_commit", "")), job.side))
        current_request = anonymous
        diagnostics: list[dict[str, Any]] = []
        max_retries = max(0, int(job.runtime_policy.get("judge_max_retries", 1)))
        for attempt in range(max_retries + 1):
            try:
                output = self.judge_adapter.judge(
                    current_request,
                    model=str(job.runtime_policy["judge_model"]),
                    reasoning_effort=str(job.runtime_policy["judge_reasoning_effort"]),
                )
            except (ConnectionError, TimeoutError) as exc:
                if attempt < max_retries:
                    continue
                message = "Semantic Judge request remained unavailable after bounded retries"
                raise ScenarioRetryableExecutionError(
                    message,
                    attempt_record=_attempt_record_with_judge_error(
                        attempt_record,
                        error_type=type(exc).__name__,
                        message=message,
                        diagnostics=diagnostics,
                    ),
                ) from exc
            if not isinstance(output, dict):
                contract_error = JudgeContractError("Semantic Judge output is invalid")
            else:
                try:
                    scores = validate_scenario_judgment(output, anonymous)
                except JudgeContractError as exc:
                    contract_error = exc
                else:
                    return output, scores, diagnostics
            diagnostic = {
                "attempt": attempt + 1,
                "error": str(contract_error)[:1_000],
                "output": _bounded_judge_output(output) if isinstance(output, dict) else {"invalid_type": type(output).__name__},
            }
            diagnostics.append(diagnostic)
            if attempt >= max_retries:
                message = f"Semantic Judge output failed trusted validation: {contract_error}"
                raise ScenarioCodexExecutionError(
                    message,
                    attempt_record=_attempt_record_with_judge_error(
                        attempt_record,
                        error_type="JudgeContractError",
                        message=message,
                        diagnostics=diagnostics,
                    ),
                ) from contract_error
            if isinstance(output, dict):
                current_request = _judge_validation_retry_request(
                    anonymous,
                    error=contract_error,
                    output=output,
                )
        raise AssertionError("unreachable")

    @staticmethod
    def _outcome_score(job: ClaimedScenarioJob, scores: dict[str, float]) -> float:
        profile = _object(job.contract.get("scoring_profile"), "Scoring Profile")
        weights = _object(profile.get("outcome_weights"), "Outcome weights")
        if set(scores) != set(weights):
            raise ScenarioCodexExecutionError("Outcome Judge returned unexpected frozen axes")
        return round(sum(scores[axis] * float(weight) / 100 for axis, weight in weights.items()), 2)

    @staticmethod
    def _workflow_score(job: ClaimedScenarioJob, scores: dict[str, float]) -> float:
        profile = _object(job.contract.get("scoring_profile"), "Scoring Profile")
        weights = _object(profile.get("workflow_axes"), "Workflow axes")
        if set(scores) != set(weights):
            raise ScenarioCodexExecutionError("Workflow Judge returned unexpected frozen axes")
        return round(sum(scores[axis] * float(weight) / 100 for axis, weight in weights.items()), 2)


def _strategy(contract: dict[str, object], side: str) -> dict[str, Any]:
    strategies = _object(contract.get("strategies"), "Strategies")
    return _object(strategies.get(side), f"{side} Strategy")


def _build_changed_paths_patch(
    workspace: Path,
    paths: list[str],
    *,
    tracked_paths: set[str],
    untracked_paths: set[str],
) -> bytes:
    """Build one initial-to-final patch, including newly created files."""

    if not paths:
        return b""
    patch_parts: list[bytes] = []
    tracked = [path for path in paths if path in tracked_paths]
    if tracked:
        diff = subprocess.run(
            ("git", "diff", "--no-ext-diff", "--binary", "HEAD", "--", *tracked),
            cwd=workspace,
            check=False,
            capture_output=True,
        )
        if diff.returncode != 0:
            raise ScenarioCodexExecutionError("Unable to collect tracked implementation diff")
        patch_parts.append(diff.stdout)
    for relative_text in paths:
        if relative_text not in untracked_paths:
            continue
        diff = subprocess.run(
            ("git", "diff", "--no-ext-diff", "--binary", "--no-index", "/dev/null", relative_text),
            cwd=workspace,
            check=False,
            capture_output=True,
        )
        if diff.returncode not in {0, 1}:
            raise ScenarioCodexExecutionError("Unable to collect untracked implementation diff")
        patch_parts.append(diff.stdout)
    return b"".join(patch_parts)


def _discover_code_context_paths(
    workspace: Path,
    production_paths: list[str],
    *,
    excluded_paths: set[str],
) -> list[str]:
    """Select bounded callers and referenced types without letting Judge browse."""

    if not production_paths:
        return []
    changed_texts: list[str] = []
    changed_suffixes: set[str] = set()
    for relative_text in production_paths:
        relative = safe_relative_path(relative_text, label="Changed production path")
        target = safe_workspace_path(workspace, relative, label="Changed production path")
        if not target.is_file() or target.is_symlink():
            continue
        changed_texts.append(target.read_text(encoding="utf-8", errors="replace"))
        changed_suffixes.add(relative.suffix.lower())
    combined = "\n".join(changed_texts)
    if not combined:
        return []
    declared_symbols = {
        symbol
        for text in changed_texts
        for symbol in _TOP_LEVEL_DECLARED_SYMBOL_PATTERN.findall(text)
        if symbol not in _CONTEXT_SYMBOL_STOPWORDS
    }
    referenced_types = {
        symbol
        for symbol in _IDENTIFIER_PATTERN.findall(combined)
        if symbol not in _CONTEXT_SYMBOL_STOPWORDS
    }
    listed = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=workspace,
        check=False,
        capture_output=True,
    )
    if listed.returncode != 0:
        raise ScenarioCodexExecutionError("Unable to discover implementation context")
    ranked: list[tuple[int, str]] = []
    for relative_text in listed.stdout.decode("utf-8", errors="replace").split("\0"):
        if not relative_text or relative_text in excluded_paths:
            continue
        relative = safe_relative_path(relative_text, label="Implementation context path")
        if not _is_code_path(relative) or _is_test_path(relative):
            continue
        target = safe_workspace_path(workspace, relative, label="Implementation context path")
        if not target.is_file() or target.is_symlink() or target.stat().st_size > _MAX_CODE_FILE_BYTES:
            continue
        stem_tokens = {
            relative.stem,
            "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", relative.stem) if part),
        }
        score = 0
        if stem_tokens & referenced_types:
            score += 100
        content = target.read_text(encoding="utf-8", errors="replace")
        caller_hits = sum(
            1
            for symbol in declared_symbols
            if re.search(rf"\b{re.escape(symbol)}\b", content)
        )
        if caller_hits:
            score += 50 + min(caller_hits, 5)
        if score and relative.suffix.lower() in changed_suffixes:
            score += 10
        if score:
            ranked.append((score, relative_text))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in ranked[:_MAX_CODE_CONTEXT_FILES]]


def _line_count(content: bytes) -> int:
    """Return an inclusive line range for one complete evidence file."""

    if not content:
        return 0
    return content.count(b"\n") + (0 if content.endswith(b"\n") else 1)


def _is_code_path(path: Path) -> bool:
    if path.parts[:1] in {(".git",), ("docs",)} or path.name.startswith(".env"):
        return False
    return path.suffix.lower() in _CODE_SUFFIXES or path.name in _CODE_FILENAMES


def _is_test_path(path: Path) -> bool:
    lowered = tuple(part.lower() for part in path.parts)
    return any(part in {"test", "tests", "testing", "__tests__"} for part in lowered) or any(
        marker in path.name.lower() for marker in ("test", "spec")
    )


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioCodexExecutionError(f"{label} must be an object")
    return value


def _is_allowed_read_repository_change(status: str, path: str) -> bool:
    """Allow workflow metadata while keeping read-only implementation trees immutable."""

    if path.startswith("docs/specs/"):
        return True
    return status == "??" and (
        path == "AGENTS.md"
        or path == "docs/.helixharness-init-state.json"
        or path.startswith(("docs/harness/", "docs/hooks/", "docs/knowledge/"))
    )


def _object_list(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ScenarioCodexExecutionError(f"{label} must be a list of objects")
    return value
