"""Recover pre-Run blocked experiments without changing their frozen history."""

import json
from dataclasses import replace

import pytest

from evolution.models import ExperimentState
from evolution.service import EvolutionService
from evolution.storage import EvolutionStore
from evolution.tests.test_service import _Builder, _capability, _evidence, _FailingSubmitGateway, _Gateway


class _VersionedBuilder(_Builder):
    def build(self, experiment_id, hypothesis, capability, *, base_commit=None):
        candidate = super().build(experiment_id, hypothesis, capability, base_commit=base_commit)
        return replace(candidate, candidate_id=f"cand-{hypothesis.hypothesis_id}", base_commit=f"master-{self.calls}")


def _blocked(tmp_path):
    store = EvolutionStore(tmp_path / "evolution.db")
    builder = _VersionedBuilder()
    service = EvolutionService(store, (_capability(),), candidate_builder=builder, evaluation_gateway=_FailingSubmitGateway())
    hypothesis_id = service.run_cycle((_evidence(),)).hypothesis_ids[0]
    service.prepare_hypothesis_candidate(hypothesis_id)
    outcome = service.start_selected_hypothesis(hypothesis_id)
    return service, builder, hypothesis_id, outcome.experiment_id


@pytest.mark.parametrize("deleted", [False, True])
def test_retry_preserves_history_and_requires_new_candidate_review(tmp_path, deleted):
    service, builder, original_id, experiment_id = _blocked(tmp_path)
    store = service.store
    if deleted:
        service.delete_experiment(experiment_id, "从列表移除")
    original = store.get_hypothesis(original_id)
    old_experiment = store.get_experiment(experiment_id)
    old_candidate = store.get_hypothesis_candidate(original_id)
    old_cycle = store.cycle_for_source_digest(store.cycle_summary()["latest"]["source_digest"])

    retry_id = service.retry_blocked_experiment(experiment_id)

    assert retry_id != original_id
    assert service.retry_blocked_experiment(experiment_id) == retry_id
    assert builder.calls == 1
    retry = store.get_hypothesis(retry_id)
    assert retry["selected_at"] is None
    assert retry["cycle_id"] != original["cycle_id"]
    assert json.loads(retry["payload_json"])["retry_of_experiment_id"] == experiment_id
    assert store.get_hypothesis_candidate(retry_id) is None
    with pytest.raises(ValueError, match="no frozen Candidate"):
        service.start_selected_hypothesis(retry_id)
    candidate = service.prepare_hypothesis_candidate(retry_id)
    assert candidate.base_commit == "master-2"
    assert builder.base_commits == [None, None]
    assert len(store.list_experiments(include_deleted=True)) == 1
    service.evaluation_gateway = _Gateway()
    outcome = service.start_selected_hypothesis(retry_id)
    assert outcome.state == "AWAITING_EVALUATION"
    assert outcome.experiment_id != experiment_id
    assert store.get_hypothesis(original_id) == original
    assert store.get_experiment(experiment_id) == old_experiment
    assert store.get_hypothesis_candidate(original_id) == old_candidate
    assert store.cycle_for_source_digest(old_cycle["source_digest"]) == old_cycle
    with pytest.raises(ValueError, match="no longer selectable"):
        service.start_selected_hypothesis(retry_id)
    assert service.evaluation_gateway.calls == 1
    assert len(store.list_experiments(include_deleted=True)) == 2


@pytest.mark.parametrize("change", ["running", "has_run", "deleted_hypothesis", "ignored", "remediation", "rehearsal"])
def test_retry_rejects_ineligible_sources(tmp_path, change):
    service, _, hypothesis_id, experiment_id = _blocked(tmp_path)
    store = service.store
    if change == "running":
        store.transition_experiment(experiment_id, ExperimentState.AWAITING_EVALUATION, "running")
    elif change == "has_run":
        store.attach_benchmark_run(experiment_id, "run-existing")
    elif change == "deleted_hypothesis":
        service.delete_hypothesis(hypothesis_id, "removed")
    else:
        with store.connect() as connection:
            if change == "ignored":
                connection.execute("UPDATE hypotheses SET ignored_at='now' WHERE hypothesis_id=?", (hypothesis_id,))
            else:
                payload = json.loads(store.get_hypothesis(hypothesis_id)["payload_json"])
                payload.update({"source_kind": "BENCH_RUN"} if change == "remediation" else {"semantic_source": "PARTIAL_REHEARSAL"})
                connection.execute("UPDATE hypotheses SET payload_json=? WHERE hypothesis_id=?", (json.dumps(payload), hypothesis_id))
    with pytest.raises(ValueError):
        service.retry_blocked_experiment(experiment_id)
    assert len(store.list_hypotheses(include_deleted=True)) == 1


def test_retry_does_not_recreate_a_removed_retry(tmp_path):
    service, _, _, experiment_id = _blocked(tmp_path)
    retry_id = service.retry_blocked_experiment(experiment_id)
    service.delete_hypothesis(retry_id, "removed")
    with pytest.raises(ValueError, match="已移除"):
        service.retry_blocked_experiment(experiment_id)
    assert len(service.store.list_hypotheses(include_deleted=True)) == 2
