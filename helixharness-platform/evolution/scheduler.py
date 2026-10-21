"""In-process scheduler that checks the durable 48-hour cycle watermark."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from evolution.models import CycleOutcome
from evolution.service import EvolutionService


class EvolutionScheduler:
    """Single-service scheduler; sync and Gate refresh happen before discovery."""

    def __init__(
        self,
        service: EvolutionService,
        *,
        sync_source: Callable[[], None],
        evidence_provider: Callable[[], Sequence[dict[str, Any]]],
        poll_seconds: int = 300,
        remediation_poll_seconds: float = 30.0,
        candidate_max_concurrency: int = 1,
    ) -> None:
        self.service = service
        self.sync_source = sync_source
        self.evidence_provider = evidence_provider
        self.poll_seconds = max(1, poll_seconds)
        self.remediation_poll_seconds = max(0.05, remediation_poll_seconds)
        self.candidate_max_concurrency = max(1, candidate_max_concurrency)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._remediation_thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self._remediation_lock = threading.Lock()
        self._manual_status_lock = threading.Lock()
        self._manual_thread: threading.Thread | None = None
        self._manual_status: dict[str, object | None] = {
            "state": "IDLE",
            "started_at": None,
            "finished_at": None,
            "outcome_state": None,
            "outcome_reason": None,
            "error": None,
        }
        self._candidate_status_lock = threading.Lock()
        self._candidate_threads: dict[str, threading.Thread] = {}
        self._candidate_statuses: dict[str, dict[str, object | None]] = {}
        self.last_outcome: CycleOutcome | None = None
        self.last_error: str | None = None
        self.last_remediation_error: str | None = None

    def tick(self, *, now: datetime | None = None, force: bool = False) -> CycleOutcome:
        """Run one serialized scheduler tick."""

        with self._tick_lock:
            return self._tick(now=now, force=force)

    def _tick(self, *, now: datetime | None = None, force: bool = False) -> CycleOutcome:
        tick = now or datetime.now(timezone.utc)
        self.service.reconcile_evaluations()
        self.project_pending_remediations()
        if not force and not self.service.is_due(tick):
            outcome = CycleOutcome("", "NOT_DUE", "48-hour collection window has not elapsed")
            self.last_outcome = outcome
            return outcome
        self.sync_source()
        outcome = self.service.run_cycle(self.evidence_provider(), now=tick, force=force)
        self._start_next_candidate()
        self.last_outcome = outcome
        self.last_error = None
        return outcome

    def project_pending_remediations(self) -> int:
        """Project terminal Benchmark facts and queue their Candidate outside Web requests."""

        with self._remediation_lock:
            try:
                created = self.service.discover_benchmark_remediations()
                self._start_next_candidate()
            except Exception as exc:
                # Bench remediation discovery has its own failure domain and must not
                # postpone the eval-log collection lane.
                self.last_remediation_error = str(exc)
                return 0
            self.last_remediation_error = None
            return created

    def _start_next_candidate(self) -> None:
        """Prepare one actionable hypothesis and never auto-retry a recorded failure."""

        with self._candidate_status_lock:
            statuses = {
                hypothesis_id: str(status.get("state") or "")
                for hypothesis_id, status in self._candidate_statuses.items()
            }
        if "RUNNING" in statuses.values():
            return
        for hypothesis_id in self.service.pending_candidate_preparation_ids():
            if hypothesis_id not in statuses:
                self.start_candidate_preparation(hypothesis_id)
                return

    def start_manual_cycle(self) -> bool:
        """Start one observable manual cycle and reject duplicate clicks."""

        with self._manual_status_lock:
            if self._manual_status["state"] == "RUNNING":
                return False
            self._manual_status = {
                "state": "RUNNING",
                "started_at": _utc_now(),
                "finished_at": None,
                "outcome_state": None,
                "outcome_reason": None,
                "error": None,
            }
            self.last_error = None
            thread = threading.Thread(
                target=self._run_manual_cycle,
                name="helixharness-evolution-manual-cycle",
                daemon=True,
            )
            self._manual_thread = thread
            thread.start()
        return True

    def _run_manual_cycle(self) -> None:
        try:
            outcome = self.tick(force=True)
        except Exception as exc:
            error = str(exc)
            self.last_error = error
            update = {
                "state": "FAILED",
                "outcome_state": None,
                "outcome_reason": None,
                "error": error,
            }
        else:
            update = {
                "state": "COMPLETED",
                "outcome_state": outcome.state,
                "outcome_reason": outcome.reason,
                "error": None,
            }
        with self._manual_status_lock:
            self._manual_status.update(update)
            self._manual_status["finished_at"] = _utc_now()

    def manual_cycle_status(self) -> dict[str, object | None]:
        """Return a thread-safe snapshot for Web status polling."""

        with self._manual_status_lock:
            return dict(self._manual_status)

    def wait_for_manual_cycle(self, timeout: float | None = None) -> bool:
        """Wait for the current manual cycle; used by deterministic verification."""

        with self._manual_status_lock:
            thread = self._manual_thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def start_candidate_preparation(self, hypothesis_id: str) -> bool:
        """Start one Candidate build outside the Web request thread."""

        with self._candidate_status_lock:
            current = self._candidate_statuses.get(hypothesis_id, {})
            if current.get("state") == "RUNNING":
                return False
            running = sum(
                status.get("state") == "RUNNING"
                for status in self._candidate_statuses.values()
            )
            if running >= self.candidate_max_concurrency:
                return False
            self._candidate_statuses[hypothesis_id] = {
                "state": "RUNNING",
                "started_at": _utc_now(),
                "finished_at": None,
                "error": None,
            }
            thread = threading.Thread(
                target=self._run_candidate_preparation,
                args=(hypothesis_id,),
                name=f"helixharness-evolution-candidate-{hypothesis_id}",
                daemon=True,
            )
            self._candidate_threads[hypothesis_id] = thread
            thread.start()
        return True

    def _run_candidate_preparation(self, hypothesis_id: str) -> None:
        try:
            self.service.prepare_hypothesis_candidate(hypothesis_id)
        except Exception as exc:
            update: dict[str, object | None] = {
                "state": "FAILED",
                "error": str(exc),
            }
        else:
            update = {
                "state": "COMPLETED",
                "error": None,
            }
        with self._candidate_status_lock:
            self._candidate_statuses[hypothesis_id].update(update)
            self._candidate_statuses[hypothesis_id]["finished_at"] = _utc_now()

    def candidate_preparation_statuses(self) -> dict[str, dict[str, object | None]]:
        """Return observable Candidate build states keyed by hypothesis."""

        with self._candidate_status_lock:
            return {
                hypothesis_id: dict(status)
                for hypothesis_id, status in self._candidate_statuses.items()
            }

    def wait_for_candidate_preparation(
        self,
        hypothesis_id: str,
        timeout: float | None = None,
    ) -> bool:
        """Wait for a Candidate build; used by deterministic verification."""

        with self._candidate_status_lock:
            thread = self._candidate_threads.get(hypothesis_id)
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, name="helixharness-evolution-scheduler", daemon=True)
        self._remediation_thread = threading.Thread(
            target=self._run_remediation_forever,
            name="helixharness-evolution-remediation-projector",
            daemon=True,
        )
        self._thread.start()
        self._remediation_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._remediation_thread is not None:
            self._remediation_thread.join(timeout=5)
        with self._candidate_status_lock:
            candidate_threads = tuple(self._candidate_threads.values())
        for thread in candidate_threads:
            thread.join(timeout=5)

    def _run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # scheduler must remain observable and retry on the next poll
                self.last_error = str(exc)
            self._stop.wait(self.poll_seconds)

    def _run_remediation_forever(self) -> None:
        while not self._stop.is_set():
            self.project_pending_remediations()
            self._stop.wait(self.remediation_poll_seconds)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
