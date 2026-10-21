from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bench.scenario_storage import ScenarioStore
from bench.scenarios import ScenarioSourceError, load_scenario_source


def _write_scenario_source(root: Path, *, manifest_overrides: dict[str, object] | None = None) -> Path:
    source = root / "scenario-one"
    source.mkdir()
    manifest: dict[str, object] = {
        "schema_version": "framework-eval-scenario.v2",
        "case_id": "scenario-one",
        "title": "Workflow-neutral scenario",
        "case_kind": "DEVELOPMENT_FIXTURE",
        "execution_intent": "FLOW_THROUGH",
        "initial_request_path": "initial-request.md",
        "gold_path": "gold.json",
        "clarification_policy_path": "clarification-policy.json",
        "workspace_path": "workspace",
        "runtime": {"language": "PYTHON", "python_version": "3.10"},
    }
    manifest.update(manifest_overrides or {})
    (source / "case.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "initial-request.md").write_text("Implement the observable behavior.\n", encoding="utf-8")
    (source / "clarification-policy.json").write_text(
        json.dumps(
            {
                "schema_version": "framework-eval-clarification-policy.v1",
                "execution_intent": "FLOW_THROUGH",
                "external_confirmation_required": False,
                "unknown_business_fact_policy": "CASE_INVALID",
                "repository_fact_policy": "AGENT_INSPECTS",
                "facts": [{"fact_id": "F-1", "statement": "The expected behavior is frozen.", "source": "PRD"}],
            }
        ),
        encoding="utf-8",
    )
    (source / "gold.json").write_text(
        json.dumps(
            {
                "schema_version": "framework-eval-scenario-gold.v2",
                "requirements": [
                    {
                        "requirement_id": "REQ-1",
                        "statement": "The requested behavior is observable.",
                        "evidence_ids": ["E-1"],
                        "judge_ids": ["F2P-1", "P2P-1"],
                    }
                ],
                "source_evidence": [
                    {
                        "evidence_id": "E-1",
                        "path": "initial-request.md",
                        "line_start": 1,
                        "line_end": 1,
                        "quote": "Implement the observable behavior.",
                    }
                ],
                "execution_tests": [
                    {
                        "check_id": "F2P-1",
                        "kind": "FAIL_TO_PASS",
                        "argv": ["python3", "-c", "raise SystemExit(0)"],
                        "timeout_seconds": 30,
                    },
                    {
                        "check_id": "P2P-1",
                        "kind": "PASS_TO_PASS",
                        "argv": ["python3", "-c", "raise SystemExit(0)"],
                        "timeout_seconds": 30,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    workspace = source / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("fixture\n", encoding="utf-8")
    return source


def test_scenario_source_freezes_business_facts_without_workflow_ownership(tmp_path: Path) -> None:
    source = load_scenario_source(_write_scenario_source(tmp_path))

    assert source.case_id == "scenario-one"
    assert source.preflight_passed
    assert "profile" not in source.manifest
    assert "workflow_driver" not in source.manifest
    snapshot = json.loads(source.snapshot.snapshot_json)
    assert snapshot["schema_version"] == "framework-eval-scenario-snapshot.v2"
    assert snapshot["files"]["workspace/README.md"]["visibility"] == "EXECUTION"
    assert snapshot["files"]["gold.json"]["visibility"] == "HIDDEN"


def test_scenario_source_accepts_only_the_frozen_absent_helixharness_precondition(tmp_path: Path) -> None:
    source = load_scenario_source(
        _write_scenario_source(
            tmp_path,
            manifest_overrides={
                "workspace_preconditions": {
                    "schema_version": "framework-eval-workspace-preconditions.v1",
                    "helixharness_initialization": "ABSENT",
                    "init_mode": "SERVICE",
                }
            },
        )
    )
    assert source.manifest["workspace_preconditions"]["helixharness_initialization"] == "ABSENT"

    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir()
    invalid = _write_scenario_source(
        invalid_root,
        manifest_overrides={
            "workspace_preconditions": {
                "schema_version": "framework-eval-workspace-preconditions.v1",
                "remove_paths": ["src"],
            }
        },
    )
    with pytest.raises(ScenarioSourceError, match="ABSENT HelixHarness"):
        load_scenario_source(invalid)


def test_repository_scenario_freezes_workspace_fixture_patch_as_hidden_control_data(
    tmp_path: Path,
) -> None:
    source_directory = _write_scenario_source(
        tmp_path,
        manifest_overrides={
            "case_kind": "REPOSITORY_CASE",
            "workspace_path": None,
            "repository": {
                "repository_id": "repository-one",
                "base_commit": "1" * 40,
                "solution_commit": "2" * 40,
                "solution_patch_sha256": "3" * 64,
            },
        },
    )
    manifest_path = source_directory / "case.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("workspace_path", None)
    fixture = b"diff --git a/build.gradle b/build.gradle\n"
    (source_directory / "workspace-fixture.patch").write_bytes(fixture)
    manifest["workspace_fixture_patch"] = {
        "path": "workspace-fixture.patch",
        "sha256": hashlib.sha256(fixture).hexdigest(),
        "protected_paths": ["build.gradle"],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    source = load_scenario_source(source_directory)
    snapshot = json.loads(source.snapshot.snapshot_json)

    assert snapshot["files"]["workspace-fixture.patch"]["visibility"] == "HIDDEN"


def test_workspace_fixture_patch_rejects_business_source_paths(tmp_path: Path) -> None:
    source_directory = _write_scenario_source(tmp_path)
    manifest_path = source_directory / "case.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fixture = b"fixture\n"
    (source_directory / "workspace-fixture.patch").write_bytes(fixture)
    manifest["workspace_fixture_patch"] = {
        "path": "workspace-fixture.patch",
        "sha256": hashlib.sha256(fixture).hexdigest(),
        "protected_paths": ["src/main/java/BusinessLogic.java"],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ScenarioSourceError, match="build/tooling configuration only"):
        load_scenario_source(source_directory)


@pytest.mark.parametrize("field", ["profile", "profile_version", "workflow_driver", "workflow_policy"])
def test_scenario_source_rejects_run_owned_manifest_fields(tmp_path: Path, field: str) -> None:
    source_directory = _write_scenario_source(tmp_path, manifest_overrides={field: "legacy"})

    with pytest.raises(ScenarioSourceError, match=field):
        load_scenario_source(source_directory)


@pytest.mark.parametrize("field", ["artifact_paths", "stage_artifact_contract", "milestones", "judge_guidance"])
def test_scenario_source_rejects_workflow_scoring_fields_in_gold(tmp_path: Path, field: str) -> None:
    source_directory = _write_scenario_source(tmp_path)
    gold_path = source_directory / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold[field] = []
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    with pytest.raises(ScenarioSourceError, match=field):
        load_scenario_source(source_directory)


def test_scenario_store_publishes_immutable_versions_in_isolated_database(tmp_path: Path) -> None:
    source_directory = _write_scenario_source(tmp_path)
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()

    first = store.publish(load_scenario_source(source_directory))
    unchanged = store.publish(load_scenario_source(source_directory))
    (source_directory / "initial-request.md").write_text("Implement changed observable behavior.\n", encoding="utf-8")
    gold_path = source_directory / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["source_evidence"][0]["quote"] = "Implement changed observable behavior."
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    changed = store.publish(load_scenario_source(source_directory))

    assert first.source_hash == unchanged.source_hash
    assert changed.source_hash != first.source_hash
    assert [item.source_hash for item in store.list_versions("scenario-one")] == [
        first.source_hash,
        changed.source_hash,
    ]
    assert store.get("scenario-one") == changed


def test_scenario_store_deletes_only_current_case_and_preserves_history(tmp_path: Path) -> None:
    source = load_scenario_source(_write_scenario_source(tmp_path))
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    published = store.publish(source)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            INSERT INTO scenario_runs(
                run_id, status, run_kind, execution_adapter, contract_json,
                contract_hash, runtime_policy_json, preview_hash, created_by, created_at
            ) VALUES ('run-completed', 'COMPLETED', 'FORMAL', 'CODEX', '{}',
                      'contract', '{}', 'preview', 'operator', '2026-08-23T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT INTO scenario_run_cases(run_id, case_id, sequence_number, source_hash, snapshot_json)
            VALUES ('run-completed', ?, 1, ?, ?)
            """,
            (published.case_id, published.source_hash, published.snapshot_json),
        )

    store.delete_current(published.case_id)

    assert store.get(published.case_id) is None
    assert store.list_versions(published.case_id) == [published]
    with sqlite3.connect(store.database_path) as connection:
        frozen = connection.execute(
            "SELECT source_hash, snapshot_json FROM scenario_run_cases WHERE run_id = 'run-completed'"
        ).fetchone()
    assert frozen == (published.source_hash, published.snapshot_json)


@pytest.mark.parametrize("run_status", ["QUEUED", "SUSPENDED"])
def test_scenario_store_blocks_case_delete_while_run_is_unfinished(
    tmp_path: Path,
    run_status: str,
) -> None:
    source = load_scenario_source(_write_scenario_source(tmp_path))
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    published = store.publish(source)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            """
            INSERT INTO scenario_runs(
                run_id, status, run_kind, execution_adapter, contract_json,
                contract_hash, runtime_policy_json, preview_hash, created_by, created_at
            ) VALUES ('run-active', ?, 'FORMAL', 'CODEX', '{}',
                      'contract', '{}', 'preview', 'operator', '2026-08-23T00:00:00Z')
            """,
            (run_status,),
        )
        connection.execute(
            """
            INSERT INTO scenario_run_cases(run_id, case_id, sequence_number, source_hash, snapshot_json)
            VALUES ('run-active', ?, 1, ?, ?)
            """,
            (published.case_id, published.source_hash, published.snapshot_json),
        )

    with pytest.raises(ValueError, match="run-active"):
        store.delete_current(published.case_id)

    assert store.get(published.case_id) == published
    assert store.active_case_run_ids(published.case_id) == ("run-active",)


def test_scenario_store_overview_metrics_count_current_assets_and_durable_results(
    tmp_path: Path,
) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    snapshot = json.dumps({"schema_version": "framework-eval-scenario-snapshot.v2", "files": {}})
    with sqlite3.connect(store.database_path) as connection:
        for index, state in enumerate(
            ("CALIBRATED", "PENDING_DETERMINISTIC_CALIBRATION", "FAILED"),
            start=1,
        ):
            connection.execute(
                """
                INSERT INTO scenarios(
                    case_id, source_hash, title, case_kind, snapshot_json,
                    publication_status, calibration_state, published_at
                ) VALUES (?, ?, ?, 'REPOSITORY_CASE', ?, 'PUBLISHED', ?, ?)
                """,
                (
                    f"case-{index}",
                    f"source-{index}",
                    f"Case {index}",
                    snapshot,
                    state,
                    f"2026-08-23T00:0{index}:00Z",
                ),
            )
        for index, status in enumerate(("COMPLETED", "FAILED", "RUNNING"), start=1):
            connection.execute(
                """
                INSERT INTO scenario_runs(
                    run_id, status, run_kind, execution_adapter, contract_json,
                    contract_hash, runtime_policy_json, preview_hash, created_by, created_at
                ) VALUES (?, ?, 'FORMAL', 'CODEX', '{}', ?, '{}', ?, 'operator', ?)
                """,
                (
                    f"sv2_{index:032x}",
                    status,
                    f"contract-{index}",
                    f"preview-{index}",
                    f"2026-08-23T01:0{index}:00Z",
                ),
            )
        connection.execute(
            """
            INSERT INTO scenario_calculations(
                calculation_id, run_id, sequence_number, reason, contract_hash,
                contract_json, result_json, created_at
            ) VALUES ('calc-overview', ?, 1, 'initial', 'contract-1', '{}', '{}',
                      '2026-08-23T02:00:00Z')
            """,
            (f"sv2_{1:032x}",),
        )
        connection.execute(
            "UPDATE scenario_runs SET runtime_policy_json = ? WHERE run_id = ?",
            ('{"origin":"EVOLUTION_DISCOVERY"}', f"sv2_{2:032x}"),
        )
        connection.execute(
            "UPDATE scenario_runs SET runtime_policy_json = ? WHERE run_id = ?",
            ('{"origin":"EVOLUTION_REMEDIATION"}', f"sv2_{3:032x}"),
        )
        connection.execute(
            """
            INSERT INTO scenario_reports(calculation_id, run_id, report_json, report_hash, created_at)
            VALUES ('calc-overview', ?, '{}', 'hash', '2026-08-23T02:00:00Z')
            """,
            (f"sv2_{1:032x}",),
        )

    assert store.overview_metrics() == {
        "published_scenarios": 3,
        "calibrated_scenarios": 1,
        "pending_scenarios": 1,
        "failed_scenarios": 1,
        "runs": 3,
        "completed_runs": 1,
        "failed_runs": 1,
        "active_runs": 1,
        "reported_runs": 1,
    }
    assert store.run_metrics_by_origin() == {
        "MANUAL": {
            "runs": 1,
            "completed_runs": 1,
            "failed_runs": 0,
            "active_runs": 0,
            "reported_runs": 1,
        },
        "EVOLUTION": {
            "runs": 2,
            "completed_runs": 0,
            "failed_runs": 1,
            "active_runs": 1,
            "reported_runs": 0,
        },
        "EVOLUTION_DISCOVERY": {
            "runs": 1,
            "completed_runs": 0,
            "failed_runs": 1,
            "active_runs": 0,
            "reported_runs": 0,
        },
        "EVOLUTION_REMEDIATION": {
            "runs": 1,
            "completed_runs": 0,
            "failed_runs": 0,
            "active_runs": 1,
            "reported_runs": 0,
        },
    }


def test_scenario_store_resource_summary_uses_live_worker_heartbeats(tmp_path: Path) -> None:
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()

    store.record_worker_status("worker-idle", "CODEX", "IDLE")
    store.record_worker_status("worker-busy", "CODEX", "RUNNING")

    assert store.resource_summary() == {
        "worker_count": 2,
        "busy_worker_count": 1,
        "idle_worker_count": 1,
        "active_work_count": 0,
        "pending_work_count": 0,
        "unfinished_run_count": 0,
        "head_run": None,
    }

    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE scenario_worker_status SET updated_at = '2026-01-01T00:00:00+00:00'"
        )
    assert store.resource_summary()["worker_count"] == 0


def test_source_evidence_files_are_frozen_even_when_not_manifest_entrypoints(tmp_path: Path) -> None:
    source_directory = _write_scenario_source(tmp_path)
    (source_directory / "business-facts.md").write_text("Accepted business fact.\n", encoding="utf-8")
    gold_path = source_directory / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["source_evidence"][0] = {
        "evidence_id": "E-1",
        "path": "business-facts.md",
        "line_start": 1,
        "line_end": 1,
        "quote": "Accepted business fact.",
    }
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    source = load_scenario_source(source_directory)
    snapshot = json.loads(source.snapshot.snapshot_json)

    assert snapshot["files"]["business-facts.md"]["visibility"] == "CONTROL"


def test_scenario_source_accepts_f2p_and_p2p_without_hidden_checks(tmp_path: Path) -> None:
    source_directory = _write_scenario_source(tmp_path)
    gold_path = source_directory / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["execution_tests"] = [
        {
            "check_id": "F2P-BEHAVIOR",
            "kind": "FAIL_TO_PASS",
            "argv": ["python3", "-m", "pytest", "tests/test_behavior.py"],
            "timeout_seconds": 30,
        },
        {
            "check_id": "P2P-BEHAVIOR",
            "kind": "PASS_TO_PASS",
            "argv": ["python3", "-m", "pytest", "tests/test_regression.py"],
            "timeout_seconds": 30,
        },
    ]
    gold["requirements"][0]["judge_ids"] = ["F2P-BEHAVIOR", "P2P-BEHAVIOR"]
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    source = load_scenario_source(source_directory)

    assert source.preflight_passed


def test_scenario_source_rejects_gold_without_any_deterministic_business_check(tmp_path: Path) -> None:
    source_directory = _write_scenario_source(tmp_path)
    gold_path = source_directory / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["execution_tests"] = []
    gold["requirements"][0]["judge_ids"] = []
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    with pytest.raises(ScenarioSourceError, match="must declare F2P and P2P"):
        load_scenario_source(source_directory)


def test_scenario_source_rejects_retired_hidden_checks(tmp_path: Path) -> None:
    source_directory = _write_scenario_source(tmp_path)
    gold_path = source_directory / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["hidden_checks"] = []
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    with pytest.raises(ScenarioSourceError, match="hidden_checks is retired"):
        load_scenario_source(source_directory)
