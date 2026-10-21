from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from evolution.analysis_context import HISTORY_WINDOW_DAYS, build_analysis_contexts
from evolution.models import Capability, EvolutionEvidence


def _capability() -> Capability:
    return Capability(
        capability_id="workflow",
        name="工作流",
        description="工作流纪律",
        selectors=("archive",),
        owned_paths=("commands/",),
        protected_paths=("eval/",),
        case_version_ids=("case-1",),
    )


def _evidence(evidence_id: str, occurred_at: str, **overrides: object) -> EvolutionEvidence:
    values = {
        "evidence_id": evidence_id,
        "source_path": "/tmp/eval-log.json",
        "source_commit": "base",
        "source_hash": "hash",
        "feature": "feature-a",
        "project_name": "demo",
        "helixharness_version": "2.0.0",
        "command": "/helix:archive",
        "stage_kind": "archive",
        "status": "blocked",
        "outcome_reason": "missing evidence",
        "gate_result": "fail",
        "evidence_complete": False,
        "occurred_at": occurred_at,
        "fixed_evidence": {"stage": {"status": "blocked"}},
        "async_evidence": {"tools": {"failed": 1}},
        "code_evidence": {"commit_id": "abc"},
    }
    values.update(overrides)
    return EvolutionEvidence(**values)


def test_context_keeps_all_triggers_and_summarizes_the_full_recent_window() -> None:
    tick = datetime(2026, 8, 23, tzinfo=timezone.utc)
    trigger = _evidence("new:1", "2026-08-23T00:00:00Z")
    unmatched_trigger = _evidence(
        "new:other",
        "2026-08-23T00:01:00Z",
        command="/helix:init",
        stage_kind="requirement",
        outcome_reason="success",
    )
    history = tuple(
        replace(
            trigger,
            evidence_id=f"old:{index}",
            occurred_at=(tick - timedelta(days=index + 1)).isoformat().replace("+00:00", "Z"),
            project_name=f"project-{index % 3}",
        )
        for index in range(100)
    )
    unrelated_recent = _evidence(
        "old:unrelated",
        "2026-08-22T00:00:00Z",
        command="/helix:init",
        stage_kind="requirement",
        outcome_reason="success",
        project_name="another-project",
    )
    undated = _evidence("old:undated", "", admitted_at=None)
    future = _evidence("old:future", "2026-08-24T00:00:00Z")
    snapshots = build_analysis_contexts(
        (trigger, unmatched_trigger, *history, unrelated_recent, undated, future),
        {trigger.evidence_id, unmatched_trigger.evidence_id},
        (_capability(),),
        cycle_id="cycle-1",
        source_digest="digest-1",
        as_of=tick,
        prior_hypotheses=(
            {
                "hypothesis_id": "hyp-old",
                "capability_id": "workflow",
                "title": "旧假设",
                "payload_json": (
                    '{"hypothesis_id":"hyp-old","capability_id":"workflow",'
                    '"kind":"REPAIR","title":"旧假设","evidence_ids":["old:1"]}'
                ),
            },
        ),
        prior_experiments=(
            {
                "experiment_id": "exp-old",
                "hypothesis_id": "hyp-old",
                "capability_id": "workflow",
                "state": "REJECTED",
                "decision_reason": "no clear gain",
            },
        ),
    )

    assert len(snapshots) == 1
    payload = snapshots[0].payload
    assert payload["schema_version"] == "evolution-analysis-context.v2"
    assert [item["evidence_id"] for item in payload["latest_increment"]] == [
        "new:1",
        "new:other",
    ]
    assert [item["evidence_id"] for item in payload["capability_trigger_evidence"]] == [
        "new:1"
    ]
    assert "relevant_history" not in payload
    assert "history_rollups" not in payload
    history_summary = payload["history_summary"]
    assert history_summary["window_days"] == HISTORY_WINDOW_DAYS
    assert history_summary["available_history_count"] == 103
    assert history_summary["in_window_count"] == 91
    assert history_summary["excluded_older_count"] == 10
    assert history_summary["excluded_future_count"] == 1
    assert history_summary["undated_count"] == 1
    assert history_summary["project_count"] == 4
    assert sum(item["evidence_count"] for item in history_summary["weekly_trend"]) == 91
    capability_summary = payload["capability_history_summary"]
    assert capability_summary["available_history_count"] == 102
    assert capability_summary["in_window_count"] == 90
    assert payload["prior_hypotheses"][0]["hypothesis_id"] == "hyp-old"
    assert payload["prior_experiments"][0]["state"] == "REJECTED"
    assert payload["context_evidence_ids"] == ["new:1", "new:other"]
    assert snapshots[0].context_count == 93
    assert "old:99" not in str(payload)


def test_history_window_uses_time_not_similarity_to_the_trigger() -> None:
    tick = datetime(2026, 8, 23, tzinfo=timezone.utc)
    trigger = _evidence("new:1", "2026-08-23T00:00:00Z")
    unrelated_recent = _evidence(
        "old:recent",
        "2026-08-22T00:00:00Z",
        status="completed",
        gate_result="pass",
        outcome_reason="success",
        project_name="another-project",
        command="/helix:init",
        stage_kind="requirement",
    )
    matching_old = _evidence("old:matching", "2025-01-01T00:00:00Z")

    snapshot = build_analysis_contexts(
        (trigger, unrelated_recent, matching_old),
        {trigger.evidence_id},
        (_capability(),),
        cycle_id="cycle-1",
        source_digest="digest-1",
        as_of=tick,
    )[0]

    assert snapshot.payload["history_summary"]["in_window_count"] == 1
    assert snapshot.payload["history_summary"]["excluded_older_count"] == 1
    assert snapshot.payload["capability_history_summary"]["in_window_count"] == 0
    assert snapshot.payload["capability_history_summary"]["excluded_older_count"] == 1
