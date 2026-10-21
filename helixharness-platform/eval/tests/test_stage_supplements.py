from __future__ import annotations

import json

import pytest

import eval.stage_supplements as stage_supplements
from eval.stage_supplements import StageSupplementError, StageSupplementStore


def _supplement() -> dict:
    return {
        "schema": "helixharness-stage-supplement/1.0",
        "supplement_id": "supp_0123456789abcdef",
        "revision": 1,
        "supersedes": None,
        "entry_id": "entry-123",
        "requirement_id": "req_0123456789abcdef01234567",
        "requirement_name": "checkout-timeout",
        "feature": "checkout-timeout",
        "session_hash": "session-123",
        "platform": "codex",
        "collected_at": "2026-08-12T01:02:00Z",
        "usage": {"input_tokens": 20, "output_tokens": 10},
        "evidence": {"schema": "helixharness-evolution-evidence/1.0"},
    }


def _runtime_health_event() -> dict:
    return {
        "schema": "helixharness-telemetry-runtime-health/2.0",
        "scope_kind": "runtime", "scope_id": "runtime", "circuit_open": True,
        "event_id": "health_0123456789abcdef",
        "event": "circuit_open",
        "runtime_id": "runtime_0123456789abcdef",
        "observed_at": "2026-08-15T05:39:41Z",
        "reason_code": "collection_failed",
        "consecutive_failures": 8,
        "circuit_opened_at": "2026-08-15T05:39:41Z",
        "circuit_resume_at": "2026-08-15T06:39:41Z",
        "host_os": "darwin",
        "runtime_version": "2.2.0",
    }


def test_store_is_idempotent_and_links_only_after_ledger_arrives(tmp_path) -> None:
    store = StageSupplementStore(tmp_path / "supplements.db")
    first = store.ingest(_supplement())
    duplicate = store.ingest(_supplement())

    assert first == {"supplement_id": "supp_0123456789abcdef", "status": "accepted", "linked": False}
    assert duplicate == {"supplement_id": "supp_0123456789abcdef", "status": "duplicate", "linked": False}
    assert store.active_linked() == []

    ledger_root = tmp_path / "repo"
    ledger_path = ledger_root / "docs" / "eval" / "checkout-timeout" / "eval-log.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        '{"schema":"helixharness-eval-ledger/1.0","feature":"checkout-timeout",'
        '"requirement":{"id":"req_0123456789abcdef01234567","name":"checkout-timeout"},'
        '"entries":[{"entry_id":"entry-123"}]}',
        encoding="utf-8",
    )
    assert store.refresh_links([ledger_root]) == 1
    assert [item["entry_id"] for item in store.active_linked()] == ["entry-123"]


def test_store_accepts_protocol_payload_without_optional_collection_time(tmp_path) -> None:
    item = _supplement()
    item.pop("collected_at")

    result = StageSupplementStore(tmp_path / "supplements.db").ingest(item)

    assert result["status"] == "accepted"


def test_store_persists_runtime_health_event_idempotently(tmp_path) -> None:
    store = StageSupplementStore(tmp_path / "supplements.db")
    event = _runtime_health_event()

    first = store.ingest_runtime_health(event)
    duplicate = store.ingest_runtime_health(
        event
    )

    assert first == {"event_id": "health_0123456789abcdef", "status": "accepted"}
    assert duplicate == {"event_id": "health_0123456789abcdef", "status": "duplicate"}


def test_link_requires_entry_requirement_and_feature_to_match(tmp_path) -> None:
    store = StageSupplementStore(tmp_path / "supplements.db")
    store.ingest(_supplement())
    ledger_root = tmp_path / "repo"
    ledger_path = ledger_root / "docs" / "eval" / "other-feature" / "eval-log.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        '{"schema":"helixharness-eval-ledger/1.0","feature":"other-feature",'
        '"requirement":{"id":"req_0123456789abcdef01234567","name":"checkout-timeout"},'
        '"entries":[{"entry_id":"entry-123"}]}',
        encoding="utf-8",
    )

    assert store.refresh_links([ledger_root]) == 0
    assert store.active_linked() == []


def test_eval_log_discovery_time_activates_early_supplement_and_is_stable(tmp_path, monkeypatch) -> None:
    timestamps = iter(
        [
            "2026-08-01T00:00:00Z",
            "2026-08-10T00:00:00Z",
            "2026-08-20T00:00:00Z",
        ]
    )
    monkeypatch.setattr(stage_supplements, "_utc_now", lambda: next(timestamps))
    store = StageSupplementStore(tmp_path / "supplements.db")
    store.ingest(_supplement())

    ledger_root = tmp_path / "repo"
    ledger_path = ledger_root / "docs" / "eval" / "checkout-timeout" / "eval-log.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        '{"schema":"helixharness-eval-ledger/1.0","feature":"checkout-timeout",'
        '"requirement":{"id":"req_0123456789abcdef01234567","name":"checkout-timeout"},'
        '"entries":[{"entry_id":"entry-123"}]}',
        encoding="utf-8",
    )

    assert store.refresh_links([ledger_root]) == 1
    first = store.active_linked_records()[0]
    assert first.received_at == "2026-08-01T00:00:00Z"
    assert first.effective_at == "2026-08-10T00:00:00Z"

    assert store.refresh_links([ledger_root]) == 0
    assert store.active_linked_records()[0].effective_at == "2026-08-10T00:00:00Z"


def test_link_finalizes_sanitized_snapshot_once(tmp_path) -> None:
    store = StageSupplementStore(tmp_path / "supplements.db")
    item = _supplement()
    item["execution"] = {
        "tool_call_count": 5,
        "tool_error_count": 1,
        "subagent_spawn_count": 2,
        "context_reads": {"harness": 3, "knowledge": 2, "specs": 1, "hooks": 0},
    }
    item["evidence"] = {
        "schema": "helixharness-evolution-evidence/1.0",
        "interactions": [{"role": "user", "text": "private user message"}],
        "assistant_replies": ["private assistant message"],
        "interaction_summary": {"user_turns": 4, "assistant_messages": 25},
        "coverage": {"in_window": 120, "retained": 30},
        "tools": {"total": 5, "failed": 1},
        "failed_tools": [{"name": "Bash", "input_summary": "private command"}],
    }
    item["workflow_trace"] = {
        "status": "collected",
        "gates": [{"name": "verify", "status": "failed", "details": "private details"}],
        "fallbacks": [{"name": "retry", "details": "private fallback"}],
    }
    store.ingest(item)
    ledger_root = _write_ledger(tmp_path)

    assert store.refresh_links([ledger_root]) == 1
    assert store.refresh_links([]) == 0

    active = store.active_linked_records()
    assert len(active) == 1
    snapshot = active[0].payload
    assert snapshot["usage"] == {"input_tokens": 20, "output_tokens": 10}
    assert snapshot["execution"]["tool_call_count"] == 5
    assert snapshot["evidence"]["interaction_summary"] == {"user_turns": 4, "assistant_messages": 25}
    assert snapshot["evidence"]["failed_tools"] == [{"name": "Bash"}]
    assert snapshot["workflow_trace"]["gates"] == [{"name": "verify", "status": "failed"}]
    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert "private user message" not in serialized
    assert "private assistant message" not in serialized
    assert "private command" not in serialized
    assert "private details" not in serialized
    assert "private fallback" not in serialized


def test_finalized_entry_rejects_later_revision(tmp_path) -> None:
    store = StageSupplementStore(tmp_path / "supplements.db")
    ledger_root = _write_ledger(tmp_path)
    store.refresh_links([ledger_root])
    assert store.ingest(_supplement())["linked"] is True

    revised = {**_supplement(), "revision": 2, "supplement_id": "supp_fedcba9876543210"}
    with pytest.raises(StageSupplementError) as exc_info:
        store.ingest(revised)

    assert exc_info.value.code == "finalized_entry"


def test_cleanup_keeps_final_snapshot_after_seven_day_raw_grace(tmp_path, monkeypatch) -> None:
    now = {"value": "2026-08-01T00:00:00Z"}
    monkeypatch.setattr(stage_supplements, "_utc_now", lambda: now["value"])
    store = StageSupplementStore(tmp_path / "supplements.db")
    ledger_root = _write_ledger(tmp_path)
    store.refresh_links([ledger_root])
    store.ingest(_supplement())

    now["value"] = "2026-08-07T23:59:59Z"
    assert store.cleanup_expired() == {"linked_raw": 0, "unlinked": 0}
    now["value"] = "2026-08-09T00:00:00Z"
    assert store.cleanup_expired() == {"linked_raw": 1, "unlinked": 0}

    assert [item.payload["entry_id"] for item in store.active_linked_records()] == ["entry-123"]
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM stage_supplements").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM final_stage_supplements").fetchone()[0] == 1


def test_cleanup_deletes_unlinked_raw_after_thirty_days(tmp_path, monkeypatch) -> None:
    now = {"value": "2026-08-01T00:00:00Z"}
    monkeypatch.setattr(stage_supplements, "_utc_now", lambda: now["value"])
    store = StageSupplementStore(tmp_path / "supplements.db")
    store.ingest(_supplement())

    now["value"] = "2026-08-16T00:00:00Z"
    assert store.cleanup_expired() == {"linked_raw": 0, "unlinked": 0}
    now["value"] = "2026-08-30T23:59:59Z"
    assert store.cleanup_expired() == {"linked_raw": 0, "unlinked": 0}
    now["value"] = "2026-08-31T00:00:00Z"
    assert store.cleanup_expired() == {"linked_raw": 0, "unlinked": 1}
    assert store.active_linked_records() == []


def _write_ledger(tmp_path):
    ledger_root = tmp_path / "repo"
    ledger_path = ledger_root / "docs" / "eval" / "checkout-timeout" / "eval-log.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        '{"schema":"helixharness-eval-ledger/1.0","feature":"checkout-timeout",'
        '"requirement":{"id":"req_0123456789abcdef01234567","name":"checkout-timeout"},'
        '"entries":[{"entry_id":"entry-123"}]}',
        encoding="utf-8",
    )
    return ledger_root
