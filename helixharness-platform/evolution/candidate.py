"""Isolated Git candidate generation with a hard path policy."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol

from evolution.codex_runtime import resolve_codex_executable
from evolution.models import CandidateVersion, Capability, Hypothesis


class CandidateBuildError(RuntimeError):
    """Candidate generation failed before a benchmark could be created."""


class CandidatePolicyError(CandidateBuildError):
    """Generated changes crossed the capability or governance boundary."""


class CandidateRunner(Protocol):
    """Agent boundary used inside a newly created isolated worktree."""

    def run(self, worktree: Path, prompt: str) -> None: ...


class CodexCandidateRunner:
    """Workspace-write Codex runner used only inside an isolated worktree."""

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning_effort: str = "high",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 900,
        execution_user: str | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") if base_url else None
        self.timeout_seconds = timeout_seconds
        self.execution_user = execution_user
        self._execution_identity: tuple[int, int] | None = None

    def prepare(self, worktree: Path, owned_paths: tuple[str, ...]) -> None:
        """Grant a low-privilege process write access only to catalog-owned paths."""

        if not self.execution_user:
            return
        try:
            account = pwd.getpwnam(self.execution_user)
        except KeyError as exc:
            raise CandidateBuildError("candidate isolation user is not available") from exc
        if account.pw_uid == 0:
            raise CandidateBuildError("candidate isolation user must not be root")
        if shutil.which("setpriv") is None:
            raise CandidateBuildError("setpriv is required for candidate user isolation")
        writable_roots: list[Path] = []
        for rule in owned_paths:
            relative = PurePosixPath(rule).as_posix().lstrip("./").rstrip("/")
            target = worktree / relative
            if not target.exists() or target.is_symlink():
                raise CandidateBuildError(f"candidate writable root is unavailable: {relative}")
            writable_roots.append(target)
        for target in writable_roots:
            _chown_tree(target, account.pw_uid, account.pw_gid)
        self._execution_identity = (account.pw_uid, account.pw_gid)

    def run(self, worktree: Path, prompt: str) -> None:
        last_message_path = self._last_message_path(worktree)
        last_message_path.unlink(missing_ok=True)
        command = [str(resolve_codex_executable()),
            "exec",
            "--cd",
            str(worktree),
            "--ephemeral",
            "--output-last-message",
            str(last_message_path),
            "--config",
            f'model_reasoning_effort="{self.reasoning_effort}"',
            "--config",
            'approval_policy="never"',
        ]
        environment = None
        if self.execution_user:
            if self._execution_identity is None:
                raise CandidateBuildError("candidate user isolation was not prepared")
            uid, gid = self._execution_identity
            runtime_home = self._runtime_home(worktree, uid, gid)
            command = [
                str(shutil.which("setpriv")),
                f"--reuid={uid}",
                f"--regid={gid}",
                "--clear-groups",
                "--",
                *command,
                "--dangerously-bypass-approvals-and-sandbox",
            ]
            environment = dict(os.environ)
            environment.update(
                {
                    "CODEX_HOME": str(runtime_home),
                    "HOME": str(runtime_home),
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "safe.directory",
                    "GIT_CONFIG_VALUE_0": str(worktree),
                    "GIT_OPTIONAL_LOCKS": "0",
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
        else:
            command[5:5] = ["--sandbox", "workspace-write"]
        if self.api_key and self.base_url:
            provider = "evaluation_proxy"
            command.extend(
                [
                    "--config",
                    f'model_provider="{provider}"',
                    "--config",
                    f"model_providers.{provider}.base_url={json.dumps(self.base_url)}",
                    "--config",
                    f'model_providers.{provider}.env_key="OPENAI_API_KEY"',
                    "--config",
                    f'model_providers.{provider}.name="Evaluation proxy"',
                    "--config",
                    f'model_providers.{provider}.wire_api="responses"',
                ]
            )
            environment = environment or dict(os.environ)
            environment["OPENAI_API_KEY"] = self.api_key
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")
        run_options: dict[str, object] = {
            "input": prompt,
            "text": True,
            "capture_output": True,
            "check": False,
            "timeout": self.timeout_seconds,
        }
        if environment is not None:
            run_options["env"] = environment
        try:
            completed = subprocess.run(command, **run_options)
        except subprocess.TimeoutExpired as exc:
            raise CandidateBuildError(
                f"Codex candidate generation timed out after {self.timeout_seconds} seconds"
            ) from exc
        if completed.returncode != 0:
            raise CandidateBuildError(f"Codex candidate generation failed with exit code {completed.returncode}")

    def no_change_reason(self, worktree: Path) -> str:
        """Reduce the model's runtime explanation to a safe operational reason."""

        path = self._last_message_path(worktree)
        if not path.is_file():
            return "candidate made no changes and returned no final explanation"
        message = path.read_text(encoding="utf-8", errors="replace").lower()
        if "bwrap" in message and "namespace" in message:
            return "candidate sandbox unavailable: bwrap cannot create a namespace"
        if "no changes" in message or "未修改" in message:
            return "candidate made no changes; final explanation is retained in runtime diagnostics"
        return "candidate made no changes; final explanation is retained in runtime diagnostics"

    @staticmethod
    def _last_message_path(worktree: Path) -> Path:
        runtime_dir = worktree.parent / ".candidate-runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir / f"{worktree.name}-last-message.txt"

    def _runtime_home(self, worktree: Path, uid: int, gid: int) -> Path:
        runtime_dir = self._last_message_path(worktree).parent
        home = runtime_dir / f"{worktree.name}-home"
        home.mkdir(parents=True, exist_ok=True)
        _chown_tree(runtime_dir, uid, gid)
        return home


class CandidateBuilder:
    """Build and freeze one hypothesis in its own Git worktree and ref."""

    def __init__(
        self,
        repository: str | Path,
        workspace_root: str | Path,
        runner: CandidateRunner,
        *,
        disk_budget_mb: int | None = None,
        base_commit_resolver: Callable[[Path], str] | None = None,
    ) -> None:
        self.repository = Path(repository).expanduser().resolve()
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.runner = runner
        self.base_commit_resolver = base_commit_resolver
        self.disk_budget_bytes = (
            None if disk_budget_mb is None else max(0, disk_budget_mb) * 1_024 * 1_024
        )

    def build(
        self,
        experiment_id: str,
        hypothesis: Hypothesis,
        capability: Capability,
        *,
        base_commit: str | None = None,
    ) -> CandidateVersion:
        """Prefer an explicit source commit, otherwise resolve the configured baseline (locally HEAD)."""

        if base_commit is None and self.base_commit_resolver is not None:
            base_commit = self.base_commit_resolver(self.repository)
            if not base_commit:
                raise CandidateBuildError("candidate baseline resolver returned no commit")
        base_commit = self._git("rev-parse", "--verify", f"{base_commit or 'HEAD'}^{{commit}}")
        candidate_id = "cand_" + hashlib.sha256(
            f"{experiment_id}:{hypothesis.hypothesis_id}:{base_commit}".encode("utf-8")
        ).hexdigest()[:16]
        worktree = self.workspace_root / candidate_id
        if worktree.exists():
            existing_commit = _run_git(worktree, "rev-parse", "HEAD")
            if existing_commit != base_commit:
                raise CandidateBuildError(f"candidate worktree has unexpected base: {worktree}")
        else:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            self._git("worktree", "add", "--detach", str(worktree), base_commit)
        prompt = _candidate_prompt(hypothesis, capability)
        prepare = getattr(self.runner, "prepare", None)
        if callable(prepare):
            prepare(worktree, capability.owned_paths)
        self.runner.run(worktree, prompt)
        changed_paths = _changed_paths(worktree)
        if not changed_paths:
            diagnostic = getattr(self.runner, "no_change_reason", None)
            reason = diagnostic(worktree) if callable(diagnostic) else "candidate made no changes"
            raise CandidateBuildError(reason)
        _validate_paths(changed_paths, capability)
        symlinks = [path for path in changed_paths if (worktree / path).is_symlink()]
        if symlinks:
            raise CandidatePolicyError(f"candidate introduced or changed symlinks: {', '.join(symlinks)}")
        if self.disk_budget_bytes is not None and _tree_size(worktree) > self.disk_budget_bytes:
            raise CandidatePolicyError("candidate exceeded its workload disk budget")
        _run_git(worktree, "add", "--all")
        diff_summary = _run_git(worktree, "diff", "--cached", "--stat", "--no-color")
        diff_patch = _run_git(worktree, "diff", "--cached", "--no-color", "--unified=3")
        diff_limit = 100_000
        diff_truncated = len(diff_patch) > diff_limit
        if diff_truncated:
            diff_patch = diff_patch[:diff_limit] + "\n… diff truncated by evolution Candidate policy …\n"
        _run_git(
            worktree,
            "-c",
            "user.name=HelixHarness Evolution",
            "-c",
            "user.email=evolution@localhost",
            "commit",
            "-m",
            f"evolution: {hypothesis.title}",
        )
        candidate_commit = _run_git(worktree, "rev-parse", "HEAD")
        candidate_ref = f"refs/helixharness/evolution/{candidate_id}"
        self._git("update-ref", candidate_ref, candidate_commit)
        return CandidateVersion(
            candidate_id=candidate_id,
            hypothesis_id=hypothesis.hypothesis_id,
            base_commit=base_commit,
            candidate_commit=candidate_commit,
            candidate_ref=candidate_ref,
            worktree_path=str(worktree),
            changed_paths=changed_paths,
            diff_summary=diff_summary,
            diff_patch=diff_patch,
            diff_truncated=diff_truncated,
        )

    def _git(self, *args: str) -> str:
        return _run_git(self.repository, *args)


def _candidate_prompt(hypothesis: Hypothesis, capability: Capability) -> str:
    payload = {
        "hypothesis": {
            "id": hypothesis.hypothesis_id,
            "kind": hypothesis.kind.value,
            "title": hypothesis.title,
            "rationale": hypothesis.rationale,
            "expected_effect": hypothesis.acceptance.expected_effect,
            "evidence_admission": hypothesis.semantic_source,
            "source_quality_issues": hypothesis.source_quality_issues,
            "retry_of_experiment_id": hypothesis.retry_of_experiment_id,
            "source_run_id": hypothesis.source_run_id,
            "source_candidate_commit": hypothesis.source_candidate_commit,
            "intervention": hypothesis.intervention,
            "falsification_condition": hypothesis.falsification_condition,
            "validation_plan": hypothesis.validation_plan,
            "generation_basis": hypothesis.generation_basis,
        },
        "capability": {
            "id": capability.capability_id,
            "owned_paths": capability.owned_paths,
            "protected_paths": capability.protected_paths,
        },
    }
    return (
        "Implement exactly one HelixHarness capability hypothesis. Read repository instructions first. "
        "Change only owned_paths and never protected_paths. Preserve telemetry, evaluation, capability "
        "catalog, Case/Gold/Judge, scoring, and promotion policy. Add the minimum relevant tests and run "
        "them. Do not stop at analysis: inspect the owned workflow instructions and implement one minimal, "
        "generalizable improvement tied to the sanitized failure context. If the evidence cannot identify "
        "an exact feature-specific defect, improve owned guidance, gates, or recovery instructions without "
        "inventing results. Do not commit, push, deploy, or edit credentials.\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _tree_size(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def _changed_paths(worktree: Path) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise CandidateBuildError("unable to inspect candidate changes")
    paths: set[str] = set()
    records = completed.stdout.decode("utf-8", errors="replace").split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        if not record:
            index += 1
            continue
        if len(record) < 4:
            raise CandidateBuildError("unable to parse candidate change record")
        status = record[:2]
        paths.add(PurePosixPath(record[3:]).as_posix())
        if "R" in status or "C" in status:
            index += 1
            if index >= len(records) or not records[index]:
                raise CandidateBuildError("rename or copy change is missing its source path")
            paths.add(PurePosixPath(records[index]).as_posix())
        index += 1
    return tuple(sorted(paths))


def _validate_paths(paths: tuple[str, ...], capability: Capability) -> None:
    violations: list[str] = []
    for path in paths:
        if any(_path_matches(path, protected) for protected in capability.protected_paths):
            violations.append(path)
            continue
        if not any(_path_matches(path, owned) for owned in capability.owned_paths):
            violations.append(path)
    if violations:
        raise CandidatePolicyError(f"candidate changed forbidden paths: {', '.join(violations)}")


def _path_matches(path: str, rule: str) -> bool:
    normalized = PurePosixPath(rule).as_posix().lstrip("./")
    return path == normalized or (rule.endswith("/") and path.startswith(normalized.rstrip("/") + "/"))


def _run_git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        message = detail[-1] if detail else "git command failed"
        raise CandidateBuildError(message)
    return completed.stdout.strip()


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)
    if not path.is_dir():
        return
    for root, directories, files in os.walk(path):
        root_path = Path(root)
        for name in directories:
            os.chown(root_path / name, uid, gid, follow_symlinks=False)
        for name in files:
            os.chown(root_path / name, uid, gid, follow_symlinks=False)
