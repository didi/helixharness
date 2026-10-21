from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bench.scenario_diagnostics import (
    ScenarioDiagnosticRetentionPolicy,
    ScenarioDiagnosticRetentionService,
)


def _run_id(index: int) -> str:
    return f"sv2_{index:032x}"


def _job_id(index: int) -> str:
    return f"sj_{index:032x}"


def _outcome() -> dict[str, object]:
    return {
        "terminal_status": "FAILED",
        "case_hard_gates": ["WORKFLOW_TERMINAL_FAILURE"],
        "global_hard_gates": [],
        "incompatibility_reasons": ["WORKFLOW_ARTIFACT_EVIDENCE_UNAVAILABLE"],
        "reached_comparable_business_terminal": False,
        "assessment_compatible": False,
        "resources": {
            "agent_tokens": 101,
            "active_execution_seconds": 12.5,
            "workflow_rework_actions": 1,
        },
        "verification": {
            "status": "FAIL",
            "checks": [
                {
                    "check_id": "F2P-1",
                    "kind": "FAIL_TO_PASS",
                    "status": "FAIL",
                    "duration_ms": 123,
                    "timed_out": False,
                    "raw_output": "private verification output",
                }
            ],
        },
        "initialization": {"status": "NOT_APPLICABLE"},
        "evidence": [
            {
                "evidence_id": "code-change",
                "kind": "CODE_CHANGESET",
                "status": "PRESENT",
                "changed_paths": ["src/SecretImplementation.java"],
                "content": "class SecretImplementation {}",
            }
        ],
        "execution": {
            "thread_id": "thread-1",
            "turn_count": 1,
            "completed_stage_ids": [],
            "terminal_reason": "TURN_EXPLICIT_FAILED",
            "turn_evidence": [
                {
                    "turn_sequence": 1,
                    "turn_id": "turn-1",
                    "input_kind": "WORKFLOW_STEP",
                    "input_text": "private requirement text",
                    "workflow_step_id": "STEP_01",
                    "milestone_id": "STEP_01_COMPLETED",
                    "classification": "FAILED",
                    "started_at_ms": 1_000,
                    "completed_at_ms": 2_000,
                    "duration_ms": 1_000,
                    "final_response": "class SecretImplementation {} api_key=super-secret",
                    "error": {
                        "type": "TurnFailed",
                        "message": "provider failed api_key=super-secret",
                    },
                    "events": [
                        {
                            "event_type": "workflow/closure",
                            "category": "workflow",
                            "status": "FAILED",
                            "summary": "raw response summary",
                            "payload": {
                                "reason": "STAGE_REWORK_CYCLE_LIMIT_EXHAUSTED",
                                "cycle": 3,
                                "raw_content": "private tool output",
                            },
                            "occurred_at_ms": 2_000,
                        }
                    ],
                }
            ],
        },
        "immutable_judge_evidence": [{"content": "private source and code"}],
    }


def test_capture_keeps_structured_diagnostics_without_raw_conversation_or_code(tmp_path: Path) -> None:
    observed_at = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    service = ScenarioDiagnosticRetentionService(tmp_path / "diagnostics")

    target = service.capture(
        run_id=_run_id(1),
        job_id=_job_id(1),
        case_id="case-1",
        side="CANDIDATE",
        attempt_number=1,
        outcome=_outcome(),
        captured_at=observed_at,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    serialized = target.read_text(encoding="utf-8")
    projected = payload["outcome"]
    assert projected["terminal_status"] == "FAILED"
    assert projected["execution"]["terminal_reason"] == "TURN_EXPLICIT_FAILED"
    assert projected["execution"]["turns"][0]["workflow_step_id"] == "STEP_01"
    assert projected["execution"]["turns"][0]["final_response_length"] > 0
    assert len(projected["execution"]["turns"][0]["final_response_sha256"]) == 64
    assert projected["evidence"][0]["changed_path_count"] == 1
    assert "private requirement text" not in serialized
    assert "SecretImplementation" not in serialized
    assert "private source and code" not in serialized
    assert "super-secret" not in serialized
    assert "[REDACTED]" in serialized
    assert not list(target.parent.glob("*.tmp"))


def test_terminal_diagnostics_are_retained_then_removed_after_policy_window(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    run_id = _run_id(2)
    service = ScenarioDiagnosticRetentionService(
        tmp_path / "diagnostics",
        policy=ScenarioDiagnosticRetentionPolicy(
            terminal_retention=timedelta(days=7),
            orphan_retention=timedelta(days=30),
            sweep_interval=timedelta(0),
        ),
    )
    service.capture(
        run_id=run_id,
        job_id=_job_id(2),
        case_id="case-2",
        side="BASE",
        attempt_number=1,
        outcome=_outcome(),
        captured_at=now,
    )
    service.mark_terminal(run_id, completed_at=now)

    retained = service.sweep(now=now + timedelta(days=6))
    removed = service.sweep(now=now + timedelta(days=8))

    assert retained.retained_run_ids == (run_id,)
    assert retained.removed_run_ids == ()
    assert removed.removed_run_ids == (run_id,)
    assert not (service.diagnostic_root / run_id).exists()


def test_sweep_removes_old_unmarked_orphans_but_ignores_unknown_and_linked_paths(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "diagnostics"
    service = ScenarioDiagnosticRetentionService(
        root,
        policy=ScenarioDiagnosticRetentionPolicy(orphan_retention=timedelta(days=1)),
    )
    orphan = service.capture(
        run_id=_run_id(3),
        job_id=_job_id(3),
        case_id="case-3",
        side="BASE",
        attempt_number=1,
        outcome=_outcome(),
        captured_at=now - timedelta(days=2),
    ).parent
    old_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(orphan, (old_timestamp, old_timestamp))
    unknown = root / "unknown"
    unknown.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / _run_id(4)).symlink_to(outside, target_is_directory=True)

    report = service.sweep(now=now)

    assert report.removed_run_ids == (_run_id(3),)
    assert unknown.is_dir()
    assert outside.is_dir()
    assert (root / _run_id(4)).is_symlink()
