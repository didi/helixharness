"""Candidate baseline and Benchmark compatibility through production composition."""

from dataclasses import replace
from pathlib import Path

import pytest

from evolution.candidate import CandidateBuildError
from evolution.models import Capability
from evolution.service import EvolutionService
from evolution.storage import EvolutionStore
from web_ui import main
from web_ui.tests.test_evolution_gateway import _gateway, _hypothesis
from web_ui.tests.test_scenario import _git


class _Runner:
    def __init__(self) -> None:
        self.called = False

    def run(self, worktree: Path, prompt: str) -> None:
        self.called = True
        (worktree / "commands" / "archive.md").write_text("fixed archive workflow\n", encoding="utf-8")


def _configured_builder(tmp_path, monkeypatch):
    gateway, store, base_commit, newer_commit = _gateway(tmp_path)
    origin = tmp_path / "helixharness-origin.git"
    _git(origin, "update-ref", "refs/heads/release", base_commit)
    gateway.configuration = replace(gateway.configuration, reference_ref="release")
    capability = Capability(
        "workflow", "工作流", "工作流纪律", ("archive",), ("commands/",), ("eval/",),
        ("runtime-scenario",), True, False,
    )
    runner = _Runner()
    monkeypatch.setattr(main, "evaluation_provider_config", lambda _: gateway.provider)
    monkeypatch.setattr(main, "task_platform_configuration", lambda _: gateway.configuration)
    monkeypatch.setattr(main, "load_catalog", lambda _: (capability,))
    monkeypatch.setattr(main, "codex_runtime_available", lambda: True)
    def configured_runner(**kwargs):
        runner.configuration = kwargs
        return runner

    monkeypatch.setattr(main, "CodexCandidateRunner", configured_runner)
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_SEMANTIC_ANALYZER_ENABLED", "0")
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    service = main._evolution_service(store, EvolutionStore(tmp_path / "evolution.db"))
    assert service.candidate_builder is not None
    return service.candidate_builder, gateway, capability, runner, base_commit, newer_commit


@pytest.mark.parametrize("uid,override,expected", [(501, None, None), (0, None, "nobody"), (501, "worker", "worker")])
def test_candidate_service_selects_usable_user_isolation(tmp_path, monkeypatch, uid, override, expected):
    monkeypatch.setattr(main.os, "geteuid", lambda: uid)
    if override is not None:
        monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_EXECUTION_USER", override)
    _, _, _, runner, _, _ = _configured_builder(tmp_path, monkeypatch)
    assert runner.configuration["execution_user"] == expected


@pytest.mark.parametrize("tracking_already_refreshed", [False, True])
def test_candidate_from_stale_checkout_uses_remote_master_and_can_submit(
    tmp_path, monkeypatch, tracking_already_refreshed,
) -> None:
    builder, gateway, capability, _, old_head, remote_master = _configured_builder(tmp_path, monkeypatch)
    repository = gateway.repository
    _git(tmp_path / "helixharness-origin.git", "update-ref", "refs/heads/release", remote_master)
    if tracking_already_refreshed:
        _git(repository, "fetch", "origin")
    (repository / "commands" / "apply.md").write_text("uncommitted local work\n", encoding="utf-8")
    (repository / "notes.txt").write_text("keep local notes\n", encoding="utf-8")
    before_status = _git(repository, "status", "--porcelain")

    candidate = builder.build("candidate-test", _hypothesis(), capability)

    assert candidate.base_commit == remote_master
    assert _git(repository, "rev-parse", f"{candidate.candidate_commit}^") == remote_master
    assert _git(repository, "show", f"{candidate.candidate_commit}:commands/apply.md") == "candidate apply workflow"
    assert _git(repository, "rev-parse", "HEAD") == old_head
    assert _git(repository, "status", "--porcelain") == before_status
    assert (repository / "commands" / "apply.md").read_text() == "uncommitted local work\n"
    assert (repository / "notes.txt").read_text() == "keep local notes\n"

    submission = gateway.submit(_hypothesis(), candidate.candidate_commit, candidate.base_commit)

    assert submission.state == "QUEUED"
    assert submission.plan["base_commit"] == remote_master
    assert submission.plan["candidate_commit"] == candidate.candidate_commit


@pytest.mark.parametrize("failure", ["missing_master", "unavailable_origin"])
def test_candidate_stops_before_model_when_remote_master_cannot_be_resolved(tmp_path, monkeypatch, failure) -> None:
    builder, gateway, capability, runner, old_head, _ = _configured_builder(tmp_path, monkeypatch)
    if failure == "missing_master":
        _git(tmp_path / "helixharness-origin.git", "update-ref", "-d", "refs/heads/release")
    else:
        _git(gateway.repository, "remote", "set-url", "origin", str(tmp_path / "unavailable.git"))

    with pytest.raises(CandidateBuildError, match="release"):
        builder.build("candidate-test", _hypothesis(), capability)

    assert not runner.called
    assert not (tmp_path / "worktrees").exists()
    assert _git(gateway.repository, "rev-parse", "HEAD") == old_head


def test_remediation_keeps_explicit_source_candidate_without_remote_refresh(tmp_path, monkeypatch) -> None:
    builder, gateway, capability, _, _, source_candidate = _configured_builder(tmp_path, monkeypatch)
    _git(gateway.repository, "remote", "set-url", "origin", str(tmp_path / "unavailable.git"))

    candidate = builder.build("candidate-repair", _hypothesis(), capability, base_commit=source_candidate)

    assert candidate.base_commit == source_candidate
    assert _git(gateway.repository, "rev-parse", f"{candidate.candidate_commit}^") == source_candidate


def test_retry_of_stale_candidate_builds_latest_master_and_creates_new_run(tmp_path, monkeypatch) -> None:
    builder, gateway, capability, _, old_head, latest_master = _configured_builder(tmp_path, monkeypatch)
    store = EvolutionStore(tmp_path / "retry-evolution.db")
    source = _hypothesis()
    store.begin_cycle("source-cycle", "source-digest", source.created_at, 1)
    store.record_hypotheses("source-cycle", (source,))
    service = EvolutionService(store, (capability,), candidate_builder=builder, evaluation_gateway=gateway)
    old_candidate = service.prepare_hypothesis_candidate(source.hypothesis_id)
    assert old_candidate.base_commit == old_head
    _git(tmp_path / "helixharness-origin.git", "update-ref", "refs/heads/release", latest_master)
    blocked = service.start_selected_hypothesis(source.hypothesis_id)
    assert blocked.state == "BLOCKED"
    assert "configured baseline Commit" in blocked.reason
    service.delete_experiment(blocked.experiment_id, "从列表移除")
    frozen_history = store.get_experiment(blocked.experiment_id)

    retry_id = service.retry_blocked_experiment(blocked.experiment_id)
    candidate = service.prepare_hypothesis_candidate(retry_id)
    assert candidate.base_commit == latest_master
    assert candidate.candidate_id != old_candidate.candidate_id
    assert _git(gateway.repository, "rev-parse", "HEAD") == old_head
    outcome = service.start_selected_hypothesis(retry_id)

    assert outcome.state == "AWAITING_EVALUATION"
    assert store.get_experiment(outcome.experiment_id)["benchmark_run_id"]
    assert store.get_experiment(blocked.experiment_id) == frozen_history
