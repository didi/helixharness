from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from bench.scenario_calibration import ScenarioCalibrationError, ScenarioCalibrationService
from bench.scenario_storage import ScenarioStore
from bench.scenarios import load_scenario_source


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *args),
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository_scenario(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "business-repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Scenario Test")
    _git(repository, "config", "user.email", "scenario@example.test")
    (repository / "result.txt").write_text("pending\n", encoding="utf-8")
    _git(repository, "add", "result.txt")
    _git(repository, "commit", "-m", "base")
    base_commit = _git(repository, "rev-parse", "HEAD")
    (repository / "result.txt").write_text("complete\n", encoding="utf-8")
    _git(repository, "add", "result.txt")
    _git(repository, "commit", "-m", "solution")
    solution_commit = _git(repository, "rev-parse", "HEAD")
    patch = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "diff",
            "--binary",
            "--full-index",
            base_commit,
            solution_commit,
        ),
        check=True,
        capture_output=True,
    ).stdout
    hidden_test_patch = (
        b"diff --git a/check.py b/check.py\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/check.py\n"
        b"@@ -0,0 +1,2 @@\n"
        b"+from pathlib import Path\n"
        b"+raise SystemExit(0 if 'complete' in Path('result.txt').read_text() else 1)\n"
    )

    source = tmp_path / "sources" / "repository-scenario"
    source.mkdir(parents=True)
    manifest = {
        "schema_version": "framework-eval-scenario.v2",
        "case_id": "repository-scenario",
        "title": "Repository calibration scenario",
        "case_kind": "REPOSITORY_CASE",
        "execution_intent": "FLOW_THROUGH",
        "initial_request_path": "initial-request.md",
        "gold_path": "gold.json",
        "clarification_policy_path": "clarification-policy.json",
        "runtime": {"language": "PYTHON", "python_version": "3.10"},
        "hidden_test_patch": {
            "path": "hidden-tests.patch",
            "sha256": hashlib.sha256(hidden_test_patch).hexdigest(),
            "protected_paths": ["check.py"],
        },
        "repository": {
            "repository_id": "business-repo",
            "base_commit": base_commit,
            "solution_commit": solution_commit,
            "solution_patch_sha256": hashlib.sha256(patch).hexdigest(),
        },
    }
    (source / "case.json").write_text(json.dumps(manifest), encoding="utf-8")
    (source / "initial-request.md").write_text("Make the result complete.\n", encoding="utf-8")
    (source / "hidden-tests.patch").write_bytes(hidden_test_patch)
    (source / "clarification-policy.json").write_text(
        json.dumps(
            {
                "schema_version": "framework-eval-clarification-policy.v1",
                "external_confirmation_required": False,
                "facts": [{"fact_id": "F-1", "statement": "Complete is the target."}],
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
                        "statement": "Result is complete.",
                        "evidence_ids": ["E-1"],
                        "judge_ids": ["F2P-1", "P2P-1"],
                    }
                ],
                "source_evidence": [
                    {
                        "evidence_id": "E-1",
                        "kind": "SOLUTION_FILE",
                        "commit": solution_commit,
                        "path": "result.txt",
                        "sha256": hashlib.sha256(b"complete\n").hexdigest(),
                        "description": "Frozen observable solution output",
                    }
                ],
                "execution_tests": [
                    {
                        "check_id": "F2P-1",
                        "kind": "FAIL_TO_PASS",
                        "argv": ["python3", "check.py"],
                        "timeout_seconds": 10,
                    },
                    {
                        "check_id": "P2P-1",
                        "kind": "PASS_TO_PASS",
                        "argv": ["python3", "-c", "raise SystemExit(0)"],
                        "timeout_seconds": 10,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return source, repository


def test_repository_scenario_calibration_proves_base_solution_and_patch(tmp_path: Path) -> None:
    source, repository = _repository_scenario(tmp_path)
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    published = store.publish(load_scenario_source(source))

    result = ScenarioCalibrationService(store, repositories={"business-repo": repository}).calibrate(
        published.case_id
    )

    assert result.status == "CALIBRATED"
    assert result.solution_patch_sha256_verified is True
    assert result.tests[0].base_status == "FAIL"
    assert result.tests[0].solution_status == "PASS"
    assert store.get(published.case_id).calibration_state == "CALIBRATED"


def test_multi_repository_scenario_calibrates_each_frozen_repository(tmp_path: Path) -> None:
    source, primary_repository = _repository_scenario(tmp_path)
    secondary_repository = tmp_path / "secondary-repository"
    secondary_repository.mkdir()
    _git(secondary_repository, "init")
    _git(secondary_repository, "config", "user.name", "Scenario Test")
    _git(secondary_repository, "config", "user.email", "scenario@example.test")
    (secondary_repository / "secondary.txt").write_text("pending\n", encoding="utf-8")
    _git(secondary_repository, "add", "secondary.txt")
    _git(secondary_repository, "commit", "-m", "base")
    secondary_base = _git(secondary_repository, "rev-parse", "HEAD")
    (secondary_repository / "secondary.txt").write_text("complete\n", encoding="utf-8")
    _git(secondary_repository, "add", "secondary.txt")
    _git(secondary_repository, "commit", "-m", "solution")
    secondary_solution = _git(secondary_repository, "rev-parse", "HEAD")
    secondary_patch = subprocess.run(
        (
            "git",
            "-C",
            str(secondary_repository),
            "diff",
            "--binary",
            "--full-index",
            secondary_base,
            secondary_solution,
        ),
        check=True,
        capture_output=True,
    ).stdout

    manifest_path = source / "case.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    primary_contract = manifest.pop("repository")
    primary_contract.update({"mount_path": "business-repo", "impact": "WRITE"})
    primary_patch = manifest.pop("hidden_test_patch")
    primary_patch["repository_id"] = "business-repo"
    manifest.update(
        {
            "repository_mode": "MULTI_REPOSITORY",
            "workspace_path": "workspace",
            "workspace": {
                "primary_repository_id": "business-repo",
                "repositories": [
                    primary_contract,
                    {
                        "repository_id": "secondary-repo",
                        "mount_path": "secondary-repo",
                        "impact": "WRITE",
                        "base_commit": secondary_base,
                        "solution_commit": secondary_solution,
                        "solution_patch_sha256": hashlib.sha256(secondary_patch).hexdigest(),
                    },
                ],
            },
            "hidden_test_patches": [primary_patch],
        }
    )
    workspace_assets = source / "workspace"
    workspace_assets.mkdir()
    (workspace_assets / "helixharness.workspace.yaml").write_text(
        "version: 1\nworkspace:\n  name: calibration-test\n  root: .\n  mode: multi-repo\n",
        encoding="utf-8",
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    gold_path = source / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["source_evidence"][0]["repository_id"] = "business-repo"
    gold["source_evidence"].append(
        {
            "evidence_id": "E-2",
            "kind": "SOLUTION_FILE",
            "repository_id": "secondary-repo",
            "commit": secondary_solution,
            "path": "secondary.txt",
            "sha256": hashlib.sha256(b"complete\n").hexdigest(),
            "description": "Frozen secondary repository output",
        }
    )
    gold["requirements"][0]["evidence_ids"].append("E-2")
    for test in gold["execution_tests"]:
        test["working_directory"] = "business-repo"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    published = store.publish(load_scenario_source(source))
    result = ScenarioCalibrationService(
        store,
        repositories={
            "business-repo": primary_repository,
            "secondary-repo": secondary_repository,
        },
    ).calibrate(published.case_id)

    assert result.status == "CALIBRATED"
    assert result.solution_patch_sha256_verified is True
    assert result.source_evidence_verified is True


def test_calibration_isolates_each_execution_test_like_formal_verification(tmp_path: Path) -> None:
    source, repository = _repository_scenario(tmp_path)
    hidden_test_patch = (
        b"diff --git a/check.py b/check.py\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/check.py\n"
        b"@@ -0,0 +1,9 @@\n"
        b"+import sys\n"
        b"+from pathlib import Path\n"
        b"+if sys.argv[1] == 'behavior':\n"
        b"+    Path('execution-test-marker').write_text('created')\n"
        b"+    raise SystemExit(0 if 'complete' in Path('result.txt').read_text() else 1)\n"
        b"+if sys.argv[1] == 'isolation':\n"
        b"+    raise SystemExit(1 if Path('execution-test-marker').exists() else 0)\n"
        b"+raise SystemExit(2)\n"
        b"+\n"
    )
    (source / "hidden-tests.patch").write_bytes(hidden_test_patch)
    manifest_path = source / "case.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["hidden_test_patch"]["sha256"] = hashlib.sha256(hidden_test_patch).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    gold_path = source / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["execution_tests"][0]["argv"] = ["python3", "check.py", "behavior"]
    gold["execution_tests"][1]["argv"] = ["python3", "check.py", "isolation"]
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    published = store.publish(load_scenario_source(source))

    result = ScenarioCalibrationService(store, repositories={"business-repo": repository}).calibrate(
        published.case_id
    )

    assert result.status == "CALIBRATED"
    assert result.tests[1].base_status == "PASS"
    assert result.tests[1].solution_status == "PASS"


def test_calibration_does_not_accept_behavior_supplied_by_solution_test_changes(tmp_path: Path) -> None:
    source, repository = _repository_scenario(tmp_path)
    manifest_path = source / "case.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_base = manifest["repository"]["base_commit"]
    _git(repository, "checkout", "--detach", original_base)
    fixture = repository / "src/test/reference-fixture.txt"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("base\n", encoding="utf-8")
    _git(repository, "add", "src/test/reference-fixture.txt")
    _git(repository, "commit", "-m", "fixture base")
    base_commit = _git(repository, "rev-parse", "HEAD")
    (repository / "result.txt").write_text("complete\n", encoding="utf-8")
    fixture.write_text("solution\n", encoding="utf-8")
    _git(repository, "add", "result.txt", "src/test/reference-fixture.txt")
    _git(repository, "commit", "-m", "solution with reference-only test change")
    solution_commit = _git(repository, "rev-parse", "HEAD")
    solution_patch = subprocess.run(
        ("git", "-C", str(repository), "diff", "--binary", "--full-index", base_commit, solution_commit),
        check=True,
        capture_output=True,
    ).stdout
    manifest["repository"]["base_commit"] = base_commit
    manifest["repository"]["solution_commit"] = solution_commit
    manifest["repository"]["solution_patch_sha256"] = hashlib.sha256(solution_patch).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    hidden_test_patch = (
        b"diff --git a/check.py b/check.py\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/check.py\n"
        b"@@ -0,0 +1,4 @@\n"
        b"+from pathlib import Path\n"
        b"+result = Path('result.txt').read_text()\n"
        b"+fixture = Path('src/test/reference-fixture.txt').read_text()\n"
        b"+raise SystemExit(0 if 'complete' in result and 'solution' in fixture else 1)\n"
    )
    (source / "hidden-tests.patch").write_bytes(hidden_test_patch)
    manifest["hidden_test_patch"]["sha256"] = hashlib.sha256(hidden_test_patch).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    gold_path = source / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["source_evidence"][0]["commit"] = solution_commit
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    published = store.publish(load_scenario_source(source))
    result = ScenarioCalibrationService(store, repositories={"business-repo": repository}).calibrate(
        published.case_id
    )

    assert result.status == "FAILED"
    assert result.tests[0].base_status == "FAIL"
    assert result.tests[0].solution_status == "FAIL"
    assert result.failures == ("F2P-1 does not satisfy FAIL_TO_PASS Base/Solution behavior",)


def test_solution_file_names_containing_test_or_spec_are_not_treated_as_test_fixtures(tmp_path: Path) -> None:
    source, repository = _repository_scenario(tmp_path)
    manifest_path = source / "case.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_base = manifest["repository"]["base_commit"]
    _git(repository, "checkout", "--detach", original_base)
    production = repository / "src/main/java/example/ContestSpecification.java"
    production.parent.mkdir(parents=True)
    production.write_text("base\n", encoding="utf-8")
    _git(repository, "add", str(production.relative_to(repository)))
    _git(repository, "commit", "-m", "production base")
    base_commit = _git(repository, "rev-parse", "HEAD")
    production.write_text("complete\n", encoding="utf-8")
    _git(repository, "add", str(production.relative_to(repository)))
    _git(repository, "commit", "-m", "production solution")
    solution_commit = _git(repository, "rev-parse", "HEAD")
    solution_patch = subprocess.run(
        ("git", "-C", str(repository), "diff", "--binary", "--full-index", base_commit, solution_commit),
        check=True,
        capture_output=True,
    ).stdout
    manifest["repository"]["base_commit"] = base_commit
    manifest["repository"]["solution_commit"] = solution_commit
    manifest["repository"]["solution_patch_sha256"] = hashlib.sha256(solution_patch).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    hidden_test_patch = (
        b"diff --git a/check.py b/check.py\n"
        b"new file mode 100644\n"
        b"--- /dev/null\n"
        b"+++ b/check.py\n"
        b"@@ -0,0 +1,3 @@\n"
        b"+from pathlib import Path\n"
        b"+value = Path('src/main/java/example/ContestSpecification.java').read_text()\n"
        b"+raise SystemExit(0 if 'complete' in value else 1)\n"
    )
    (source / "hidden-tests.patch").write_bytes(hidden_test_patch)
    manifest["hidden_test_patch"]["sha256"] = hashlib.sha256(hidden_test_patch).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    gold_path = source / "gold.json"
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    gold["source_evidence"][0]["commit"] = solution_commit
    gold["source_evidence"][0]["path"] = "src/main/java/example/ContestSpecification.java"
    gold["source_evidence"][0]["sha256"] = hashlib.sha256(b"complete\n").hexdigest()
    gold_path.write_text(json.dumps(gold), encoding="utf-8")

    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    published = store.publish(load_scenario_source(source))
    result = ScenarioCalibrationService(store, repositories={"business-repo": repository}).calibrate(
        published.case_id
    )

    assert result.status == "CALIBRATED"
    assert result.tests[0].base_status == "FAIL"
    assert result.tests[0].solution_status == "PASS"


def test_java_calibration_stops_before_execution_when_declared_jdk_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, repository = _repository_scenario(tmp_path)
    manifest_path = source / "case.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"] = {
        "language": "JAVA",
        "java_major": 8,
        "java_home_env": "SCENARIO_TEST_JAVA_HOME",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.delenv("SCENARIO_TEST_JAVA_HOME", raising=False)
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    published = store.publish(load_scenario_source(source))

    with pytest.raises(ScenarioCalibrationError, match="SCENARIO_TEST_JAVA_HOME"):
        ScenarioCalibrationService(store, repositories={"business-repo": repository}).calibrate(
            published.case_id
        )

    assert store.get(published.case_id).calibration_state == "PENDING_DETERMINISTIC_CALIBRATION"


def test_calibration_enforces_its_workload_disk_budget(tmp_path: Path) -> None:
    source, repository = _repository_scenario(tmp_path)
    store = ScenarioStore(tmp_path / "evaluation.db")
    store.initialize()
    published = store.publish(load_scenario_source(source))

    with pytest.raises(ScenarioCalibrationError, match="disk budget"):
        ScenarioCalibrationService(
            store,
            repositories={"business-repo": repository},
            disk_budget_mb=0,
        ).calibrate(published.case_id)
