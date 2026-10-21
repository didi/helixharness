"""Persisted health contracts: compatibility, scope attribution and immutable retries."""
from __future__ import annotations

import json

import pytest

from eval.runtime_health import RUNTIME_HEALTH_V2_SCHEMA
from eval.stage_supplements import StageSupplementError, StageSupplementStore


def health_event(**changes) -> dict:
    return {
        "schema": RUNTIME_HEALTH_V2_SCHEMA,
        "event_id": "health_collection_open_1",
        "runtime_id": "runtime_client_a",
        "event": "circuit_open",
        "observed_at": "2026-09-09T03:00:00Z",
        "host_os": "darwin",
        "runtime_version": "test-next",
        "scope_kind": "collection",
        "scope_id": "repo_a_codex",
        "repo_name": "repo-a",
        "platform": "codex",
        "user_name": "test-user",
        "feature": "test-feature",
        "entry_id": "entry-test",
        "reason_code": "consent_epoch_mismatch",
        "consecutive_failures": 6,
        "circuit_open": True,
        "circuit_opened_at": "2026-09-09T03:00:00Z",
        "circuit_resume_at": "2026-09-09T04:00:00Z",
        **changes,
    }


def test_health_v2_preserves_open_recovery_and_heartbeat(tmp_path):
    path = tmp_path / "inbox.db"
    store = StageSupplementStore(path)
    events = [
        health_event(),
        health_event(event_id="health_recovered", event="circuit_closed", circuit_open=False,
                     consecutive_failures=0, observed_at="2026-09-09T03:05:00+00:00"),
        health_event(event_id="health_heartbeat", event="heartbeat", circuit_open=False,
                     consecutive_failures=0, observed_at="2026-09-09T03:10:00Z"),
    ]
    # Delayed arrival must not overwrite any other event or scope.
    for event in reversed(events):
        assert store.ingest_runtime_health(event)["status"] == "accepted"
    assert store.ingest_runtime_health(events[0])["status"] == "duplicate"
    with StageSupplementStore(path)._connect() as connection:
        rows = connection.execute("SELECT payload_json, received_at FROM runtime_health_events").fetchall()
        assert len(rows) == 3
        assert all(row["received_at"] for row in rows)
        assert {json.loads(row["payload_json"])["event_id"] for row in rows} == {e["event_id"] for e in events}
        assert all(json.loads(row["payload_json"])["user_name"] == "test-user" for row in rows)


def test_v2_conflicting_retry_does_not_overwrite_original(tmp_path):
    store = StageSupplementStore(tmp_path / "inbox.db")
    store.ingest_runtime_health(health_event())
    with pytest.raises(StageSupplementError, match="different content") as error:
        store.ingest_runtime_health(health_event(feature="different-feature"))
    assert error.value.code == "identity_conflict"
    with store._connect() as connection:
        row = connection.execute("SELECT payload_json FROM runtime_health_events").fetchone()
        assert json.loads(row[0])["feature"] == "test-feature"


@pytest.mark.parametrize("change", [
    {"scope_kind": "all_projects"}, {"platform": "invalid"}, {"circuit_open": "true"},
    {"consecutive_failures": True}, {"consecutive_failures": -1}, {"consecutive_failures": 0},
    {"event": "circuit_closed"}, {"observed_at": "2026-09-09T03:00:00"},
    {"circuit_resume_at": "2026-09-08T03:00:00Z"}, {"reason_code": ""},
    {"session": {"text": "must not be accepted"}}, {"user_name": "x" * 513},
    {"scope_kind": "runtime"}, {"event_id": "invalid"}, {"runtime_id": "invalid"},
])
def test_health_v2_rejects_invalid_metadata(tmp_path, change):
    store = StageSupplementStore(tmp_path / "inbox.db")
    with pytest.raises(StageSupplementError):
        store.ingest_runtime_health(health_event(**change))
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_health_events").fetchone()[0] == 0


@pytest.mark.parametrize("missing", ["scope_id", "repo_name", "platform", "reason_code", "circuit_opened_at"])
def test_health_v2_requires_scope_and_failure_context(tmp_path, missing):
    event = health_event()
    del event[missing]
    with pytest.raises(StageSupplementError):
        StageSupplementStore(tmp_path / "inbox.db").ingest_runtime_health(event)


@pytest.mark.parametrize("kind,scope", [("runtime", "runtime"), ("delivery", "primary-report")])
def test_health_v2_accepts_minimal_healthy_scope(tmp_path, kind, scope):
    event = {key: value for key, value in health_event().items() if key not in {
        "repo_name", "platform", "user_name", "feature", "entry_id", "reason_code",
        "circuit_opened_at", "circuit_resume_at",
    }}
    event.update(scope_kind=kind, scope_id=scope, circuit_open=False, consecutive_failures=0, event="heartbeat")
    assert StageSupplementStore(tmp_path / "inbox.db").ingest_runtime_health(event)["status"] == "accepted"
