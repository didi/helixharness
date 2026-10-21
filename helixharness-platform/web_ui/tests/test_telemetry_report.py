from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_ui import create_app


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_FRAMEWORK_EVAL_DB_PATH", str(tmp_path / "framework.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_TELEMETRY_DB_PATH", str(tmp_path / "quality.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_STAGE_SUPPLEMENT_DB_PATH", str(tmp_path / "supplements.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_DB_PATH", str(tmp_path / "evolution.db"))
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_SCHEDULER_ENABLED", "0")
    return TestClient(create_app())


def _supplement_payload() -> dict:
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
        "session": {
            "platform": "codex",
            "session_id": "session-private",
            "session_hash": "session-123",
            "session_kind": "primary",
        },
        "usage": {"input_tokens": 20, "output_tokens": 10, "cached_input_tokens": 4},
        "execution": {"tool_call_count": 1, "subagent_spawn_count": 0},
        "evidence": {
            "schema": "helixharness-evolution-evidence/1.0",
            "interactions": [{"role": "user", "text": "修复问题"}],
        },
        "workflow_trace": {
            "schema": "helixharness-workflow-trace/1.0",
            "status": "collected",
        },
    }


def test_report_accepts_mixed_health_versions_and_supplements(client: TestClient) -> None:
    from eval.tests.test_runtime_health import health_event
    from eval.tests.test_stage_supplements import _runtime_health_event

    v2 = health_event()
    v1 = _runtime_health_event()
    response = client.post("/report", json={"items": [v2, v1, _supplement_payload()]})
    assert response.status_code == 200
    assert [item["status"] for item in response.json()["items"]] == ["accepted"] * 3
    replay = client.post("/report", json={"items": [v2, v1]})
    assert [item["status"] for item in replay.json()["items"]] == ["duplicate"] * 2
    with client.app.state.stage_supplement_store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runtime_health_events").fetchone()[0] == 2


def test_report_rejects_bad_health_item_without_blocking_recovery(client: TestClient) -> None:
    from eval.tests.test_runtime_health import health_event

    opened = health_event()
    client.post("/report", json={"items": [opened]})
    recovered = health_event(event_id="health_recovery", event="circuit_closed", circuit_open=False,
                             consecutive_failures=0)
    response = client.post("/report", content=gzip.compress(json.dumps({"items": [
        {**opened, "feature": "conflict"}, {**opened, "event_id": "health_bad", "session": {}}, recovered,
    ]}).encode()), headers={"content-encoding": "gzip"})
    items = response.json()["items"]
    assert [item["status"] for item in items] == ["rejected", "rejected", "accepted"]
    assert items[0]["error"]["code"] == "identity_conflict"
    assert items[1]["event_id"] == "health_bad"


def test_report_returns_per_item_ack_and_duplicate(client: TestClient) -> None:
    item = _supplement_payload()
    first = client.post("/report", json={"items": [item]})
    second = client.post("/report", json={"items": [item]})

    assert first.status_code == 200
    assert first.json()["items"] == [
        {"supplement_id": "supp_0123456789abcdef", "status": "accepted", "linked": False}
    ]
    assert second.json()["items"][0]["status"] == "duplicate"


def test_report_queues_ai_regeneration_for_new_linked_supplement(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval import usage_sync

    ledger_root = tmp_path / "repo"
    ledger_path = ledger_root / "docs" / "eval" / "checkout-timeout" / "eval-log.json"
    ledger_path.parent.mkdir(parents=True)
    ledger_path.write_text(
        '{"schema":"helixharness-eval-ledger/1.0","feature":"checkout-timeout",'
        '"requirement":{"id":"req_0123456789abcdef01234567","name":"checkout-timeout"},'
        '"entries":[{"entry_id":"entry-123"}]}',
        encoding="utf-8",
    )
    client.app.state.stage_supplement_store.refresh_links([ledger_root])
    scheduled: list[list[str]] = []
    monkeypatch.setattr(usage_sync, "schedule_usage_ai_regeneration", lambda roots: scheduled.append(roots))

    first = client.post("/report", json={"items": [_supplement_payload()]})
    duplicate = client.post("/report", json={"items": [_supplement_payload()]})

    assert first.json()["items"][0]["linked"] is True
    assert duplicate.json()["items"][0]["status"] == "duplicate"
    assert scheduled == [[]]


def test_report_rejects_invalid_item_without_rejecting_valid_sibling(client: TestClient) -> None:
    response = client.post(
        "/report",
        json={
            "items": [
                {"schema": "bad", "supplement_id": "broken"},
                {
                    "schema": "helixharness-stage-supplement/1.0",
                    "supplement_id": "supp_fedcba9876543210",
                    "revision": 1,
                    "supersedes": None,
                    "entry_id": "entry-456",
                    "requirement_id": "req_fedcba9876543210fedcba98",
                    "requirement_name": "valid",
                    "feature": "valid",
                    "session_hash": "session-456",
                    "platform": "claude-code",
                    "collected_at": "2026-08-12T01:02:00Z",
                    "usage": {},
                    "evidence": {"schema": "helixharness-evolution-evidence/1.0"},
                },
            ]
        },
    )
    assert response.status_code == 200
    assert [item["status"] for item in response.json()["items"]] == ["rejected", "accepted"]


def test_report_accepts_gzip_batch(client: TestClient) -> None:
    payload = {
        "items": [
            {
                "schema": "helixharness-stage-supplement/1.0",
                "supplement_id": "supp_aaaaaaaaaaaaaaaa",
                "revision": 1,
                "supersedes": None,
                "entry_id": "entry-gzip",
                "requirement_id": "req_aaaaaaaaaaaaaaaaaaaaaaaa",
                "requirement_name": "gzip",
                "feature": "gzip",
                "session_hash": "session-gzip",
                "platform": "codex",
                "collected_at": "2026-08-12T01:02:00Z",
                "usage": {},
                "evidence": {"schema": "helixharness-evolution-evidence/1.0"},
            }
        ]
    }
    response = client.post(
        "/report",
        content=gzip.compress(json.dumps(payload).encode("utf-8")),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "accepted"


def test_report_accepts_feature_oe_workflow_runtime_health_event(client: TestClient) -> None:
    event = {
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

    first = client.post(
        "/report",
        content=gzip.compress(json.dumps({"items": [event]}).encode("utf-8")),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    second = client.post(
        "/report",
        json={"items": [event]},
    )

    assert first.status_code == 200
    assert first.json()["items"] == [
        {"event_id": "health_0123456789abcdef", "status": "accepted"}
    ]
    assert second.json()["items"] == [
        {"event_id": "health_0123456789abcdef", "status": "duplicate"}
    ]
