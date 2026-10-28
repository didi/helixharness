from __future__ import annotations

import json
import subprocess
from dataclasses import replace

import pytest

from evolution.candidate import CandidateBuilder, CandidatePolicyError
from evolution.models import AcceptanceContract, Capability, Hypothesis, HypothesisKind


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], text=True, capture_output=True, check=True
    ).stdout.strip()


def _repo(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.com")
    (repository / "commands").mkdir()
    (repository / "commands" / "archive.md").write_text("old\n", encoding="utf-8")
    (repository / "eval").mkdir()
    (repository / "eval" / "gate.py").write_text("protected\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")
    return repository




def _add_plugin_binding(repository) -> None:
    (repository / ".codex-plugin").mkdir()
    (repository / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "helix"}) + "\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".codex-plugin/plugin.json")
    _git(repository, "commit", "-m", "add plugin binding")


def _hypothesis():
    return Hypothesis(
        hypothesis_id="hyp-1",
        kind=HypothesisKind.REPAIR,
        capability_id="workflow",
        title="修复归档",
        rationale="归档失败",
        evidence_ids=("e-1",),
        confidence=0.9,
        acceptance=AcceptanceContract("workflow", ("case-1",), "归档成功", ("case-1",), ("eval/",)),
        requires_human_selection=False,
        semantic_source="RULE",
        created_at="2026-08-01T00:00:00Z",
    )


def _capability():
    return Capability(
        "workflow",
        "工作流",
        "工作流纪律",
        ("archive",),
        ("commands/",),
        ("eval/",),
        ("case-1",),
        True,
        False,
    )


class _AllowedRunner:
    def run(self, worktree, prompt):
        (worktree / "commands" / "archive.md").write_text("fixed\n", encoding="utf-8")


def test_combined_candidate_keeps_root_path_policy(tmp_path):
    repository = _repo(tmp_path)
    _add_plugin_binding(repository)
    (repository / ".claude-plugin").mkdir()
    (repository / ".claude-plugin/plugin.json").write_text('{}\n')
    _git(repository, "add", ".claude-plugin")
    (repository / "helixharness").mkdir()
    _git(repository, "mv", "commands", ".codex-plugin", ".claude-plugin", "helixharness/")
    _git(repository, "commit", "-m", "combined source")

    class Runner:
        def run(self, worktree, prompt):
            assert "helixharness/commands/" in prompt
            (worktree / "helixharness/commands/archive.md").write_text("fixed\n")

    capability = replace(_capability(), owned_paths=("helixharness/commands/",))
    result = CandidateBuilder(repository, tmp_path / "candidates", Runner()).build(
        "combined-experiment", _hypothesis(), capability,
    )
    assert set(result.changed_paths) == {
        "helixharness/commands/archive.md",
    }
    assert _git(repository, "show", f"{result.candidate_commit}:eval/gate.py") == "protected"


class _ForbiddenRunner:
    def run(self, worktree, prompt):
        (worktree / "eval" / "gate.py").write_text("bypassed\n", encoding="utf-8")


class _RetryRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, worktree, prompt):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient model failure")
        (worktree / "commands" / "archive.md").write_text("fixed after retry\n", encoding="utf-8")


class _AllowedRenameRunner:
    def run(self, worktree, prompt):
        _git(worktree, "mv", "commands/archive.md", "commands/archive-v2.md")


class _ProtectedMoveRunner:
    def run(self, worktree, prompt):
        _git(worktree, "mv", "eval/gate.py", "commands/gate.py")


def test_candidate_is_frozen_as_commit_and_dedicated_ref(tmp_path) -> None:
    repository = _repo(tmp_path)
    builder = CandidateBuilder(repository, tmp_path / "worktrees", _AllowedRunner())

    candidate = builder.build("exp-1", _hypothesis(), _capability())

    assert candidate.base_commit != candidate.candidate_commit
    assert candidate.changed_paths == ("commands/archive.md",)
    assert "commands/archive.md" in candidate.diff_summary
    assert "-old" in candidate.diff_patch
    assert "+fixed" in candidate.diff_patch
    assert candidate.diff_truncated is False
    assert _git(repository, "rev-parse", candidate.candidate_ref) == candidate.candidate_commit




def test_candidate_preserves_public_plugin_manifest(tmp_path) -> None:
    repository = _repo(tmp_path)
    _add_plugin_binding(repository)
    builder = CandidateBuilder(repository, tmp_path / "worktrees", _AllowedRunner())

    candidate = builder.build("exp-plugin", _hypothesis(), _capability())

    assert candidate.changed_paths == ("commands/archive.md",)
    binding = json.loads(
        _git(repository, "show", f"{candidate.candidate_commit}:.codex-plugin/plugin.json")
    )
    assert "bundle_digest" not in binding




def test_candidate_can_start_from_one_frozen_failed_candidate_commit(tmp_path) -> None:
    repository = _repo(tmp_path)
    source_candidate = _git(repository, "rev-parse", "HEAD")
    (repository / "commands" / "archive.md").write_text("unrelated head\n", encoding="utf-8")
    _git(repository, "commit", "-am", "advance repository head")
    current_head = _git(repository, "rev-parse", "HEAD")
    builder = CandidateBuilder(repository, tmp_path / "worktrees", _AllowedRunner())

    candidate = builder.build(
        "exp-from-failed-candidate",
        _hypothesis(),
        _capability(),
        base_commit=source_candidate,
    )

    assert candidate.base_commit == source_candidate
    assert candidate.base_commit != current_head
    assert _git(repository, "rev-parse", f"{candidate.candidate_ref}^") == source_candidate


def test_protected_path_change_is_rejected_before_commit(tmp_path) -> None:
    repository = _repo(tmp_path)
    builder = CandidateBuilder(repository, tmp_path / "worktrees", _ForbiddenRunner())

    with pytest.raises(CandidatePolicyError, match="eval/gate.py"):
        builder.build("exp-1", _hypothesis(), _capability())


def test_existing_isolated_worktree_can_be_reused_for_one_technical_retry(tmp_path) -> None:
    repository = _repo(tmp_path)
    runner = _RetryRunner()
    builder = CandidateBuilder(repository, tmp_path / "worktrees", runner)

    with pytest.raises(RuntimeError, match="transient"):
        builder.build("exp-1", _hypothesis(), _capability())
    candidate = builder.build("exp-1", _hypothesis(), _capability())

    assert runner.calls == 2
    assert candidate.changed_paths == ("commands/archive.md",)


def test_allowed_rename_validates_both_complete_paths(tmp_path) -> None:
    repository = _repo(tmp_path)
    builder = CandidateBuilder(repository, tmp_path / "worktrees", _AllowedRenameRunner())

    candidate = builder.build("exp-1", _hypothesis(), _capability())

    assert set(candidate.changed_paths) == {
        "commands/archive.md",
        "commands/archive-v2.md",
    }


def test_moving_a_protected_path_reports_the_complete_source_path(tmp_path) -> None:
    repository = _repo(tmp_path)
    builder = CandidateBuilder(repository, tmp_path / "worktrees", _ProtectedMoveRunner())

    with pytest.raises(CandidatePolicyError, match="eval/gate.py"):
        builder.build("exp-1", _hypothesis(), _capability())


def test_candidate_enforces_its_workload_disk_budget(tmp_path) -> None:
    repository = _repo(tmp_path)
    builder = CandidateBuilder(
        repository,
        tmp_path / "worktrees",
        _AllowedRunner(),
        disk_budget_mb=0,
    )

    with pytest.raises(CandidatePolicyError, match="disk budget"):
        builder.build("exp-1", _hypothesis(), _capability())
