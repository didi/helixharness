"""Workflow-neutral Evaluation Scenario source contracts."""

from __future__ import annotations

import json
import re
from base64 import b64encode
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from bench.scenario_workspace import scenario_workspace

SCENARIO_SCHEMA_VERSION = "framework-eval-scenario.v2"
SCENARIO_GOLD_SCHEMA_VERSION = "framework-eval-scenario-gold.v2"

_REQUIRED_MANIFEST_FIELDS = (
    "schema_version",
    "case_id",
    "title",
    "case_kind",
    "execution_intent",
    "initial_request_path",
    "gold_path",
    "clarification_policy_path",
    "runtime",
)
_RUN_OWNED_MANIFEST_FIELDS = {
    "profile",
    "profile_version",
    "workflow_driver",
    "workflow_policy",
    "workflow_commands",
    "scoring_profile",
}
_RUN_OWNED_GOLD_FIELDS = {
    "artifact_paths",
    "stage_artifact_contract",
    "milestones",
    "judge_guidance",
    "score_axes",
}
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_WORKSPACE_FIXTURE_BUILD_FILES = {
    "build.gradle",
    "build.gradle.kts",
    "gradle.properties",
    "package.json",
    "pom.xml",
    "settings.gradle",
    "settings.gradle.kts",
}


class ScenarioSourceError(ValueError):
    """Raised when a v2 Scenario Source violates the publication contract."""


@dataclass(frozen=True)
class ScenarioSnapshot:
    """Canonical immutable bytes captured from one valid Scenario Source."""

    source_hash: str
    snapshot_json: str


@dataclass(frozen=True)
class ScenarioSource:
    """One workflow-neutral source package ready for publication."""

    case_id: str
    title: str
    case_kind: str
    source_directory: Path
    manifest: dict[str, Any]
    gold: dict[str, Any]
    snapshot: ScenarioSnapshot

    @property
    def preflight_passed(self) -> bool:
        """A returned source has passed every deterministic preflight check."""

        return True


def load_scenario_source(source_directory: Path) -> ScenarioSource:
    """Validate and freeze one ``framework-eval-scenario.v2`` source directory."""

    source_directory = source_directory.resolve()
    manifest = _read_json_object(source_directory / "case.json", "case.json")
    _validate_manifest(manifest)

    initial_request_path = _declared_file(source_directory, manifest, "initial_request_path")
    gold_path = _declared_file(source_directory, manifest, "gold_path")
    clarification_path = _declared_file(source_directory, manifest, "clarification_policy_path")
    initial_request = _read_text(initial_request_path, "initial_request_path")
    if not initial_request.strip():
        raise ScenarioSourceError("initial_request_path must reference non-empty UTF-8 text")

    gold = _read_json_object(gold_path, "gold_path")
    _validate_gold(
        gold,
        source_directory=source_directory,
        allow_repository_evidence=manifest["case_kind"] == "REPOSITORY_CASE",
    )
    if manifest["case_kind"] == "REPOSITORY_CASE":
        _validate_repository_workspace_assets(manifest, gold, source_directory=source_directory)
    clarification = _read_json_object(clarification_path, "clarification_policy_path")
    _validate_clarification_policy(clarification)

    declared_paths = {
        "case.json",
        str(manifest["initial_request_path"]),
        str(manifest["gold_path"]),
        str(manifest["clarification_policy_path"]),
    }
    declared_paths.update(
        str(item["path"])
        for item in gold["source_evidence"]
        if (
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and "line_start" in item
        )
    )
    for optional_path_field in ("provenance_path",):
        value = manifest.get(optional_path_field)
        if value is not None:
            _declared_file(source_directory, manifest, optional_path_field)
            declared_paths.add(str(value))

    workspace_value = manifest.get("workspace_path")
    if workspace_value is not None:
        workspace = _safe_path(source_directory, workspace_value, "workspace_path")
        if not workspace.is_dir() or workspace.is_symlink():
            raise ScenarioSourceError("workspace_path must reference a real directory")
        workspace_files = [path for path in sorted(workspace.rglob("*")) if path.is_file()]
        if not workspace_files:
            raise ScenarioSourceError("workspace_path must not be empty")
        if any(path.is_symlink() for path in workspace_files):
            raise ScenarioSourceError("workspace_path must not contain symbolic links")
        declared_paths.update(str(path.relative_to(source_directory)) for path in workspace_files)

    hidden_patch = manifest.get("hidden_test_patch")
    if hidden_patch is not None:
        if not isinstance(hidden_patch, dict) or not isinstance(hidden_patch.get("path"), str):
            raise ScenarioSourceError("hidden_test_patch.path must be a declared file")
        patch_path = _safe_path(source_directory, hidden_patch["path"], "hidden_test_patch.path")
        if not patch_path.is_file() or patch_path.is_symlink():
            raise ScenarioSourceError("hidden_test_patch.path must reference a real file")
        declared_paths.add(str(hidden_patch["path"]))

    hidden_patches = manifest.get("hidden_test_patches")
    if hidden_patches is not None:
        if not isinstance(hidden_patches, list) or not hidden_patches:
            raise ScenarioSourceError("hidden_test_patches must be a non-empty list")
        for item in hidden_patches:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ScenarioSourceError("hidden_test_patches entries require a path")
            patch_path = _safe_path(source_directory, item["path"], "hidden_test_patches.path")
            if not patch_path.is_file() or patch_path.is_symlink():
                raise ScenarioSourceError("hidden_test_patches.path must reference a real file")
            declared_paths.add(str(item["path"]))

    workspace_fixture_patch = manifest.get("workspace_fixture_patch")
    if workspace_fixture_patch is not None:
        if not isinstance(workspace_fixture_patch, dict) or not isinstance(
            workspace_fixture_patch.get("path"), str
        ):
            raise ScenarioSourceError("workspace_fixture_patch.path must be a declared file")
        fixture_path = _safe_path(
            source_directory,
            workspace_fixture_patch["path"],
            "workspace_fixture_patch.path",
        )
        if not fixture_path.is_file() or fixture_path.is_symlink():
            raise ScenarioSourceError("workspace_fixture_patch.path must reference a real file")
        declared_paths.add(str(workspace_fixture_patch["path"]))

    snapshot = _build_snapshot(
        source_directory,
        manifest,
        declared_paths=declared_paths,
    )
    return ScenarioSource(
        case_id=str(manifest["case_id"]),
        title=str(manifest["title"]),
        case_kind=str(manifest["case_kind"]),
        source_directory=source_directory,
        manifest=manifest,
        gold=gold,
        snapshot=snapshot,
    )


def _validate_manifest(manifest: dict[str, Any]) -> None:
    """Enforce Scenario ownership and the frozen runtime/repository boundary."""

    missing = [field for field in _REQUIRED_MANIFEST_FIELDS if field not in manifest]
    if missing:
        raise ScenarioSourceError("case.json missing required fields: " + ", ".join(missing))
    if manifest.get("schema_version") != SCENARIO_SCHEMA_VERSION:
        raise ScenarioSourceError(f"schema_version must be {SCENARIO_SCHEMA_VERSION}")
    forbidden = sorted(_RUN_OWNED_MANIFEST_FIELDS.intersection(manifest))
    if forbidden:
        raise ScenarioSourceError("Case Source contains Run-owned fields: " + ", ".join(forbidden))
    for field in ("case_id", "title", "case_kind"):
        if not isinstance(manifest.get(field), str) or not str(manifest[field]).strip():
            raise ScenarioSourceError(f"{field} must be non-empty text")
    if manifest["case_kind"] not in {"DEVELOPMENT_FIXTURE", "REPOSITORY_CASE"}:
        raise ScenarioSourceError("case_kind must be DEVELOPMENT_FIXTURE or REPOSITORY_CASE")
    if manifest["execution_intent"] not in {"FLOW_THROUGH", "BLOCKING_PROBE"}:
        raise ScenarioSourceError("execution_intent must be FLOW_THROUGH or BLOCKING_PROBE")
    runtime = manifest.get("runtime")
    if not isinstance(runtime, dict) or not isinstance(runtime.get("language"), str):
        raise ScenarioSourceError("runtime must declare a language")
    workspace_preconditions = manifest.get("workspace_preconditions")
    if workspace_preconditions is not None:
        expected_keys = {"schema_version", "helixharness_initialization", "init_mode"}
        if (
            not isinstance(workspace_preconditions, dict)
            or set(workspace_preconditions) != expected_keys
            or workspace_preconditions.get("schema_version")
            != "framework-eval-workspace-preconditions.v1"
            or workspace_preconditions.get("helixharness_initialization") != "ABSENT"
            or workspace_preconditions.get("init_mode") not in {"SERVICE", "FRONTEND", "WORKSPACE"}
        ):
            raise ScenarioSourceError(
                "workspace_preconditions requires a frozen ABSENT HelixHarness state and init_mode"
            )
    workspace_fixture_patch = manifest.get("workspace_fixture_patch")
    if workspace_fixture_patch is not None:
        _validate_workspace_fixture_patch(workspace_fixture_patch)
    if manifest["case_kind"] == "DEVELOPMENT_FIXTURE" and "workspace_path" not in manifest:
        raise ScenarioSourceError("DEVELOPMENT_FIXTURE requires workspace_path")
    if manifest["case_kind"] == "REPOSITORY_CASE":
        try:
            workspace = scenario_workspace(manifest)
        except ValueError as exc:
            raise ScenarioSourceError(str(exc)) from exc
        for repository in workspace.repositories:
            if (
                _COMMIT_PATTERN.fullmatch(repository.base_commit) is None
                or _COMMIT_PATTERN.fullmatch(repository.solution_commit) is None
                or _SHA256_PATTERN.fullmatch(repository.solution_patch_sha256) is None
            ):
                raise ScenarioSourceError("REPOSITORY_CASE repository interval hashes are invalid")


def _validate_gold(
    gold: dict[str, Any],
    *,
    source_directory: Path,
    allow_repository_evidence: bool,
) -> None:
    """Validate traceable business Gold without accepting workflow scoring policy."""

    if gold.get("schema_version") != SCENARIO_GOLD_SCHEMA_VERSION:
        raise ScenarioSourceError(f"gold schema_version must be {SCENARIO_GOLD_SCHEMA_VERSION}")
    forbidden = sorted(_RUN_OWNED_GOLD_FIELDS.intersection(gold))
    if forbidden:
        raise ScenarioSourceError("Gold contains workflow or scoring fields: " + ", ".join(forbidden))

    requirements = gold.get("requirements")
    evidence = gold.get("source_evidence")
    if not isinstance(requirements, list) or not requirements:
        raise ScenarioSourceError("gold requirements must be a non-empty list")
    if not isinstance(evidence, list) or not evidence:
        raise ScenarioSourceError("gold source_evidence must be a non-empty list")
    if "hidden_checks" in gold:
        raise ScenarioSourceError(
            "gold hidden_checks is retired; use FAIL_TO_PASS and PASS_TO_PASS execution_tests"
        )

    evidence_ids: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict):
            raise ScenarioSourceError("source_evidence entries must be objects")
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id or evidence_id in evidence_ids:
            raise ScenarioSourceError("source_evidence evidence_id values must be unique")
        evidence_ids.add(evidence_id)
        _validate_source_evidence(
            item,
            source_directory,
            allow_repository_evidence=allow_repository_evidence,
        )

    judge_ids: set[str] = set()
    execution_tests = gold.get("execution_tests", [])
    if not isinstance(execution_tests, list) or not execution_tests:
        raise ScenarioSourceError("gold execution_tests must declare F2P and P2P checks")
    execution_kinds: set[str] = set()
    for item in execution_tests:
        if not isinstance(item, dict):
            raise ScenarioSourceError("execution_tests entries must be objects")
        check_id = item.get("check_id")
        argv = item.get("argv")
        timeout = item.get("timeout_seconds")
        if (
            not isinstance(check_id, str)
            or not check_id
            or check_id in judge_ids
            or item.get("kind") not in {"FAIL_TO_PASS", "PASS_TO_PASS"}
            or not isinstance(argv, list)
            or not argv
            or any(not isinstance(argument, str) or not argument for argument in argv)
            or not isinstance(timeout, int)
            or timeout < 1
        ):
            raise ScenarioSourceError("execution_tests must declare unique IDs, F2P/P2P kind, argv, and timeout")
        judge_ids.add(check_id)
        execution_kinds.add(str(item["kind"]))

    if execution_kinds != {"FAIL_TO_PASS", "PASS_TO_PASS"}:
        raise ScenarioSourceError("gold execution_tests must include both F2P and P2P checks")

    requirement_ids: set[str] = set()
    for item in requirements:
        if not isinstance(item, dict):
            raise ScenarioSourceError("requirements entries must be objects")
        requirement_id = item.get("requirement_id")
        mapped_evidence = item.get("evidence_ids")
        mapped_judges = item.get("judge_ids")
        if not isinstance(requirement_id, str) or not requirement_id or requirement_id in requirement_ids:
            raise ScenarioSourceError("requirement_id values must be unique")
        requirement_ids.add(requirement_id)
        if not isinstance(mapped_evidence, list) or not mapped_evidence or not set(mapped_evidence) <= evidence_ids:
            raise ScenarioSourceError(f"{requirement_id} must map valid evidence_ids")
        if not isinstance(mapped_judges, list) or not mapped_judges or not set(mapped_judges) <= judge_ids:
            raise ScenarioSourceError(f"{requirement_id} must map valid judge_ids")


def _validate_source_evidence(
    item: dict[str, Any],
    source_directory: Path,
    *,
    allow_repository_evidence: bool,
) -> None:
    """Validate either source-package quotes or frozen repository evidence."""

    if "line_start" not in item:
        if not allow_repository_evidence:
            raise ScenarioSourceError("repository source_evidence is only valid for REPOSITORY_CASE")
        _validate_repository_evidence(item)
        return
    path_value = item.get("path")
    line_start = item.get("line_start")
    line_end = item.get("line_end")
    quote = item.get("quote")
    if (
        not isinstance(path_value, str)
        or not isinstance(line_start, int)
        or not isinstance(line_end, int)
        or line_start < 1
        or line_end < line_start
        or not isinstance(quote, str)
        or not quote
    ):
        raise ScenarioSourceError("source_evidence must declare path, line range, and quote")
    path = _safe_path(source_directory, path_value, "source_evidence.path")
    lines = _read_text(path, "source_evidence.path").splitlines()
    if line_end > len(lines) or quote not in "\n".join(lines[line_start - 1 : line_end]):
        raise ScenarioSourceError("source_evidence quote does not match the declared source range")


def _validate_repository_evidence(item: dict[str, Any]) -> None:
    """Validate a canonical provenance entry anchored to an immutable Commit."""

    path_value = item.get("path")
    relative = PurePosixPath(path_value) if isinstance(path_value, str) else None
    if (
        not isinstance(item.get("kind"), str)
        or not isinstance(item.get("commit"), str)
        or _COMMIT_PATTERN.fullmatch(str(item["commit"])) is None
        or relative is None
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != path_value
        or not isinstance(item.get("sha256"), str)
        or _SHA256_PATTERN.fullmatch(str(item["sha256"])) is None
        or not isinstance(item.get("description"), str)
        or not str(item["description"]).strip()
    ):
        raise ScenarioSourceError("repository source_evidence must declare kind, Commit, path, SHA-256, and description")


def _validate_clarification_policy(policy: dict[str, Any]) -> None:
    """Require closed business facts without embedding workflow choices."""

    if policy.get("schema_version") != "framework-eval-clarification-policy.v1":
        raise ScenarioSourceError("clarification policy schema is invalid")
    if policy.get("external_confirmation_required") is not False:
        raise ScenarioSourceError("Scenario business facts must not require external confirmation")
    if "workflow_choice_policy" in policy:
        raise ScenarioSourceError("workflow_choice_policy is owned by the Run Execution Strategy")
    facts = policy.get("facts")
    if not isinstance(facts, list) or not facts:
        raise ScenarioSourceError("clarification policy must freeze at least one business fact")


def _validate_repository_workspace_assets(
    manifest: dict[str, Any],
    gold: dict[str, Any],
    *,
    source_directory: Path,
) -> None:
    """Bind multi-repo tests, patches, and evidence to declared workspace members."""

    workspace = scenario_workspace(manifest)
    if not workspace.is_multi_repository:
        return
    if manifest.get("workspace_fixture_patch") is not None:
        raise ScenarioSourceError("MULTI_REPOSITORY does not support single-repo workspace_fixture_patch")
    workspace_path = manifest.get("workspace_path")
    if not isinstance(workspace_path, str) or not workspace_path:
        raise ScenarioSourceError("MULTI_REPOSITORY Case requires workspace_path")
    registry = _safe_path(
        source_directory,
        f"{workspace_path.rstrip('/')}/helixharness.workspace.yaml",
        "workspace_path",
    )
    if not registry.is_file() or registry.is_symlink():
        raise ScenarioSourceError("MULTI_REPOSITORY workspace requires helixharness.workspace.yaml")
    repository_ids = {item.repository_id for item in workspace.repositories}
    working_directories = {".", *(item.mount_path for item in workspace.repositories)}
    for test in [*gold.get("execution_tests", []), *gold.get("solution_checks", [])]:
        working_directory = test.get("working_directory", ".") if isinstance(test, dict) else None
        if working_directory not in working_directories:
            raise ScenarioSourceError("multi-repository test working_directory is not a workspace member")
    for evidence in gold.get("source_evidence", []):
        if not isinstance(evidence, dict) or "line_start" in evidence:
            continue
        if evidence.get("repository_id") not in repository_ids:
            raise ScenarioSourceError("multi-repository source evidence requires a declared repository_id")
    hidden_patches = manifest.get("hidden_test_patches")
    if not isinstance(hidden_patches, list) or not hidden_patches:
        raise ScenarioSourceError("MULTI_REPOSITORY Case requires hidden_test_patches")
    seen: set[str] = set()
    for item in hidden_patches:
        if (
            not isinstance(item, dict)
            or item.get("repository_id") not in repository_ids
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("sha256"), str)
            or _SHA256_PATTERN.fullmatch(str(item.get("sha256"))) is None
            or not isinstance(item.get("protected_paths"), list)
            or not item["protected_paths"]
        ):
            raise ScenarioSourceError("multi-repository hidden test patch contract is invalid")
        repository_id = str(item["repository_id"])
        if repository_id in seen:
            raise ScenarioSourceError("each repository may declare at most one hidden test patch")
        seen.add(repository_id)


def _validate_workspace_fixture_patch(contract: object) -> None:
    """Allow an explicit fixture to touch build/tooling configuration only."""

    if not isinstance(contract, dict) or set(contract) != {
        "path",
        "sha256",
        "protected_paths",
    }:
        raise ScenarioSourceError("workspace_fixture_patch contract is invalid")
    if (
        not isinstance(contract.get("path"), str)
        or not contract["path"]
        or not isinstance(contract.get("sha256"), str)
        or _SHA256_PATTERN.fullmatch(contract["sha256"]) is None
    ):
        raise ScenarioSourceError("workspace_fixture_patch path or Hash is invalid")
    protected_paths = contract.get("protected_paths")
    if (
        not isinstance(protected_paths, list)
        or not protected_paths
        or any(not isinstance(item, str) or not item for item in protected_paths)
        or len(set(protected_paths)) != len(protected_paths)
    ):
        raise ScenarioSourceError("workspace_fixture_patch protected_paths are invalid")
    for value in protected_paths:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != value
            or not (
                path.name in _WORKSPACE_FIXTURE_BUILD_FILES
                or path.parts[0] in {".mvn", "gradle"}
            )
        ):
            raise ScenarioSourceError(
                "workspace_fixture_patch may change build/tooling configuration only"
            )


def _build_snapshot(
    source_directory: Path,
    manifest: dict[str, Any],
    *,
    declared_paths: set[str],
) -> ScenarioSnapshot:
    """Freeze declared bytes with visibility metadata and a stable hash."""

    gold_path = str(manifest["gold_path"])
    clarification_path = str(manifest["clarification_policy_path"])
    provenance_path = str(manifest.get("provenance_path") or "")
    hidden_patch = manifest.get("hidden_test_patch")
    hidden_patch_path = str(hidden_patch.get("path") or "") if isinstance(hidden_patch, dict) else ""
    hidden_patch_paths = {
        str(item.get("path") or "")
        for item in manifest.get("hidden_test_patches", [])
        if isinstance(item, dict)
    }
    workspace_fixture_patch = manifest.get("workspace_fixture_patch")
    workspace_fixture_patch_path = (
        str(workspace_fixture_patch.get("path") or "")
        if isinstance(workspace_fixture_patch, dict)
        else ""
    )
    workspace = str(manifest.get("workspace_path") or "").rstrip("/")
    execution_prefix = f"{workspace}/" if workspace else ""
    hidden_paths = {
        gold_path,
        clarification_path,
        provenance_path,
        hidden_patch_path,
        *hidden_patch_paths,
        workspace_fixture_patch_path,
    } - {""}
    files: dict[str, dict[str, str]] = {}
    for relative_path in sorted(declared_paths):
        path = _safe_path(source_directory, relative_path, relative_path)
        if not path.is_file() or path.is_symlink():
            raise ScenarioSourceError(f"declared file is missing or unsafe: {relative_path}")
        content = path.read_bytes()
        files[relative_path] = {
            "content_base64": b64encode(content).decode("ascii"),
            "sha256": sha256(content).hexdigest(),
            "visibility": (
                "HIDDEN"
                if relative_path in hidden_paths
                else "EXECUTION"
                if execution_prefix and relative_path.startswith(execution_prefix)
                else "CONTROL"
            ),
        }
    payload = {
        "schema_version": "framework-eval-scenario-snapshot.v2",
        "case_id": manifest["case_id"],
        "files": files,
    }
    snapshot_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ScenarioSnapshot(
        source_hash=sha256(snapshot_json.encode("utf-8")).hexdigest(),
        snapshot_json=snapshot_json,
    )


def _declared_file(source_directory: Path, manifest: dict[str, Any], field: str) -> Path:
    """Resolve one required manifest file after traversal checks."""

    value = manifest.get(field)
    path = _safe_path(source_directory, value, field)
    if not path.is_file() or path.is_symlink():
        raise ScenarioSourceError(f"{field} must reference a real file")
    return path


def _safe_path(source_directory: Path, value: object, field: str) -> Path:
    """Resolve a POSIX relative path that remains inside the source root."""

    if not isinstance(value, str) or not value:
        raise ScenarioSourceError(f"{field} must be a non-empty relative path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != value:
        raise ScenarioSourceError(f"{field} must be a safe POSIX relative path")
    path = (source_directory / Path(*relative.parts)).resolve()
    if source_directory not in path.parents:
        raise ScenarioSourceError(f"{field} escapes the Scenario Source")
    return path


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    """Read one UTF-8 JSON object with a source-contract error."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScenarioSourceError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ScenarioSourceError(f"{label} must contain a JSON object")
    return value


def _read_text(path: Path, label: str) -> str:
    """Read required UTF-8 text with a source-contract error."""

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ScenarioSourceError(f"{label} is not readable UTF-8 text: {exc}") from exc
