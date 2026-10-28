from __future__ import annotations

import threading
from datetime import datetime, timezone

from evolution.models import CycleOutcome
from evolution.scheduler import EvolutionScheduler


class _Service:
    def __init__(self, due: bool, *, remediation_error: str | None = None) -> None:
        self.due = due
        self.remediation_error = remediation_error
        self.calls = 0
        self.remediation_calls = 0
        self.second_remediation_call = threading.Event()

    def is_due(self, now):
        return self.due

    def reconcile_evaluations(self):
        return 0

    def discover_benchmark_remediations(self):
        self.remediation_calls += 1
        if self.remediation_calls >= 2:
            self.second_remediation_call.set()
        if self.remediation_error:
            raise RuntimeError(self.remediation_error)
        return 0

    def pending_candidate_preparation_ids(self):
        return ()

    def run_cycle(self, evidence, *, now, force):
        self.calls += 1
        return CycleOutcome("cycle-1", "NOOP", "done")


def test_scheduler_syncs_before_reading_evidence_when_due() -> None:
    events = []
    service = _Service(True)
    scheduler = EvolutionScheduler(
        service,
        sync_source=lambda: events.append("sync"),
        evidence_provider=lambda: events.append("evidence") or [],
    )

    outcome = scheduler.tick(now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert outcome.state == "NOOP"
    assert events == ["sync", "evidence"]
    assert service.calls == 1


def test_scheduler_defaults_remediation_poll_to_thirty_seconds() -> None:
    scheduler = EvolutionScheduler(
        _Service(False),
        sync_source=lambda: None,
        evidence_provider=lambda: [],
    )

    assert scheduler.remediation_poll_seconds == 30.0


def test_scheduler_does_not_sync_before_collection_window_elapsed() -> None:
    events = []
    service = _Service(False)
    scheduler = EvolutionScheduler(
        service,
        sync_source=lambda: events.append("sync"),
        evidence_provider=lambda: events.append("evidence") or [],
    )

    outcome = scheduler.tick(now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert outcome.state == "NOT_DUE"
    assert events == []
    assert service.calls == 0
    assert service.remediation_calls == 1


def test_remediation_discovery_failure_does_not_block_eval_log_cycle() -> None:
    events = []
    service = _Service(True, remediation_error="bench unavailable")
    scheduler = EvolutionScheduler(
        service,
        sync_source=lambda: events.append("sync"),
        evidence_provider=lambda: events.append("evidence") or [],
    )

    outcome = scheduler.tick(now=datetime(2026, 8, 1, tzinfo=timezone.utc))

    assert outcome.state == "NOOP"
    assert events == ["sync", "evidence"]
    assert service.calls == 1
    assert scheduler.last_remediation_error == "bench unavailable"
    assert scheduler.last_error is None


def test_scheduler_projects_benchmark_remediation_without_waiting_for_collection_poll() -> None:
    service = _Service(False)
    scheduler = EvolutionScheduler(
        service,
        sync_source=lambda: None,
        evidence_provider=lambda: [],
        poll_seconds=60,
        remediation_poll_seconds=0.02,
    )

    scheduler.start()
    try:
        assert service.second_remediation_call.wait(timeout=0.3)
    finally:
        scheduler.stop()

    assert service.remediation_calls >= 2


def test_manual_cycle_is_observable_and_rejects_duplicate_start() -> None:
    sync_started = threading.Event()
    release_sync = threading.Event()

    def blocking_sync() -> None:
        sync_started.set()
        assert release_sync.wait(timeout=1)

    scheduler = EvolutionScheduler(
        _Service(True),
        sync_source=blocking_sync,
        evidence_provider=lambda: [],
    )

    assert scheduler.start_manual_cycle() is True
    assert sync_started.wait(timeout=0.2)
    assert scheduler.manual_cycle_status()["state"] == "RUNNING"
    assert scheduler.start_manual_cycle() is False

    release_sync.set()
    assert scheduler.wait_for_manual_cycle(timeout=1)
    status = scheduler.manual_cycle_status()
    assert status["state"] == "COMPLETED"
    assert status["outcome_state"] == "NOOP"
    assert status["outcome_reason"] == "done"


def test_candidate_preparation_is_observable_and_rejects_duplicate_start() -> None:
    started = threading.Event()
    release = threading.Event()

    class CandidateService(_Service):
        def prepare_hypothesis_candidate(self, hypothesis_id: str) -> None:
            assert hypothesis_id == "hyp-1"
            started.set()
            assert release.wait(timeout=1)

    scheduler = EvolutionScheduler(
        CandidateService(True),
        sync_source=lambda: None,
        evidence_provider=lambda: [],
    )

    assert scheduler.start_candidate_preparation("hyp-1") is True
    assert started.wait(timeout=0.2)
    assert scheduler.candidate_preparation_statuses()["hyp-1"]["state"] == "RUNNING"
    assert scheduler.start_candidate_preparation("hyp-1") is False
    assert scheduler.start_candidate_preparation("hyp-2") is False

    release.set()
    assert scheduler.wait_for_candidate_preparation("hyp-1", timeout=1)
    status = scheduler.candidate_preparation_statuses()["hyp-1"]
    assert status["state"] == "COMPLETED"
    assert status["error"] is None


def test_scheduler_automatically_prepares_one_pending_candidate_from_any_source() -> None:
    started = threading.Event()
    release = threading.Event()

    class CandidateService(_Service):
        def pending_candidate_preparation_ids(self):
            return ("hyp-eval-1", "hyp-bench-1")

        def prepare_hypothesis_candidate(self, hypothesis_id: str) -> None:
            assert hypothesis_id == "hyp-eval-1"
            started.set()
            assert release.wait(timeout=1)

    scheduler = EvolutionScheduler(
        CandidateService(False),
        sync_source=lambda: None,
        evidence_provider=lambda: [],
    )

    assert scheduler.project_pending_remediations() == 0
    assert started.wait(timeout=0.2)
    assert scheduler.candidate_preparation_statuses()["hyp-eval-1"]["state"] == "RUNNING"
    assert "hyp-bench-1" not in scheduler.candidate_preparation_statuses()

    release.set()
    assert scheduler.wait_for_candidate_preparation("hyp-eval-1", timeout=1)


def test_projector_does_not_automatically_retry_failed_candidate() -> None:
    calls: list[str] = []

    class CandidateService(_Service):
        def pending_candidate_preparation_ids(self):
            return ("hyp-bench",)

        def prepare_hypothesis_candidate(self, hypothesis_id: str) -> None:
            calls.append(hypothesis_id)
            raise RuntimeError("model unavailable")

    scheduler = EvolutionScheduler(
        CandidateService(False),
        sync_source=lambda: None,
        evidence_provider=lambda: [],
    )

    scheduler.project_pending_remediations()
    assert scheduler.wait_for_candidate_preparation("hyp-bench", timeout=1)
    scheduler.project_pending_remediations()

    assert calls == ["hyp-bench"]
    assert scheduler.candidate_preparation_statuses()["hyp-bench"]["state"] == "FAILED"
