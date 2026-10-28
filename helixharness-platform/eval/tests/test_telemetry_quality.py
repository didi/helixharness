"""Tests for the consumer-side eval-log quality gate."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from eval.stage_supplements import LinkedStageSupplement
from eval.telemetry_quality import (
    TelemetryQualityStore,
    _commit_code_evidence,
)
from eval.tests.test_usage_loader import _workflow_log, _workflow_supplement


def test_workflow_v2_enters_automation_only_after_supplement_links_at_discovery_time(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    feature_dir = root / "docs" / "eval" / "example-feature"
    feature_dir.mkdir(parents=True)
    path = feature_dir / "eval-log.json"
    ledger = _workflow_log()
    template = ledger["entries"][0]
    records = []
    ledger["entries"] = []
    for index, (command, stage_kind) in enumerate(
        (("propose", "requirement"), ("apply", "coding"), ("archive", "archive")),
        start=1,
    ):
        entry_id = f"entry-{index:016x}"
        entry = {**template, "entry_id": entry_id, "command": command, "stage_kind": stage_kind}
        ledger["entries"].append(entry)
        supplement = {**_workflow_supplement(), "entry_id": entry_id, "supplement_id": f"supp_{index:016x}"}
        records.append(
            LinkedStageSupplement(
                source_path=str(path.resolve()),
                effective_at="2026-08-10T00:00:00Z",
                received_at="2026-08-01T00:00:00Z",
                payload_hash=str(index) * 64,
                payload=supplement,
            )
        )
    path.write_text(json.dumps(ledger, ensure_ascii=False), encoding="utf-8")
    store = TelemetryQualityStore(tmp_path / "telemetry.db")

    before = store.refresh([root])
    assert before.partial == 1
    assert store.automation_evidence() == []

    after = store.refresh([root], records)

    assert after.valid == 1
    evidence = store.automation_evidence()
    assert len(evidence) == 3
    coding = next(item for item in evidence if item["stage_kind"] == "coding")
    assert "workflow_variant" not in coding
    assert coding["admitted_at"] == "2026-08-10T00:00:00Z"
    assert coding["fixed_evidence"]["stage"]["stage_kind"] == "coding"
    assert coding["async_evidence"]["tools"] == {"failed": 0, "succeeded": 2}
    assert coding["async_evidence"]["resource_usage"]["input_tokens"] == 30
    assert coding["async_evidence"]["interactions"][0] == {
        "role": "user",
        "text": "修复结算超时",
    }
    original = path.read_bytes()
    store.refresh([])
    assert store.automation_evidence() == []
    assert store.summary()["total"] == 0
    assert path.read_bytes() == original
    store.refresh([root], records)
    assert len(store.automation_evidence()) == 3


def test_invalid_json_is_quarantined_without_entry_projection(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    feature_dir = root / "docs" / "eval" / "broken"
    feature_dir.mkdir(parents=True)
    (feature_dir / "eval-log.json").write_text("{not-json", encoding="utf-8")
    store = TelemetryQualityStore(tmp_path / "telemetry.db")

    result = store.refresh([root])

    assert result.invalid == 1
    assert store.summary()["counts"]["INVALID"] == 1
    assert store.automation_evidence() == []


def test_commit_code_evidence_uses_checkpoint_diff_metadata(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "config", "user.email", "test@example.com")
    source = repository / "workflow.md"
    source.write_text("before\n", encoding="utf-8")
    _git(repository, "add", "workflow.md")
    _git(repository, "commit", "-qm", "base")
    base = _git(repository, "rev-parse", "HEAD")
    source.write_text("before\nafter\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "head")
    head = _git(repository, "rev-parse", "HEAD")

    evidence = _commit_code_evidence(repository, {"base_commit": base, "head_commit": head})

    assert evidence["status"] == "collected"
    assert evidence["base_commit"] == base
    assert evidence["head_commit"] == head
    assert evidence["changed_files"] == [{"path": "workflow.md", "added": 1, "deleted": 0}]
    assert "1 insertion" in evidence["summary"]


def test_refresh_accepts_generator_roots_and_removes_missing_documents(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = root / "docs" / "eval" / "demo" / "eval-log.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_workflow_log()), encoding="utf-8")
    store = TelemetryQualityStore(tmp_path / "telemetry.db")
    store.refresh(value for value in (root,))
    path.unlink()

    store.refresh(value for value in (root,))

    assert store.summary()["total"] == 0


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_rebuild_outdated_projection_preserves_source_and_unrelated_tables(tmp_path: Path) -> None:
    import sqlite3

    root = tmp_path / "repo"
    source = root / "docs/eval/example-feature/eval-log.json"
    source.parent.mkdir(parents=True)
    original = json.dumps(_workflow_log()).encode()
    source.write_bytes(original)
    database = tmp_path / "projection.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE telemetry_documents (source_path TEXT PRIMARY KEY, obsolete_tag TEXT)")
        connection.execute("CREATE TABLE telemetry_entries (source_path TEXT)")
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('keep')")
    store = TelemetryQualityStore(database)
    assert store.refresh([root]).partial == 1
    assert store.refresh([root]).unchanged == 1
    assert store.summary()["supported_total"] == 1
    assert source.read_bytes() == original
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone() == ("keep",)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(telemetry_documents)")}
        assert "document_schema" in columns
        assert "obsolete_tag" not in columns


def test_ledger_version_pair_is_required_for_quality_and_dashboard(tmp_path: Path) -> None:
    from eval.telemetry_materialization import materialize_eval_log
    from eval.telemetry_quality import QualityStatus, assess_eval_log
    from eval.usage_loader import _load_eval_log

    path = tmp_path / "eval-log.json"
    for schema, version, valid in (
        ("helixharness-eval-ledger/1.0", "1.0", True),
        ("helixharness-eval-ledger/1.0", "2.0", False),
        ("helixharness-eval-ledger/2.0", "2.0", False),
        (None, "1.0", False),
    ):
        ledger = _workflow_log()
        ledger.update(schema=schema, schema_version=version)
        path.write_text(json.dumps(ledger))
        assessment = assess_eval_log(materialize_eval_log(ledger) if schema == "helixharness-eval-ledger/1.0" else ledger)
        loaded = _load_eval_log(str(path))
        if valid:
            assert assessment.status != QualityStatus.INVALID
            assert loaded and not loaded.get("_invalid")
        else:
            assert assessment.status == QualityStatus.INVALID
            assert loaded and loaded.get("_invalid")
