"""Versioned known-HelixHarness adapter for Scenario v2 task authoring."""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from bench.evaluation_contracts import (
    ArtifactSource,
    ExecutionStrategy,
    StrategyStage,
    freeze_evaluation_contract,
)
from bench.helixharness_source import HelixHarnessSourceError, frozen_plugin_source, git_repository_root
from bench.workflow_contracts import (
    DEFAULT_WORKFLOW_REWORK_LIMIT,
    WorkflowContractError,
    resolve_workflow_contract,
)

_WORKFLOW_CLOSURE = {
    "schema_version": "framework-eval-helixharness-closure.v1",
    "max_stage_rework_cycles": DEFAULT_WORKFLOW_REWORK_LIMIT,
    "stage_findings_prompt": (
        "当前原生工作流阶段未完成。保留已经正确的实现和产物，只依据当前失败正文、"
        "冻结需求、仓库事实与已有验证证据定位并修复问题；不得补造业务事实。完成必要修改和"
        "最低成本相关验证后返回，下一轮平台会重新执行同一条冻结阶段命令。"
    ),
    "transitions": [],
}


class ScenarioStrategyDraftError(ValueError):
    """Raised when the known adapter cannot build a confirmable contract."""


class HelixHarnessRepositoryRefreshError(RuntimeError):
    """Raised when the trusted repository cannot refresh a remote branch."""


def _refresh_remote_branch(repository: Path, branch: str, *, label: str) -> str | None:
    """Fetch one advertised origin branch and return its exact Commit."""

    if not (repository / ".git").exists():
        raise HelixHarnessRepositoryRefreshError("平台尚未配置可用的 HelixHarness 版本仓库")
    remotes = subprocess.run(
        ["git", "-C", str(repository), "remote"],
        check=False, capture_output=True, text=True,
    )
    if remotes.returncode != 0:
        raise HelixHarnessRepositoryRefreshError("平台尚未配置可用的 HelixHarness 版本仓库")
    if "origin" not in remotes.stdout.splitlines():
        return None
    remote_ref = f"refs/heads/{branch}"
    tracking_ref = f"refs/remotes/origin/{branch}"
    advertised = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "ls-remote",
            "--exit-code",
            "origin",
            remote_ref,
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if advertised.returncode == 2:
        return None
    if advertised.returncode != 0:
        raise HelixHarnessRepositoryRefreshError(
            f"无法刷新 HelixHarness 远端 {label}；请检查服务器仓库的 origin 和网络连接"
        )
    fetched = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "fetch",
            "--no-tags",
            "origin",
            f"+{remote_ref}:{tracking_ref}",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    if fetched.returncode != 0:
        raise HelixHarnessRepositoryRefreshError(
            f"无法刷新 HelixHarness 远端 {label}；请检查服务器仓库的 origin 和网络连接"
        )
    resolved = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--verify",
            f"{tracking_ref}^{{commit}}",
        ),
        check=False,
        capture_output=True,
        text=True,
    )
    commit = resolved.stdout.strip()
    if resolved.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise HelixHarnessRepositoryRefreshError(f"HelixHarness 远端 {label} 无法解析为准确版本")
    return commit


def resolve_reference_commit(repository: Path, reference_ref: str) -> str:
    """Freeze the configured baseline, refreshing an origin branch when present."""

    try:
        repository = git_repository_root(repository)
    except HelixHarnessSourceError as exc:
        raise HelixHarnessRepositoryRefreshError(str(exc)) from exc
    reference_ref = reference_ref.strip()
    if not reference_ref or reference_ref.startswith("-"):
        raise HelixHarnessRepositoryRefreshError("HelixHarness 基准引用不能为空或以 - 开头")
    if re.fullmatch(r"[0-9a-f]{40}", reference_ref) is None:
        commit = _refresh_remote_branch(repository, reference_ref, label=reference_ref)
        if commit is not None:
            return commit
    resolved = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", "--end-of-options", f"{reference_ref}^{{commit}}"],
        check=False, capture_output=True, text=True,
    )
    commit = resolved.stdout.strip()
    if resolved.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise HelixHarnessRepositoryRefreshError(f"HelixHarness 基准引用无法解析为准确版本：{reference_ref}")
    return commit


@dataclass(frozen=True)
class ScenarioStrategyDraft:
    """A complete frozen contract plus a Chinese-facing strategy summary."""

    evaluation_contract: dict[str, object]
    strategy_summary: dict[str, dict[str, object]]


class KnownHelixHarnessStrategyResolver:
    """Resolve user-authored HelixHarness commands against exact source Commits."""

    def __init__(self, repository: Path, *, plugin_path: str | None = None) -> None:
        self.repository = repository.resolve()
        self.plugin_path = plugin_path

    def draft(
        self,
        *,
        base_ref: str = "master",
        candidate_ref: str,
        base_workflow_commands: Iterable[str],
        candidate_workflow_commands: Iterable[str],
    ) -> ScenarioStrategyDraft:
        """Resolve Base/Candidate and freeze one unified artifact policy per side."""

        if not candidate_ref.strip():
            raise ScenarioStrategyDraftError("候选版本不能为空")
        normalized_base_ref = base_ref.strip() or "master"
        try:
            base_commit = resolve_reference_commit(self.repository, normalized_base_ref)
        except HelixHarnessRepositoryRefreshError as exc:
            raise ScenarioStrategyDraftError(str(exc)) from exc
        candidate_commit = self._commit(candidate_ref.strip(), label="候选版本")
        commands = {
            "BASE": self._normalize_commands(base_workflow_commands, label="基准侧"),
            "CANDIDATE": self._normalize_commands(candidate_workflow_commands, label="候选侧"),
        }
        commits = {"BASE": base_commit, "CANDIDATE": candidate_commit}
        workflows = {
            side: self._resolve_workflow(side, commits[side], commands[side])
            for side in ("BASE", "CANDIDATE")
        }
        refs = {
            "BASE": normalized_base_ref,
            "CANDIDATE": candidate_ref.strip(),
        }
        strategies = {
            side: self._strategy(
                side,
                commits[side],
                commands[side],
                workflows[side],
                source_ref=refs[side],
            )
            for side in ("BASE", "CANDIDATE")
        }
        contract = freeze_evaluation_contract(
            base_strategy=strategies["BASE"],
            candidate_strategy=strategies["CANDIDATE"],
        )
        return ScenarioStrategyDraft(
            evaluation_contract=contract,
            strategy_summary={
                "BASE": self._summary(
                    strategies["BASE"],
                    version_label=normalized_base_ref,
                ),
                "CANDIDATE": self._summary(
                    strategies["CANDIDATE"],
                    version_label=candidate_ref.strip(),
                ),
            },
        )

    def _commit(self, revision: str, *, label: str) -> str:
        try:
            return resolve_reference_commit(self.repository, revision)
        except HelixHarnessRepositoryRefreshError as exc:
            raise ScenarioStrategyDraftError(f"{label}无法解析为准确版本：{exc}") from exc

    def _strategy(
        self,
        side: str,
        commit: str,
        commands: tuple[str, ...],
        workflow: dict[str, object],
        *,
        source_ref: str,
    ) -> ExecutionStrategy:
        documents = self._command_documents(commands)
        try:
            source = frozen_plugin_source(self.repository, commit, plugin_path=self.plugin_path)
        except HelixHarnessSourceError as exc:
            raise ScenarioStrategyDraftError(str(exc)) from exc
        contents = {path: self._git_file(source.tree, path) for path in documents}
        self._validate_command_options(side, commands, contents)
        hashes = tuple((path, hashlib.sha256(contents[path]).hexdigest()) for path in documents)
        feature_name = str(workflow["feature"])
        completed_root = f"docs/specs/completed/{feature_name}"
        side_slug = side.lower()
        artifact_sources = (
            ArtifactSource(
                source_id=f"{side_slug}-completed-feature",
                root=completed_root,
            ),
        )
        completion_states = tuple(f"STEP_{position:02d}_COMPLETED" for position in range(1, len(commands) + 1))
        workflow_steps = workflow.get("steps")
        if (
            not isinstance(workflow_steps, list)
            or len(workflow_steps) != len(commands)
            or any(not isinstance(step, dict) for step in workflow_steps)
        ):
            raise ScenarioStrategyDraftError("平台无法解析完整的工作流步骤")
        stages = tuple(
            StrategyStage(
                stage_id=f"STEP_{position:02d}",
                semantic_role=str(workflow_steps[position - 1]["stage_semantics"]),
                command=command,
                completion_state=completion_states[position - 1],
                checkpoint=True,
            )
            for position, command in enumerate(commands, start=1)
        )
        command_hash = hashlib.sha256("\n".join(commands).encode("utf-8")).hexdigest()[:12]
        return ExecutionStrategy(
            strategy_id=f"user-helixharness-{command_hash}",
            version="user-helixharness-strategy.v1",
            adapter_id="known-helixharness",
            adapter_version="1",
            source_commit=commit,
            source_document_hashes=hashes,
            commands=commands,
            stages=stages,
            completion_states=completion_states,
            artifact_sources=artifact_sources,
            finding_policy="NONE",
            finding_closures=(),
            recovery_policy={
                "schema_version": "framework-eval-strategy-recovery.v1",
                "feature": feature_name,
                "workflow_goal_control": workflow["goal_control"],
            },
            source_ref=source_ref,
            source_plugin_path=source.path,
        )

    @staticmethod
    def _normalize_commands(commands: Iterable[str], *, label: str) -> tuple[str, ...]:
        normalized = tuple(command.strip() for command in commands)
        if not normalized or any(not command for command in normalized):
            raise ScenarioStrategyDraftError(f"{label}工作流命令不能为空")
        if len(normalized) > 20:
            raise ScenarioStrategyDraftError(f"{label}工作流最多包含 20 条命令")
        return normalized

    @staticmethod
    def _resolve_workflow(side: str, commit: str, commands: tuple[str, ...]) -> dict[str, object]:
        try:
            workflow = resolve_workflow_contract(
                commands,
                profile_name="UNIFIED_EVALUATION",
                side=side,
                source_commit=commit,
                workflow_closure=_WORKFLOW_CLOSURE,
            )
        except (ValueError, WorkflowContractError) as exc:
            raise ScenarioStrategyDraftError(str(exc)) from exc
        return workflow

    @staticmethod
    def _command_documents(commands: tuple[str, ...]) -> tuple[str, ...]:
        documents: list[str] = []
        for command in commands:
            tokens = shlex.split(command)
            command_name = tokens[0].split(":", 1)[1]
            path = f"commands/{command_name}.md"
            if path not in documents:
                documents.append(path)
        return tuple(documents)

    @staticmethod
    def _validate_command_options(
        side: str,
        commands: tuple[str, ...],
        documents: dict[str, bytes],
    ) -> None:
        label = "基准侧" if side == "BASE" else "候选侧"
        for command in commands:
            tokens = shlex.split(command)
            command_name = tokens[0].split(":", 1)[1]
            content = documents[f"commands/{command_name}.md"].decode("utf-8", errors="replace")
            has_explicit_feature = (
                command_name != "init"
                and len(tokens) >= 2
                and not tokens[1].startswith("--")
            )
            option_tokens = tokens[2:] if has_explicit_feature else tokens[1:]
            for option in (token for token in option_tokens if token.startswith("--")):
                if option not in content:
                    raise ScenarioStrategyDraftError(f"{label}命令参数 {option} 在所选版本中不可用")

    def _git_file(self, tree: str, path: str) -> bytes:
        completed = subprocess.run(
            ("git", "-C", str(self.repository), "show", f"{tree}:{path}"),
            check=False,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise ScenarioStrategyDraftError(f"准确版本缺少执行策略文档：{path}")
        return completed.stdout

    @staticmethod
    def _summary(strategy: ExecutionStrategy, *, version_label: str) -> dict[str, object]:
        return {
            "version_label": version_label,
            "commit": strategy.source_commit,
            "commands": list(strategy.commands),
            "artifact_roots": [source.root for source in strategy.artifact_sources],
        }
