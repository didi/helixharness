"""Malformed root states are rejected without repairing source evidence."""

import json

import pytest

from eval.telemetry_quality import QualityStatus, TelemetryQualityStore, assess_eval_log
from eval.tests.test_usage_loader import _workflow_log


@pytest.mark.parametrize("status", ["completed", "failed", "done", "", None])
def test_invalid_root_status_is_rejected(status, tmp_path):
    data = _workflow_log()
    data["status"] = status
    path = tmp_path / "repo/docs/eval/example-feature/eval-log.json"
    path.parent.mkdir(parents=True)
    original = json.dumps(data)
    path.write_text(original)
    store = TelemetryQualityStore(tmp_path / "telemetry.db")
    assert store.refresh([tmp_path / "repo"]).invalid == 1
    assert assess_eval_log(data).status is QualityStatus.INVALID
    assert store.automation_evidence() == []
    assert path.read_text() == original
