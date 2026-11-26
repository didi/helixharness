"""Unavailable transcripts stay unknown across AI facts and persisted projections."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from eval.stage_supplements import EVIDENCE_SCHEMA, StageSupplementStore
from eval.telemetry_materialization import materialize_eval_log, materialized_hash
from eval.telemetry_quality import TelemetryQualityStore
from eval.tests.test_stage_evidence_availability import _linked, _unavailable
from eval.tests.test_usage_loader import _workflow_log, _workflow_supplement
from eval.usage_ai import _entry_evidence, usage_data_state_hash
from eval.usage_ai_daily import daily_records, generate_daily_insight, global_facts
from eval.usage_loader import _collection, aggregate_feature


def test_uncollected_messages_stay_unknown_in_ai_detail(tmp_path):
    _, projected = _linked(tmp_path, _unavailable())
    entry, = projected["entries"]
    counts = _collection(entry)
    assert all(counts[key] is None for key in ("events_scanned", "messages_scanned", "messages_used"))
    dto = _entry_evidence(entry, 180)["collection"]
    assert dto["messages_scanned"] is None
    assert dto["messages_used"] is None
    assert dto["user_messages"] is None
    assert dto["semantic_status"] == "unavailable"
    assert dto["unavailable_reason"] == "host_transcript_not_collected"


@pytest.mark.parametrize("count", [0, 7])
def test_measured_messages_keep_zero_and_nonzero_in_ai_detail(tmp_path, count):
    _, projected = _linked(tmp_path, {
        "schema": EVIDENCE_SCHEMA,
        "interaction_summary": {"user_turns": count, "assistant_messages": count},
        "coverage": {"in_window": count, "retained": count},
    })
    dto = _entry_evidence(projected["entries"][0], 180)["collection"]
    assert dto["messages_scanned"] == count * 2
    assert dto["messages_used"] == count
    assert dto["user_messages"] == count
    assert dto["semantic_status"] == "supplement_collected"
    assert dto["unavailable_reason"] is None


def test_legacy_omitted_collection_counts_keep_their_defaults():
    assert _collection({})["messages_scanned"] == 0
    assert _collection({})["events_scanned"] == 0


@pytest.mark.parametrize("evidence,execution,expected", [
    (_unavailable(), {}, None),
    (_unavailable(), {"tool_error_count": 0}, 0),
    (_unavailable(), {"tool_error_count": 3}, 3),
    (_unavailable(), {"collection_status": "unavailable", "tool_error_count": 0}, None),
    ({"schema": EVIDENCE_SCHEMA, "tools": {"failed": 0}}, {}, 0),
    ({"schema": EVIDENCE_SCHEMA, "tools": {"failed": 3}}, {}, 3),
])
def test_daily_tool_counts_and_observation_denominators(tmp_path, evidence, execution, expected):
    _, projected = _linked(tmp_path, evidence, execution=execution)
    records = daily_records([aggregate_feature(projected)])
    entry, = [record for record in records if record["kind"] == "entry"]
    assert entry["failed_tool_count"] == expected
    coverage = global_facts({"daily_records": records})["evidence_coverage"]
    assert coverage["async_linked_entries"] == 1
    assert coverage["tool_observed_entries"] == (expected is not None)


@pytest.mark.parametrize("linked,failed_tools,expected", [
    (True, [], 0), (True, [{"name": "Bash"}], 1), (False, [{"name": "Bash"}], None),
])
def test_daily_legacy_failed_tool_lists_remain_supported(linked, failed_tools, expected):
    records = daily_records([{"project_id": "synthetic", "feature": "legacy", "entries": [{
        "entry_id": "entry-legacy", "telemetry_status": "complete" if linked else "pending",
        "async_evidence": {"failed_tools": failed_tools},
    }]}])
    entry, = [record for record in records if record["kind"] == "entry"]
    assert entry["failed_tool_count"] == expected


@pytest.mark.parametrize("count", [None, 0, 4])
def test_unavailable_transcript_does_not_override_independent_execution(count):
    records = daily_records([{"project_id": "synthetic", "feature": "unavailable", "entries": [{
        "entry_id": "entry-unavailable", "telemetry_status": "complete",
        "execution_stats": {} if count is None else {"tool_error_count": count},
        "async_evidence": {"status": "unavailable", "tools": {"failed": 9},
                           "failed_tools": [{"name": "synthetic-unobserved-tool"}]},
    }]}])
    entry, = [record for record in records if record["kind"] == "entry"]
    assert entry["failed_tool_count"] == count
    assert entry["failed_tool_names"] == []


def test_corrected_daily_observation_updates_existing_history(tmp_path):
    _, projected = _linked(tmp_path, _unavailable())
    current = {"daily_records": daily_records([aggregate_feature(projected)])}
    previous = deepcopy(current)
    for record in previous["daily_records"]:
        if record["kind"] == "entry":
            record["failed_tool_count"] = 0
    inputs = []

    def generate(payload):
        inputs.append(payload)
        return {"status": "success", "answer": "Synthetic observation"}

    for day, data in ((10, previous), (11, current)):
        generate_daily_insight(data, tmp_path / "history", [tmp_path / "repo"], generate, lambda _: "Synthetic",
                               now=datetime(2026, 11, day, 4, tzinfo=timezone.utc))
    assert inputs[-1]["daily_window"]["bootstrap"] is False
    assert inputs[-1]["daily_window"]["change_counts"] == {"updated": 1}
    assert inputs[-1]["history"]["generated_day_count"] == 1
    assert inputs[-1]["evidence_coverage"]["tool_observed_entries"] == 0


def test_new_ai_fact_contract_invalidates_old_answers_without_changing_source():
    aggregate = {"features": [], "summary": {}}
    current = usage_data_state_hash(aggregate)
    with patch("eval.usage_ai.AI_FACT_CONTRACT_VERSION", "facts-v4-daily"):
        assert usage_data_state_hash(aggregate) != current


def _legacy_materialized_hash(raw, records):
    """Frozen pre-fix cache contract, independent of current version constants."""
    identity = {"materializer": "helixharness-eval-materialized/2.1", "supplements": {
        entry_id: {"payload_hash": record.payload_hash, "effective_at": record.effective_at}
        for entry_id, record in sorted(records.items())
    }}
    digest = hashlib.sha256(raw)
    digest.update(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return digest.hexdigest()


@pytest.mark.parametrize("measured_usage", [False, True])
def test_existing_quality_projection_refreshes_after_semantic_upgrade(tmp_path, measured_usage):
    repo = tmp_path / "repo"
    path = repo / "docs/eval/example-feature/eval-log.json"
    path.parent.mkdir(parents=True)
    ledger = _workflow_log()
    template = ledger["entries"][0]
    ledger["entries"] = []
    supplements = StageSupplementStore(tmp_path / "supplements.db")
    for index, (command, stage) in enumerate((("propose", "requirement"), ("apply", "coding"), ("archive", "archive"))):
        entry = {**template, "entry_id": f"entry-synthetic-{index}", "command": command,
                 "stage_kind": stage, "platform": "opencode"}
        ledger["entries"].append(entry)
        payload = {**_workflow_supplement(), "entry_id": entry["entry_id"],
                   "supplement_id": f"supp_synthetic_{index}", "platform": "opencode",
                   "usage": {"input_tokens": 0, "output_tokens": 0} if measured_usage else {},
                   "execution": {}, "evidence": _unavailable()}
        assert supplements.ingest(payload)["status"] == "accepted"
    path.write_text(json.dumps(ledger), encoding="utf-8")
    original = path.read_bytes()
    assert supplements.refresh_links([repo]) == 3
    records = supplements.active_linked_records()
    snapshots = deepcopy([record.payload for record in records])
    indexed = {record.payload["entry_id"]: record for record in records}
    old_hash = _legacy_materialized_hash(original, indexed)
    legacy = materialize_eval_log(ledger, indexed)
    for entry in legacy["entries"]:
        entry["async_evidence"].pop("status", None)
        entry["async_evidence"].pop("reason_code", None)
    quality = TelemetryQualityStore(tmp_path / "quality.db")
    with patch("eval.telemetry_quality.materialized_hash", return_value=old_hash), \
         patch("eval.telemetry_quality.materialize_eval_log", return_value=legacy):
        seeded = quality.refresh([repo], records)
    assert (seeded.valid if measured_usage else seeded.partial) == 1
    upgraded = quality.refresh([repo], records)
    assert upgraded.unchanged == 0
    assert (upgraded.valid if measured_usage else upgraded.partial) == 1
    new_hash = materialized_hash(original, indexed)
    assert new_hash != old_hash

    def read(store):
        return store.automation_evidence() if measured_usage else store.partial_rehearsal_evidence(new_hash)["evidence"]

    fresh = TelemetryQualityStore(tmp_path / "fresh-quality.db")
    fresh.refresh([repo], records)
    assert [item["async_evidence"] for item in read(quality)] == [item["async_evidence"] for item in read(fresh)]
    assert len(read(quality)) == 3
    assert all(item["async_evidence"]["status"] == "unavailable" for item in read(quality))
    assert quality.refresh([repo], records).unchanged == 1
    assert path.read_bytes() == original
    assert [record.payload for record in supplements.active_linked_records()] == snapshots
