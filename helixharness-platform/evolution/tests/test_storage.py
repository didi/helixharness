from __future__ import annotations

from dataclasses import replace

from evolution.models import (
    AcceptanceContract,
    CandidateVersion,
    ExperimentState,
    Hypothesis,
    HypothesisKind,
)
from evolution.storage import EvolutionStore


def test_list_source_run_hypotheses_returns_the_cross_module_projection(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    store.begin_cycle("cycle-1", "digest-1", "2026-08-11T00:00:00Z", 1)
    repair = Hypothesis(
        hypothesis_id="hypothesis-1",
        kind=HypothesisKind.REPAIR,
        capability_id="workflow-discipline",
        title="修复评测发现的问题",
        rationale="评测分数回退。",
        evidence_ids=("evidence-1",),
        confidence=0.7,
        acceptance=AcceptanceContract(
            capability_id="workflow-discipline",
            case_version_ids=("case-1",),
            expected_effect="关联重评通过。",
            regression_scope=("case-1",),
            protected_paths=(),
        ),
        intervention="修复最小问题范围。",
        falsification_condition="关联重评仍回退。",
        validation_plan="复用冻结评测配置。",
        requires_human_selection=True,
        semantic_source="DETERMINISTIC",
        created_at="2026-08-11T00:00:00Z",
        source_run_id="run-source",
        source_candidate_commit="candidate-commit",
    )
    growth = replace(
        repair,
        hypothesis_id="hypothesis-2",
        kind=HypothesisKind.GROWTH,
        title="扩展新的评测能力",
        source_run_id=None,
        source_candidate_commit=None,
    )
    store.record_hypotheses("cycle-1", (repair, growth))

    assert store.list_source_run_hypotheses("run-source") == [
        {
            "hypothesis_id": "hypothesis-1",
            "kind": "REPAIR",
            "title": "修复评测发现的问题",
            "requires_human_selection": True,
            "selected_at": None,
            "cycle_state": "RUNNING",
            "experiment_id": None,
            "experiment_state": None,
            "candidate_id": None,
            "benchmark_run_id": None,
        }
    ]
    assert store.list_source_run_hypotheses("another-run") == []
    assert store.cycle_summary()["hypothesis_counts"] == {"REPAIR": 1, "GROWTH": 1}


def test_ignore_hypothesis_preserves_record_and_prevents_selection(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    store.begin_cycle("cycle-1", "digest-1", "2026-08-11T00:00:00Z", 1)
    store.record_hypotheses(
        "cycle-1",
        (
            Hypothesis(
                hypothesis_id="hypothesis-1",
                kind=HypothesisKind.REPAIR,
                capability_id="workflow-discipline",
                title="不采纳的改进",
                rationale="证据不足。",
                evidence_ids=("evidence-1",),
                confidence=0.5,
                acceptance=AcceptanceContract(
                    capability_id="workflow-discipline",
                    case_version_ids=("case-1",),
                    expected_effect="无",
                    regression_scope=("case-1",),
                    protected_paths=(),
                ),
                requires_human_selection=True,
                semantic_source="RULE",
                created_at="2026-08-11T00:00:00Z",
            ),
        ),
    )

    assert store.ignore_hypothesis("hypothesis-1", "人工判断不采纳") is True
    row = store.get_hypothesis("hypothesis-1")
    assert row is not None
    assert row["ignored_at"] is not None
    assert row["ignore_reason"] == "人工判断不采纳"
    assert store.select_hypothesis("hypothesis-1") is False


def test_hypothesis_candidate_is_persisted_before_any_experiment(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    store.begin_cycle("cycle-1", "digest-1", "2026-08-11T00:00:00Z", 1)
    store.record_hypotheses(
        "cycle-1",
        (
            Hypothesis(
                hypothesis_id="hypothesis-1",
                kind=HypothesisKind.REPAIR,
                capability_id="workflow-discipline",
                title="修复归档",
                rationale="归档顺序错误。",
                evidence_ids=("evidence-1",),
                confidence=0.8,
                acceptance=AcceptanceContract(
                    capability_id="workflow-discipline",
                    case_version_ids=("case-1",),
                    expected_effect="先验证再归档",
                    regression_scope=("case-1",),
                    protected_paths=(),
                ),
                requires_human_selection=True,
                semantic_source="BENCH_REMEDIATION",
                created_at="2026-08-11T00:00:00Z",
            ),
        ),
    )
    candidate = CandidateVersion(
        candidate_id="candidate-1",
        hypothesis_id="hypothesis-1",
        base_commit="base",
        candidate_commit="candidate",
        candidate_ref="refs/helixharness/evolution/candidate-1",
        worktree_path="/tmp/candidate-1",
        changed_paths=("commands/archive.md",),
        diff_patch="diff --git a/commands/archive.md b/commands/archive.md",
    )

    store.record_hypothesis_candidate(candidate)

    assert store.list_experiments() == []
    stored = store.get_hypothesis_candidate("hypothesis-1")
    assert stored is not None
    assert stored["candidate_id"] == "candidate-1"
    assert "commands/archive.md" in stored["payload_json"]


def test_deleted_hypothesis_and_experiment_leave_worklists_but_keep_audit_records(tmp_path) -> None:
    store = EvolutionStore(tmp_path / "evolution.db")
    store.begin_cycle("cycle-1", "digest-1", "2026-08-11T00:00:00Z", 1)
    store.record_hypotheses(
        "cycle-1",
        (
            Hypothesis(
                hypothesis_id="hypothesis-1",
                kind=HypothesisKind.REPAIR,
                capability_id="workflow-discipline",
                title="可删除的改进",
                rationale="仅用于工作台清理。",
                evidence_ids=("evidence-1",),
                confidence=0.5,
                acceptance=AcceptanceContract(
                    capability_id="workflow-discipline",
                    case_version_ids=("case-1",),
                    expected_effect="保留审计链",
                    regression_scope=("case-1",),
                    protected_paths=(),
                ),
                requires_human_selection=True,
                semantic_source="RULE",
                created_at="2026-08-11T00:00:00Z",
            ),
        ),
    )
    store.create_experiment(
        "experiment-1",
        "cycle-1",
        "hypothesis-1",
        ExperimentState.AWAITING_EVALUATION,
        "正在等待评测",
        "2026-08-11T00:10:00Z",
    )

    assert store.delete_hypothesis("hypothesis-1", "人工从工作台删除") is True
    assert store.list_hypotheses() == []
    hypothesis = store.get_hypothesis("hypothesis-1")
    assert hypothesis is not None
    assert hypothesis["deleted_at"] is not None
    assert store.list_experiments()[0]["experiment_id"] == "experiment-1"

    assert store.delete_experiment("experiment-1", "人工从工作台删除") is True
    assert store.list_experiments() == []
    assert store.list_experiments(include_deleted=True)[0]["experiment_id"] == "experiment-1"
    experiment = store.get_experiment("experiment-1")
    assert experiment is not None
    assert experiment["deleted_at"] is not None
