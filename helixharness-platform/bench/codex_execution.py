"""Isolated real-Episode execution through the Codex Python SDK."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, ContextManager, Protocol, TypedDict
from urllib.parse import urlparse

from bench.codex_runtime_launcher import (
    isolated_gradle_home,
    sanitized_environment,
)
from bench.gradle_runtime import configure_evaluator_gradle_home
from bench.report_models import (
    ClaimedJob,
)
from bench.runtime_secrets import (
    EvaluationProviderConfig,
    codex_responses_base_url,
)
from bench.turn_control import (
    TURN_STATUS_PREFIX,
    TURN_STATUS_REPAIR_PROMPT,
    FrozenTurnClassifier,
    TurnClassification,
    has_valid_turn_status,
    initial_turn_prompt,
    resumed_workflow_step_prompt,
    workflow_bootstrap_prompt,
)
from bench.workflow_goal_control import (
    WorkflowGoalDecision,
    WorkflowGoalObservation,
    WorkflowGoalPolicy,
    WorkflowGoalProgress,
    infer_next_workflow_actions,
)


class CodexExecutionError(RuntimeError):
    """Raised when a Codex-configured Run cannot be executed safely."""


def _repeats_failed_archive_without_recovery(decision: WorkflowGoalDecision) -> bool:
    """Let stage recovery handle an Archive failure before retrying Archive."""

    return decision.status == "EXECUTE" and decision.actions == ("ARCHIVE",)


def _deterministic_frozen_stage_recommendation(
    workflow_steps: tuple[FrozenWorkflowStep, ...],
    failed_step: FrozenWorkflowStep,
    failed_response: str | None,
) -> FrozenWorkflowStep | None:
    """Resolve one exact Router recommendation to a frozen recovery stage."""

    if not failed_response:
        return None
    failed_tokens = shlex.split(failed_step.command)
    if not failed_tokens:
        return None
    allowed_contracts = {
        "HELIXHARNESS_APPLY": {"HELIXHARNESS_PROPOSE", "HELIXHARNESS_APPLY"},
        "HELIXHARNESS_ARCHIVE": {"HELIXHARNESS_PROPOSE", "HELIXHARNESS_APPLY", "HELIXHARNESS_ARCHIVE"},
    }.get(failed_step.artifact_contract)
    if allowed_contracts is None:
        return None
    try:
        failed_index = workflow_steps.index(failed_step)
    except ValueError:
        return None
    recommended = set(_anchored_helixharness_recommendations(failed_response))
    candidates = [
        step for step in workflow_steps[:failed_index + 1]
        if step.artifact_contract in allowed_contracts and step.command in recommended
    ]
    return candidates[0] if len(candidates) == 1 else None


def _anchored_helixharness_recommendations(response: str | None) -> tuple[str, ...]:
    """Return only exact standalone Router recommendation fields."""

    if not response:
        return ()
    return tuple(
        match.group(1)
        for raw_line in response.splitlines()
        if (
            match := re.fullmatch(
                r"recommendation(?:=|:\s*)`?(/helix:[^`\r\n]+)`?",
                raw_line.strip(),
            )
        )
    )


def _deterministic_workflow_policy_response(
    workflow_step: FrozenWorkflowStep | None,
    response: str | None,
    *,
    universal_response: str,
) -> str | None:
    """Resolve a known non-business workflow choice from the frozen Run policy."""

    if workflow_step is None or not response:
        return None
    if workflow_step.command.startswith("/helix:propose "):
        asks_for_confirmation = bool(
            re.search(r"回复[‘’“”\"']?确认[‘’“”\"']?", response)
        )
        if asks_for_confirmation and "spec.md" in response and "tasks.md" in response:
            return f"确认。{universal_response}"
        return None
    if not workflow_step.command.startswith("/helix:archive "):
        return None
    if (
        "提交已封存的 Apply Scope" not in response
        or "暂不提交 Apply Scope" not in response
        or "取消归档" not in response
    ):
        return None
    option = re.search(
        r"(?m)^\s*([0-9]+)[.、]\s*暂不提交 Apply Scope[^\r\n]*$",
        response,
    )
    if option is None:
        return None
    return (
        f"选择 {option.group(1)}：暂不提交 Apply Scope，继续归档并保留未提交状态。"
        "这是冻结 Run 的流程策略，不是业务事实；不要 commit、不要 push。"
    )


def _recommended_frozen_stage_recovery(
    workflow_steps: tuple[FrozenWorkflowStep, ...],
    failed_step: FrozenWorkflowStep,
    failed_response: str | None,
) -> FrozenWorkflowStep:
    """Honor an exact frozen command recommended by a failed stage."""

    recommended = _deterministic_frozen_stage_recommendation(
        workflow_steps,
        failed_step,
        failed_response,
    )
    if recommended is not None:
        return recommended
    return failed_step


def _workflow_recovery_suffix_index(
    workflow_steps: tuple[FrozenWorkflowStep, ...],
    recovery_step: FrozenWorkflowStep,
    current_index: int,
) -> int:
    """Rewind a fresh request while preserving an already-restored suffix."""

    try:
        return workflow_steps.index(recovery_step) + 1
    except ValueError:
        return 0


def _stage_rework_count_after_rerun(
    current_count: int,
    workflow_steps: tuple[FrozenWorkflowStep, ...],
    current_index: int,
) -> int:
    """Keep the bounded recovery budget until its downstream suffix finishes."""

    return 0 if current_index >= len(workflow_steps) else current_count


def _stage_validation_repair_hint(detail: str) -> str | None:
    """Give bounded, non-forging guidance for known mechanical stage failures."""

    normalized = " ".join(detail.lower().split())
    if "review state has invalid identity" in normalized:
        return (
            "请修复 review-state.json 的公开身份字段：schema_version 必须是 JSON 数字 1（不是字符串），"
            "feature 必须与当前冻结命令中的 feature 名完全一致。不得改写其他已通过的业务实现、"
            "review scope、双轴 review 或 verify-report，也不得手工猜测其他 identity 字段。"
        )
    return None


def validate_java8_runtime(java_home: Path) -> None:
    """Require an executable Java 8 runtime before materializing an Episode."""

    java = java_home.expanduser() / "bin" / "java"
    if not java.is_file() or not os.access(java, os.X_OK):
        raise CodexExecutionError("统一 Java 8 运行时不可用：JAVA_HOME/bin/java 不可执行")
    try:
        completed = subprocess.run(
            [str(java), "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CodexExecutionError("统一 Java 8 运行时校验失败") from exc
    version_output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0 or not re.search(r'version\s+"1\.8(?:\.|\")', version_output):
        raise CodexExecutionError("评测 Episode 必须使用 Java 8 运行时")


def seed_isolated_gradle_home(
    codex_home_path: Path,
    *,
    source: Path,
) -> Path:
    """Clone runtime-only Gradle caches without copying personal configuration."""

    target = configure_evaluator_gradle_home(isolated_gradle_home(codex_home_path))
    for directory_name in ("wrapper", "caches"):
        source_directory = source / directory_name
        target_directory = target / directory_name
        if not source_directory.is_dir() or target_directory.exists():
            continue
        _clone_tree_copy_on_write(source_directory, target_directory)
    for stale_lock in target.rglob("*.lock"):
        if stale_lock.is_file() or stale_lock.is_symlink():
            stale_lock.unlink()
    return target


def _clone_tree_copy_on_write(source: Path, target: Path) -> None:
    """Clone one tree with native CoW support and a portable copy fallback."""

    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    executable = shutil.which("cp")
    command: tuple[str, ...] | None = None
    if executable and sys.platform == "darwin":
        command = (executable, "-cR", str(source), str(target))
    elif executable and sys.platform.startswith("linux"):
        command = (executable, "--archive", "--reflink=auto", str(source), str(target))
    cloned = False
    if command is not None:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            cloned = completed.returncode == 0
        except OSError:
            pass  # A missing or unusable native executable must not block portable copying.
    if not cloned:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        shutil.copytree(source, target, symlinks=True)


def _clone_gradle_cache_directory(source: Path, target: Path) -> None:
    """Atomically clone one cache directory without exposing partial state."""

    if not source.is_dir() or target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
    try:
        _clone_tree_copy_on_write(source, temporary)
        for stale_lock in temporary.rglob("*.lock"):
            if stale_lock.is_file() or stale_lock.is_symlink():
                stale_lock.unlink()
        try:
            temporary.replace(target)
        except FileExistsError:
            shutil.rmtree(temporary)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def _gradle_wrapper_cache_names(workspace: Path) -> tuple[tuple[str, str], ...]:
    cache_names: set[tuple[str, str]] = set()
    for properties_path in workspace.rglob("gradle-wrapper.properties"):
        try:
            properties = properties_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        match = re.search(r"(?:^|/)gradle-([^/]+)-(all|bin)\.zip(?:\s|$)", properties, re.MULTILINE)
        if match is None:
            continue
        version, distribution_kind = match.groups()
        cache_names.add((f"gradle-{version}-{distribution_kind}", version))
    return tuple(sorted(cache_names))


def prepare_managed_gradle_home(
    target: Path,
    *,
    source: Path,
    workspace: Path,
) -> Path:
    """Seed an evaluator-owned reusable Gradle cache from runtime-only host data."""

    configure_evaluator_gradle_home(target)
    _clone_gradle_cache_directory(
        source / "caches" / "modules-2",
        target / "caches" / "modules-2",
    )
    for distribution_name, version in _gradle_wrapper_cache_names(workspace):
        _clone_gradle_cache_directory(
            source / "wrapper" / "dists" / distribution_name,
            target / "wrapper" / "dists" / distribution_name,
        )
        _clone_gradle_cache_directory(
            source / "caches" / version,
            target / "caches" / version,
        )
    init_directory = target / "init.d"
    init_directory.mkdir(parents=True, exist_ok=True)
    (init_directory / "helixharness-network.gradle").write_text(
        "// Evaluator-owned adapter for the dynamic Codex sandbox proxy.\n"
        "[['http', 'HTTP_PROXY'], ['https', 'HTTPS_PROXY']].each { proxyConfig ->\n"
        "    def scheme = proxyConfig[0]\n"
        "    def rawProxy = System.getenv(proxyConfig[1])\n"
        "    if (rawProxy) {\n"
        "        def proxyUri = new java.net.URI(rawProxy)\n"
        "        System.setProperty(scheme + '.proxyHost', proxyUri.host)\n"
        "        System.setProperty(scheme + '.proxyPort', String.valueOf(proxyUri.port))\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    return target


def repository_network_domains(workspace: Path) -> tuple[str, ...]:
    """Return constrained build-network destinations declared by Gradle inputs."""

    domains = {
        "plugins.gradle.org",
        "plugins-artifacts.gradle.org",
        "repo.maven.apache.org",
        "repo1.maven.org",
    }
    candidates: set[Path] = set()
    for pattern in ("*.gradle", "*.gradle.kts", "gradle-wrapper.properties"):
        candidates.update(workspace.rglob(pattern))
    for path in sorted(candidates):
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw_url in re.findall(r"https?\\?://[^\s'\"()]+", text):
            hostname = urlparse(raw_url.replace("\\:", ":")).hostname
            if hostname:
                domains.add(hostname.lower())
    return tuple(sorted(domains))


def repository_change_scope_prompt(prompt: str) -> str:
    """Tell the Agent how baseline failures are attributed for repository Cases."""

    return (
        f"{prompt}\n\n[冻结 Case 变更范围契约]\n"
        "本次评测范围以 Initial Request 的范围与验收标准为准。"
        "Base 中已存在且位于本次范围外的编译或测试失败，只能如实记录为历史基线限制，"
        "不得归因给 Candidate，也不得为了追求全量测试全绿而修改范围外代码、历史测试或构建脚本。"
        "Candidate 新增的失败、目标行为失败，或冻结 PASS_TO_PASS 从通过变为失败，仍然必须阻断。"
        "运行全量构建或测试遇到失败时，应先判断该失败是否能在未修改的 Base 上复现，并在验证报告中"
        "分别记录本次范围内结论与范围外基线限制。\n"
    )


def write_isolated_codex_config(
    codex_home_path: Path,
    workspace_path: Path,
    snapshot_path: Path,
    *,
    gradle_home_path: Path | None = None,
    network_domains: tuple[str, ...] = (),
) -> Path:
    """Write the non-interactive config for one trusted-directory Episode."""

    config_path = codex_home_path / "config.toml"
    consent_directory = codex_home_path / "helixharness"
    gradle_home = gradle_home_path or isolated_gradle_home(codex_home_path)
    temp_directory = codex_home_path / "tmp"
    consent_directory.mkdir(parents=True, exist_ok=True)
    configure_evaluator_gradle_home(gradle_home)
    temp_directory.mkdir(parents=True, exist_ok=True)
    network_domain_pairs = ", ".join(f'{json.dumps(domain)} = "allow"' for domain in network_domains)
    network_domain_rule = f"domains = {{ {network_domain_pairs} }}\n" if network_domain_pairs else ""
    config_path.write_text(
        '[history]\npersistence = "none"\n\n'
        '[otel]\nexporter = "none"\nlog_user_prompt = false\n\n'
        "[features]\n"
        "multi_agent = true\n"
        "multi_agent_v2 = true\n\n"
        "[features.network_proxy]\n"
        "enabled = true\n"
        "allow_local_binding = true\n" + network_domain_rule,
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return config_path


class CodexPollResult(TypedDict):
    """Observable result of one Codex Worker poll."""

    status: str
    job_id: str | None


@dataclass(frozen=True)
class CodexExecutionEvent:
    """One credential-safe normalized Codex SDK event."""

    event_type: str
    category: str
    status: str
    summary: str
    payload: dict[str, Any]
    occurred_at_ms: int | None


@dataclass(frozen=True)
class FrozenWorkflowStep:
    """One public CHAIN workflow command and its externally visible milestone."""

    step_id: str
    command: str
    milestone_id: str
    artifact_contract: str | None = None
    stage_semantics: str | None = None


@dataclass(frozen=True)
class FrozenWorkflowClosurePolicy:
    """Task-frozen conditional transitions for normal HelixHarness closure."""

    stage_findings_prompt: str = ""
    max_stage_rework_cycles: int = 0
    goal_policy: WorkflowGoalPolicy | None = None


@dataclass(frozen=True)
class FrozenWorkflowClosureResume:
    """Durable continuation point inside one task-frozen closure transition."""

    origin_step: FrozenWorkflowStep
    next_input_kind: str
    stage_rework_count: int = 0
    goal_archive_attempts: int = 0
    goal_inferred_actions: int = 0


@dataclass(frozen=True)
class CodexExecutionRequest:
    """Frozen inputs passed to one fresh Codex SDK thread."""

    prompt: str
    workspace_path: Path
    codex_home_path: Path
    helixharness_snapshot_path: Path
    model: str
    reasoning_effort: str
    permission_mode: str
    wall_timeout_seconds: int
    token_budget: int
    tool_call_budget: int
    disk_budget_mb: int
    token_budget_enforced: bool = False
    universal_user_response: str = ""
    max_business_clarifications: int = 0
    approval_mode: str = "deny_all"
    max_api_step_retries: int = 5
    workflow_steps: tuple[FrozenWorkflowStep, ...] = ()
    workflow_driver_steps: tuple[FrozenWorkflowStep, ...] = ()
    resumed_after_step_id: str | None = None
    workflow_closure: FrozenWorkflowClosurePolicy | None = None
    workflow_closure_resume: FrozenWorkflowClosureResume | None = None
    gradle_home_path: Path | None = None
    direct_build_network: bool = False
    on_workflow_step_completed: Callable[[tuple["CodexTurnExecutionResult", ...]], None] | None = None
    on_workflow_closure_checkpoint: Callable[
        [
            tuple["CodexTurnExecutionResult", ...],
            FrozenWorkflowClosureResume,
            tuple[FrozenWorkflowStep, ...],
        ],
        None,
    ] | None = None
    on_turn_completed: Callable[[tuple["CodexTurnExecutionResult", ...]], None] | None = None


@dataclass(frozen=True)
class CodexTurnExecutionResult:
    """One classified SDK Turn within a logical Episode."""

    turn_id: str | None
    classification: str
    input_kind: str
    input_text: str
    started_at_ms: int | None
    completed_at_ms: int | None
    duration_ms: int | None
    final_response: str | None
    usage: dict[str, int]
    error: dict[str, Any] | None
    events: tuple[CodexExecutionEvent, ...]
    workflow_step_id: str | None = None
    milestone_id: str | None = None


@dataclass(frozen=True)
class CodexExecutionResult:
    """Normalized terminal result returned by one logical Codex Episode."""

    thread_id: str | None
    turn_id: str | None
    status: str
    started_at_ms: int | None
    completed_at_ms: int | None
    duration_ms: int | None
    final_response: str | None
    usage: dict[str, int]
    error: dict[str, Any] | None
    events: tuple[CodexExecutionEvent, ...]
    turns: tuple[CodexTurnExecutionResult, ...] = ()
    clarification_count: int = 0


def checkpoint_consumed_resources(
    prior_turns: tuple[CodexTurnExecutionResult, ...],
) -> tuple[int, int, int]:
    """Return cumulative wall seconds, tokens, and completed tool calls."""

    prior_usage = prior_turns[-1].usage if prior_turns else {}
    wall_seconds = sum(turn.duration_ms or 0 for turn in prior_turns) // 1000
    tool_calls = sum(
        1
        for turn in prior_turns
        for event in turn.events
        if event.category in {"command", "tool"} and event.status == "COMPLETED"
    )
    return wall_seconds, int(prior_usage.get("total_tokens", 0)), tool_calls


def cumulative_checkpoint_turns(
    prior_turns: tuple[CodexTurnExecutionResult, ...],
    current_turns: tuple[CodexTurnExecutionResult, ...],
) -> tuple[CodexTurnExecutionResult, ...]:
    """Normalize fresh-thread usage to cumulative logical-Episode usage."""

    if not prior_turns:
        return current_turns
    prior_usage = prior_turns[-1].usage
    normalized_current = tuple(
        replace(
            turn,
            usage={
                key: int(prior_usage.get(key, 0)) + int(turn.usage.get(key, 0))
                for key in set(prior_usage) | set(turn.usage)
            },
        )
        for turn in current_turns
    )
    return prior_turns + normalized_current


def checkpoint_remaining_resources(
    runtime: dict[str, Any],
    prior_turns: tuple[CodexTurnExecutionResult, ...],
) -> tuple[int, int, int, tuple[str, int] | None]:
    """Return exact remaining budgets and any enforced exhausted dimension."""

    consumed_wall_seconds, consumed_tokens, consumed_tool_calls = checkpoint_consumed_resources(
        prior_turns
    )
    remaining_wall_seconds = int(runtime["wall_timeout_seconds"]) - consumed_wall_seconds
    remaining_tokens = int(runtime["token_budget"]) - consumed_tokens
    remaining_tool_calls = int(runtime["tool_call_budget"]) - consumed_tool_calls
    exhausted_budget = next(
        (
            (name, limit)
            for name, limit, enforced in (
                ("wall_timeout_seconds", remaining_wall_seconds, True),
                (
                    "token_budget",
                    remaining_tokens,
                    bool(runtime.get("token_budget_enforced", False)),
                ),
                ("tool_call_budget", remaining_tool_calls, True),
            )
            if enforced and limit <= 0
        ),
        None,
    )
    return (
        remaining_wall_seconds,
        remaining_tokens,
        remaining_tool_calls,
        exhausted_budget,
    )


def checkpoint_budget_exhausted_result(
    budget: str,
    limit: int,
) -> CodexExecutionResult:
    """Return a terminal result without starting another model Turn."""

    now_ms = int(time.time() * 1000)
    return CodexExecutionResult(
        thread_id=None,
        turn_id=None,
        status="BUDGET_EXHAUSTED",
        started_at_ms=now_ms,
        completed_at_ms=now_ms,
        duration_ms=0,
        final_response=None,
        usage={},
        error={
            "type": "BudgetExceeded",
            "budget": budget,
            "limit": limit,
            "message": f"Frozen {budget} exhausted before checkpoint resume",
        },
        events=(),
        turns=(),
    )


def merge_checkpoint_execution(
    prior_turns: tuple[CodexTurnExecutionResult, ...],
    result: CodexExecutionResult,
) -> CodexExecutionResult:
    """Merge a resumed Attempt into one cumulative logical execution."""

    prior_usage = prior_turns[-1].usage if prior_turns else {}
    usage = {
        key: int(prior_usage.get(key, 0)) + int(result.usage.get(key, 0))
        for key in set(prior_usage) | set(result.usage)
    }
    prior_events = tuple(event for turn in prior_turns for event in turn.events)
    return replace(
        result,
        started_at_ms=prior_turns[0].started_at_ms if prior_turns else result.started_at_ms,
        duration_ms=sum(turn.duration_ms or 0 for turn in prior_turns) + (result.duration_ms or 0),
        usage=usage,
        events=prior_events + result.events,
        turns=prior_turns + result.turns,
    )


def completed_checkpoint_execution(
    prior_turns: tuple[CodexTurnExecutionResult, ...],
) -> CodexExecutionResult:
    """Build a completed logical result without rerunning a finished workflow."""

    if not prior_turns:
        raise CodexExecutionError("Completed workflow checkpoint has no Turns")
    first = prior_turns[0]
    last = prior_turns[-1]
    return CodexExecutionResult(
        thread_id=None,
        turn_id=last.turn_id,
        status="COMPLETED",
        started_at_ms=first.started_at_ms,
        completed_at_ms=last.completed_at_ms,
        duration_ms=sum(turn.duration_ms or 0 for turn in prior_turns),
        final_response=last.final_response,
        usage=dict(last.usage),
        error=None,
        events=tuple(event for turn in prior_turns for event in turn.events),
        turns=prior_turns,
    )


@dataclass(frozen=True)
class RestoredWorkflowCheckpoint:
    """One durable stage boundary restored into a fresh Attempt."""

    last_completed_step_id: str
    milestone_id: str
    initial_tree_hash: str
    remaining_steps: tuple[FrozenWorkflowStep, ...]
    prior_turns: tuple[CodexTurnExecutionResult, ...]
    closure_resume: FrozenWorkflowClosureResume | None = None


class WorkflowCheckpointStore:
    """Persist completed stage or autonomous-Turn boundaries outside Attempts."""

    _SCHEMA_VERSION = "framework-eval-workflow-checkpoint.v2"
    _REBUILDABLE_DIRECTORY_NAMES = frozenset(
        {
            ".cache",
            ".gradle",
            ".mypy_cache",
            ".next",
            ".pytest_cache",
            ".ruff_cache",
            ".venv",
            "__pycache__",
            "build",
            "dist",
            "node_modules",
            "target",
            "venv",
        }
    )
    _CLOSURE_INPUT_KINDS = frozenset(
        {
            "HANDLE_STAGE_FINDINGS",
            "WORKFLOW_STEP_RERUN",
        }
    )
    _STAGE_COMPLETING_CLOSURE_INPUT_KINDS = frozenset(
        {
            "WORKFLOW_STEP_RERUN",
        }
    )
    _STAGE_CONTINUATION_INPUT_KINDS = _CLOSURE_INPUT_KINDS | frozenset(
        {
            "UNIVERSAL_USER_RESPONSE",
            "WORKFLOW_POLICY_RESPONSE",
            "TURN_STATUS_REPAIR",
        }
    )
    _STAGE_COMPLETING_CONTINUATION_INPUT_KINDS = (
        _STAGE_COMPLETING_CLOSURE_INPUT_KINDS
        | frozenset(
            {
                "UNIVERSAL_USER_RESPONSE",
                "WORKFLOW_POLICY_RESPONSE",
                "TURN_STATUS_REPAIR",
            }
        )
    )

    def __init__(self, root: Path) -> None:
        self.root = root

    def has_checkpoint(self, job: ClaimedJob) -> bool:
        """Return whether the Episode has at least one completed stage."""

        episode_root = self._episode_root(job.episode_id)
        if not episode_root.exists():
            return False
        return any(
            path.is_dir()
            and not path.is_symlink()
            and (path / "checkpoint.json").is_file()
            and not (path / "checkpoint.json").is_symlink()
            for pattern in (
                "[0-9][0-9]-*",
                "turn-[0-9][0-9][0-9][0-9]",
                "closure-[0-9][0-9][0-9][0-9]-*",
            )
            for path in episode_root.glob(pattern)
        )

    def save(
        self,
        job: ClaimedJob,
        *,
        workspace_path: Path,
        initial_tree_hash: str,
        workflow_steps: tuple[FrozenWorkflowStep, ...],
        turns: tuple[CodexTurnExecutionResult, ...],
    ) -> RestoredWorkflowCheckpoint:
        """Clone state only when completed Turns form a valid workflow prefix."""

        self._assert_regular_checkpoint_tree(workspace_path)
        completed_turns = tuple(
            turn
            for turn in turns
            if turn.workflow_step_id is not None and turn.classification == TurnClassification.COMPLETED.value
        )
        steps_by_id = {step.step_id: step for step in workflow_steps}
        completed_ids: list[str] = []
        for turn in completed_turns:
            step_id = str(turn.workflow_step_id)
            contracted_step = steps_by_id.get(step_id)
            if contracted_step is None:
                raise CodexExecutionError(
                    "Workflow checkpoint is not a completed stage prefix"
                )
            if turn.milestone_id != contracted_step.milestone_id:
                raise CodexExecutionError(
                    "Workflow checkpoint milestone does not match the frozen driver"
                )
            if turn.input_kind in self._STAGE_CONTINUATION_INPUT_KINDS:
                if step_id in completed_ids:
                    continue
                if (
                    turn.input_kind in self._STAGE_COMPLETING_CONTINUATION_INPUT_KINDS
                    and len(completed_ids) < len(workflow_steps)
                    and step_id == workflow_steps[len(completed_ids)].step_id
                ):
                    completed_ids.append(step_id)
                    continue
                raise CodexExecutionError(
                    "Workflow checkpoint is not a completed stage prefix"
                )
            if turn.input_kind != "WORKFLOW_STEP":
                raise CodexExecutionError("Workflow checkpoint Turn kind is invalid")
            if completed_ids and step_id == completed_ids[-1]:
                step = workflow_steps[len(completed_ids) - 1]
            elif len(completed_ids) < len(workflow_steps):
                step = workflow_steps[len(completed_ids)]
                if step_id != step.step_id:
                    raise CodexExecutionError(
                        "Workflow checkpoint is not a completed stage prefix"
                    )
                completed_ids.append(step_id)
            else:
                raise CodexExecutionError(
                    "Workflow checkpoint is not a completed stage prefix"
                )
        if not completed_ids:
            raise CodexExecutionError("Workflow checkpoint is not a completed stage prefix")
        last_index = len(completed_ids) - 1
        last_step = workflow_steps[last_index]
        self._validate_stage_artifacts(
            workspace_path,
            last_step,
        )

        episode_root = self._episode_root(job.episode_id)
        safe_step_id = re.sub(r"[^A-Za-z0-9_.-]", "_", last_step.step_id)
        checkpoint_root = episode_root / f"{last_index + 1:02d}-{safe_step_id}"
        manifest_path = checkpoint_root / "checkpoint.json"
        if not checkpoint_root.exists():
            episode_root.mkdir(parents=True, exist_ok=True)
            temporary_root = episode_root / f".tmp-{uuid.uuid4().hex}"
            temporary_root.mkdir()
            try:
                checkpoint_workspace = temporary_root / "workspace"
                self._clone_tree(workspace_path, checkpoint_workspace)
                manifest = {
                    "schema_version": self._SCHEMA_VERSION,
                    "checkpoint_kind": "WORKFLOW_STAGE",
                    "run_id": job.run_id,
                    "case_id": job.case_id,
                    "side": job.side,
                    "episode_id": job.episode_id,
                    "attempt_id": job.attempt_id,
                    "attempt_number": job.attempt_number,
                    "workflow_step_ids": [step.step_id for step in workflow_steps],
                    "workflow_steps": self._workflow_step_contract(workflow_steps),
                    "last_completed_step_id": last_step.step_id,
                    "milestone_id": last_step.milestone_id,
                    "initial_tree_hash": initial_tree_hash,
                    "workspace_tree_hash": workspace_tree_hash(checkpoint_workspace),
                    "turns": [asdict(turn) for turn in turns],
                }
                (temporary_root / "checkpoint.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    encoding="utf-8",
                )
                temporary_root.rename(checkpoint_root)
            except Exception:
                shutil.rmtree(temporary_root, ignore_errors=True)
                raise
        return self._load_checkpoint(
            manifest_path,
            workflow_steps=workflow_steps,
            expected_job=job,
        )

    def save_turn(
        self,
        job: ClaimedJob,
        *,
        workspace_path: Path,
        initial_tree_hash: str,
        turns: tuple[CodexTurnExecutionResult, ...],
    ) -> RestoredWorkflowCheckpoint:
        """Persist the latest fully completed autonomous Agent Turn."""

        self._assert_regular_checkpoint_tree(workspace_path)
        completed_turns = tuple(
            turn
            for turn in turns
            if turn.classification == TurnClassification.COMPLETED.value
        )
        if not completed_turns:
            raise CodexExecutionError("Autonomous checkpoint requires a completed Turn")
        sequence = len(completed_turns)
        checkpoint_id = f"TURN_{sequence:04d}"
        episode_root = self._episode_root(job.episode_id)
        checkpoint_root = episode_root / f"turn-{sequence:04d}"
        manifest_path = checkpoint_root / "checkpoint.json"
        if not checkpoint_root.exists():
            episode_root.mkdir(parents=True, exist_ok=True)
            temporary_root = episode_root / f".tmp-{uuid.uuid4().hex}"
            temporary_root.mkdir()
            try:
                checkpoint_workspace = temporary_root / "workspace"
                self._clone_tree(workspace_path, checkpoint_workspace)
                manifest = {
                    "schema_version": self._SCHEMA_VERSION,
                    "checkpoint_kind": "AUTONOMOUS_TURN",
                    "run_id": job.run_id,
                    "case_id": job.case_id,
                    "side": job.side,
                    "episode_id": job.episode_id,
                    "attempt_id": job.attempt_id,
                    "attempt_number": job.attempt_number,
                    "workflow_step_ids": [],
                    "workflow_steps": [],
                    "last_completed_step_id": checkpoint_id,
                    "milestone_id": "TURN_COMPLETED",
                    "initial_tree_hash": initial_tree_hash,
                    "workspace_tree_hash": workspace_tree_hash(checkpoint_workspace),
                    "turns": [asdict(turn) for turn in turns],
                }
                (temporary_root / "checkpoint.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    encoding="utf-8",
                )
                temporary_root.rename(checkpoint_root)
            except Exception:
                shutil.rmtree(temporary_root, ignore_errors=True)
                raise
        return self._load_checkpoint(
            manifest_path,
            workflow_steps=(),
            expected_job=job,
        )

    def save_closure(
        self,
        job: ClaimedJob,
        *,
        workspace_path: Path,
        initial_tree_hash: str,
        workflow_steps: tuple[FrozenWorkflowStep, ...],
        turns: tuple[CodexTurnExecutionResult, ...],
        closure_resume: FrozenWorkflowClosureResume,
        remaining_steps: tuple[FrozenWorkflowStep, ...],
    ) -> RestoredWorkflowCheckpoint:
        """Persist the next exact action inside a frozen closure transition."""

        self._assert_regular_checkpoint_tree(workspace_path)
        step_ids = tuple(step.step_id for step in workflow_steps)
        remaining_ids = tuple(step.step_id for step in remaining_steps)
        if closure_resume.origin_step not in workflow_steps:
            raise CodexExecutionError("Workflow closure origin is outside the frozen driver")
        origin_index = workflow_steps.index(closure_resume.origin_step)
        if remaining_ids != step_ids[origin_index + 1 :]:
            raise CodexExecutionError("Workflow closure suffix is outside the frozen driver")
        if (
            closure_resume.next_input_kind not in self._CLOSURE_INPUT_KINDS
            or closure_resume.stage_rework_count < 0
            or closure_resume.goal_archive_attempts < 0
            or closure_resume.goal_inferred_actions < 0
        ):
            raise CodexExecutionError("Workflow closure continuation is invalid")
        if not turns:
            raise CodexExecutionError("Workflow closure checkpoint requires a Turn")

        episode_root = self._episode_root(job.episode_id)
        safe_kind = re.sub(r"[^A-Za-z0-9_.-]", "_", closure_resume.next_input_kind)
        checkpoint_root = episode_root / f"closure-{len(turns):04d}-{safe_kind}"
        manifest_path = checkpoint_root / "checkpoint.json"
        if not checkpoint_root.exists():
            episode_root.mkdir(parents=True, exist_ok=True)
            temporary_root = episode_root / f".tmp-{uuid.uuid4().hex}"
            temporary_root.mkdir()
            try:
                checkpoint_workspace = temporary_root / "workspace"
                self._clone_tree(workspace_path, checkpoint_workspace)
                manifest = {
                    "schema_version": self._SCHEMA_VERSION,
                    "checkpoint_kind": "WORKFLOW_CLOSURE",
                    "run_id": job.run_id,
                    "case_id": job.case_id,
                    "side": job.side,
                    "episode_id": job.episode_id,
                    "attempt_id": job.attempt_id,
                    "attempt_number": job.attempt_number,
                    "workflow_step_ids": list(step_ids),
                    "workflow_steps": self._workflow_step_contract(workflow_steps),
                    "last_completed_step_id": closure_resume.origin_step.step_id,
                    "milestone_id": closure_resume.origin_step.milestone_id,
                    "initial_tree_hash": initial_tree_hash,
                    "workspace_tree_hash": workspace_tree_hash(checkpoint_workspace),
                    "turns": [asdict(turn) for turn in turns],
                    "remaining_step_ids": list(remaining_ids),
                    "closure_resume": asdict(closure_resume),
                }
                (temporary_root / "checkpoint.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    encoding="utf-8",
                )
                temporary_root.rename(checkpoint_root)
            except Exception:
                shutil.rmtree(temporary_root, ignore_errors=True)
                raise
        return self._load_checkpoint(
            manifest_path,
            workflow_steps=workflow_steps,
            expected_job=job,
        )

    @staticmethod
    def _validate_stage_artifacts(
        workspace_path: Path,
        step: FrozenWorkflowStep,
    ) -> None:
        """Require observable HelixHarness artifacts before trusting a stage."""

        tokens = shlex.split(step.command)
        if step.artifact_contract == "GENERIC_STAGE_EVIDENCE":
            if not tokens:
                raise CodexExecutionError("Workflow checkpoint command is invalid")
            return
        if len(tokens) < 2 or re.fullmatch(r"[A-Za-z0-9_.-]+", tokens[1]) is None:
            raise CodexExecutionError("Workflow checkpoint feature name is invalid")
        command_match = re.fullmatch(
            r"/[a-z][a-z0-9-]*:([a-z][a-z0-9-]*)",
            tokens[0],
        )
        if command_match is None:
            raise CodexExecutionError("Workflow checkpoint command is invalid")
        command_name = command_match.group(1)
        feature_name = tokens[1]
        active = workspace_path / "docs/specs/active" / feature_name
        completed = workspace_path / "docs/specs/completed" / feature_name
        contract = step.artifact_contract or {
            "propose": "HELIXHARNESS_PROPOSE", "apply": "HELIXHARNESS_APPLY", "archive": "HELIXHARNESS_ARCHIVE",
        }.get(command_name)
        workspace_mode = (workspace_path / "helixharness.workspace.yaml").is_file()
        if contract == "HELIXHARNESS_PROPOSE":
            names = ("spec.md",) if workspace_mode else ("spec.md", "tasks.md")
            required = tuple(active / name for name in names)
        elif contract == "HELIXHARNESS_APPLY":
            names = ("spec.md",) if workspace_mode else ("spec.md", "tasks.md", "verify-report.md")
            required = tuple(active / name for name in names)
        elif contract == "HELIXHARNESS_ARCHIVE":
            if active.exists() or not completed.is_dir():
                raise CodexExecutionError("Workflow checkpoint archive move is incomplete")
            names = ("spec.md",) if workspace_mode else ("spec.md", "tasks.md", "verify-report.md")
            required = tuple(completed / name for name in names)
        else:
            raise CodexExecutionError("Frozen workflow artifact contract is invalid")
        missing = tuple(path for path in required if not path.is_file() or path.is_symlink() or path.stat().st_size == 0)
        if missing:
            rendered = ", ".join(str(path.relative_to(workspace_path)) for path in missing)
            raise CodexExecutionError(f"Workflow checkpoint required artifacts are missing: {rendered}")

    def restore_latest(
        self,
        job: ClaimedJob,
        *,
        workflow_steps: tuple[FrozenWorkflowStep, ...],
        target_workspace: Path,
    ) -> RestoredWorkflowCheckpoint | None:
        """Restore the latest valid stage and return only the unfinished suffix."""

        episode_root = self._episode_root(job.episode_id)
        if not episode_root.exists():
            return None
        candidates = sorted(
            (
                path
                for pattern in (
                    "[0-9][0-9]-*",
                    "turn-[0-9][0-9][0-9][0-9]",
                    "closure-[0-9][0-9][0-9][0-9]-*",
                )
                for path in episode_root.glob(pattern)
                if path.is_dir() and not path.is_symlink()
            ),
            key=self._checkpoint_order,
            reverse=True,
        )
        if not candidates:
            return None
        checkpoint_root = candidates[0]
        restored = self._load_checkpoint(
            checkpoint_root / "checkpoint.json",
            workflow_steps=workflow_steps,
            expected_job=job,
        )
        if target_workspace.exists():
            raise CodexExecutionError(f"Checkpoint restore target already exists: {target_workspace}")
        checkpoint_workspace = checkpoint_root / "workspace"
        self._assert_regular_checkpoint_tree(checkpoint_workspace)
        manifest = self._manifest(checkpoint_root)
        if workspace_tree_hash(checkpoint_workspace) != manifest.get(
            "workspace_tree_hash"
        ):
            raise CodexExecutionError("Workflow checkpoint source hash mismatch")
        self._clone_tree(checkpoint_workspace, target_workspace)
        if (
            workspace_tree_hash(target_workspace)
            != manifest["workspace_tree_hash"]
        ):
            raise CodexExecutionError("Restored workflow checkpoint hash mismatch")
        return restored

    def restore_latest_from(
        self,
        *,
        source_run_id: str,
        source_case_id: str,
        source_side: str,
        source_episode_id: str,
        source_checkpoint_name: str,
        target_job: ClaimedJob,
        workflow_steps: tuple[FrozenWorkflowStep, ...],
        target_workspace: Path,
    ) -> RestoredWorkflowCheckpoint | None:
        """Seed a linked recovery Episode from a verified source checkpoint."""

        if target_job.case_id != source_case_id or target_job.side != source_side:
            raise CodexExecutionError("Checkpoint recovery target does not match the source side")
        episode_root = self._episode_root(source_episode_id)
        if not episode_root.exists():
            return None
        candidates = sorted(
            (
                path
                for pattern in (
                    "[0-9][0-9]-*",
                    "turn-[0-9][0-9][0-9][0-9]",
                    "closure-[0-9][0-9][0-9][0-9]-*",
                )
                for path in episode_root.glob(pattern)
                if path.is_dir() and not path.is_symlink()
            ),
            key=self._checkpoint_order,
            reverse=True,
        )
        if not candidates:
            return None
        checkpoint_root = candidates[0]
        if checkpoint_root.name != source_checkpoint_name:
            raise CodexExecutionError("Checkpoint recovery source has changed")
        manifest = self._manifest(checkpoint_root)
        source_job = ClaimedJob(
            job_id="checkpoint-recovery-source",
            run_id=source_run_id,
            case_id=source_case_id,
            side=source_side,
            sequence_number=target_job.sequence_number,
            run_config=target_job.run_config,
            episode_id=source_episode_id,
            attempt_id=str(manifest.get("attempt_id", "")),
            attempt_number=int(manifest.get("attempt_number", 0)),
            lease_token="checkpoint-recovery-source",
        )
        restored = self._load_checkpoint(
            checkpoint_root / "checkpoint.json",
            workflow_steps=workflow_steps,
            expected_job=source_job,
        )
        if target_workspace.exists():
            raise CodexExecutionError(
                f"Checkpoint restore target already exists: {target_workspace}"
            )
        checkpoint_workspace = checkpoint_root / "workspace"
        self._assert_regular_checkpoint_tree(checkpoint_workspace)
        if workspace_tree_hash(checkpoint_workspace) != manifest.get(
            "workspace_tree_hash"
        ):
            raise CodexExecutionError("Workflow checkpoint source hash mismatch")
        self._clone_tree(checkpoint_workspace, target_workspace)
        if workspace_tree_hash(target_workspace) != manifest.get(
            "workspace_tree_hash"
        ):
            raise CodexExecutionError("Restored workflow checkpoint hash mismatch")
        return restored

    def _load_checkpoint(
        self,
        manifest_path: Path,
        *,
        workflow_steps: tuple[FrozenWorkflowStep, ...],
        expected_job: ClaimedJob,
    ) -> RestoredWorkflowCheckpoint:
        manifest = self._manifest(manifest_path.parent)
        expected_step_contract = self._workflow_step_contract(workflow_steps)
        manifest_step_contract = manifest.get("workflow_steps")
        if (
            manifest.get("schema_version") != self._SCHEMA_VERSION
            or manifest.get("run_id") != expected_job.run_id
            or manifest.get("case_id") != expected_job.case_id
            or manifest.get("side") != expected_job.side
            or manifest.get("episode_id") != expected_job.episode_id
            or manifest.get("workflow_step_ids") != [step.step_id for step in workflow_steps]
            or manifest_step_contract != expected_step_contract
        ):
            raise CodexExecutionError("Workflow checkpoint does not match the frozen Episode")
        last_step_id = str(manifest.get("last_completed_step_id", ""))
        raw_turns = manifest.get("turns")
        if not isinstance(raw_turns, list):
            raise CodexExecutionError("Workflow checkpoint Turns are invalid")
        if manifest.get("checkpoint_kind") == "WORKFLOW_CLOSURE":
            raw_resume = manifest.get("closure_resume")
            remaining_ids = manifest.get("remaining_step_ids")
            if not isinstance(raw_resume, dict) or not isinstance(remaining_ids, list):
                raise CodexExecutionError("Workflow closure checkpoint is invalid")
            raw_origin = raw_resume.get("origin_step")
            if not isinstance(raw_origin, dict):
                raise CodexExecutionError("Workflow closure origin is invalid")
            try:
                closure_resume = FrozenWorkflowClosureResume(
                    origin_step=FrozenWorkflowStep(**raw_origin),
                    next_input_kind=str(raw_resume["next_input_kind"]),
                    stage_rework_count=int(raw_resume["stage_rework_count"]),
                    goal_archive_attempts=int(raw_resume["goal_archive_attempts"]),
                    goal_inferred_actions=int(raw_resume["goal_inferred_actions"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CodexExecutionError("Workflow closure continuation is invalid") from exc
            steps_by_id = {step.step_id: step for step in workflow_steps}
            try:
                remaining_steps = tuple(steps_by_id[str(step_id)] for step_id in remaining_ids)
            except KeyError as exc:
                raise CodexExecutionError("Workflow closure suffix is invalid") from exc
            if (
                closure_resume.origin_step not in workflow_steps
                or closure_resume.next_input_kind
                not in {
                    "HANDLE_STAGE_FINDINGS",
                    "WORKFLOW_STEP_RERUN",
                }
                or closure_resume.stage_rework_count < 0
                or closure_resume.goal_archive_attempts < 0
                or closure_resume.goal_inferred_actions < 0
            ):
                raise CodexExecutionError("Workflow closure continuation is invalid")
            origin_index = workflow_steps.index(closure_resume.origin_step)
            if remaining_steps != workflow_steps[origin_index + 1 :]:
                raise CodexExecutionError("Workflow closure suffix is invalid")
            return RestoredWorkflowCheckpoint(
                last_completed_step_id=last_step_id,
                milestone_id=str(manifest.get("milestone_id", "")),
                initial_tree_hash=str(manifest.get("initial_tree_hash", "")),
                remaining_steps=remaining_steps,
                prior_turns=tuple(self._turn_from_json(item) for item in raw_turns),
                closure_resume=closure_resume,
            )
        if not workflow_steps:
            if (
                manifest.get("checkpoint_kind") != "AUTONOMOUS_TURN"
                or re.fullmatch(r"TURN_[0-9]{4}", last_step_id) is None
            ):
                raise CodexExecutionError("Autonomous checkpoint is invalid")
            return RestoredWorkflowCheckpoint(
                last_completed_step_id=last_step_id,
                milestone_id=str(manifest.get("milestone_id", "")),
                initial_tree_hash=str(manifest.get("initial_tree_hash", "")),
                remaining_steps=(),
                prior_turns=tuple(self._turn_from_json(item) for item in raw_turns),
            )
        step_ids = tuple(step.step_id for step in workflow_steps)
        if last_step_id not in step_ids:
            raise CodexExecutionError("Workflow checkpoint step is not in the frozen driver")
        next_index = step_ids.index(last_step_id) + 1
        return RestoredWorkflowCheckpoint(
            last_completed_step_id=last_step_id,
            milestone_id=str(manifest.get("milestone_id", "")),
            initial_tree_hash=str(manifest.get("initial_tree_hash", "")),
            remaining_steps=workflow_steps[next_index:],
            prior_turns=tuple(self._turn_from_json(item) for item in raw_turns),
        )

    def _checkpoint_order(self, checkpoint_root: Path) -> tuple[int, int, str]:
        manifest = self._manifest(checkpoint_root)
        raw_turns = manifest.get("turns")
        turn_count = len(raw_turns) if isinstance(raw_turns, list) else -1
        kind_priority = 0 if manifest.get("checkpoint_kind") == "WORKFLOW_CLOSURE" else 1
        return turn_count, kind_priority, checkpoint_root.name

    def _episode_root(self, episode_id: str) -> Path:
        """Resolve one checkpoint Episode without following a mutable link root."""

        if (
            not episode_id
            or episode_id in {".", ".."}
            or Path(episode_id).name != episode_id
            or self.root.is_symlink()
        ):
            raise CodexExecutionError("Workflow checkpoint Episode directory is invalid")
        episode_root = self.root / episode_id
        if episode_root.is_symlink() or (episode_root.exists() and not episode_root.is_dir()):
            raise CodexExecutionError("Workflow checkpoint Episode directory is invalid")
        return episode_root

    @staticmethod
    def _manifest(checkpoint_root: Path) -> dict[str, Any]:
        if checkpoint_root.is_symlink() or not checkpoint_root.is_dir():
            raise CodexExecutionError("Workflow checkpoint directory is invalid")
        manifest_path = checkpoint_root / "checkpoint.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise CodexExecutionError("Workflow checkpoint manifest is invalid")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexExecutionError("Workflow checkpoint manifest is invalid") from exc
        if not isinstance(manifest, dict):
            raise CodexExecutionError("Workflow checkpoint manifest must be an object")
        return manifest

    @staticmethod
    def _workflow_step_contract(
        workflow_steps: tuple[FrozenWorkflowStep, ...],
    ) -> list[dict[str, Any]]:
        return [asdict(step) for step in workflow_steps]

    @staticmethod
    def _assert_regular_checkpoint_tree(root: Path) -> None:
        """Reject links and special files before a checkpoint tree is read."""

        if root.is_symlink() or not root.is_dir():
            raise CodexExecutionError("Workflow checkpoint workspace is invalid")
        for current_root, directory_names, file_names in os.walk(root, followlinks=False):
            current = Path(current_root)
            directory_names[:] = [
                name
                for name in directory_names
                if name not in WorkflowCheckpointStore._REBUILDABLE_DIRECTORY_NAMES
            ]
            for name in (*directory_names, *file_names):
                path = current / name
                try:
                    mode = path.lstat().st_mode
                except OSError as exc:
                    raise CodexExecutionError("Workflow checkpoint workspace is invalid") from exc
                if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise CodexExecutionError(
                        "Workflow checkpoint workspace contains a link or special file"
                    )

    @staticmethod
    def _turn_from_json(value: Any) -> CodexTurnExecutionResult:
        if not isinstance(value, dict):
            raise CodexExecutionError("Workflow checkpoint Turn is invalid")
        raw_events = value.get("events", [])
        if not isinstance(raw_events, list):
            raise CodexExecutionError("Workflow checkpoint Turn events are invalid")
        events = tuple(CodexExecutionEvent(**event) for event in raw_events if isinstance(event, dict))
        return CodexTurnExecutionResult(
            **{key: item for key, item in value.items() if key != "events"},
            events=events,
        )

    @staticmethod
    def _clone_tree(source: Path, target: Path) -> None:
        shutil.copytree(
            source,
            target,
            ignore=lambda _directory, names: [
                name
                for name in names
                if name in WorkflowCheckpointStore._REBUILDABLE_DIRECTORY_NAMES
            ],
        )


class CodexSdkAdapter(Protocol):
    """Replaceable seam used by tests and the real Codex implementation."""

    def execute(self, request: CodexExecutionRequest) -> CodexExecutionResult:
        """Execute one fresh isolated thread."""


class SnapshotInstaller(Protocol):
    """Install exactly one frozen HelixHarness snapshot into a fresh home."""

    def install(self, snapshot_path: Path, codex_home_path: Path) -> None:
        """Install the snapshot without consulting personal Codex state."""


class TelemetryConfigurator(Protocol):
    """Preconfigure HelixHarness usage telemetry without Agent interaction."""

    def disable(
        self,
        snapshot_path: Path,
        workspace_path: Path,
        codex_home_path: Path,
    ) -> Path:
        """Persist disabled consent through the frozen Snapshot interface."""


class CodexPythonSdkAdapter:
    """Production adapter for the pinned ``openai-codex`` Python package."""

    def __init__(
        self,
        *,
        codex_factory: Callable[[Any], ContextManager[Any]] | None = None,
        turn_classifier: FrozenTurnClassifier | None = None,
        provider_config: EvaluationProviderConfig | None = None,
    ) -> None:
        self._codex_factory = codex_factory
        self._turn_classifier = turn_classifier or FrozenTurnClassifier()
        self._provider_config = provider_config

    def execute(self, request: CodexExecutionRequest) -> CodexExecutionResult:
        """Start a persisted execution thread and normalize its structured event stream."""

        from codex_cli_bin import bundled_codex_path, bundled_path_dir
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

        codex_factory = self._codex_factory or Codex
        if request.permission_mode != "workspace-write":
            raise CodexExecutionError(f"Unsupported Codex permission mode: {request.permission_mode}")
        approval_mode = {
            "deny_all": ApprovalMode.deny_all,
        }.get(request.approval_mode)
        if approval_mode is None:
            raise CodexExecutionError(f"Unsupported Codex approval mode: {request.approval_mode}")

        started_wall_ms = int(time.time() * 1000)
        thread_id: str | None = None
        turn_id: str | None = None
        events: list[CodexExecutionEvent] = []
        turns: list[CodexTurnExecutionResult] = []
        final_response: str | None = None
        usage: dict[str, int] = {}
        completed_at_ms: int | None = None
        duration_ms: int | None = None
        budget_error: dict[str, Any] | None = None
        budget_lock = threading.Lock()
        timeout_timer: threading.Timer | None = None
        active_turn: Any | None = None
        clarification_count = 0
        workflow_policy_response_count = 0
        terminal_result: CodexExecutionResult | None = None
        protocol_repair_count = 0

        def finish_episode(**kwargs: Any) -> CodexExecutionResult:
            nonlocal terminal_result
            terminal_result = self._episode_result(**kwargs)
            return terminal_result
        try:
            sdk_environment = {
                "CODEX_HOME": str(request.codex_home_path),
                "CODEX_SQLITE_HOME": str(request.codex_home_path),
            }
            if request.gradle_home_path is not None:
                sdk_environment["HELIXHARNESS_EVAL_GRADLE_HOME"] = str(request.gradle_home_path)
            if os.environ.get("JAVA_HOME"):
                sdk_environment["JAVA_HOME"] = os.environ["JAVA_HOME"]
            thread_config: dict[str, Any] = {}
            model_provider: str | None = None
            if self._provider_config is not None:
                provider = self._provider_config
                if not provider.api_key or not provider.base_url:
                    raise CodexExecutionError("Codex evaluation provider is not configured")
                sdk_environment["OPENAI_API_KEY"] = provider.api_key
                model_provider = "evaluation-proxy"
                thread_config["model_providers"] = {
                    model_provider: {
                        "base_url": codex_responses_base_url(provider.base_url),
                        "env_key": "OPENAI_API_KEY",
                        "name": "Evaluation proxy",
                        # Turn-start retries are owned by _start_turn_with_retries so
                        # one API operation never nests two independent retry loops.
                        "request_max_retries": 0,
                        "stream_max_retries": request.max_api_step_retries,
                        "wire_api": "responses",
                    }
                }
            bundled_tools = bundled_path_dir()
            if bundled_tools is not None:
                sdk_environment["PATH"] = os.pathsep.join(
                    [
                        str(bundled_tools),
                        os.environ.get("PATH", ""),
                    ]
                ).rstrip(os.pathsep)
            config = CodexConfig(
                cwd=str(request.workspace_path),
                env=sdk_environment,
                launch_args_override=(
                    sys.executable,
                    "-I",
                    str(Path(__file__).with_name("codex_runtime_launcher.py")),
                    str(bundled_codex_path()),
                    "--config",
                    'history.persistence="none"',
                    "--config",
                    ("features.network_proxy=false" if request.direct_build_network else "features.network_proxy=true"),
                    # Each Episode already installs one frozen local snapshot. Remote discovery
                    # would add an unrelated network clone to every isolated CODEX_HOME.
                    "--config",
                    "features.remote_plugin=false",
                    "app-server",
                    "--listen",
                    "stdio://",
                ),
            )
            with codex_factory(config) as codex:
                thread_options: dict[str, Any] = {
                    "approval_mode": approval_mode,
                    "config": thread_config,
                    "cwd": str(request.workspace_path),
                    # Evaluation agents may invoke collaboration tools at runtime. Codex can
                    # fail to register ephemeral roots with the collaboration router, so every
                    # full execution uses an attempt-isolated persisted root thread.
                    "ephemeral": False,
                    "model": request.model,
                    "sandbox": Sandbox.full_access,
                }
                if model_provider is not None:
                    thread_options["model_provider"] = model_provider
                thread = codex.thread_start(
                    **thread_options,
                )
                thread_id = str(thread.id)
                tool_calls = 0

                def stop_for_budget(kind: str, limit: int) -> None:
                    nonlocal budget_error
                    with budget_lock:
                        if budget_error is not None:
                            return
                        budget_error = {
                            "type": "BudgetExceeded",
                            "budget": kind,
                            "limit": limit,
                            "message": f"{kind} budget exceeded",
                        }
                    if active_turn is not None:
                        try:
                            active_turn.interrupt()
                        except Exception:
                            return

                timeout_timer = threading.Timer(
                    request.wall_timeout_seconds,
                    stop_for_budget,
                    args=("wall_timeout_seconds", request.wall_timeout_seconds),
                )
                timeout_timer.daemon = True
                timeout_timer.start()
                runtime_workflow_steps = request.workflow_steps
                workflow_driver_steps = (
                    request.workflow_driver_steps or runtime_workflow_steps
                )
                turn_input = (
                    workflow_bootstrap_prompt(request.prompt)
                    if runtime_workflow_steps or workflow_driver_steps
                    else initial_turn_prompt(request.prompt)
                )
                turn_input_kind = "INITIAL_REQUEST"
                workflow_step_index = 0
                active_workflow_step: FrozenWorkflowStep | None = None
                closure_origin_step: FrozenWorkflowStep | None = None
                stage_rework_count = 0
                goal_progress = WorkflowGoalProgress()
                pending_closure_resume = request.workflow_closure_resume
                if pending_closure_resume is not None and request.workflow_closure is None:
                    raise CodexExecutionError(
                        "Workflow closure resume requires its frozen policy"
                    )
                if (
                    request.resumed_after_step_id is not None
                    and runtime_workflow_steps
                    and pending_closure_resume is None
                ):
                    active_workflow_step = runtime_workflow_steps[0]
                    workflow_step_index = 1
                    turn_input = resumed_workflow_step_prompt(
                        request.prompt,
                        active_workflow_step.command,
                        request.resumed_after_step_id,
                    )
                    turn_input_kind = "WORKFLOW_STEP"
                if pending_closure_resume is not None:
                    stage_rework_count = pending_closure_resume.stage_rework_count
                    goal_progress = WorkflowGoalProgress(
                        archive_attempts=pending_closure_resume.goal_archive_attempts,
                        inferred_actions=pending_closure_resume.goal_inferred_actions,
                    )

                def schedule_goal_decision(
                    decision: WorkflowGoalDecision,
                    *,
                    fallback_step: FrozenWorkflowStep,
                ) -> None:
                    """Schedule the first exact action from one inferred frozen plan."""

                    nonlocal active_workflow_step
                    nonlocal closure_origin_step
                    nonlocal goal_progress
                    nonlocal turn_input
                    nonlocal turn_input_kind
                    nonlocal stage_rework_count

                    closure = request.workflow_closure
                    policy = closure.goal_policy if closure is not None else None
                    if policy is None or decision.status != "EXECUTE" or not decision.actions:
                        raise CodexExecutionError("Workflow goal decision cannot be scheduled")
                    goal_progress = decision.progress
                    first_action = decision.actions[0]
                    frozen_action = policy.action(first_action)
                    origin_step = self._goal_action_step(
                        workflow_driver_steps,
                        action=first_action,
                        action_input=frozen_action.input,
                        fallback_step=fallback_step,
                    )
                    self._record_goal_decision(
                        events=events,
                        turns=turns,
                        decision=decision,
                        workflow_step=origin_step,
                    )
                    closure_origin_step = origin_step
                    remaining_steps = self._remaining_workflow_steps(
                        workflow_driver_steps,
                        origin_step,
                    )
                    if first_action == "ARCHIVE":
                        active_workflow_step = origin_step
                        turn_input = frozen_action.input
                        turn_input_kind = "WORKFLOW_STEP_RERUN"
                    else:
                        raise CodexExecutionError("Workflow goal selected an unsupported action")
                    self._save_closure_checkpoint(
                        request=request,
                        turns=turns,
                        origin_step=origin_step,
                        next_input_kind=turn_input_kind,
                        goal_progress=goal_progress,
                        remaining_steps=remaining_steps,
                        stage_rework_count=stage_rework_count,
                    )
                while True:
                    turn_events: list[CodexExecutionEvent] = []
                    turn_started_at_ms: int | None = None
                    turn_error: dict[str, Any] | None = None
                    final_response = None
                    completed_structured_response: str | None = None
                    workflow_checkpoint_saved = False
                    active_turn, retry_events = self._start_turn_with_retries(
                        thread,
                        turn_input,
                        reasoning_effort=request.reasoning_effort,
                        max_retries=request.max_api_step_retries,
                    )
                    events.extend(retry_events)
                    turn_events.extend(retry_events)
                    turn_id = str(active_turn.id)
                    try:
                        for notification in active_turn.stream():
                            event = self._normalize_notification(notification)
                            events.append(event)
                            turn_events.append(event)
                            payload = self._payload_dict(notification.payload)
                            if event.category == "message":
                                item = payload.get("item")
                                if isinstance(item, dict) and item.get("type") == "agentMessage":
                                    text = item.get("text")
                                    if isinstance(text, str):
                                        final_response = text
                                        if (
                                            notification.method == "item/completed"
                                            and self._has_structured_turn_status(text)
                                        ):
                                            completed_structured_response = text
                            if event.category == "usage":
                                usage = self._usage(payload)
                                if (
                                    request.token_budget_enforced
                                    and usage.get("total_tokens", 0) > request.token_budget
                                ):
                                    stop_for_budget("token_budget", request.token_budget)
                            if event.event_type == "item/completed" and event.category in {"command", "tool"}:
                                tool_calls += 1
                                if tool_calls > request.tool_call_budget:
                                    stop_for_budget(
                                        "tool_call_budget",
                                        request.tool_call_budget,
                                    )
                                if (
                                    sum(
                                        self._directory_size_bytes(path)
                                        for path in (
                                            request.workspace_path,
                                            request.codex_home_path,
                                        )
                                    )
                                    > request.disk_budget_mb * 1024 * 1024
                                ):
                                    stop_for_budget(
                                        "disk_budget_mb",
                                        request.disk_budget_mb,
                                    )
                            if notification.method == "turn/completed":
                                turn_payload = payload.get("turn")
                                if isinstance(turn_payload, dict):
                                    turn_started_at_ms = self._timestamp_ms(
                                        turn_payload,
                                        "startedAt",
                                        "startedAtMs",
                                    )
                                    completed_at_ms = self._timestamp_ms(
                                        turn_payload,
                                        "completedAt",
                                        "completedAtMs",
                                    )
                                    duration = turn_payload.get("durationMs")
                                    duration_ms = int(duration) if duration is not None else None
                                    raw_error = turn_payload.get("error")
                                    raw_status = str(turn_payload.get("status", "")).lower()
                                    if isinstance(raw_error, dict):
                                        turn_error = raw_error
                                    elif raw_status == "failed":
                                        turn_error = {
                                            "type": "TurnFailed",
                                            "message": "Codex turn failed",
                                        }
                    except Exception as exc:
                        if not (
                            completed_structured_response is not None
                            and self._is_transient_sdk_error(exc)
                        ):
                            raise
                        final_response = completed_structured_response
                        completed_at_ms = int(time.time() * 1000)
                        turn_started_at_ms = turn_started_at_ms or started_wall_ms
                        duration_ms = max(0, completed_at_ms - turn_started_at_ms)
                        recovered_event = CodexExecutionEvent(
                            event_type="stream/disconnect-after-final",
                            category="lifecycle",
                            status="RECOVERED",
                            summary="Codex stream disconnected after a structured final response",
                            payload={"error_type": type(exc).__name__},
                            occurred_at_ms=completed_at_ms,
                        )
                        events.append(recovered_event)
                        turn_events.append(recovered_event)

                    if (
                        turn_error is not None
                        and self._has_structured_turn_status(final_response)
                        and self._is_transient_sdk_error(turn_error)
                    ):
                        recovered_event = CodexExecutionEvent(
                            event_type="turn/disconnect-after-final",
                            category="lifecycle",
                            status="RECOVERED",
                            summary="Codex turn reported a transient disconnect after a structured final response",
                            payload={
                                "error_type": str(turn_error.get("type") or "TurnFailed"),
                            },
                            occurred_at_ms=completed_at_ms,
                        )
                        events.append(recovered_event)
                        turn_events.append(recovered_event)
                        turn_error = None

                    if budget_error is not None:
                        budget_event = CodexExecutionEvent(
                            event_type="budget/exceeded",
                            category="error",
                            status="FAILED",
                            summary=str(budget_error["message"]),
                            payload=budget_error,
                            occurred_at_ms=int(time.time() * 1000),
                        )
                        events.append(budget_event)
                        turn_events.append(budget_event)
                        turns.append(
                            CodexTurnExecutionResult(
                                turn_id=turn_id,
                                classification=TurnClassification.FAILED.value,
                                input_kind=turn_input_kind,
                                input_text=turn_input,
                                started_at_ms=turn_started_at_ms,
                                completed_at_ms=completed_at_ms,
                                duration_ms=duration_ms,
                                final_response=final_response,
                                usage=usage,
                                error=budget_error,
                                events=tuple(turn_events),
                                workflow_step_id=(active_workflow_step.step_id if active_workflow_step else None),
                                milestone_id=(active_workflow_step.milestone_id if active_workflow_step else None),
                            )
                        )
                        return finish_episode(
                            status="BUDGET_EXHAUSTED",
                            thread_id=thread_id,
                            started_at_ms=started_wall_ms,
                            turns=turns,
                            clarification_count=clarification_count,
                            error=budget_error,
                        )

                    sdk_status = "FAILED" if turn_error is not None else "COMPLETED"
                    turn_status_missing = (
                        sdk_status == "COMPLETED"
                        and turn_error is None
                        and not has_valid_turn_status(final_response)
                    )
                    classification = self._turn_classifier.classify(
                        sdk_status=sdk_status,
                        final_response=final_response,
                        error=turn_error,
                    )
                    recommended_stage_recovery = None
                    force_stage_recovery = False
                    workflow_policy_response = _deterministic_workflow_policy_response(
                        active_workflow_step,
                        final_response,
                        universal_response=request.universal_user_response,
                    )
                    workflow_policy_exhausted = (
                        workflow_policy_response is not None
                        and workflow_policy_response_count
                        >= max(1, request.max_business_clarifications)
                    )
                    if workflow_policy_response is not None:
                        policy_event = CodexExecutionEvent(
                            event_type="workflow/policy-response",
                            category="workflow",
                            status=(
                                "FAILED" if workflow_policy_exhausted else "TRANSITIONED"
                            ),
                            summary=(
                                "Deterministic workflow policy response limit exhausted"
                                if workflow_policy_exhausted
                                else "Resolved a non-business workflow choice from the frozen Run policy"
                            ),
                            payload={
                                "workflow_step_id": (
                                    active_workflow_step.step_id
                                    if active_workflow_step is not None
                                    else None
                                ),
                                "policy_response_number": workflow_policy_response_count + 1,
                            },
                            occurred_at_ms=completed_at_ms,
                        )
                        events.append(policy_event)
                        turn_events.append(policy_event)
                    if (
                        active_workflow_step is not None
                        and turn_input_kind
                        in {
                            "WORKFLOW_STEP",
                            "WORKFLOW_STEP_RERUN",
                            "UNIVERSAL_USER_RESPONSE",
                            "WORKFLOW_POLICY_RESPONSE",
                            "TURN_STATUS_REPAIR",
                        }
                    ):
                        is_workflow_routable_stage = (
                            active_workflow_step.artifact_contract
                            in {"HELIXHARNESS_APPLY", "HELIXHARNESS_ARCHIVE"}
                        )
                        anchored_workflow_recommendation = (
                            is_workflow_routable_stage
                            and bool(
                                _anchored_helixharness_recommendations(final_response)
                            )
                        )
                        if (
                            classification is TurnClassification.FAILED
                            or is_workflow_routable_stage
                        ):
                            recommended_stage_recovery = (
                                _deterministic_frozen_stage_recommendation(
                                    workflow_driver_steps,
                                    active_workflow_step,
                                    final_response,
                                )
                            )
                        force_stage_recovery = (
                            recommended_stage_recovery is not None
                            or anchored_workflow_recommendation
                        )
                        if force_stage_recovery:
                            classification = TurnClassification.FAILED
                            recovery_step = (
                                recommended_stage_recovery or active_workflow_step
                            )
                            recommendation_event = CodexExecutionEvent(
                                event_type="workflow/stage-recommendation",
                                category="workflow",
                                status=(
                                    "TRANSITIONED"
                                    if recommended_stage_recovery is not None
                                    else "FAILED"
                                ),
                                summary=(
                                    "Router selected an exact frozen recovery stage"
                                    if recommended_stage_recovery is not None
                                    else "Router recommendation did not select exactly one frozen stage"
                                ),
                                payload={
                                    "failed_workflow_step_id": active_workflow_step.step_id,
                                    "recovery_workflow_step_id": recovery_step.step_id,
                                    "recommendation_resolved": (
                                        recommended_stage_recovery is not None
                                    ),
                                },
                                occurred_at_ms=completed_at_ms,
                            )
                            events.append(recommendation_event)
                            turn_events.append(recommendation_event)
                    if (
                        classification is TurnClassification.COMPLETED
                        and active_workflow_step is not None
                        and turn_input_kind
                        in {
                            "WORKFLOW_STEP",
                            "WORKFLOW_STEP_RERUN",
                            "UNIVERSAL_USER_RESPONSE",
                            "WORKFLOW_POLICY_RESPONSE",
                            "TURN_STATUS_REPAIR",
                        }
                        and request.on_workflow_step_completed is not None
                    ):
                        provisional_turn = CodexTurnExecutionResult(
                            turn_id=turn_id,
                            classification=TurnClassification.COMPLETED.value,
                            input_kind=turn_input_kind,
                            input_text=turn_input,
                            started_at_ms=turn_started_at_ms,
                            completed_at_ms=completed_at_ms,
                            duration_ms=duration_ms,
                            final_response=final_response,
                            usage=usage,
                            error=turn_error,
                            events=tuple(turn_events),
                            workflow_step_id=active_workflow_step.step_id,
                            milestone_id=active_workflow_step.milestone_id,
                        )
                        try:
                            request.on_workflow_step_completed(
                                (*turns, provisional_turn),
                            )
                        except CodexExecutionError as exc:
                            classification = TurnClassification.FAILED
                            validation_event = CodexExecutionEvent(
                                event_type="stage/validation",
                                category="validation",
                                status="FAILED",
                                summary=str(exc),
                                payload={
                                    "workflow_step_id": active_workflow_step.step_id,
                                    "milestone_id": active_workflow_step.milestone_id,
                                    "error_type": type(exc).__name__,
                                    "error": str(exc),
                                },
                                occurred_at_ms=completed_at_ms,
                            )
                            events.append(validation_event)
                            turn_events.append(validation_event)
                        else:
                            workflow_checkpoint_saved = True
                    classifier_event = CodexExecutionEvent(
                        event_type="turn/classified",
                        category="classification",
                        status=classification.value,
                        summary=classification.value,
                        payload={"classification": classification.value},
                        occurred_at_ms=completed_at_ms,
                    )
                    events.append(classifier_event)
                    turn_events.append(classifier_event)
                    turns.append(
                        CodexTurnExecutionResult(
                            turn_id=turn_id,
                            classification=classification.value,
                            input_kind=turn_input_kind,
                            input_text=turn_input,
                            started_at_ms=turn_started_at_ms,
                            completed_at_ms=completed_at_ms,
                            duration_ms=duration_ms,
                            final_response=final_response,
                            usage=usage,
                            error=turn_error,
                            events=tuple(turn_events),
                            workflow_step_id=(active_workflow_step.step_id if active_workflow_step else None),
                            milestone_id=(active_workflow_step.milestone_id if active_workflow_step else None),
                        )
                    )
                    if workflow_policy_exhausted:
                        return finish_episode(
                            status="FAILED",
                            thread_id=thread_id,
                            started_at_ms=started_wall_ms,
                            turns=turns,
                            clarification_count=clarification_count,
                            error=None,
                        )
                    if workflow_policy_response is not None:
                        workflow_policy_response_count += 1
                        protocol_repair_count = 0
                        turn_input = workflow_policy_response
                        turn_input_kind = "WORKFLOW_POLICY_RESPONSE"
                        continue
                    if turn_status_missing and protocol_repair_count < 1:
                        protocol_repair_count += 1
                        turn_input = TURN_STATUS_REPAIR_PROMPT
                        turn_input_kind = "TURN_STATUS_REPAIR"
                        continue
                    if not turn_status_missing:
                        protocol_repair_count = 0
                    if turn_error is not None:
                        return finish_episode(
                            status="FAILED",
                            thread_id=thread_id,
                            started_at_ms=started_wall_ms,
                            turns=turns,
                            clarification_count=clarification_count,
                            error=turn_error,
                        )
                    if classification is TurnClassification.COMPLETED:
                        if (
                            active_workflow_step is not None
                            and request.on_workflow_step_completed is not None
                            and not workflow_checkpoint_saved
                        ):
                            request.on_workflow_step_completed(tuple(turns))
                        if request.on_turn_completed is not None:
                            request.on_turn_completed(tuple(turns))
                        if turn_input_kind == "HANDLE_STAGE_FINDINGS":
                            if request.workflow_closure is None or closure_origin_step is None:
                                raise CodexExecutionError("Stage recovery lost its frozen origin")
                            active_workflow_step = closure_origin_step
                            turn_input = closure_origin_step.command
                            turn_input_kind = "WORKFLOW_STEP_RERUN"
                            self._save_closure_checkpoint(
                                request=request,
                                turns=turns,
                                origin_step=closure_origin_step,
                                next_input_kind=turn_input_kind,
                                goal_progress=goal_progress,
                                remaining_steps=tuple(
                                    runtime_workflow_steps[workflow_step_index:]
                                ),
                                stage_rework_count=stage_rework_count,
                            )
                            continue
                        if turn_input_kind == "WORKFLOW_STEP_RERUN":
                            closure_origin_step = None
                            stage_rework_count = _stage_rework_count_after_rerun(
                                stage_rework_count,
                                runtime_workflow_steps,
                                workflow_step_index,
                            )
                        if (
                            active_workflow_step is not None
                            and self._workflow_command_name(active_workflow_step) == "archive"
                            and request.workflow_closure is not None
                        ):
                            self._record_closure_transition(
                                events=events,
                                turns=turns,
                                transition="ARCHIVE_PASSED",
                                actions=(),
                                status="COMPLETED",
                                workflow_step=active_workflow_step,
                            )
                        if (
                            turn_input_kind == "INITIAL_REQUEST"
                            and pending_closure_resume is not None
                        ):
                            closure_origin_step = pending_closure_resume.origin_step
                            next_kind = pending_closure_resume.next_input_kind
                            pending_closure_resume = None
                            feature_name = self._workflow_feature_name(closure_origin_step)
                            if feature_name is None or request.workflow_closure is None:
                                raise CodexExecutionError(
                                    "Workflow closure resume feature is invalid"
                                )
                            if next_kind == "HANDLE_STAGE_FINDINGS":
                                active_workflow_step = None
                                turn_input = self._stage_findings_input(
                                    request.workflow_closure.stage_findings_prompt,
                                    turns,
                                )
                            elif next_kind == "WORKFLOW_STEP_RERUN":
                                active_workflow_step = closure_origin_step
                                turn_input = closure_origin_step.command
                            else:
                                raise CodexExecutionError(
                                    "Workflow closure resume action is invalid"
                                )
                            turn_input_kind = next_kind
                            continue
                        if workflow_step_index < len(runtime_workflow_steps):
                            next_workflow_step = runtime_workflow_steps[workflow_step_index]
                            workflow_step_index += 1
                            active_workflow_step = next_workflow_step

                            turn_input = active_workflow_step.command
                            turn_input_kind = "WORKFLOW_STEP"
                            continue
                        if (
                            request.workflow_closure is not None
                            and request.workflow_closure.goal_policy is not None
                            and workflow_driver_steps
                        ):
                            goal_step = active_workflow_step or workflow_driver_steps[-1]
                            decision = infer_next_workflow_actions(
                                request.workflow_closure.goal_policy,
                                self._workflow_goal_observation(
                                    request.workspace_path,
                                    goal_step,
                                ),
                                goal_progress,
                            )
                            if decision.status == "EXECUTE":
                                schedule_goal_decision(
                                    decision,
                                    fallback_step=goal_step,
                                )
                                continue
                            if decision.status == "STOP":
                                self._record_goal_decision(
                                    events=events,
                                    turns=turns,
                                    decision=decision,
                                    workflow_step=goal_step,
                                )
                                return finish_episode(
                                    status="FAILED",
                                    thread_id=thread_id,
                                    started_at_ms=started_wall_ms,
                                    turns=turns,
                                    clarification_count=clarification_count,
                                    error=None,
                                )
                        return finish_episode(
                            status="COMPLETED",
                            thread_id=thread_id,
                            started_at_ms=started_wall_ms,
                            turns=turns,
                            clarification_count=clarification_count,
                            error=None,
                        )
                    if classification is TurnClassification.FAILED:
                        if (
                            turn_input_kind == "HANDLE_STAGE_FINDINGS"
                            and closure_origin_step is not None
                            and request.workflow_closure is not None
                        ):
                            active_workflow_step = closure_origin_step
                            turn_input = closure_origin_step.command
                            turn_input_kind = "WORKFLOW_STEP_RERUN"
                            self._save_closure_checkpoint(
                                request=request,
                                turns=turns,
                                origin_step=closure_origin_step,
                                next_input_kind=turn_input_kind,
                                goal_progress=goal_progress,
                                remaining_steps=tuple(
                                    runtime_workflow_steps[workflow_step_index:]
                                ),
                                stage_rework_count=stage_rework_count,
                            )
                            continue
                        if (
                            active_workflow_step is not None
                            and self._workflow_command_name(active_workflow_step) == "archive"
                            and request.workflow_closure is not None
                            and request.workflow_closure.goal_policy is not None
                            and not force_stage_recovery
                        ):
                            decision = infer_next_workflow_actions(
                                request.workflow_closure.goal_policy,
                                self._workflow_goal_observation(
                                    request.workspace_path,
                                    active_workflow_step,
                                ),
                                goal_progress,
                            )
                            if (
                                decision.status == "EXECUTE"
                                and not _repeats_failed_archive_without_recovery(decision)
                            ):
                                schedule_goal_decision(
                                    decision,
                                    fallback_step=active_workflow_step,
                                )
                                continue
                            if decision.status == "STOP":
                                self._record_goal_decision(
                                    events=events,
                                    turns=turns,
                                    decision=decision,
                                    workflow_step=active_workflow_step,
                                )
                                return finish_episode(
                                    status="FAILED",
                                    thread_id=thread_id,
                                    started_at_ms=started_wall_ms,
                                    turns=turns,
                                    clarification_count=clarification_count,
                                    error=None,
                                )
                        if (
                            active_workflow_step is not None
                            and request.workflow_closure is not None
                            and request.workflow_closure.max_stage_rework_cycles > 0
                            and request.workflow_closure.stage_findings_prompt.strip()
                            and turn_input_kind
                            in {
                                "WORKFLOW_STEP",
                                "WORKFLOW_STEP_RERUN",
                                "UNIVERSAL_USER_RESPONSE",
                                "WORKFLOW_POLICY_RESPONSE",
                                "TURN_STATUS_REPAIR",
                            }
                        ):
                            if stage_rework_count >= request.workflow_closure.max_stage_rework_cycles:
                                self._record_closure_transition(
                                    events=events,
                                    turns=turns,
                                    transition="STAGE_FAILED",
                                    actions=(),
                                    status="FAILED",
                                    workflow_step=active_workflow_step,
                                    cycle=stage_rework_count,
                                    reason="STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED",
                                )
                            else:
                                stage_rework_count += 1
                                self._record_closure_transition(
                                    events=events,
                                    turns=turns,
                                    transition="STAGE_FAILED",
                                    actions=("HANDLE_STAGE_FINDINGS", "RERUN_STAGE"),
                                    status="TRANSITIONED",
                                    workflow_step=active_workflow_step,
                                    cycle=stage_rework_count,
                                )
                                closure_origin_step = (
                                    recommended_stage_recovery
                                    or _recommended_frozen_stage_recovery(
                                        workflow_driver_steps,
                                        active_workflow_step,
                                        turns[-1].final_response,
                                    )
                                )
                                recovery_suffix = self._remaining_workflow_steps(
                                    workflow_driver_steps,
                                    closure_origin_step,
                                )
                                if closure_origin_step not in runtime_workflow_steps:
                                    runtime_workflow_steps = recovery_suffix
                                workflow_step_index = _workflow_recovery_suffix_index(
                                    runtime_workflow_steps,
                                    closure_origin_step,
                                    workflow_step_index,
                                )
                                self._save_closure_checkpoint(
                                    request=request,
                                    turns=turns,
                                    origin_step=closure_origin_step,
                                    next_input_kind="HANDLE_STAGE_FINDINGS",
                                    goal_progress=goal_progress,
                                    remaining_steps=recovery_suffix,
                                    stage_rework_count=stage_rework_count,
                                )
                                active_workflow_step = None
                                turn_input = self._stage_findings_input(
                                    request.workflow_closure.stage_findings_prompt,
                                    turns,
                                )
                                turn_input_kind = "HANDLE_STAGE_FINDINGS"
                                continue
                        return finish_episode(
                            status="FAILED",
                            thread_id=thread_id,
                            started_at_ms=started_wall_ms,
                            turns=turns,
                            clarification_count=clarification_count,
                            error=None,
                        )
                    if turn_input_kind == "HANDLE_STAGE_FINDINGS":
                        return finish_episode(
                            status="FAILED",
                            thread_id=thread_id,
                            started_at_ms=started_wall_ms,
                            turns=turns,
                            clarification_count=clarification_count,
                            error=None,
                        )
                    if clarification_count >= request.max_business_clarifications:
                        return finish_episode(
                            status="STUCK_CLARIFICATION",
                            thread_id=thread_id,
                            started_at_ms=started_wall_ms,
                            turns=turns,
                            clarification_count=clarification_count,
                            error=None,
                        )
                    clarification_count += 1
                    turn_input = request.universal_user_response
                    turn_input_kind = "UNIVERSAL_USER_RESPONSE"
        except Exception as exc:
            if terminal_result is not None and self._is_transient_sdk_error(exc):
                recovered_event = CodexExecutionEvent(
                    event_type="session/disconnect-after-terminal",
                    category="lifecycle",
                    status="RECOVERED",
                    summary="Codex session disconnected after the terminal result was constructed",
                    payload={"error_type": type(exc).__name__},
                    occurred_at_ms=int(time.time() * 1000),
                )
                recovered_turns = terminal_result.turns
                if recovered_turns:
                    recovered_turns = (
                        *recovered_turns[:-1],
                        replace(
                            recovered_turns[-1],
                            events=(*recovered_turns[-1].events, recovered_event),
                        ),
                    )
                return replace(
                    terminal_result,
                    events=(*terminal_result.events, recovered_event),
                    turns=recovered_turns,
                )
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            events.append(
                CodexExecutionEvent(
                    event_type="adapter/error",
                    category="error",
                    status="FAILED",
                    summary=str(exc),
                    payload=error,
                    occurred_at_ms=int(time.time() * 1000),
                )
            )
            return self._failed_result(
                thread_id=thread_id,
                turn_id=turn_id,
                started_at_ms=started_wall_ms,
                completed_at_ms=int(time.time() * 1000),
                duration_ms=None,
                final_response=final_response,
                usage=usage,
                error=error,
                events=events,
                turns=turns,
                clarification_count=clarification_count,
            )
        finally:
            if timeout_timer is not None:
                timeout_timer.cancel()

    @staticmethod
    def _has_structured_turn_status(final_response: str | None) -> bool:
        if not isinstance(final_response, str):
            return False
        final_line = final_response.rstrip().rsplit("\n", maxsplit=1)[-1]
        if not final_line.startswith(TURN_STATUS_PREFIX):
            return False
        return final_line.removeprefix(TURN_STATUS_PREFIX) in {
            item.value for item in TurnClassification
        }

    @staticmethod
    def _is_transient_sdk_error(error: BaseException | dict[str, Any]) -> bool:
        if isinstance(error, dict):
            lowered_type = str(error.get("type") or "").lower()
            lowered_message = str(error.get("message") or "").lower()
            return CodexPythonSdkAdapter._matches_transient_sdk_error(
                lowered_type,
                lowered_message,
            )
        current: BaseException | None = error
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            if isinstance(current, (ConnectionError, TimeoutError)):
                return True
            lowered_type = type(current).__name__.lower()
            lowered_message = str(current).lower()
            if CodexPythonSdkAdapter._matches_transient_sdk_error(
                lowered_type,
                lowered_message,
            ):
                return True
            seen.add(id(current))
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _matches_transient_sdk_error(lowered_type: str, lowered_message: str) -> bool:
        return any(
            marker in lowered_type
            for marker in ("connection", "timeout", "transport", "unavailable")
        ) or any(
            marker in lowered_message
            for marker in (
                "reconnecting...",
                "stream disconnected",
                "connection reset",
                "connection closed",
                "temporarily unavailable",
            )
        )

    @staticmethod
    def _start_turn_with_retries(
        thread: Any,
        turn_input: str,
        *,
        reasoning_effort: str,
        max_retries: int,
    ) -> tuple[Any, tuple[CodexExecutionEvent, ...]]:
        """Retry transient SDK turn-start failures inside one Episode Attempt."""

        retry_events: list[CodexExecutionEvent] = []
        for retry_number in range(max_retries + 1):
            try:
                return (
                    thread.turn(
                        turn_input,
                        effort=reasoning_effort,
                    ),
                    tuple(retry_events),
                )
            except Exception as exc:
                transient = CodexPythonSdkAdapter._is_transient_sdk_error(exc)
                if not transient or retry_number >= max_retries:
                    raise
                retry_events.append(
                    CodexExecutionEvent(
                        event_type="api/retry",
                        category="error",
                        status="RETRYING",
                        summary=str(exc),
                        payload={
                            "error_type": type(exc).__name__,
                            "retry_number": retry_number + 1,
                            "max_retries": max_retries,
                        },
                        occurred_at_ms=int(time.time() * 1000),
                    )
                )
        raise AssertionError("unreachable API retry loop")

    @staticmethod
    def _workflow_command_name(step: FrozenWorkflowStep) -> str | None:
        if isinstance(step.stage_semantics, str) and step.stage_semantics:
            return step.stage_semantics.lower()
        try:
            command = shlex.split(step.command)[0]
        except (IndexError, ValueError):
            return None
        match = re.fullmatch(r"/helix:([a-z][a-z0-9-]*)", command)
        return match.group(1) if match is not None else None

    @staticmethod
    def _workflow_feature_name(step: FrozenWorkflowStep) -> str | None:
        try:
            tokens = shlex.split(step.command)
        except ValueError:
            return None
        if len(tokens) < 2 or re.fullmatch(r"[A-Za-z0-9_.-]+", tokens[1]) is None:
            return None
        return tokens[1]

    @classmethod
    def _workflow_goal_observation(
        cls,
        workspace_path: Path,
        workflow_step: FrozenWorkflowStep,
    ) -> WorkflowGoalObservation:
        """Read only the mechanically trusted state used by goal inference."""

        feature_name = cls._workflow_feature_name(workflow_step)
        archive_completed = False
        if feature_name is not None:
            active = workspace_path / "docs/specs/active" / feature_name
            completed = workspace_path / "docs/specs/completed" / feature_name
            archive_completed = not active.exists() and completed.is_dir()
        return WorkflowGoalObservation(
            archive_completed=archive_completed,
        )

    @staticmethod
    def _stage_findings_input(
        frozen_prompt: str,
        turns: list[CodexTurnExecutionResult],
    ) -> str:
        """Attach the latest mechanical stage failure without inventing evidence."""

        for turn in reversed(turns):
            for event in reversed(turn.events):
                if event.event_type != "stage/validation" or event.status != "FAILED":
                    continue
                detail = event.payload.get("error")
                if not isinstance(detail, str) or not detail.strip():
                    detail = event.summary
                detail = detail.strip()[:2000]
                if not detail:
                    return frozen_prompt
                repair_hint = _stage_validation_repair_hint(detail)
                rendered_hint = (
                    f"\n[平台定向修复提示]\n{repair_hint}" if repair_hint is not None else ""
                )
                return (
                    f"{frozen_prompt}\n\n"
                    "[平台机械校验结果]\n"
                    f"{detail}{rendered_hint}\n"
                    "必须修复上述具体产物或契约缺口并真实落盘；不得只在回复中声明阶段通过，"
                    "也不得伪造未执行的验证证据。当前轮不得执行其他 /helix:* 阶段命令，"
                    "不得在 docs/specs/active 与 docs/specs/completed 之间移动需求目录；"
                    "下一轮平台会重新执行冻结的恢复阶段。"
                )
        return frozen_prompt

    @classmethod
    def _goal_action_step(
        cls,
        workflow_steps: tuple[FrozenWorkflowStep, ...],
        *,
        action: str,
        action_input: str,
        fallback_step: FrozenWorkflowStep,
    ) -> FrozenWorkflowStep:
        """Bind an inferred action back to an exact frozen workflow step."""

        target_semantics = {
            "ARCHIVE": "archive",
        }.get(action)
        if target_semantics is None:
            return fallback_step
        match = next(
            (
                step
                for step in workflow_steps
                if cls._workflow_command_name(step) == target_semantics
                and step.command == action_input
            ),
            None,
        )
        if match is None:
            raise CodexExecutionError(
                f"Workflow goal action is outside the frozen steps: {action}"
            )
        return match

    @staticmethod
    def _remaining_workflow_steps(
        workflow_steps: tuple[FrozenWorkflowStep, ...],
        origin_step: FrozenWorkflowStep,
    ) -> tuple[FrozenWorkflowStep, ...]:
        """Return the frozen suffix used by a durable goal checkpoint."""

        try:
            origin_index = workflow_steps.index(origin_step)
        except ValueError as exc:
            raise CodexExecutionError(
                "Workflow goal origin is outside the frozen steps"
            ) from exc
        return workflow_steps[origin_index + 1 :]


    @staticmethod
    def _record_closure_transition(
        *,
        events: list[CodexExecutionEvent],
        turns: list[CodexTurnExecutionResult],
        transition: str,
        actions: tuple[str, ...],
        status: str,
        workflow_step: FrozenWorkflowStep,
        cycle: int | None = None,
        reason: str | None = None,
    ) -> None:
        """Attach one auditable frozen-state transition to the triggering Turn."""

        payload: dict[str, Any] = {
            "transition": transition,
            "actions": list(actions),
            "workflow_step_id": workflow_step.step_id,
            "milestone_id": workflow_step.milestone_id,
        }
        if cycle is not None:
            payload["cycle"] = cycle
        if reason is not None:
            payload["reason"] = reason
        event = CodexExecutionEvent(
            event_type="workflow/closure",
            category="workflow",
            status=status,
            summary=(
                f"冻结自动闭环：{transition}"
                if reason is None
                else f"冻结自动闭环停止：{transition} · {reason}"
            ),
            payload=payload,
            occurred_at_ms=turns[-1].completed_at_ms,
        )
        events.append(event)
        turns[-1] = replace(
            turns[-1],
            events=(*turns[-1].events, event),
        )

    @staticmethod
    def _record_goal_decision(
        *,
        events: list[CodexExecutionEvent],
        turns: list[CodexTurnExecutionResult],
        decision: WorkflowGoalDecision,
        workflow_step: FrozenWorkflowStep,
    ) -> None:
        """Persist why the bounded controller selected actions or stopped."""

        event = CodexExecutionEvent(
            event_type="workflow/goal-control",
            category="workflow",
            status=("TRANSITIONED" if decision.status == "EXECUTE" else decision.status),
            summary=f"目标驱动工作流：{decision.reason}",
            payload={
                "decision": decision.status,
                "reason": decision.reason,
                "actions": list(decision.actions),
                "workflow_step_id": workflow_step.step_id,
                "milestone_id": workflow_step.milestone_id,
                "progress": asdict(decision.progress),
            },
            occurred_at_ms=turns[-1].completed_at_ms,
        )
        events.append(event)
        turns[-1] = replace(
            turns[-1],
            events=(*turns[-1].events, event),
        )

    @staticmethod
    def _save_closure_checkpoint(
        *,
        request: CodexExecutionRequest,
        turns: list[CodexTurnExecutionResult],
        origin_step: FrozenWorkflowStep,
        next_input_kind: str,
        goal_progress: WorkflowGoalProgress,
        remaining_steps: tuple[FrozenWorkflowStep, ...],
        stage_rework_count: int = 0,
    ) -> None:
        callback = request.on_workflow_closure_checkpoint
        if callback is None:
            return
        callback(
            tuple(turns),
            FrozenWorkflowClosureResume(
                origin_step=origin_step,
                next_input_kind=next_input_kind,
                stage_rework_count=stage_rework_count,
                goal_archive_attempts=goal_progress.archive_attempts,
                goal_inferred_actions=goal_progress.inferred_actions,
            ),
            remaining_steps,
        )

    @staticmethod
    def _failed_result(
        *,
        thread_id: str | None,
        turn_id: str | None,
        started_at_ms: int | None,
        completed_at_ms: int | None,
        duration_ms: int | None,
        final_response: str | None,
        usage: dict[str, int],
        error: dict[str, Any],
        events: list[CodexExecutionEvent],
        turns: list[CodexTurnExecutionResult] | None = None,
        clarification_count: int = 0,
    ) -> CodexExecutionResult:
        return CodexExecutionResult(
            thread_id=thread_id,
            turn_id=turn_id,
            status="FAILED",
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            final_response=final_response,
            usage=usage,
            error=error,
            events=tuple(events),
            turns=tuple(turns or ()),
            clarification_count=clarification_count,
        )

    @staticmethod
    def _episode_result(
        *,
        status: str,
        thread_id: str,
        started_at_ms: int,
        turns: list[CodexTurnExecutionResult],
        clarification_count: int,
        error: dict[str, Any] | None,
    ) -> CodexExecutionResult:
        last_turn = turns[-1]
        completed_at_ms = last_turn.completed_at_ms or int(time.time() * 1000)
        return CodexExecutionResult(
            thread_id=thread_id,
            turn_id=last_turn.turn_id,
            status=status,
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=max(0, completed_at_ms - started_at_ms),
            final_response=last_turn.final_response,
            usage=last_turn.usage,
            error=error,
            events=tuple(event for turn in turns for event in turn.events),
            turns=tuple(turns),
            clarification_count=clarification_count,
        )

    @classmethod
    def _normalize_notification(cls, notification: Any) -> CodexExecutionEvent:
        method = str(notification.method)
        payload = cls._payload_dict(notification.payload)
        item = payload.get("item")
        item_payload = item if isinstance(item, dict) else {}
        item_type = str(item_payload.get("type", ""))
        category = "lifecycle"
        if "tokenUsage" in method:
            category = "usage"
        elif "error" in method.lower():
            category = "error"
        elif item_type in {"agentMessage", "userMessage"}:
            category = "message"
        elif item_type == "commandExecution":
            category = "command"
        elif item_type in {"mcpToolCall", "dynamicToolCall"}:
            category = "tool"

        status = cls._event_status(payload, item_payload)
        summary = cls._event_summary(category, payload, item_payload)
        safe_payload = cls._safe_event_payload(category, payload, item_payload)
        occurred_at_ms = cls._timestamp_ms(
            payload,
            "completedAt",
            "completedAtMs",
            "startedAt",
            "startedAtMs",
        )
        return CodexExecutionEvent(
            event_type=method,
            category=category,
            status=status,
            summary=summary,
            payload=safe_payload,
            occurred_at_ms=occurred_at_ms,
        )

    @staticmethod
    def _payload_dict(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if hasattr(payload, "model_dump"):
            result = payload.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            )
            return result if isinstance(result, dict) else {}
        return {}

    @staticmethod
    def _event_status(
        payload: dict[str, Any],
        item: dict[str, Any],
    ) -> str:
        value = item.get("status")
        if value is None:
            turn = payload.get("turn")
            value = turn.get("status") if isinstance(turn, dict) else None
        if value is None:
            return "UPDATED"
        return str(value).upper().replace("-", "_")

    @classmethod
    def _event_summary(
        cls,
        category: str,
        payload: dict[str, Any],
        item: dict[str, Any],
    ) -> str:
        if category == "message":
            return str(item.get("text", ""))[:1000]
        if category == "command":
            return str(item.get("command", "command"))[:1000]
        if category == "tool":
            return str(item.get("tool") or item.get("server") or "tool")[:1000]
        if category == "usage":
            usage = cls._usage(payload)
            return f"{usage.get('total_tokens', 0)} tokens"
        if category == "error":
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message", "Codex error"))[:1000]
            return "Codex error"
        turn = payload.get("turn")
        if isinstance(turn, dict):
            return f"turn {turn.get('status', 'updated')}"
        return "Codex lifecycle event"

    @classmethod
    def _safe_event_payload(
        cls,
        category: str,
        payload: dict[str, Any],
        item: dict[str, Any],
    ) -> dict[str, Any]:
        if category == "message":
            return {"item": {key: item[key] for key in ("type", "phase", "text") if key in item}}
        if category == "command":
            return {
                "item": {
                    key: item[key]
                    for key in (
                        "type",
                        "command",
                        "cwd",
                        "status",
                        "exitCode",
                        "durationMs",
                    )
                    if key in item
                }
            }
        if category == "tool":
            return {
                "item": {
                    key: item[key]
                    for key in (
                        "type",
                        "server",
                        "tool",
                        "status",
                        "success",
                        "durationMs",
                        "error",
                    )
                    if key in item
                }
            }
        if category == "usage":
            return cls._usage(payload)
        if category == "error":
            error = payload.get("error")
            return error if isinstance(error, dict) else {"message": "Codex error"}
        turn = payload.get("turn")
        if isinstance(turn, dict):
            return {
                "turn": {
                    key: turn[key]
                    for key in (
                        "id",
                        "status",
                        "startedAt",
                        "completedAt",
                        "durationMs",
                        "error",
                    )
                    if key in turn
                }
            }
        return {"method_payload_type": type(payload).__name__}

    @staticmethod
    def _usage(payload: dict[str, Any]) -> dict[str, int]:
        token_usage = payload.get("tokenUsage")
        if not isinstance(token_usage, dict):
            return {}
        total = token_usage.get("total")
        if not isinstance(total, dict):
            return {}
        aliases = {
            "inputTokens": "input_tokens",
            "cachedInputTokens": "cached_input_tokens",
            "outputTokens": "output_tokens",
            "reasoningOutputTokens": "reasoning_output_tokens",
            "totalTokens": "total_tokens",
        }
        return {
            target: int(total[source])
            for source, target in aliases.items()
            if isinstance(total.get(source), (int, float))
        }

    @staticmethod
    def _timestamp_ms(payload: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = payload.get(key)
            if not isinstance(value, (int, float)):
                continue
            return int(value if key.endswith("Ms") else value * 1000)
        return None

    @staticmethod
    def _directory_size_bytes(directory: Path) -> int:
        total = 0
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total


class PinnedCodexPluginInstaller:
    """Install a local frozen marketplace with the SDK-pinned Codex runtime."""

    def install(self, snapshot_path: Path, codex_home_path: Path) -> None:
        """Install only HelixHarness into a newly-created ``CODEX_HOME``."""

        try:
            from codex_cli_bin import bundled_codex_path
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise CodexExecutionError("Pinned Codex CLI runtime is unavailable") from exc

        command = str(bundled_codex_path())
        environment = sanitized_environment(
            os.environ,
            codex_home=codex_home_path,
        )
        environment.pop("CODEX_ACCESS_TOKEN", None)
        environment.pop("OPENAI_API_KEY", None)
        commands = (
            (
                command,
                "plugin",
                "marketplace",
                "add",
                str(snapshot_path),
                "--json",
            ),
            (
                command,
                "plugin",
                "add",
                "helix@helixharness-marketplace",
                "--json",
            ),
        )
        for args in commands:
            completed = subprocess.run(
                args,
                cwd=snapshot_path,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if completed.returncode != 0:
                message = completed.stderr.strip() or completed.stdout.strip()
                raise CodexExecutionError(f"HelixHarness snapshot installation failed: {message[:1000]}")


class SnapshotTelemetryConfigurator:
    """Disable HelixHarness telemetry through the Side Snapshot's own CLI."""

    def disable(
        self,
        snapshot_path: Path,
        workspace_path: Path,
        codex_home_path: Path,
    ) -> Path:
        """Write and verify consent inside the trusted Episode directory."""

        script_path = snapshot_path / "scripts" / "telemetry_consent.py"
        if not script_path.is_file():
            raise CodexExecutionError("HelixHarness snapshot lacks scripts/telemetry_consent.py")
        episode_root = workspace_path.parent
        if snapshot_path.parent != episode_root or codex_home_path.parent != episode_root:
            raise CodexExecutionError("Telemetry setup paths must share one Episode root")
        cache_path = codex_home_path / "helixharness" / "telemetry-consent-cache.json"
        environment = sanitized_environment(
            os.environ,
            codex_home=codex_home_path,
        )
        for name in (
            "CODEX_ACCESS_TOKEN",
            "OPENAI_API_KEY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        ):
            environment.pop(name, None)
        completed = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                "-c",
                (
                    "import os,runpy,sys;"
                    "script=sys.argv[1];"
                    "sys.path.insert(0,os.path.dirname(script));"
                    "sys.argv=sys.argv[1:];"
                    "runpy.run_path(script,run_name='__main__')"
                ),
                str(script_path),
                "--repo-root",
                str(workspace_path),
                "--platform",
                "codex",
                "--cache-path",
                str(cache_path),
                "set",
                "--scope",
                "user",
                "--mode",
                "disabled",
            ),
            cwd=episode_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            message = completed.stderr.strip() or "empty response"
            raise CodexExecutionError(f"HelixHarness telemetry consent returned invalid JSON: {message[:4000]}") from exc
        if (
            completed.returncode != 0
            or not isinstance(result, dict)
            or result.get("effective_mode") != "disabled"
            or result.get("prompt_due") is not False
        ):
            message = result.get("error") if isinstance(result, dict) else completed.stderr.strip()
            raise CodexExecutionError(
                f"HelixHarness telemetry disable failed: {str(message or 'unexpected result')[:1000]}"
            )
        if not cache_path.is_file():
            raise CodexExecutionError("HelixHarness telemetry disabled state was not persisted")
        if cache_path.is_symlink() or cache_path.stat().st_size > 1024 * 1024:
            raise CodexExecutionError("HelixHarness telemetry consent cache is unsafe")
        try:
            persisted = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexExecutionError("HelixHarness telemetry consent cache is invalid") from exc
        user_state = persisted.get("user") if isinstance(persisted, dict) else None
        if (
            persisted.get("schema_version") != "1.0"
            or not isinstance(user_state, dict)
            or user_state.get("mode") != "disabled"
        ):
            raise CodexExecutionError("HelixHarness telemetry consent cache is not disabled")
        return cache_path


def workspace_tree_hash(workspace_path: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(workspace_path.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(workspace_path)
        if relative_path.parts and relative_path.parts[0] == ".git":
            continue
        relative = relative_path.as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
