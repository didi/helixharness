from __future__ import annotations

import json
import subprocess
from base64 import b64encode
from pathlib import Path

from bench.scenario_storage import PublishedScenario
from web_ui.scenario_views import scenario_case_detail_view, scenario_case_view


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_scenario_case_view_projects_frozen_interval_difficulty(tmp_path: Path) -> None:
    repository = tmp_path / "business-repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario View Test")
    _git(repository, "config", "user.email", "scenario-view@example.test")
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    base_commit = _git(repository, "rev-parse", "HEAD")
    (repository / "feature.py").write_text("enabled = True\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "solution")
    solution_commit = _git(repository, "rev-parse", "HEAD")
    manifest = {
        "repository": {
            "repository_id": "business-repository",
            "base_commit": base_commit,
            "solution_commit": solution_commit,
        },
        "runtime": {"language": "PYTHON", "python_version": "3.10"},
    }
    snapshot = {
        "files": {
            "case.json": {
                "content_base64": b64encode(
                    json.dumps(manifest).encode("utf-8")
                ).decode("ascii")
            }
        }
    }
    scenario = PublishedScenario(
        case_id="difficulty-case",
        source_hash="a" * 64,
        title="Difficulty Case",
        case_kind="REPOSITORY_CASE",
        snapshot_json=json.dumps(snapshot),
        publication_status="PUBLISHED",
        calibration_state="CALIBRATED",
        published_at="2026-08-23T00:00:00Z",
    )

    view = scenario_case_view(
        scenario,
        repositories={"business-repository": repository},
    )

    assert view["difficulty"] == "EASY"
    assert view["difficulty_label"] == "简单"
    assert view["difficulty_evidence"] == "业务代码改动 1 行"


def test_scenario_case_detail_projects_latest_contract_sections() -> None:
    def payload(value: object) -> dict[str, str]:
        return {
            "content_base64": b64encode(json.dumps(value).encode("utf-8")).decode("ascii"),
            "sha256": "b" * 64,
            "visibility": "HIDDEN",
        }

    manifest = {
        "schema_version": "framework-eval-scenario.v2",
        "initial_request_path": "initial-request.md",
        "gold_path": "gold.json",
        "clarification_policy_path": "clarification.json",
        "provenance_path": "provenance.json",
        "execution_intent": "FLOW_THROUGH",
        "repository": {
            "repository_id": "business-repository",
            "base_commit": "1" * 40,
            "solution_commit": "2" * 40,
            "solution_patch_sha256": "3" * 64,
        },
        "runtime": {"language": "JAVA", "java_major": 8, "java_home_env": "JAVA_HOME"},
        "hidden_test_patch": {
            "path": "hidden.patch",
            "sha256": "4" * 64,
            "protected_paths": ["hidden-checks"],
        },
    }
    gold = {
        "schema_version": "framework-eval-scenario-gold.v2",
        "requirements": [
            {
                "requirement_id": "REQ-1",
                "statement": "### REQ-1: Keep the business result stable.",
                "acceptance_criteria": ["Result remains stable"],
                "evidence_ids": ["E-1"],
                "judge_ids": ["F2P-1", "SC-1"],
            }
        ],
        "source_evidence": [
            {
                "evidence_id": "E-1",
                "kind": "SOLUTION_FILE",
                "commit": "2" * 40,
                "path": "src/Feature.java",
                "sha256": "5" * 64,
                "description": "Frozen implementation evidence",
            }
        ],
        "execution_tests": [
            {
                "check_id": "F2P-1",
                "kind": "FAIL_TO_PASS",
                "argv": ["./gradlew", "test"],
                "timeout_seconds": 120,
            },
            {
                "check_id": "P2P-1",
                "kind": "PASS_TO_PASS",
                "argv": ["./gradlew", "regressionTest"],
                "timeout_seconds": 120,
            },
        ],
        "solution_checks": [
            {
                "check_id": "SC-1",
                "scope": "SOLUTION",
                "argv": ["./gradlew", "check"],
                "timeout_seconds": 120,
            }
        ],
    }
    clarification = {
        "schema_version": "framework-eval-clarification-policy.v1",
        "external_confirmation_required": False,
        "facts": [{"fact_id": "F-1", "source": "PRD", "statement": "The result is stable."}],
    }
    provenance = {
        "schema_version": "framework-eval-provenance.v1",
        "requirement_source": "PRD",
        "requirement_path": "requirements.md",
        "source_interval": {"base_commit": "1" * 40, "solution_commit": "2" * 40},
    }
    snapshot = {
        "schema_version": "framework-eval-scenario-snapshot.v2",
        "files": {
            "case.json": payload(manifest),
            "gold.json": payload(gold),
            "clarification.json": payload(clarification),
            "provenance.json": payload(provenance),
            "initial-request.md": {
                "content_base64": b64encode(b"Implement the frozen requirement.\n").decode("ascii"),
                "sha256": "6" * 64,
                "visibility": "CONTROL",
            },
        },
    }
    scenario = PublishedScenario(
        case_id="contract-case",
        source_hash="a" * 64,
        title="Contract Case",
        case_kind="REPOSITORY_CASE",
        snapshot_json=json.dumps(snapshot),
        publication_status="PUBLISHED",
        calibration_state="CALIBRATED",
        published_at="2026-08-23T00:00:00Z",
    )

    detail = scenario_case_detail_view(scenario)

    assert detail["repository"]["base_commit"] == "1" * 40
    assert detail["runtime"]["java_major"] == 8
    assert detail["verification_count"] == 3
    assert detail["requirements"][0]["acceptance_criteria"] == ["Result remains stable"]
    assert detail["requirements"][0]["display_statement"] == "Keep the business result stable."
    assert detail["requirements"][0]["statement"] == "### REQ-1: Keep the business result stable."
    assert detail["provenance"]["requirement_source"] == "PRD"
    assert detail["hidden_test_patch"]["protected_paths"] == ["hidden-checks"]
    assert detail["schema_versions"]["冻结快照"] == "framework-eval-scenario-snapshot.v2"
    assert detail["contract_files"] == [
        {
            "label": "Case 定义",
            "path": "case.json",
            "version": "framework-eval-scenario.v2",
            "purpose": "仓库、运行环境和各评测资产的总入口",
        },
        {
            "label": "评测目标",
            "path": "initial-request.md",
            "version": "",
            "purpose": "提供给执行模型的业务任务说明",
        },
        {
            "label": "验收标准",
            "path": "gold.json",
            "version": "framework-eval-scenario-gold.v2",
            "purpose": "需求、来源证据和校验项之间的冻结关系",
        },
        {
            "label": "澄清答案",
            "path": "clarification.json",
            "version": "framework-eval-clarification-policy.v1",
            "purpose": "执行中允许直接使用的预置事实和澄清边界",
        },
        {
            "label": "来源记录",
            "path": "provenance.json",
            "version": "framework-eval-provenance.v1",
            "purpose": "记录 Case 由哪些历史材料整理而来，仅用于审计",
        },
        {
            "label": "隐藏校验补丁",
            "path": "hidden.patch",
            "version": "",
            "purpose": "仅由平台在评测时加载的隐藏校验资产",
        },
    ]
