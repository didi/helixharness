"""48-hour evolution cycle and single-hypothesis experiment orchestration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any, Callable, Mapping, Protocol, Sequence

from evolution.analysis_context import build_analysis_contexts
from evolution.candidate import CandidateBuilder, CandidateBuildError, CandidatePolicyError
from evolution.discovery import SemanticAnalyzer, SemanticProposal, discover_hypotheses
from evolution.models import (
    AcceptanceContract,
    BenchmarkRemediationEvidence,
    CandidateVersion,
    Capability,
    CycleOutcome,
    EvaluationSubmission,
    EvaluationVerdict,
    EvolutionEvidence,
    ExperimentState,
    Hypothesis,
    HypothesisKind,
)
from evolution.storage import EvolutionStore


class EvaluationGateway(Protocol):
    """Composition boundary implemented by web_ui using the Benchmark service."""

    def validate(self, hypothesis: Hypothesis) -> None: ...

    def submit(self, hypothesis: Hypothesis, candidate_commit: str, base_commit: str) -> EvaluationSubmission: ...

    def remediation_candidates(self) -> Sequence[BenchmarkRemediationEvidence]: ...

    def complete_remediation_projection(
        self,
        run_id: str,
        *,
        status: str,
        reason: str,
        hypothesis_id: str | None = None,
    ) -> None: ...

    def validate_remediation(self, hypothesis: Hypothesis) -> None: ...

    def submit_remediation(self, hypothesis: Hypothesis, candidate_commit: str) -> EvaluationSubmission: ...

    def verdict(self, run_id: str) -> EvaluationVerdict | None: ...


class _SnapshotRecordingAnalyzer:
    """Record every model attempt while keeping the inner analyzer stateless."""

    def __init__(
        self,
        store: EvolutionStore,
        analyzer: SemanticAnalyzer,
        context_ids: Mapping[str, str],
    ) -> None:
        self.store = store
        self.analyzer = analyzer
        self.context_ids = dict(context_ids)

    def analyze(
        self,
        capability: Capability,
        evidence: Sequence[EvolutionEvidence],
        *,
        analysis_context: Mapping[str, Any] | None = None,
    ):
        context_id = self.context_ids[capability.capability_id]
        attempt_id = self.store.begin_analysis_attempt(context_id, _utc_now())
        try:
            proposals = self.analyzer.analyze(
                capability,
                evidence,
                analysis_context=analysis_context,
            )
        except Exception as exc:
            self.store.complete_analysis_attempt(
                attempt_id,
                status="FAILED",
                completed_at=_utc_now(),
                error=str(exc)[:1000],
            )
            raise
        self.store.complete_analysis_attempt(
            attempt_id,
            status="ANALYZED",
            completed_at=_utc_now(),
        )
        return proposals


class EvolutionService:
    """Run safe cycles; Benchmark remains the only score and verdict authority."""

    def __init__(
        self,
        store: EvolutionStore,
        capabilities: Sequence[Capability],
        *,
        analyzer: SemanticAnalyzer | None = None,
        candidate_builder: CandidateBuilder | None = None,
        evaluation_gateway: EvaluationGateway | None = None,
        interval: timedelta = timedelta(hours=48),
    ) -> None:
        self.store = store
        self.capabilities = tuple(capabilities)
        self.capability_by_id = {item.capability_id: item for item in capabilities}
        self.analyzer = analyzer
        self.candidate_builder = candidate_builder
        self.evaluation_gateway = evaluation_gateway
        self.interval = interval
        self.runtime_provider: Callable[
            [], tuple[SemanticAnalyzer | None, CandidateBuilder | None, EvaluationGateway | None]
        ] | None = None
        self._cycle_lock = Lock()

    def _refresh_runtime(self) -> None:
        """Resolve settings at an operation boundary without changing active calls."""
        if self.runtime_provider is not None:
            self.analyzer, self.candidate_builder, self.evaluation_gateway = self.runtime_provider()

    def replace_capabilities(self, capabilities: Sequence[Capability]) -> None:
        """Hot-reload one already validated human-owned capability catalog."""

        with self._cycle_lock:
            self.capabilities = tuple(capabilities)
            self.capability_by_id = {item.capability_id: item for item in capabilities}

    def is_due(self, now: datetime | None = None) -> bool:
        tick = now or datetime.now(timezone.utc)
        latest = _parse_timestamp(self.store.latest_collection_at())
        return latest is None or tick - latest >= self.interval

    def run_cycle(
        self,
        evidence_rows: Sequence[dict[str, Any] | EvolutionEvidence],
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> CycleOutcome:
        with self._cycle_lock:
            self._refresh_runtime()
            return self._run_cycle(evidence_rows, now=now, force=force)

    def _run_cycle(
        self,
        evidence_rows: Sequence[dict[str, Any] | EvolutionEvidence],
        *,
        now: datetime | None = None,
        force: bool = False,
    ) -> CycleOutcome:
        tick = now or datetime.now(timezone.utc)
        latest = _parse_timestamp(self.store.latest_collection_at())
        if not force and latest and tick - latest < self.interval:
            return CycleOutcome("", "NOT_DUE", "48-hour collection window has not elapsed")
        evidence = tuple(
            row if isinstance(row, EvolutionEvidence) else EvolutionEvidence.from_mapping(row)
            for row in evidence_rows
        )
        evidence_ids = tuple(sorted(item.evidence_id for item in evidence))
        source_digest = _evidence_digest(evidence)
        started_at = tick.isoformat().replace("+00:00", "Z")
        existing_cycle = self.store.cycle_for_source_digest(source_digest)
        if existing_cycle and str(existing_cycle["state"]) not in {"FAILED", "RUNNING"}:
            existing_cycle_id = str(existing_cycle["cycle_id"])
            if existing_cycle_id:
                self.store.record_cycle_evidence(existing_cycle_id, evidence_ids)
            self.store.record_collection_check(started_at)
            return CycleOutcome("", "NOOP", "no new qualified eval-log evidence")
        known_evidence_ids = self.store.known_evidence_ids()
        focus_evidence_ids = {
            item.evidence_id for item in evidence if item.evidence_id not in known_evidence_ids
        }
        proposed_cycle_id = "cycle_" + hashlib.sha256(
            f"{started_at}:{source_digest}".encode("utf-8")
        ).hexdigest()[:16]
        cycle_id = self.store.begin_or_resume_cycle(
            proposed_cycle_id,
            source_digest,
            started_at,
            len(evidence),
        )
        if not evidence:
            self.store.record_cycle_evidence(cycle_id, evidence_ids)
            outcome = CycleOutcome(cycle_id, "NOOP", "no qualified eval-log evidence")
            self.store.complete_cycle(outcome)
            return outcome
        if not focus_evidence_ids:
            self.store.record_cycle_evidence(cycle_id, evidence_ids)
            outcome = CycleOutcome(
                cycle_id,
                "NOOP",
                "qualified HelixHarness snapshot changed, but contains no newly admitted or updated evidence",
            )
            self.store.complete_cycle(outcome)
            return outcome
        analysis_contexts: dict[str, dict[str, Any]] = {}
        analysis_context_ids: dict[str, str] = {}
        analyzer: SemanticAnalyzer | None = self.analyzer
        try:
            if self.analyzer is not None:
                stored_contexts = {
                    str(row["capability_id"]): row
                    for row in self.store.list_analysis_contexts(cycle_id)
                }
                snapshots = build_analysis_contexts(
                    evidence,
                    focus_evidence_ids,
                    self.capabilities,
                    cycle_id=cycle_id,
                    source_digest=source_digest,
                    as_of=tick,
                    prior_hypotheses=self.store.list_hypotheses(include_deleted=True),
                    prior_experiments=self.store.list_experiments(include_deleted=True),
                )
                for snapshot in snapshots:
                    row = stored_contexts.get(snapshot.capability_id)
                    if row is None:
                        row = self.store.save_analysis_context(snapshot, started_at)
                    analysis_contexts[snapshot.capability_id] = json.loads(
                        str(row["payload_json"])
                    )
                    analysis_context_ids[snapshot.capability_id] = str(
                        row["analysis_context_id"]
                    )
                analyzer = _SnapshotRecordingAnalyzer(
                    self.store,
                    self.analyzer,
                    analysis_context_ids,
                )
            hypotheses = discover_hypotheses(
                evidence,
                self.capabilities,
                analyzer=analyzer,
                focus_evidence_ids=focus_evidence_ids,
                analysis_contexts=analysis_contexts,
                analysis_context_ids=analysis_context_ids,
                now=tick,
            )
        except Exception as exc:
            outcome = CycleOutcome(
                cycle_id,
                "FAILED",
                f"semantic analysis failed: {exc}",
            )
            self.store.complete_cycle(outcome)
            raise
        self.store.record_cycle_evidence(cycle_id, evidence_ids)
        new_hypotheses = self.store.record_hypotheses(cycle_id, hypotheses)
        if not new_hypotheses:
            reason = (
                "no evidence-backed hypothesis discovered"
                if not hypotheses
                else "all discovered hypotheses already exist; repeat loop suppressed"
            )
            outcome = CycleOutcome(cycle_id, "NOOP", reason)
            self.store.complete_cycle(outcome)
            return outcome
        outcome = CycleOutcome(
            cycle_id,
            "AWAITING_HUMAN_SELECTION",
            "candidate preparation may continue automatically; entering an experiment requires human adoption",
            tuple(item.hypothesis_id for item in new_hypotheses),
        )
        self.store.complete_cycle(outcome)
        return outcome

    def start_selected_hypothesis(self, hypothesis_id: str) -> CycleOutcome:
        with self._cycle_lock:
            self._refresh_runtime()
            return self._start_selected_hypothesis(hypothesis_id)

    def _start_selected_hypothesis(self, hypothesis_id: str) -> CycleOutcome:
        row = self.store.get_hypothesis(hypothesis_id)
        if row is None:
            raise ValueError("hypothesis not found")
        if row.get("deleted_at"):
            raise ValueError("deleted hypothesis cannot start an experiment")
        if row.get("ignored_at"):
            raise ValueError("ignored hypothesis cannot start an experiment")
        hypothesis = _decode_hypothesis(row["payload_json"])
        capability = self.capability_by_id.get(hypothesis.capability_id)
        if capability is None:
            raise ValueError("capability no longer exists")
        candidate_row = self.store.get_hypothesis_candidate(hypothesis_id)
        if candidate_row is None:
            raise ValueError("hypothesis has no frozen Candidate; generate and review it before experiment")
        candidate = _decode_candidate(candidate_row["payload_json"])
        if not self.store.select_hypothesis(hypothesis_id):
            raise ValueError("hypothesis is no longer selectable")
        return self._start_experiment(
            row["cycle_id"],
            hypothesis,
            (hypothesis,),
            frozen_candidate=candidate,
        )

    def prepare_hypothesis_candidate(self, hypothesis_id: str) -> CandidateVersion:
        """Build and freeze the code proposal before any Benchmark Experiment starts."""

        with self._cycle_lock:
            self._refresh_runtime()
            row = self.store.get_hypothesis(hypothesis_id)
            if row is None:
                raise ValueError("hypothesis not found")
            if row.get("deleted_at"):
                raise ValueError("deleted hypothesis cannot generate a Candidate")
            if row.get("ignored_at"):
                raise ValueError("ignored hypothesis cannot generate a Candidate")
            existing = self.store.get_hypothesis_candidate(hypothesis_id)
            if existing is not None:
                return _decode_candidate(existing["payload_json"])
            if row.get("selected_at"):
                raise ValueError("selected hypothesis cannot replace its frozen Candidate")
            if self.candidate_builder is None:
                raise ValueError("candidate builder is not configured")
            hypothesis = _decode_hypothesis(row["payload_json"])
            capability = self.capability_by_id.get(hypothesis.capability_id)
            if capability is None:
                raise ValueError("capability no longer exists")
            blocker = self._candidate_preparation_blocker(hypothesis)
            if blocker is not None:
                raise ValueError(blocker)
            try:
                candidate = self._build_and_freeze_candidate(
                    row["cycle_id"],
                    hypothesis,
                    capability,
                    max_attempts=1,
                )
            except CandidateBuildError as exc:
                raise ValueError(f"Candidate preparation blocked: {exc}") from exc
            return candidate

    def retry_blocked_experiment(self, experiment_id: str) -> str:
        """Create a separate reviewable attempt for an ordinary pre-Run blocker."""

        with self._cycle_lock:
            experiment = self.store.get_experiment(experiment_id)
            if experiment is None:
                raise ValueError("实验不存在")
            if experiment["state"] != ExperimentState.BLOCKED.value or experiment.get("benchmark_run_id"):
                raise ValueError("仅支持尚未创建评测 Run 的阻断实验重新生成候选")
            original = self.store.get_hypothesis(experiment["hypothesis_id"])
            if original is None or original.get("deleted_at") or original.get("ignored_at"):
                raise ValueError("来源假设已移除或已忽略，不能重新生成")
            hypothesis = _decode_hypothesis(original["payload_json"])
            if hypothesis.source_kind != "EVAL_LOG" or hypothesis.semantic_source in {"PARTIAL_REHEARSAL", "BENCH_REMEDIATION"}:
                raise ValueError("评测修复和演练需要保留其专属恢复流程")
            identity = hashlib.sha256(f"blocked-experiment-retry:{experiment_id}".encode()).hexdigest()
            retry_id = "hyp_" + identity[:16]
            existing = self.store.get_hypothesis(retry_id)
            if existing is not None:
                if existing.get("deleted_at"):
                    raise ValueError("本次重新生成记录已移除，原实验与候选仍保留")
                return retry_id
            blocker = self._candidate_preparation_blocker(hypothesis)
            if blocker:
                raise ValueError(blocker)
            if self.candidate_builder is None:
                raise ValueError("candidate builder is not configured")
            retry = replace(
                hypothesis,
                hypothesis_id=retry_id,
                retry_of_experiment_id=experiment_id,
                created_at=_utc_now(),
            )
            self.store.record_retry_hypothesis("cycle_" + identity[:16], identity, retry)
            return retry_id

    def ignore_hypothesis(self, hypothesis_id: str, reason: str) -> None:
        """Record a human non-adoption decision without deleting evidence history."""

        row = self.store.get_hypothesis(hypothesis_id)
        if row is None:
            raise ValueError("hypothesis not found")
        if row.get("deleted_at"):
            raise ValueError("deleted hypothesis cannot be ignored")
        if row.get("ignored_at"):
            return
        if not self.store.ignore_hypothesis(hypothesis_id, reason.strip() or "人工判断不采纳"):
            raise ValueError("selected or started hypothesis cannot be ignored")

    def delete_hypothesis(self, hypothesis_id: str, reason: str) -> None:
        """Remove one hypothesis from the worklist without destroying its audit chain."""

        row = self.store.get_hypothesis(hypothesis_id)
        if row is None:
            raise ValueError("hypothesis not found")
        if row.get("deleted_at"):
            return
        if not self.store.delete_hypothesis(
            hypothesis_id,
            reason.strip() or "人工从工作台删除",
        ):
            raise ValueError("hypothesis could not be deleted")

    def delete_experiment(self, experiment_id: str, reason: str) -> None:
        """Remove one experiment from the worklist while its Run continues to reconcile."""

        row = self.store.get_experiment(experiment_id)
        if row is None:
            raise ValueError("experiment not found")
        if row.get("deleted_at"):
            return
        if not self.store.delete_experiment(
            experiment_id,
            reason.strip() or "人工从工作台删除",
        ):
            raise ValueError("experiment could not be deleted")

    def discover_benchmark_remediations(self) -> int:
        """Persist repair hypotheses from terminal Benchmark evidence."""

        if self.evaluation_gateway is None:
            return 0
        provider = getattr(self.evaluation_gateway, "remediation_candidates", None)
        if not callable(provider):
            return 0
        created = 0
        for evidence in provider():
            hypothesis_id, inserted = self._record_benchmark_remediation(evidence)
            completer = getattr(
                self.evaluation_gateway,
                "complete_remediation_projection",
                None,
            )
            if callable(completer):
                completer(
                    evidence.source_run_id,
                    status="READY" if hypothesis_id else "NOT_ELIGIBLE",
                    reason=(
                        "具体修复假设已生成"
                        if hypothesis_id
                        else "终态结果没有形成可执行且证据充分的修复假设"
                    ),
                    hypothesis_id=hypothesis_id,
                )
            if inserted:
                created += 1
        return created

    def pending_candidate_preparation_ids(self) -> tuple[str, ...]:
        """Return every actionable hypothesis that still needs a frozen Candidate."""

        prepared_ids = {
            str(row["hypothesis_id"])
            for row in self.store.list_hypothesis_candidates()
        }
        pending: list[str] = []
        for row in self.store.list_hypotheses():
            hypothesis_id = str(row["hypothesis_id"])
            if (
                hypothesis_id in prepared_ids
                or row.get("ignored_at")
                or row.get("selected_at")
            ):
                continue
            hypothesis = _decode_hypothesis(row["payload_json"])
            if self._candidate_preparation_blocker(hypothesis) is None:
                pending.append(hypothesis_id)
        return tuple(pending)

    def _candidate_preparation_blocker(self, hypothesis: Hypothesis) -> str | None:
        capability = self.capability_by_id.get(hypothesis.capability_id)
        if capability is None:
            return "目标能力已不在当前能力目录"
        return None

    def _record_benchmark_remediation(
        self,
        evidence: BenchmarkRemediationEvidence,
    ) -> tuple[str | None, bool]:
        if evidence.decision_status not in {"BLOCKED", "DO_NOT_RECOMMEND", "NO_CLEAR_GAIN", "RECOMMEND"}:
            return None, False
        if not evidence.findings or evidence.base_commit == evidence.candidate_commit:
            return None, False
        source_digest = hashlib.sha256(
            f"benchmark-remediation:{evidence.source_run_id}".encode("utf-8")
        ).hexdigest()
        if self.store.source_digest_exists(source_digest):
            existing = self.store.list_source_run_hypotheses(evidence.source_run_id)
            return (
                str(existing[0]["hypothesis_id"]) if existing else None,
                False,
            )
        capability = self._remediation_capability(evidence)
        if capability is None:
            return None, False
        cycle_id = "cycle_bench_" + hashlib.sha256(
            evidence.source_run_id.encode("utf-8")
        ).hexdigest()[:12]
        evidence_ids = tuple(
            sorted(
                {
                    evidence_id
                    for finding in evidence.findings
                    for evidence_id in finding.evidence_ids
                }
            )
        )
        proposal = _benchmark_remediation_proposal(capability, evidence)
        narrative_analyzer = getattr(self.analyzer, "analyze_benchmark_remediation", None)
        if callable(narrative_analyzer):
            try:
                proposal = narrative_analyzer(capability, evidence) or proposal
            except (KeyError, RuntimeError, TypeError, ValueError):
                pass
        hypothesis_id = "hyp_bench_" + hashlib.sha256(
            f"{evidence.source_run_id}:{capability.capability_id}:{evidence_ids}".encode("utf-8")
        ).hexdigest()[:12]
        hypothesis = Hypothesis(
            hypothesis_id=hypothesis_id,
            kind=HypothesisKind.REPAIR,
            capability_id=capability.capability_id,
            title=proposal.title,
            rationale=proposal.rationale,
            evidence_ids=evidence_ids,
            confidence=min(0.95, 0.65 + 0.05 * len(evidence.findings)),
            acceptance=AcceptanceContract(
                capability_id=capability.capability_id,
                case_version_ids=evidence.case_ids,
                expected_effect=proposal.expected_effect,
                regression_scope=evidence.case_ids,
                protected_paths=capability.protected_paths,
                case_selection_mode="EXPLICIT",
                case_selection_max_cases=len(evidence.case_ids),
                allowed_case_profiles=(evidence.profile,),
                auto_workflow_confirmation=True,
            ),
            requires_human_selection=True,
            semantic_source="BENCH_REMEDIATION",
            created_at=evidence.created_at,
            source_kind="BENCH_RUN",
            source_run_id=evidence.source_run_id,
            source_root_run_id=evidence.root_run_id,
            source_base_commit=evidence.base_commit,
            source_candidate_commit=evidence.candidate_commit,
            intervention=proposal.intervention,
            falsification_condition=proposal.falsification_condition,
            validation_plan=proposal.validation_plan,
            generation_basis=(
                {
                    "source_kind": "BENCH_RUN",
                    "source_run_id": evidence.source_run_id,
                    "decision_status": evidence.decision_status,
                    "decision_reason": evidence.decision_reason,
                    "findings": [asdict(finding) for finding in evidence.findings],
                },
            ),
        )
        self.store.begin_cycle(
            cycle_id,
            source_digest,
            evidence.created_at,
            len(evidence_ids),
        )
        inserted = self.store.record_hypotheses(cycle_id, (hypothesis,))
        outcome = CycleOutcome(
            cycle_id,
            "AWAITING_HUMAN_SELECTION" if inserted else "NOOP",
            "terminal Benchmark produced one remediation plan draft" if inserted else "remediation draft already exists",
            tuple(item.hypothesis_id for item in inserted),
        )
        self.store.complete_cycle(outcome)
        return (hypothesis_id if inserted else None), bool(inserted)

    def _remediation_capability(
        self,
        evidence: BenchmarkRemediationEvidence,
    ) -> Capability | None:
        searchable = " ".join(
            (
                evidence.profile,
                evidence.decision_reason,
                *(finding.summary for finding in evidence.findings),
            )
        ).lower()
        ranked = sorted(
            (
                (
                    sum(selector.lower() in searchable for selector in capability.selectors),
                    capability.capability_id,
                    capability,
                )
                for capability in self.capabilities
            ),
            key=lambda item: (-item[0], item[1]),
        )
        return ranked[0][2] if ranked and ranked[0][0] > 0 else None

    def run_partial_rehearsal(
        self,
        evidence_rows: Sequence[dict[str, Any] | EvolutionEvidence],
        *,
        source_hash: str,
        issue_codes: Sequence[str],
        retry_of: str | None = None,
        now: datetime | None = None,
    ) -> CycleOutcome:
        """Exercise Candidate and Benchmark with one PARTIAL source, never promotion.

        The eval module must explicitly admit the source as an archived, terminal
        PARTIAL document before calling this method.  Rehearsal evidence receives a
        separate identity and provenance so it cannot be confused with VALID input.
        """

        with self._cycle_lock:
            tick = now or datetime.now(timezone.utc)
            normalized_hash = source_hash.strip()
            if not normalized_hash or not issue_codes:
                raise ValueError("PARTIAL rehearsal requires source hash and quality issues")
            raw_evidence = tuple(
                row if isinstance(row, EvolutionEvidence) else EvolutionEvidence.from_mapping(row)
                for row in evidence_rows
            )
            if not raw_evidence:
                raise ValueError("PARTIAL rehearsal has no projected telemetry entries")
            if any(item.source_hash != normalized_hash for item in raw_evidence):
                raise ValueError("PARTIAL rehearsal evidence does not match the selected source hash")
            if retry_of:
                previous = self.store.get_experiment(retry_of)
                if previous is None:
                    raise ValueError("PARTIAL rehearsal retry target does not exist")
                previous_hypothesis = json.loads(previous["hypothesis_json"])
                if (
                    previous["state"] != ExperimentState.BLOCKED.value
                    or previous.get("candidate_id") is not None
                    or previous.get("benchmark_run_id") is not None
                    or previous_hypothesis.get("semantic_source") != "PARTIAL_REHEARSAL"
                ):
                    raise ValueError("PARTIAL rehearsal retry requires a pre-Benchmark blocked rehearsal")
            identity_prefix = (
                f"partial-rehearsal-retry:{retry_of}"
                if retry_of
                else "partial-rehearsal"
            )
            evidence = tuple(
                replace(item, evidence_id=f"{identity_prefix}:{item.evidence_id}")
                for item in raw_evidence
            )
            hypotheses = tuple(
                item
                for item in discover_hypotheses(
                    evidence,
                    self.capabilities,
                    analyzer=None,
                    now=tick,
                )
                if item.kind is HypothesisKind.REPAIR
            )
            if len(hypotheses) != 1:
                raise ValueError("PARTIAL rehearsal must resolve to exactly one repair hypothesis")
            failure_context = "; ".join(
                sorted(
                    {
                        (
                            f"stage={item.stage_kind or 'unknown'}, command={item.command or 'unknown'}, "
                            f"status={item.status or 'unknown'}, outcome={item.outcome_reason or 'unknown'}, "
                            f"gate={item.gate_result or 'unknown'}, "
                            f"evidence_complete={item.evidence_complete}"
                        )
                        for item in raw_evidence
                        if item.status in {"blocked", "partial"}
                        or (item.gate_result or "").lower() in {"fail", "failed", "reject", "rejected"}
                        or item.evidence_complete is False
                    }
                )
            )
            rehearsal_label = "PARTIAL 演练重试" if retry_of else "PARTIAL 演练"
            hypothesis = replace(
                hypotheses[0],
                title=f"[{rehearsal_label}] {hypotheses[0].title}",
                rationale=(
                    f"{hypotheses[0].rationale}；已清洗失败上下文：{failure_context}"
                    + (f"；retry_of={retry_of}" if retry_of else "")
                ),
                requires_human_selection=False,
                semantic_source="PARTIAL_REHEARSAL",
                source_quality_issues=tuple(sorted(set(issue_codes))),
                retry_of_experiment_id=retry_of,
            )
            digest_payload = {
                "admission": "PARTIAL_REHEARSAL",
                "source_hash": normalized_hash,
                "issue_codes": sorted(set(issue_codes)),
                "evidence_digest": _evidence_digest(evidence),
                "retry_of": retry_of,
            }
            source_digest = hashlib.sha256(
                json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            started_at = tick.isoformat().replace("+00:00", "Z")
            if self.store.source_digest_exists(source_digest):
                return CycleOutcome("", "NOOP", "this PARTIAL rehearsal has already been started")
            cycle_id = "cycle_" + hashlib.sha256(
                f"{started_at}:{source_digest}".encode("utf-8")
            ).hexdigest()[:16]
            self.store.begin_cycle(cycle_id, source_digest, started_at, len(evidence))
            inserted = self.store.record_hypotheses(cycle_id, (hypothesis,))
            if not inserted:
                outcome = CycleOutcome(cycle_id, "NOOP", "PARTIAL rehearsal hypothesis already exists")
                self.store.complete_cycle(outcome)
                return outcome
            self.store.select_hypothesis(hypothesis.hypothesis_id)
            return self._prepare_and_start_experiment(cycle_id, hypothesis, (hypothesis,))

    def reconcile_evaluations(self) -> int:
        """Consume Benchmark verdicts without recomputing any score or gate."""

        if self.evaluation_gateway is None:
            return 0
        reconciled = 0
        for experiment in self.store.list_experiments(include_deleted=True):
            if experiment["state"] != ExperimentState.AWAITING_EVALUATION.value:
                continue
            run_id = experiment.get("benchmark_run_id")
            if not run_id:
                continue
            verdict = self.evaluation_gateway.verdict(str(run_id))
            if verdict is None:
                continue
            hypothesis_payload = json.loads(experiment["hypothesis_json"])
            is_partial_rehearsal = hypothesis_payload.get("semantic_source") == "PARTIAL_REHEARSAL"
            if verdict.decision_status == "RECOMMEND" and is_partial_rehearsal:
                state = ExperimentState.INCONCLUSIVE
            elif verdict.decision_status == "RECOMMEND":
                state = ExperimentState.ACCEPTED
                candidate = json.loads(experiment["candidate_json"])
                evaluation_plan = (
                    json.loads(experiment["evaluation_plan_json"])
                    if experiment.get("evaluation_plan_json")
                    else {}
                )
                self.store.record_promotion_bundle(
                    experiment["experiment_id"],
                    {
                        "schema_version": "evolution-promotion-bundle.v1",
                        "experiment_id": experiment["experiment_id"],
                        "hypothesis_id": experiment["hypothesis_id"],
                        "capability_id": experiment["capability_id"],
                        "candidate_id": candidate["candidate_id"],
                        "candidate_commit": candidate["candidate_commit"],
                        "candidate_ref": candidate["candidate_ref"],
                        "benchmark_run_id": run_id,
                        "benchmark_decision": verdict.decision_status,
                        "benchmark_calculation_id": verdict.calculation_id,
                        "evaluation_plan": evaluation_plan,
                        "release_mode": "HUMAN_ONLY",
                    },
                )
            elif verdict.decision_status == "INCONCLUSIVE":
                state = ExperimentState.INCONCLUSIVE
            else:
                state = ExperimentState.REJECTED
            decision_reason = (
                "Benchmark RECOMMEND observed, but PARTIAL_REHEARSAL is non-promotable"
                if verdict.decision_status == "RECOMMEND" and is_partial_rehearsal
                else f"Benchmark {verdict.decision_status}: {verdict.decision_reason}"
            )
            self.store.transition_experiment(
                experiment["experiment_id"],
                state,
                decision_reason,
            )
            reconciled += 1
        return reconciled

    def resume_partial_benchmark_submission(self, experiment_id: str) -> CycleOutcome:
        """Reuse one frozen rehearsal Candidate after a pre-Run submission failure."""

        row = self.store.get_experiment(experiment_id)
        if row is None:
            raise ValueError("PARTIAL rehearsal experiment not found")
        hypothesis = _decode_hypothesis(row["hypothesis_json"])
        if (
            row["state"] != ExperimentState.BLOCKED.value
            or not row.get("candidate_id")
            or row.get("benchmark_run_id")
            or hypothesis.semantic_source != "PARTIAL_REHEARSAL"
        ):
            raise ValueError("resume requires a blocked PARTIAL rehearsal with one frozen Candidate and no Run")
        if self.evaluation_gateway is None:
            raise ValueError("Benchmark gateway is not configured")
        candidate = json.loads(row["candidate_json"])
        try:
            if hypothesis.source_kind == "BENCH_RUN":
                self.evaluation_gateway.validate_remediation(hypothesis)
            else:
                self.evaluation_gateway.validate(hypothesis)
            submission = self.evaluation_gateway.submit(
                hypothesis,
                str(candidate["candidate_commit"]),
                str(candidate["base_commit"]),
            )
        except Exception as exc:
            reason = f"Benchmark submission blocked: {exc}"
            self.store.transition_experiment(experiment_id, ExperimentState.BLOCKED, reason)
            outcome = CycleOutcome(
                row["cycle_id"],
                "BLOCKED",
                reason,
                (hypothesis.hypothesis_id,),
                experiment_id,
            )
            self.store.complete_cycle(outcome)
            return outcome
        self.store.attach_benchmark_run(
            experiment_id,
            submission.run_id,
            evaluation_plan=submission.plan,
        )
        reason = f"Benchmark Run {submission.run_id} submitted from frozen Candidate"
        self.store.transition_experiment(
            experiment_id,
            ExperimentState.AWAITING_EVALUATION,
            reason,
        )
        outcome = CycleOutcome(
            row["cycle_id"],
            ExperimentState.AWAITING_EVALUATION.value,
            reason,
            (hypothesis.hypothesis_id,),
            experiment_id,
        )
        self.store.complete_cycle(outcome)
        return outcome

    def _prepare_and_start_experiment(
        self,
        cycle_id: str,
        hypothesis: Hypothesis,
        hypotheses: tuple[Hypothesis, ...],
    ) -> CycleOutcome:
        """Freeze a Candidate first, then start a Benchmark-only Experiment."""

        preflight_reason = self._benchmark_preflight_reason(hypothesis)
        if preflight_reason is not None:
            return self._record_blocked_experiment(
                cycle_id,
                hypothesis,
                hypotheses,
                preflight_reason,
            )
        capability = self.capability_by_id[hypothesis.capability_id]
        try:
            candidate = self._build_and_freeze_candidate(
                cycle_id,
                hypothesis,
                capability,
                max_attempts=2,
            )
        except CandidateBuildError as exc:
            reason = f"Candidate preparation blocked: {exc}"
            outcome = CycleOutcome(
                cycle_id,
                "BLOCKED",
                reason,
                tuple(item.hypothesis_id for item in hypotheses),
            )
            self.store.complete_cycle(outcome)
            return outcome
        return self._start_experiment(
            cycle_id,
            hypothesis,
            hypotheses,
            frozen_candidate=candidate,
            preflight_complete=True,
        )

    def _build_and_freeze_candidate(
        self,
        cycle_id: str,
        hypothesis: Hypothesis,
        capability: Capability,
        *,
        max_attempts: int,
    ) -> CandidateVersion:
        """Generate and persist the immutable code proposal outside any Experiment."""

        existing = self.store.get_hypothesis_candidate(hypothesis.hypothesis_id)
        if existing is not None:
            return _decode_candidate(existing["payload_json"])
        if self.candidate_builder is None:
            raise CandidateBuildError("candidate builder is not configured")
        candidate_build_id = "candidate_" + hashlib.sha256(
            f"{cycle_id}:{hypothesis.hypothesis_id}".encode("utf-8")
        ).hexdigest()[:16]
        candidate: CandidateVersion | None = None
        last_error: CandidateBuildError | None = None
        for _attempt in range(max_attempts):
            try:
                if hypothesis.source_kind == "BENCH_RUN":
                    if not hypothesis.source_candidate_commit:
                        raise CandidateBuildError(
                            "Benchmark remediation is missing its source Candidate commit"
                        )
                    candidate = self.candidate_builder.build(
                        candidate_build_id,
                        hypothesis,
                        capability,
                        base_commit=hypothesis.source_candidate_commit,
                    )
                else:
                    candidate = self.candidate_builder.build(
                        candidate_build_id,
                        hypothesis,
                        capability,
                    )
                break
            except CandidatePolicyError as exc:
                last_error = exc
                break
            except CandidateBuildError as exc:
                last_error = exc
        if candidate is None:
            raise last_error or CandidateBuildError("candidate generation produced no frozen version")
        self.store.record_hypothesis_candidate(candidate)
        return candidate

    def _benchmark_preflight_reason(self, hypothesis: Hypothesis) -> str | None:
        if self.evaluation_gateway is None:
            return "Benchmark gateway is not configured"
        try:
            if hypothesis.source_kind == "BENCH_RUN":
                self.evaluation_gateway.validate_remediation(hypothesis)
            else:
                self.evaluation_gateway.validate(hypothesis)
        except Exception as exc:
            return f"Benchmark preflight blocked: {exc}"
        return None

    def _record_blocked_experiment(
        self,
        cycle_id: str,
        hypothesis: Hypothesis,
        hypotheses: tuple[Hypothesis, ...],
        reason: str,
        *,
        candidate: CandidateVersion | None = None,
    ) -> CycleOutcome:
        experiment_id = "exp_" + hashlib.sha256(
            f"{cycle_id}:{hypothesis.hypothesis_id}".encode("utf-8")
        ).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.store.create_experiment(
            experiment_id,
            cycle_id,
            hypothesis.hypothesis_id,
            ExperimentState.BLOCKED,
            reason,
            now,
            candidate_id=candidate.candidate_id if candidate else None,
        )
        outcome = CycleOutcome(
            cycle_id,
            "BLOCKED",
            reason,
            tuple(item.hypothesis_id for item in hypotheses),
            experiment_id,
        )
        self.store.complete_cycle(outcome)
        return outcome

    def _start_experiment(
        self,
        cycle_id: str,
        hypothesis: Hypothesis,
        hypotheses: tuple[Hypothesis, ...],
        *,
        frozen_candidate: CandidateVersion,
        preflight_complete: bool = False,
    ) -> CycleOutcome:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        experiment_id = "exp_" + hashlib.sha256(
            f"{cycle_id}:{hypothesis.hypothesis_id}".encode("utf-8")
        ).hexdigest()[:16]
        if not preflight_complete:
            preflight_reason = self._benchmark_preflight_reason(hypothesis)
            if preflight_reason is not None:
                return self._record_blocked_experiment(
                    cycle_id,
                    hypothesis,
                    hypotheses,
                    preflight_reason,
                    candidate=frozen_candidate,
                )
        self.store.create_experiment(
            experiment_id,
            cycle_id,
            hypothesis.hypothesis_id,
            ExperimentState.AWAITING_EVALUATION,
            "frozen Candidate approved; submitting Benchmark Run",
            now,
            candidate_id=frozen_candidate.candidate_id,
        )
        try:
            if hypothesis.source_kind == "BENCH_RUN":
                submission = self.evaluation_gateway.submit_remediation(
                    hypothesis,
                    frozen_candidate.candidate_commit,
                )
            else:
                submission = self.evaluation_gateway.submit(
                    hypothesis,
                    frozen_candidate.candidate_commit,
                    frozen_candidate.base_commit,
                )
        except Exception as exc:
            reason = f"Benchmark submission blocked: {exc}"
            self.store.transition_experiment(experiment_id, ExperimentState.BLOCKED, reason)
            outcome = CycleOutcome(
                cycle_id,
                "BLOCKED",
                reason,
                tuple(item.hypothesis_id for item in hypotheses),
                experiment_id,
            )
            self.store.complete_cycle(outcome)
            return outcome
        self.store.attach_benchmark_run(
            experiment_id,
            submission.run_id,
            evaluation_plan=submission.plan,
        )
        reason = f"Benchmark Run {submission.run_id} submitted"
        outcome = CycleOutcome(
            cycle_id,
            ExperimentState.AWAITING_EVALUATION.value,
            reason,
            tuple(item.hypothesis_id for item in hypotheses),
            experiment_id,
        )
        self.store.complete_cycle(outcome)
        return outcome


def _evidence_digest(evidence: Sequence[EvolutionEvidence]) -> str:
    identity = json.dumps(sorted(item.evidence_id for item in evidence), separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode_hypothesis(raw: str) -> Hypothesis:
    item = json.loads(raw)
    acceptance = item["acceptance"]
    return Hypothesis(
        hypothesis_id=item["hypothesis_id"],
        kind=HypothesisKind(item["kind"]),
        capability_id=item["capability_id"],
        title=item["title"],
        rationale=item["rationale"],
        evidence_ids=tuple(item["evidence_ids"]),
        confidence=float(item["confidence"]),
        acceptance=AcceptanceContract(
            capability_id=acceptance["capability_id"],
            case_version_ids=tuple(acceptance["case_version_ids"]),
            expected_effect=acceptance["expected_effect"],
            regression_scope=tuple(acceptance["regression_scope"]),
            protected_paths=tuple(acceptance["protected_paths"]),
            case_selection_mode=str(acceptance.get("case_selection_mode", "EXPLICIT")),
            case_selection_max_cases=int(acceptance.get("case_selection_max_cases", 3)),
            allowed_case_profiles=tuple(acceptance.get("allowed_case_profiles", ())),
            auto_workflow_confirmation=bool(
                acceptance.get("auto_workflow_confirmation", False)
            ),
        ),
        requires_human_selection=bool(item["requires_human_selection"]),
        semantic_source=item["semantic_source"],
        created_at=item["created_at"],
        source_quality_issues=tuple(item.get("source_quality_issues") or ()),
        context_evidence_ids=tuple(item.get("context_evidence_ids") or ()),
        analysis_context_id=item.get("analysis_context_id"),
        retry_of_experiment_id=item.get("retry_of_experiment_id"),
        source_kind=str(item.get("source_kind") or "EVAL_LOG"),
        source_run_id=item.get("source_run_id"),
        source_root_run_id=item.get("source_root_run_id"),
        source_base_commit=item.get("source_base_commit"),
        source_candidate_commit=item.get("source_candidate_commit"),
        intervention=str(item.get("intervention") or ""),
        falsification_condition=str(item.get("falsification_condition") or ""),
        validation_plan=str(item.get("validation_plan") or ""),
        generation_basis=tuple(
            dict(value)
            for value in item.get("generation_basis") or ()
            if isinstance(value, dict)
        ),
    )


def _benchmark_remediation_proposal(
    capability: Capability,
    evidence: BenchmarkRemediationEvidence,
) -> SemanticProposal:
    """Create safe operator copy when model-authored wording is unavailable."""

    raw = " ".join(
        value
        for finding in evidence.findings
        for value in (
            finding.category,
            finding.summary,
            finding.repair_target,
            finding.observed_difference,
            finding.diagnosis,
        )
    ).upper()
    focus: list[str] = []
    if "CORE_BUSINESS" in raw or "业务" in raw:
        focus.append("业务结果")
    if "WORKFLOW_TERMINAL" in raw or "终态" in raw or "流程" in raw:
        focus.append("流程完成度")
    if "EVIDENCE" in raw or "证据" in raw:
        focus.append("结果可验证性")
    if "SCORE_REGRESSION" in raw or "得分" in raw or "回退" in raw:
        focus.append("整体表现")
    focus_text = "、".join(dict.fromkeys(focus)) or "执行稳定性"
    return SemanticProposal(
        title=f"提升{capability.name}的{focus_text}",
        rationale=(
            f"评测表明，当前版本在部分业务场景中的{focus_text}还不够稳定。"
            "为了避免不稳定表现进入后续使用，需要先提高不同场景下的一致性。"
        ),
        intervention=(
            f"围绕{focus_text}梳理并调整{capability.name}的处理流程，"
            "补齐容易中断、结果不一致或难以确认完成的环节。"
        ),
        expected_effect="让相同业务场景能够更稳定地完成，并产出清晰、可确认的结果。",
        validation_plan="沿用原评测的业务场景和运行条件重新对比，只替换修复后的版本。",
        falsification_condition="如果相同场景仍然不稳定，或整体表现没有改善，则本次修改不成立。",
        confidence=min(0.95, 0.65 + 0.05 * len(evidence.findings)),
    )


def _decode_candidate(raw: str) -> CandidateVersion:
    item = json.loads(raw)
    return CandidateVersion(
        candidate_id=str(item["candidate_id"]),
        hypothesis_id=str(item["hypothesis_id"]),
        base_commit=str(item["base_commit"]),
        candidate_commit=str(item["candidate_commit"]),
        candidate_ref=str(item["candidate_ref"]),
        worktree_path=str(item["worktree_path"]),
        changed_paths=tuple(item.get("changed_paths") or ()),
        diff_summary=str(item.get("diff_summary") or ""),
        diff_patch=str(item.get("diff_patch") or ""),
        diff_truncated=bool(item.get("diff_truncated", False)),
    )
