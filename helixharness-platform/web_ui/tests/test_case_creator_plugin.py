"""Contracts for the packaged Scenario v2 Case Creator plugin."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from bench.scenario_storage import ScenarioStore
from bench.scenarios import load_scenario_source

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = PROJECT_ROOT / "web_ui" / "static" / "plugins" / "helixharness-case-creator"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "helixharness-case-creator"


def _load_script(name: str) -> ModuleType:
    path = SKILL_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"helixharness_case_creator_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repository), *arguments], check=True, capture_output=True, text=True)


def _registry_snapshot(tmp_path: Path) -> Path:
    registry = tmp_path / "repositories" / "registry.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps({"schema_version": "framework-eval-repository-registry.v1", "repositories": {}}),
        encoding="utf-8",
    )
    return registry


def _write_scenario_source(root: Path, *, title: str = "Scenario one") -> Path:
    source = root / "scenario-one"
    source.mkdir(parents=True)
    (source / "case.json").write_text(
        json.dumps(
            {
                "schema_version": "framework-eval-scenario.v2",
                "case_id": "scenario-one",
                "title": title,
                "case_kind": "DEVELOPMENT_FIXTURE",
                "execution_intent": "FLOW_THROUGH",
                "initial_request_path": "initial-request.md",
                "gold_path": "gold.json",
                "clarification_policy_path": "clarification-policy.json",
                "workspace_path": "workspace",
                "runtime": {"language": "PYTHON", "python_version": "3.10"},
            }
        ),
        encoding="utf-8",
    )
    (source / "initial-request.md").write_text("Implement the observable behavior.\n", encoding="utf-8")
    (source / "clarification-policy.json").write_text(
        json.dumps(
            {
                "schema_version": "framework-eval-clarification-policy.v1",
                "execution_intent": "FLOW_THROUGH",
                "external_confirmation_required": False,
                "unknown_business_fact_policy": "CASE_INVALID",
                "repository_fact_policy": "AGENT_INSPECTS",
                "facts": [
                    {
                        "fact_id": "F-1",
                        "statement": "输入为空时保持原结果且不创建新记录。",
                        "source": "initial-request.md:1",
                    }
                ],
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


def test_plugin_is_packaged_for_scenario_v2() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    contract = (SKILL_ROOT / "references" / "case-contract.md").read_text(encoding="utf-8")

    assert manifest["name"] == "helixharness-case-creator"
    assert manifest["skills"] == "./skills/"
    assert "evaluation.db" in skill
    assert "case-sources-v2" in skill
    assert "framework-eval-scenario.v2" in contract
    assert "framework-eval-case.v1" not in contract
    assert "MULTI_REPOSITORY" in skill
    assert "workspace.repositories" in contract


def test_repository_discovery_reads_git_head_not_uncommitted_content(tmp_path: Path) -> None:
    discover = _load_script("discover_repositories")
    repository = tmp_path / "repo-a"
    feature = repository / "docs" / "specs" / "completed" / "ready-feature"
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text("# Ready\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "config", "user.email", "case@example.com")
    _git(repository, "config", "user.name", "Case Test")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")
    (feature / "spec.md").write_text("UNCOMMITTED SECRET\n", encoding="utf-8")

    payload = discover.discover(tmp_path)

    assert payload["repositories"][0]["features"][0]["eligibility"] in {"CANDIDATE", "NEEDS_REVIEW"}
    assert "UNCOMMITTED SECRET" not in json.dumps(payload)


def test_preflight_and_guard_use_only_scenario_v2_state(tmp_path: Path) -> None:
    preflight = _load_script("preflight_case")
    guard = _load_script("case_guard")
    source_root = tmp_path / "case-sources-v2"
    source = _write_scenario_source(source_root)
    database = tmp_path / "evaluation.db"
    store = ScenarioStore(database)
    store.initialize()
    published = store.publish(load_scenario_source(source))
    registry = _registry_snapshot(tmp_path)

    preflight_result = preflight.preflight(PROJECT_ROOT, source, None)
    unchanged = guard.guard(
        PROJECT_ROOT,
        source,
        None,
        update_existing=False,
        database_path=database,
        case_source_root=source_root,
        repository_registry_path=registry,
    )

    assert preflight_result["schema_version"] == "helixharness-case-creator-preflight.v2"
    assert preflight_result["source_hash"] == published.source_hash
    assert unchanged["schema_version"] == "helixharness-case-creator-guard.v2"
    assert unchanged["decision"] == "NO_CHANGE"
    assert unchanged["state_source"]["database"] == str(database.resolve())


def test_guard_requires_confirmation_for_changed_v2_case(tmp_path: Path) -> None:
    guard = _load_script("case_guard")
    source_root = tmp_path / "case-sources-v2"
    source = _write_scenario_source(source_root)
    database = tmp_path / "evaluation.db"
    store = ScenarioStore(database)
    store.initialize()
    store.publish(load_scenario_source(source))
    staged = _write_scenario_source(tmp_path / "staging", title="Changed title")
    registry = _registry_snapshot(tmp_path)

    blocked = guard.guard(
        PROJECT_ROOT,
        staged,
        None,
        update_existing=False,
        database_path=database,
        case_source_root=source_root,
        repository_registry_path=registry,
    )
    allowed = guard.guard(
        PROJECT_ROOT,
        staged,
        None,
        update_existing=True,
        database_path=database,
        case_source_root=source_root,
        repository_registry_path=registry,
    )

    assert blocked["decision"] == "UPDATE_REQUIRES_CONFIRMATION"
    assert allowed["decision"] == "READY_UPDATE_RECALIBRATION"


def test_guard_does_not_create_missing_v2_database(tmp_path: Path) -> None:
    guard = _load_script("case_guard")
    source_root = tmp_path / "case-sources-v2"
    source = _write_scenario_source(source_root)
    database = tmp_path / "evaluation.db"

    result = guard.guard(
        PROJECT_ROOT,
        source,
        None,
        update_existing=False,
        database_path=database,
        case_source_root=source_root,
        repository_registry_path=_registry_snapshot(tmp_path),
    )

    assert result["decision"] == "NO_PLATFORM_DATABASE"
    assert not database.exists()


def test_repository_interval_rejects_a_forged_patch_hash(tmp_path: Path) -> None:
    interval = _load_script("repository_interval")
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "case@example.com")
    _git(repository, "config", "user.name", "Case Test")
    (repository / "value.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    base = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repository / "value.txt").write_text("solution\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "solution")
    solution = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    patch = subprocess.run(
        ["git", "-C", str(repository), "diff", "--binary", "--full-index", f"{base}..{solution}"],
        check=True,
        capture_output=True,
    ).stdout
    manifest = {
        "case_kind": "REPOSITORY_CASE",
        "repository": {
            "repository_id": "example",
            "base_commit": base,
            "solution_commit": solution,
            "solution_patch_sha256": hashlib.sha256(patch).hexdigest(),
        },
    }

    interval.validate_repository_interval(manifest, {"example": repository})
    manifest["repository"]["solution_patch_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="真实 diff 不一致"):
        interval.validate_repository_interval(manifest, {"example": repository})


def test_repository_interval_validates_every_multi_repository_binding(tmp_path: Path) -> None:
    interval = _load_script("repository_interval")
    definitions: list[dict[str, str]] = []
    repositories: dict[str, Path] = {}
    for repository_id in ("service-a", "service-b"):
        repository = tmp_path / repository_id
        repository.mkdir()
        _git(repository, "init")
        _git(repository, "config", "user.email", "case@example.com")
        _git(repository, "config", "user.name", "Case Test")
        value = repository / "value.txt"
        value.write_text("base\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "base")
        base = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        value.write_text("solution\n", encoding="utf-8")
        _git(repository, "add", ".")
        _git(repository, "commit", "-m", "solution")
        solution = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        patch = subprocess.run(
            ["git", "-C", str(repository), "diff", "--binary", "--full-index", f"{base}..{solution}"],
            check=True,
            capture_output=True,
        ).stdout
        definitions.append(
            {
                "repository_id": repository_id,
                "mount_path": repository_id,
                "impact": "WRITE",
                "base_commit": base,
                "solution_commit": solution,
                "solution_patch_sha256": hashlib.sha256(patch).hexdigest(),
            }
        )
        repositories[repository_id] = repository

    manifest = {
        "case_kind": "REPOSITORY_CASE",
        "repository_mode": "MULTI_REPOSITORY",
        "workspace": {
            "primary_repository_id": "service-a",
            "repositories": definitions,
        },
    }

    interval.validate_repository_interval(manifest, repositories)
    with pytest.raises(ValueError, match="缺少本地仓库绑定：service-b"):
        interval.validate_repository_interval(manifest, {"service-a": repositories["service-a"]})


def test_quality_audit_accepts_concrete_portable_case(tmp_path: Path) -> None:
    quality = _load_script("audit_case_quality")
    source = _write_scenario_source(tmp_path)
    policy_path = source / "clarification-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["facts"][0]["statement"] = "订单提交后，认证配置保持既有值。"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    result = quality.audit(source)

    assert result["decision"] == "STATIC_CHECK_PASSED"
    assert result["blocker_count"] == 0
    assert result["risk_count"] == 0


def test_quality_audit_separates_blockers_from_static_risks(tmp_path: Path) -> None:
    quality = _load_script("audit_case_quality")
    source = _write_scenario_source(tmp_path)
    policy_path = source / "clarification-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["facts"] = [
        {"fact_id": "F-1", "statement": "行为已冻结。", "source": "PRD"},
        {"fact_id": "F-2", "statement": "完成后 commit 并归档。", "source": "workflow"},
    ]
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    (source / "hidden-runner.java").write_text(
        'value.getClass().getDeclaredField("mapper").setAccessible(true);\n',
        encoding="utf-8",
    )

    result = quality.audit(source)
    codes = {item["code"] for item in result["findings"]}

    assert result["decision"] == "BLOCKED"
    assert "SHELL_CLARIFICATION_FACT" in codes
    assert "WORKFLOW_CHOICE_AS_FACT" in codes
    assert "PRIVATE_IMPLEMENTATION_COUPLING" in codes


def test_quality_audit_flags_unreproducible_historical_test_quota(tmp_path: Path) -> None:
    quality = _load_script("audit_case_quality")
    source = _write_scenario_source(tmp_path)
    (source / "initial-request.md").write_text(
        "实现可观察行为，并复跑真实历史 8 项测试。\n",
        encoding="utf-8",
    )

    result = quality.audit(source)

    assert result["decision"] == "NEEDS_REVIEW"
    assert {
        item["code"] for item in result["findings"]
    } == {"UNREPRODUCIBLE_HISTORICAL_TEST_REQUIREMENT"}


def test_quality_audit_requires_candidate_visibility_boundary(tmp_path: Path) -> None:
    quality = _load_script("audit_case_quality")
    source = _write_scenario_source(tmp_path)
    request = source / "initial-request.md"
    request.write_text("AC1：Solution 3/3 通过并生成 JUnit XML。\n", encoding="utf-8")

    missing = quality.audit(source)
    request.write_text(
        "AC1：Solution 3/3 通过并生成 JUnit XML。\n"
        "该证据由评测平台在工作流后独立执行，不属于候选侧 Verify 前置证据。\n",
        encoding="utf-8",
    )
    bounded = quality.audit(source)

    assert {item["code"] for item in missing["findings"]} == {
        "CANDIDATE_INVISIBLE_CALIBRATION_REQUIREMENT"
    }
    assert bounded["decision"] == "STATIC_CHECK_PASSED"


@pytest.mark.parametrize("missing", [None, "spec.md", "tasks.md", "verify-report.md"])
def test_discovery_uses_current_three_document_contract(tmp_path: Path, missing: str | None) -> None:
    module = _load_script("discover_repositories")
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    docs = repository / "docs/specs/completed/demo"
    docs.mkdir(parents=True)
    for name in ("spec.md", "tasks.md", "verify-report.md"):
        if name != missing:
            (docs / name).write_text("Current feature evidence\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
    found = module.discover(tmp_path)["repositories"][0]["features"][0]
    assert found["missing_stage_documents"] == ([missing] if missing else [])
    assert (found["eligibility"] == "CANDIDATE") is (missing is None)
    assert found["discovery_status"] == ("MISSING_SPEC" if missing == "spec.md" else "HAS_SPEC")


def test_case_creator_contains_a_local_installable_marketplace() -> None:
    manifest = json.loads((PLUGIN_ROOT / ".claude-plugin/marketplace.json").read_text())
    assert manifest["name"] == "helixharness-platform"
    assert manifest["plugins"][0]["name"] == "helixharness-case-creator"
    assert manifest["plugins"][0]["source"] == "."
    assert "codex plugin marketplace add" in (PLUGIN_ROOT / "README.md").read_text()


def test_repository_cli_requires_explicit_identity(tmp_path: Path) -> None:
    interval = _load_script("repository_interval")
    with pytest.raises(ValueError, match="repository_id"):
        interval.parse_repositories([str(tmp_path)])
    assert interval.parse_repositories([f"example={tmp_path}"]) == {"example": tmp_path}
