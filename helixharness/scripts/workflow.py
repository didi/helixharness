#!/usr/bin/env python3
"""Deterministic guards for Lite spec, task, evidence, and archive artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from source_binding import guard_source
from typing import Any, Iterator

try:
    from atomic_files import AtomicWriteError, atomic_write_text
    from git_target import git as git_text, operation_state, resolve_target_branch
except ImportError:  # pragma: no cover - package-style imports in external callers
    from scripts.atomic_files import AtomicWriteError, atomic_write_text
    from scripts.git_target import git as git_text, operation_state, resolve_target_branch


TASK_ID = re.compile(r"\bT-[0-9]+\b")
REPAIR_ID = re.compile(r"R-[0-9]+")
REPAIR_KINDS = {"implementation_regression", "contract_amendment"}
TASK_STATUSES = {"pending", "in_progress", "blocked", "completed"}
TASK_DESCRIPTION_COLUMNS = ("task", "vertical slice")
CRITICAL_FRONTMATTER_FIELDS = {
    "feature",
    "review_fixed_point",
    "review_head",
    "review_mode",
    "review_scope_sha256",
    "requirement_sha256",
    "selection_state",
    "spec_receipt_sha256",
    "tasks_receipt_sha256",
    "execution_scope",
    "workspace_repo",
    "workspace_contract_sha256",
    "status",
    "standards_verdict",
    "spec_verdict",
    "testing_seam_confirmed",
    "must_fix_rounds",
    "repair_revision",
    "archive_review_decision",
    "archive_review_acceptance_sha256",
}

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class WorkflowError(ValueError):
    """Raised when a Lite artifact violates its public contract."""


class ReviewScopeDrift(WorkflowError):
    """Valid completion evidence whose current content needs an Archive decision."""

    def __init__(self, choice: dict[str, Any], *, finalizing: bool) -> None:
        super().__init__("verify-report.md review scope drift")
        self.choice = choice
        self.finalizing = finalizing


def repo_root(value: str) -> Path:
    candidate = Path(value).expanduser().resolve()
    if not candidate.is_dir():
        raise WorkflowError("repo root is not a directory")
    return candidate


def feature_name(value: str) -> str:
    windows_forbidden = '<>:"|?*'
    reserved_stem = value.split(".", 1)[0].casefold()
    windows_reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if (
        not value
        or value != value.strip()
        or value.endswith(".")
        or value in {".", ".."}
        or value.casefold() == ".git"
        or "/" in value
        or "\\" in value
        or any(character in value for character in windows_forbidden)
        or reserved_stem in windows_reserved
        or any(ord(character) <= 0x1F or ord(character) == 0x7F for character in value)
        or len(value.encode("utf-8")) > 255
    ):
        raise WorkflowError("invalid feature")
    return value


def feature_dir(repo: Path, feature: str) -> Path:
    path = repo / "docs" / "specs" / "active" / feature_name(feature)
    if path.is_symlink():
        raise WorkflowError("active feature directory is unsafe")
    return path


def read_regular(path: Path, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise WorkflowError(f"{label} is missing or unsafe")
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkflowError(f"cannot read {label} as UTF-8") from exc
    # Line endings are presentation details, not workflow facts. Normalize
    # them before parsing so an editor or host OS cannot invalidate an
    # otherwise equivalent Lite artifact.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def frontmatter(text: str, label: str) -> tuple[dict[str, str], list[str], int]:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise WorkflowError(f"{label} frontmatter is missing")
    end = next(
        (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if end is None:
        raise WorkflowError(f"{label} frontmatter is unterminated")

    values: dict[str, str] = {}
    for line in lines[1:end]:
        raw = line.rstrip("\r\n")
        if not raw or raw[0].isspace() or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        if key in values and key in CRITICAL_FRONTMATTER_FIELDS:
            raise WorkflowError(f"{label} has duplicate {key}")
        values[key] = frontmatter_scalar(value)
    return values, lines, end


def frontmatter_scalar(value: str) -> str:
    value = value.strip().split(" #", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def artifact_body(text: str, label: str) -> str:
    _, lines, end = frontmatter(text, label)
    return "".join(lines[end + 1 :])


def artifact_receipt(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def spec_receipt(text: str) -> str:
    values, lines, end = frontmatter(text, "spec.md")
    values.pop("status", None)
    canonical = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    canonical += "\n" + "".join(lines[end + 1 :])
    return artifact_receipt(canonical)


def implementation_contract_receipt(text: str) -> str:
    """Hash the Spec contract while excluding repair bookkeeping."""
    values, lines, end = frontmatter(text, "spec.md")
    values.pop("status", None)
    values.pop("repair_revision", None)
    body = "".join(lines[end + 1 :])
    headings = list(
        re.finditer(r"(?m)^##[ \t]+Repair Revisions[ \t]*\n", body)
    )
    if len(headings) > 1:
        raise WorkflowError("spec.md has duplicate Repair Revisions sections")
    if headings:
        start = headings[0].start()
        following = re.search(r"(?m)^##[ \t]+", body[headings[0].end() :])
        section_end = (
            headings[0].end() + following.start() if following is not None else len(body)
        )
        body = body[:start] + body[section_end:]
    canonical = json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return artifact_receipt(canonical + "\n" + body.rstrip() + "\n")


def _git_bytes(repo: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WorkflowError("Git review scope is unavailable") from exc


def _review_scope_excluded(feature: str, relative: str) -> bool:
    lifecycle_paths = {
        f"docs/specs/{state}/{feature}/{artifact}"
        for state in ("active", "completed")
        for artifact in ("spec.md", "tasks.md", "verify-report.md")
    }
    return (
        relative in lifecycle_paths
        or relative == ".helixharness"
        or relative.startswith(".helixharness/")
        or relative == "docs/eval"
        or relative.startswith("docs/eval/")
        or relative == "d-d2c"
        or relative.startswith("d-d2c/")
    )


def _working_tree_entries(
    repo: Path,
    feature: str,
    extra_paths: set[str] | None = None,
    *,
    base_ref: str = "HEAD",
) -> list[dict[str, Any]]:
    changed = set()
    for raw in (
        _git_bytes(repo, "diff", "--name-only", "-z", base_ref, "--"),
        _git_bytes(repo, "ls-files", "--others", "--exclude-standard", "-z"),
    ):
        changed.update(os.fsdecode(item) for item in raw.split(b"\0") if item)
    changed.update(extra_paths or set())

    entries: list[dict[str, Any]] = []
    for relative in sorted(changed):
        parsed = PurePosixPath(relative)
        if (
            not relative
            or parsed.is_absolute()
            or ".." in parsed.parts
            or "\\" in relative
        ):
            raise WorkflowError("Git review path is unsafe")
        if _review_scope_excluded(feature, relative):
            continue
        path = repo / relative
        try:
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                kind = "symlink"
                payload = os.fsencode(os.readlink(path))
            elif path.is_file():
                kind = "file"
                payload = path.read_bytes()
            elif path.is_dir():
                raise WorkflowError(
                    f"Git review scope contains nested repository or directory {relative}"
                )
            else:
                kind = "other"
                payload = b""
        except FileNotFoundError:
            kind = "missing"
            mode = 0
            payload = b""
        except OSError as exc:
            raise WorkflowError(f"cannot read review path {relative}") from exc
        entries.append(
            {
                "kind": kind,
                "mode": mode,
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return entries


def _canonical_manifest(manifest: list[dict[str, Any]]) -> str:
    return json.dumps(
        manifest,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_manifest(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(
        isinstance(item, dict)
        and set(item) == {"kind", "mode", "path", "sha256"}
        and isinstance(item.get("path"), str)
        and isinstance(item.get("kind"), str)
        and isinstance(item.get("mode"), int)
        and re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", "")))
        and not PurePosixPath(str(item["path"])).is_absolute()
        and ".." not in PurePosixPath(str(item["path"])).parts
        and "\\" not in str(item["path"])
        for item in value
    ):
        raise WorkflowError(f"{label} has invalid manifest")
    paths = [str(item["path"]) for item in value]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise WorkflowError(f"{label} has invalid manifest paths")
    return value


def _state_path(
    repo: Path, feature: str, filename: str, *, create_parent: bool = False
) -> Path:
    current = repo
    for part in (".helixharness", "workflow", feature_name(feature)):
        current /= part
        if os.path.lexists(current):
            if current.is_symlink() or not current.is_dir():
                raise WorkflowError("Lite private state directory is unsafe")
        elif create_parent:
            try:
                current.mkdir(mode=0o700)
            except OSError as exc:
                raise WorkflowError("cannot create Lite private state") from exc
    path = current / filename
    if os.path.lexists(path) and path.is_symlink():
        raise WorkflowError("Lite private state file is unsafe")
    return path


def _read_json_state(repo: Path, feature: str, filename: str, label: str) -> dict[str, Any]:
    path = _state_path(repo, feature, filename)
    try:
        value = json.loads(read_regular(path, label))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{label} is invalid")
    return value


def _ensure_private_state_ignored(repo: Path) -> None:
    relative_probe = ".helixharness/workflow/.probe"
    try:
        ignored = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", "--no-index", relative_probe],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise WorkflowError("cannot inspect Git exclude rules") from exc
    if ignored.returncode == 0:
        return
    raw_path = _git_bytes(repo, "rev-parse", "--git-path", "info/exclude").strip()
    exclude_path = Path(os.fsdecode(raw_path))
    if not exclude_path.is_absolute():
        exclude_path = repo / exclude_path
    if exclude_path.is_symlink() or not exclude_path.is_file():
        raise WorkflowError("Git exclude file is missing or unsafe")
    try:
        current = exclude_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise WorkflowError("cannot read Git exclude file") from exc
    rule = "/.helixharness/workflow/"
    if rule in {line.strip() for line in current.splitlines()}:
        return
    separator = "" if not current or current.endswith(("\n", "\r")) else "\n"
    updated = (
        current
        + separator
        + "# HelixHarness Lite workflow private state: never commit\n"
        + rule
        + "\n"
    )
    try:
        atomic_write_text(
            exclude_path,
            updated,
            mode=stat.S_IMODE(exclude_path.stat().st_mode),
        )
    except (AtomicWriteError, OSError) as exc:
        raise WorkflowError("cannot update Git exclude file") from exc


def _write_json_state(
    repo: Path, feature: str, filename: str, value: dict[str, Any], label: str
) -> None:
    _ensure_private_state_ignored(repo)
    path = _state_path(repo, feature, filename, create_parent=True)
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(path, text, mode=0o600)
    except AtomicWriteError as exc:
        raise WorkflowError(f"cannot write {label}") from exc


def _apply_state(repo: Path, feature: str) -> dict[str, Any]:
    label = "Apply state"
    state = _read_json_state(repo, feature, "apply-state.json", label)
    if state.get("schema_version") != 1 or state.get("feature") != feature:
        raise WorkflowError("Apply state has invalid identity")
    for key in ("apply_spec_receipt_sha256", "apply_baseline_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(state.get(key, ""))):
            raise WorkflowError(f"Apply state has invalid {key}")
    if not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", str(state.get("apply_baseline_head", ""))
    ):
        raise WorkflowError("Apply state has invalid apply_baseline_head")
    manifest = _validate_manifest(state.get("apply_baseline_manifest"), label)
    if state["apply_baseline_sha256"] != artifact_receipt(
        _canonical_manifest(manifest)
    ):
        raise WorkflowError("Apply state has invalid apply_baseline_sha256")
    return state


@contextmanager
def _repair_lock(repo: Path, feature: str) -> Iterator[None]:
    path = _state_path(repo, feature, "repair.lock", create_parent=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + 5.0
        if os.name == "nt":
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise WorkflowError("bug fix stage is already in progress") from exc
                    time.sleep(0.01)
        else:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise WorkflowError("bug fix stage is already in progress") from exc
                    time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sha256(value: str, label: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise WorkflowError(
            f"{label} must be a bare 64-character lowercase SHA-256 "
            "without Markdown formatting"
        )
    return value


def _sha256_or_absent(value: str, label: str) -> str:
    if value == "absent":
        return value
    return _sha256(value, label)


def _replace_frontmatter_value(text: str, label: str, key: str, value: str) -> str:
    _, lines, end = frontmatter(text, label)
    matches = [
        index
        for index, line in enumerate(lines[1:end], 1)
        if line and not line[0].isspace() and line.split(":", 1)[0].strip() == key
    ]
    if len(matches) != 1:
        raise WorkflowError(f"{label} has invalid {key}")
    index = matches[0]
    ending = (
        "\r\n"
        if lines[index].endswith("\r\n")
        else "\r"
        if lines[index].endswith("\r")
        else "\n"
    )
    lines[index] = f"{key}: {value}{ending}"
    return "".join(lines)


def _repair_preflight(repo: Path, feature: str) -> dict[str, Any]:
    feature = feature_name(feature)
    completed = repo / "docs" / "specs" / "completed" / feature
    if os.path.lexists(completed):
        raise WorkflowError("feature is already archived")
    directory = feature_dir(repo, feature)
    spec_text = read_regular(directory / "spec.md", "spec.md")
    spec = spec_contract(spec_text)
    if spec.get("feature") != feature:
        raise WorkflowError("spec.md feature does not match its directory")
    if spec.get("status") == "completed":
        raise WorkflowError("bug fix cannot reopen archive finalizing state")
    if spec.get("status") not in {"planning", "ready", "applying", "implemented"}:
        raise WorkflowError("spec.md status is invalid for bug fix")
    tasks_path = directory / "tasks.md"
    tasks_text = read_regular(tasks_path, "tasks.md") if tasks_path.is_file() else None
    if tasks_text is not None:
        tasks, _, _ = frontmatter(tasks_text, "tasks.md")
        if tasks.get("feature") != feature:
            raise WorkflowError("tasks.md identity is invalid for bug fix")
        if any(row["status"] == "in_progress" for row in task_rows(tasks_text)):
            raise WorkflowError("bug fix stage is already in progress")
        if any(row["status"] == "completed" for row in task_rows(tasks_text)) and not os.path.lexists(
            _state_path(repo, feature, "apply-state.json")
        ):
            raise WorkflowError("completed tasks require the original Apply baseline")
    git_state = operation_state(repo)
    if git_state["operations"] or git_state["unmerged_paths"]:
        raise WorkflowError("Git operation is in progress")
    revision = spec.get("repair_revision", "0")
    if not re.fullmatch(r"0|[1-9][0-9]*", revision):
        raise WorkflowError("spec.md has invalid repair_revision")
    return {
        "feature": feature,
        "repair_revision": int(revision),
        "spec_receipt_sha256": spec_receipt(spec_text),
        "status": spec["status"],
        "tasks_receipt_sha256": artifact_receipt(tasks_text) if tasks_text is not None else None,
    }


def _workspace_binding_context(value: Any, source_sha256: str) -> dict[str, str] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"repo", "sha256"}
        or not isinstance(value.get("repo"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value["repo"])
        or value.get("sha256") != source_sha256
    ):
        raise WorkflowError("Workspace binding context is invalid")
    return value


def _validate_workspace_binding(state: dict[str, Any], spec: dict[str, str]) -> None:
    context = _workspace_binding_context(state.get("workspace_contract"), state["source_sha256"])
    if context is not None and (
        spec.get("execution_scope") != "single"
        or spec.get("workspace_repo") != context["repo"]
        or spec.get("workspace_contract_sha256") != context["sha256"]
    ):
        raise WorkflowError("Workspace contract binding is stale or invalid")


def _repair_state(repo: Path, feature: str) -> dict[str, Any] | None:
    path = _state_path(repo, feature, "repair-state.json")
    if not path.is_file():
        return None
    state = _read_json_state(repo, feature, "repair-state.json", "Repair state")
    if state.get("schema_version") != 1 or state.get("feature") != feature:
        raise WorkflowError("Repair state has invalid identity")
    if state.get("status") not in {"in_progress", "bound"}:
        raise WorkflowError("Repair state has invalid status")
    _sha256(str(state.get("repair_key", "")), "Repair state repair_key")
    _sha256(str(state.get("source_sha256", "")), "Repair state source_sha256")
    _sha256(
        str(state.get("expected_spec_receipt_sha256", "")),
        "Repair state expected_spec_receipt_sha256",
    )
    _sha256(
        str(state.get("previous_spec_contract_sha256", "")),
        "Repair state previous_spec_contract_sha256",
    )
    _sha256_or_absent(
        str(state.get("expected_tasks_receipt_sha256", "")),
        "Repair state expected_tasks_receipt_sha256",
    )
    if state.get("repair_kind") not in REPAIR_KINDS:
        raise WorkflowError("Repair state has invalid repair_kind")
    if not isinstance(state.get("repair_revision"), int) or state["repair_revision"] < 1:
        raise WorkflowError("Repair state has invalid repair_revision")
    if state.get("source_spec_status") not in {
        "planning",
        "ready",
        "applying",
        "implemented",
    }:
        raise WorkflowError("Repair state has invalid source_spec_status")
    if not isinstance(state.get("previous_repair_rows"), list) or not isinstance(
        state.get("previous_tasks"), list
    ):
        raise WorkflowError("Repair state has invalid artifact snapshot")
    _workspace_binding_context(state.get("workspace_contract"), state["source_sha256"])
    if "workspace_binding_reopened" in state and state["workspace_binding_reopened"] is not True:
        raise WorkflowError("Workspace binding reopen marker is invalid")
    return state


def begin_repair(
    repo: Path,
    feature: str,
    *,
    repair_key: str,
    source_sha256: str,
    repair_kind: str,
    expected_spec_receipt: str,
    expected_tasks_receipt: str,
    workspace_contract: dict[str, str] | None = None,
) -> dict[str, Any]:
    repair_key = _sha256(repair_key, "repair key")
    source_sha256 = _sha256(source_sha256, "repair source receipt")
    expected_spec_receipt = _sha256(expected_spec_receipt, "expected spec receipt")
    expected_tasks_receipt = _sha256_or_absent(
        expected_tasks_receipt, "expected tasks receipt"
    )
    if repair_kind not in REPAIR_KINDS:
        raise WorkflowError("repair kind is invalid")
    workspace_contract = _workspace_binding_context(workspace_contract, source_sha256)
    if workspace_contract is not None and repair_kind != "contract_amendment":
        raise WorkflowError("Workspace binding requires a contract amendment")
    with _repair_lock(repo, feature):
        facts = _repair_preflight(repo, feature)
        state = _repair_state(repo, feature)
        if state is not None and state["repair_key"] == repair_key:
            if workspace_contract is not None:
                if state["source_sha256"] != source_sha256 or state.get("workspace_contract") not in (None, workspace_contract):
                    raise WorkflowError("Workspace binding context changed")
                directory = feature_dir(repo, feature)
                spec = spec_contract(read_regular(directory / "spec.md", "spec.md"))
                if state["status"] == "bound" and spec.get("workspace_contract_sha256") != source_sha256:
                    # Only reopen an unchanged binding before any revision task ran.
                    if (
                        facts["spec_receipt_sha256"] != state.get("bound_spec_receipt_sha256")
                        or facts["tasks_receipt_sha256"] != state.get("bound_tasks_receipt_sha256")
                        or facts["status"] != state.get("target_status")
                    ):
                        raise WorkflowError("Workspace binding artifacts changed after rebind")
                    rows = task_rows(read_regular(directory / "tasks.md", "tasks.md"))
                    revision_rows = [row for row in rows if row["revision"] == f"R-{state['repair_revision']:02d}"]
                    if not revision_rows or any(row["status"] != "pending" for row in revision_rows):
                        raise WorkflowError("Workspace binding revision has already executed")
                    apply_path = _state_path(repo, feature, "apply-state.json")
                    if os.path.lexists(_state_path(repo, feature, "review-state.json")) or (
                        state["target_status"] == "ready" and os.path.lexists(apply_path)
                    ):
                        raise WorkflowError("Workspace binding revision has already executed")
                    if state["target_status"] == "applying":
                        baseline = _apply_state(repo, feature)
                        if (
                            baseline["apply_baseline_head"] != state["apply_baseline_head"]
                            or baseline["apply_spec_receipt_sha256"] != facts["spec_receipt_sha256"]
                        ):
                            raise WorkflowError("Workspace binding Apply baseline changed")
                    state.update(status="in_progress", workspace_binding_reopened=True)
                state["workspace_contract"] = workspace_contract
                _write_json_state(repo, feature, "repair-state.json", state, "Repair state")
            return {
                "feature": feature,
                "repair_revision": state["repair_revision"],
                "resumed": True,
                "status": state["status"],
                "binding_reopened": state.get("workspace_binding_reopened", False),
            }
        if state is not None and state["status"] == "in_progress":
            raise WorkflowError("bug fix stage is already in progress")
        if state is not None and state["status"] == "bound":
            current_tasks = task_rows(
                read_regular(feature_dir(repo, feature) / "tasks.md", "tasks.md")
            )
            current_revision = f"R-{state['repair_revision']:02d}"
            unfinished = any(
                row["revision"] == current_revision and row["status"] != "completed"
                for row in current_tasks
            )
            if facts["status"] != "implemented" or unfinished:
                raise WorkflowError("bug fix stage is already in progress")
        if facts["spec_receipt_sha256"] != expected_spec_receipt:
            raise WorkflowError("spec.md changed before begin-repair")
        actual_tasks_receipt = facts["tasks_receipt_sha256"] or "absent"
        if actual_tasks_receipt != expected_tasks_receipt:
            raise WorkflowError("tasks.md changed before begin-repair")
        apply_state = (
            _apply_state(repo, feature)
            if facts["status"] in {"applying", "implemented"} or os.path.lexists(
                _state_path(repo, feature, "apply-state.json")
            )
            else None
        )
        spec_text = read_regular(feature_dir(repo, feature) / "spec.md", "spec.md")
        previous_revisions = (
            repair_rows(spec_text) if facts["repair_revision"] > 0 else []
        )
        expected_revisions = [
            f"R-{number:02d}" for number in range(1, facts["repair_revision"] + 1)
        ]
        if [row["revision"] for row in previous_revisions] != expected_revisions:
            raise WorkflowError("spec.md repair revisions are not contiguous")
        tasks_path = feature_dir(repo, feature) / "tasks.md"
        previous_tasks = (
            task_rows(read_regular(tasks_path, "tasks.md"))
            if facts["tasks_receipt_sha256"] is not None
            else []
        )
        revision = int(facts["repair_revision"]) + 1
        _write_json_state(
            repo,
            feature,
            "repair-state.json",
            {
                "apply_baseline_head": (
                    apply_state["apply_baseline_head"] if apply_state is not None else None
                ),
                "expected_spec_receipt_sha256": expected_spec_receipt,
                "expected_tasks_receipt_sha256": expected_tasks_receipt,
                "feature": feature,
                "previous_repair_rows": previous_revisions,
                "previous_spec_contract_sha256": implementation_contract_receipt(
                    spec_text
                ),
                "previous_tasks": previous_tasks,
                "repair_key": repair_key,
                "repair_kind": repair_kind,
                "repair_revision": revision,
                "schema_version": 1,
                "source_sha256": source_sha256,
                "source_spec_status": facts["status"],
                "status": "in_progress",
                "target_status": (
                    "applying" if apply_state is not None else "ready"
                ),
                **({"workspace_contract": workspace_contract} if workspace_contract is not None else {}),
            },
            "Repair state",
        )
        return {
            "feature": feature,
            "repair_revision": revision,
            "resumed": False,
            "status": "in_progress",
        }


def _validate_repair_artifacts(
    state: dict[str, Any], spec_text: str, tasks_text: str
) -> None:
    """Validate the revision and preserved history before checking or binding it."""
    spec = spec_contract(spec_text)
    _validate_workspace_binding(state, spec)
    tasks, _, _ = frontmatter(tasks_text, "tasks.md")
    for label, values in (("spec.md", spec), ("tasks.md", tasks)):
        if values.get("feature") != state["feature"]:
            raise WorkflowError(f"{label} feature does not match Repair state")
    if tasks.get("status") not in {"planning", "ready", "applying", "completed"}:
        raise WorkflowError("tasks.md status is invalid for repair")
    allowed_spec_statuses = {state["source_spec_status"]}
    # Older Propose runs may already have reopened the contract for editing.
    if state["repair_kind"] == "contract_amendment":
        allowed_spec_statuses.update({"planning", "ready"})
    if state.get("workspace_binding_reopened"):
        allowed_spec_statuses.add(state["target_status"])
    if spec.get("status") not in allowed_spec_statuses:
        raise WorkflowError("spec.md status changed during repair editing")
    if spec.get("repair_revision") != str(state["repair_revision"]):
        raise WorkflowError("spec.md has invalid repair_revision")
    identifier = f"R-{state['repair_revision']:02d}"
    revisions = repair_rows(spec_text)
    expected_revisions = [
        f"R-{number:02d}" for number in range(1, state["repair_revision"] + 1)
    ]
    if [row["revision"] for row in revisions] != expected_revisions:
        raise WorkflowError("spec.md repair revisions are not contiguous")
    if revisions[:-1] != state["previous_repair_rows"]:
        raise WorkflowError("spec.md changed a prior repair revision")
    matching = [row for row in revisions if row["revision"] == identifier]
    if len(matching) != 1:
        raise WorkflowError("spec.md is missing the current repair revision")
    row = matching[0]
    if (
        row["repair key"] != state["repair_key"]
        or row["kind"] != state["repair_kind"]
        or row["source sha-256"] != state["source_sha256"]
    ):
        raise WorkflowError("spec.md current repair revision does not match Repair state")
    if (
        state["repair_kind"] == "implementation_regression"
        and implementation_contract_receipt(spec_text)
        != state["previous_spec_contract_sha256"]
    ):
        raise WorkflowError(
            "spec.md changed the original contract for implementation regression"
        )

    previous = {row["id"]: row for row in state["previous_tasks"]}
    current = task_rows(tasks_text)
    for task_id, old in previous.items():
        now = next((item for item in current if item["id"] == task_id), None)
        if now is None or any(
            now[key] != old[key]
            for key in ("description", "blocked_by", "status")
        ) or now["revision"] != (old["revision"] or "R-00"):
            raise WorkflowError("tasks.md changed a task from before this repair")
    old_max = max(
        (int(task_id.split("-", 1)[1]) for task_id in previous), default=0
    )
    added = [item for item in current if item["id"] not in previous]
    if not added:
        raise WorkflowError("tasks.md has no task for the current repair")
    if any(
        int(item["id"].split("-", 1)[1]) <= old_max
        or item["revision"] != identifier
        or item["status"] != "pending"
        for item in added
    ):
        raise WorkflowError("tasks.md has invalid tasks for the current repair")
    validate_task_graph(current)


def rebind_repair(
    repo: Path,
    feature: str,
    *,
    repair_key: str,
    expected_spec_receipt: str,
    expected_tasks_receipt: str,
) -> dict[str, Any]:
    repair_key = _sha256(repair_key, "repair key")
    expected_spec_receipt = _sha256(expected_spec_receipt, "expected spec receipt")
    expected_tasks_receipt = _sha256(expected_tasks_receipt, "expected tasks receipt")
    with _repair_lock(repo, feature):
        state = _repair_state(repo, feature)
        if state is None or state["repair_key"] != repair_key:
            raise WorkflowError("Repair state does not match this bug fix")
        if state["status"] == "bound":
            _validate_workspace_binding(
                state, spec_contract(read_regular(feature_dir(repo, feature) / "spec.md", "spec.md"))
            )
            return {
                "feature": feature,
                "repair_revision": state["repair_revision"],
                "status": state.get("target_status", "applying"),
            }
        directory = feature_dir(repo, feature)
        spec_text = read_regular(directory / "spec.md", "spec.md")
        tasks_text = read_regular(directory / "tasks.md", "tasks.md")
        if spec_receipt(spec_text) != expected_spec_receipt:
            raise WorkflowError("spec.md changed before repair-rebind")
        if artifact_receipt(tasks_text) != expected_tasks_receipt:
            raise WorkflowError("tasks.md changed before repair-rebind")
        _validate_repair_artifacts(state, spec_text, tasks_text)

        target_status = state.get("target_status", "applying")
        if target_status not in {"ready", "applying"}:
            raise WorkflowError("Repair state has invalid target_status")
        updated_spec = _replace_frontmatter_value(
            spec_text, "spec.md", "status", target_status
        )
        updated_tasks = _replace_frontmatter_value(
            tasks_text, "tasks.md", "status", target_status
        )
        try:
            atomic_write_text(directory / "spec.md", updated_spec)
            atomic_write_text(directory / "tasks.md", updated_tasks)
        except AtomicWriteError as exc:
            raise WorkflowError("cannot rebind repair artifacts") from exc
        if target_status == "applying":
            apply_state = _apply_state(repo, feature)
            apply_state["apply_spec_receipt_sha256"] = spec_receipt(updated_spec)
            _write_json_state(repo, feature, "apply-state.json", apply_state, "Apply state")
        review_path = _state_path(repo, feature, "review-state.json")
        if review_path.is_file():
            invalidated = _state_path(
                repo, feature, f"review-state.r{state['repair_revision']}.invalidated.json"
            )
            if os.path.lexists(invalidated):
                raise WorkflowError("invalidated Review state already exists")
            try:
                os.replace(review_path, invalidated)
            except OSError as exc:
                raise WorkflowError("cannot invalidate Review state") from exc
        state.update(
            {
                "bound_spec_receipt_sha256": spec_receipt(updated_spec),
                "bound_tasks_receipt_sha256": artifact_receipt(updated_tasks),
                "status": "bound",
            }
        )
        _write_json_state(repo, feature, "repair-state.json", state, "Repair state")
        return {
            "feature": feature,
            "repair_revision": state["repair_revision"],
            "status": target_status,
        }


def _review_state(repo: Path, feature: str) -> dict[str, Any]:
    label = "Review state"
    state = _read_json_state(repo, feature, "review-state.json", label)
    if state.get("schema_version") != 1 or state.get("feature") != feature:
        raise WorkflowError("Review state has invalid identity")
    review_head = str(state.get("review_head", ""))
    scope_sha = str(state.get("review_scope_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", review_head):
        raise WorkflowError("Review state has invalid review_head")
    if not re.fullmatch(r"[0-9a-f]{64}", scope_sha):
        raise WorkflowError("Review state has invalid review_scope_sha256")
    entries = _validate_manifest(state.get("entries"), label)
    canonical = json.dumps(
        {"entries": entries, "review_head": review_head},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    if scope_sha != artifact_receipt(canonical):
        raise WorkflowError("Review state has invalid review_scope_sha256")
    return state


def _review_scope_details(
    repo: Path, feature: str, *, review_head: str | None = None,
    authorized_knowledge_paths: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    feature = feature_name(feature)
    review_head = review_head or _git_bytes(repo, "rev-parse", "HEAD").decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", review_head):
        raise WorkflowError("Git review HEAD is invalid")
    apply_state = _apply_state(repo, feature)
    baseline = apply_state["apply_baseline_manifest"]
    baseline_by_path = {str(item["path"]): item for item in baseline}
    current = _working_tree_entries(
        repo,
        feature,
        set(baseline_by_path),
        base_ref=review_head,
    )
    overlapping = [
        str(entry["path"])
        for entry in current
        if entry["path"] in baseline_by_path
        and baseline_by_path[entry["path"]] != entry
        and entry["path"] not in authorized_knowledge_paths
    ]
    if overlapping:
        display_paths = ", ".join(
            path.encode("utf-8", "backslashreplace").decode("utf-8")
            for path in overlapping
        )
        raise WorkflowError(
            "DECISION_NEEDED: Apply delta overlaps the dirty Apply baseline at paths: "
            f"{display_paths}"
        )
    excluded = [
        entry["path"]
        for entry in current
        if baseline_by_path.get(entry["path"]) == entry
    ]
    entries = [
        entry
        for entry in current
        if baseline_by_path.get(entry["path"]) != entry
    ]
    canonical = json.dumps(
        {"entries": entries, "review_head": review_head},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "changed_paths": [
            str(entry["path"]).encode("utf-8", "backslashreplace").decode("utf-8")
            for entry in entries
        ],
        "entries": entries,
        "excluded_preexisting_paths": [
            str(path).encode("utf-8", "backslashreplace").decode("utf-8")
            for path in excluded
        ],
        "review_head": review_head,
        "review_scope_sha256": artifact_receipt(canonical),
    }


def review_scope(
    repo: Path, feature: str, *, review_head: str | None = None
) -> dict[str, Any]:
    """Freeze the observable Git/code state reviewed by both Lite axes."""
    details = _review_scope_details(repo, feature, review_head=review_head)
    return {key: value for key, value in details.items() if key != "entries"}


def seal_review_scope(repo: Path, feature: str) -> dict[str, Any]:
    details = _review_scope_details(repo, feature)
    _write_json_state(
        repo,
        feature,
        "review-state.json",
        {
            "entries": details["entries"],
            "feature": feature,
            "review_head": details["review_head"],
            "review_scope_sha256": details["review_scope_sha256"],
            "schema_version": 1,
        },
        "Review state",
    )
    return {key: value for key, value in details.items() if key != "entries"}


def _bind_apply_receipt(repo: Path, feature: str) -> dict[str, str]:
    validate_tasks(repo, feature, "apply")
    directory = feature_dir(repo, feature)
    spec_text = read_regular(directory / "spec.md", "spec.md")
    spec = spec_contract(spec_text)
    if spec.get("status") != "ready":
        raise WorkflowError("spec.md status is invalid for bind-apply")
    tasks_text = read_regular(directory / "tasks.md", "tasks.md")
    tasks, _, _ = frontmatter(tasks_text, "tasks.md")
    if tasks.get("status") != "ready":
        raise WorkflowError("tasks.md status is invalid for bind-apply")
    receipt = spec_receipt(spec_text)
    state_path = _state_path(repo, feature, "apply-state.json")
    if state_path.is_file():
        state = _apply_state(repo, feature)
        if state["apply_spec_receipt_sha256"] != receipt:
            raise WorkflowError("Apply state has invalid apply_spec_receipt_sha256")
        baseline_head = state["apply_baseline_head"]
        baseline_sha = state["apply_baseline_sha256"]
    else:
        baseline_head = _git_bytes(repo, "rev-parse", "HEAD").decode("ascii").strip()
        baseline = _working_tree_entries(repo, feature)
        baseline_sha = artifact_receipt(_canonical_manifest(baseline))
        _write_json_state(
            repo,
            feature,
            "apply-state.json",
            {
                "apply_baseline_head": baseline_head,
                "apply_baseline_manifest": baseline,
                "apply_baseline_sha256": baseline_sha,
                "apply_spec_receipt_sha256": receipt,
                "feature": feature,
                "schema_version": 1,
            },
            "Apply state",
        )
    return {
        "apply_baseline_head": str(baseline_head),
        "apply_baseline_sha256": str(baseline_sha),
        "apply_spec_receipt_sha256": receipt,
        "feature": feature,
    }


def spec_contract(text: str) -> dict[str, str]:
    values, lines, end = frontmatter(text, "spec.md")
    if "selection_state" in values or "requirement_sha256" in values:
        raise WorkflowError("spec.md is still a selection receipt")
    if not values.get("feature"):
        raise WorkflowError("spec.md has empty feature")
    if values.get("status") not in {
        "planning",
        "ready",
        "applying",
        "implemented",
        "completed",
    }:
        raise WorkflowError("spec.md has invalid status")
    if values.get("status") != "planning" and values.get("testing_seam_confirmed") != "true":
        raise WorkflowError("spec.md must declare testing_seam_confirmed: true")

    body = "".join(lines[end + 1 :])
    if not body.strip():
        raise WorkflowError("spec.md body is empty")
    return values


def markdown_cells(line: str) -> list[str]:
    cells: list[str] = []
    current: list[str] = []
    index = 0
    raw = line.strip()
    while index < len(raw):
        character = raw[index]
        if character == "\\" and index + 1 < len(raw) and raw[index + 1] == "|":
            current.append("|")
            index += 2
            continue
        if character == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        index += 1
    cells.append("".join(current).strip())
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells


def markdown_table_rows(
    text: str,
    *,
    label: str,
    table_name: str,
    required_header: set[str],
) -> tuple[dict[str, int], list[dict[str, str]]]:
    lines = text.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if required_header.issubset(
                {cell.casefold() for cell in markdown_cells(line)}
            )
        ),
        None,
    )
    if start is None or start + 1 >= len(lines):
        raise WorkflowError(f"{label} {table_name} table is missing")
    actual_header = markdown_cells(lines[start])
    if len(actual_header) != len(set(cell.casefold() for cell in actual_header)):
        raise WorkflowError(f"{label} {table_name} table has duplicate columns")
    indexes = {cell.casefold(): index for index, cell in enumerate(actual_header)}
    separator = markdown_cells(lines[start + 1])
    if len(separator) != len(actual_header) or not all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in separator
    ):
        raise WorkflowError(f"{label} {table_name} table separator is invalid")

    rows: list[dict[str, str]] = []
    for line in lines[start + 2 :]:
        if not line.strip().startswith("|"):
            if rows:
                break
            continue
        cells = markdown_cells(line)
        if len(cells) != len(actual_header):
            raise WorkflowError(f"{label} {table_name} row has invalid columns")
        rows.append({name: cells[index] for name, index in indexes.items()})
    if not rows:
        raise WorkflowError(f"{label} {table_name} table is empty")
    return indexes, rows


def task_rows(text: str) -> list[dict[str, Any]]:
    indexes, table = markdown_table_rows(
        text,
        label="tasks.md",
        table_name="task",
        required_header={"id", "status"},
    )
    description_column = next(
        (name for name in TASK_DESCRIPTION_COLUMNS if name in indexes), None
    )
    if description_column is None:
        raise WorkflowError("tasks.md task table has no task description column")

    rows: list[dict[str, Any]] = []
    for raw in table:
        task = raw["id"]
        description = raw[description_column]
        blockers_raw = raw.get("blocked by", "")
        status = raw["status"]
        revision = raw.get("revision")
        if not TASK_ID.fullmatch(task):
            raise WorkflowError(f"tasks.md has invalid task id {task or '<empty>'}")
        if not description:
            raise WorkflowError(f"{task} has empty task description")
        blockers = TASK_ID.findall(blockers_raw)
        if not blockers and blockers_raw not in {"", "-", "—", "none", "None", "N/A"}:
            raise WorkflowError(f"{task} has invalid blockers")
        if status not in TASK_STATUSES:
            raise WorkflowError(f"{task} has invalid status {status}")
        if revision is not None and not REPAIR_ID.fullmatch(revision):
            raise WorkflowError(f"{task} has invalid repair revision {revision}")
        rows.append(
            {
                "id": task,
                "description": description,
                "blocked_by": blockers,
                "status": status,
                "revision": revision,
            }
        )
    task_ids = [row["id"] for row in rows]
    if len(task_ids) != len(set(task_ids)):
        raise WorkflowError("tasks.md has duplicate task id")
    return rows


def repair_rows(text: str) -> list[dict[str, str]]:
    _, rows = markdown_table_rows(
        text,
        label="spec.md",
        table_name="repair revision",
        required_header={
            "revision",
            "repair key",
            "kind",
            "source sha-256",
            "summary",
        },
    )
    for row in rows:
        if not REPAIR_ID.fullmatch(row["revision"]):
            raise WorkflowError("spec.md repair revision id is invalid")
        _sha256(row["repair key"], "spec.md Repair Key")
        if row["kind"] not in REPAIR_KINDS:
            raise WorkflowError("spec.md repair kind is invalid")
        _sha256(row["source sha-256"], "spec.md Source SHA-256")
        if not row["summary"]:
            raise WorkflowError("spec.md repair summary is empty")
    identifiers = [row["revision"] for row in rows]
    if len(identifiers) != len(set(identifiers)):
        raise WorkflowError("spec.md has duplicate repair revision")
    return rows


def nested_frontmatter(text: str, label: str) -> dict[str, str]:
    values, lines, end = frontmatter(text, label)
    result = dict(values)
    parent: str | None = None
    for line in lines[1:end]:
        raw = line.rstrip("\r\n")
        if not raw or ":" not in raw:
            continue
        indentation = len(raw) - len(raw.lstrip())
        key, value = raw.strip().split(":", 1)
        value = frontmatter_scalar(value)
        if indentation == 0:
            parent = key if not value else None
            continue
        if parent is None:
            continue
        compound = f"{parent}.{key}"
        if compound in result and compound.startswith("stage_trace."):
            raise WorkflowError(f"{label} has duplicate {compound}")
        result[compound] = value
    return result


def validate_task_graph(rows: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        task = row["id"]
        if task in by_id:
            raise WorkflowError(f"tasks.md has duplicate {task}")
        by_id[task] = row

    for row in rows:
        for blocker in row["blocked_by"]:
            if blocker == row["id"]:
                raise WorkflowError(f"{row['id']} cannot block itself")
            if blocker not in by_id:
                raise WorkflowError(f"{row['id']} references unknown blocker {blocker}")

    for row in rows:
        if row["status"] not in {"in_progress", "completed"}:
            continue
        if any(by_id[item]["status"] != "completed" for item in row["blocked_by"]):
            raise WorkflowError(f"{row['id']} has an incomplete blocker")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task: str) -> None:
        if task in visiting:
            raise WorkflowError("tasks.md dependency graph contains a cycle")
        if task in visited:
            return
        visiting.add(task)
        for blocker in by_id[task]["blocked_by"]:
            visit(blocker)
        visiting.remove(task)
        visited.add(task)

    for task in by_id:
        visit(task)

    in_progress = [row for row in rows if row["status"] == "in_progress"]
    if in_progress:
        identifiers = [str(row["id"]) for row in in_progress]
        return identifiers[0], identifiers

    for row in rows:
        if row["status"] == "pending" and all(
            by_id[item]["status"] == "completed" for item in row["blocked_by"]
        ):
            return str(row["id"]), []
    if all(row["status"] == "completed" for row in rows):
        return None, []
    raise WorkflowError("tasks.md has unfinished work but no executable task")


def validate_tasks(
    repo: Path, feature: str, stage: str, *, repair_key: str | None = None
) -> dict[str, Any]:
    directory = feature_dir(repo, feature)
    spec_text = read_regular(directory / "spec.md", "spec.md")
    spec = spec_contract(spec_text)
    tasks_text = read_regular(directory / "tasks.md", "tasks.md")
    tasks, _, _ = frontmatter(tasks_text, "tasks.md")

    for label, values in (("spec.md", spec), ("tasks.md", tasks)):
        if values.get("feature") != feature:
            raise WorkflowError(f"{label} feature does not match its directory")
    if repair_key is not None:
        if stage != "propose":
            raise WorkflowError("repair key is only valid for propose validation")
        repair_key = _sha256(repair_key, "repair key")
        state = _repair_state(repo, feature)
        if (
            state is None
            or state["status"] != "in_progress"
            or state["repair_key"] != repair_key
        ):
            raise WorkflowError("Propose requires a matching in-progress repair")
        _validate_repair_artifacts(state, spec_text, tasks_text)
    else:
        stage_statuses = {
            "propose": ({"planning", "ready"}, {"ready"}),
            "apply": (
                {"ready", "applying", "implemented", "completed"},
                {"ready", "applying", "completed"},
            ),
            "archive": ({"implemented", "completed"}, {"completed"}),
        }
        allowed_spec, allowed_tasks = stage_statuses[stage]
        if spec.get("status") not in allowed_spec:
            raise WorkflowError(f"spec.md status is invalid for {stage}")
        if tasks.get("status") not in allowed_tasks:
            raise WorkflowError(f"tasks.md status is invalid for {stage}")
        if spec.get("status") in {"applying", "implemented", "completed"}:
            expected_apply_receipt = spec_receipt(spec_text)
            state = _apply_state(repo, feature)
            if state.get("apply_spec_receipt_sha256") != expected_apply_receipt:
                raise WorkflowError("Apply state has invalid apply_spec_receipt_sha256")

    rows = task_rows(tasks_text)
    next_task, in_progress_tasks = validate_task_graph(rows)
    if repair_key is None:
        if tasks.get("status") == "ready" and any(row["status"] != "pending" for row in rows):
            raise WorkflowError("ready tasks.md must contain only pending tasks")
        if tasks.get("status") == "completed" and any(
            row["status"] != "completed" for row in rows
        ):
            raise WorkflowError("completed tasks.md contains unfinished tasks")

    return {
        "feature": feature,
        "stage": stage,
        "task_count": len(rows),
        "next_task": next_task,
        "in_progress_tasks": in_progress_tasks,
        "spec_receipt_sha256": spec_receipt(spec_text),
        "tasks_receipt_sha256": artifact_receipt(tasks_text),
    }


def _knowledge_paths(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowError("knowledge paths are invalid JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise WorkflowError("knowledge paths must be a JSON string list")
    paths: list[str] = []
    for item in value:
        parsed = PurePosixPath(item)
        if (
            parsed.is_absolute()
            or str(parsed) != item
            or len(parsed.parts) < 3
            or parsed.parts[:2] != ("docs", "knowledge")
            or ".." in parsed.parts
            or "\\" in item
        ):
            raise WorkflowError("knowledge path is unsafe")
        paths.append(item)
    if len(paths) != len(set(paths)):
        raise WorkflowError("knowledge paths contain duplicates")
    return sorted(paths)


def _validate_authorized_knowledge_files(
    repo: Path, paths: list[str], *, require_exists: bool
) -> None:
    for relative in paths:
        parsed = PurePosixPath(relative)
        current = repo
        for part in parsed.parts[:-1]:
            current /= part
            if current.is_symlink() or not current.is_dir():
                raise WorkflowError(
                    "knowledge path must name an existing regular file with safe ancestors"
                )
        target = repo / relative
        if require_exists:
            if target.is_symlink() or not target.is_file():
                raise WorkflowError(
                    "knowledge path must name an existing regular file"
                )
        elif os.path.lexists(target) and (target.is_symlink() or not target.is_file()):
            raise WorkflowError("authorized knowledge path became unsafe")


def _authorized_knowledge_paths(
    repo: Path, feature: str, evidence: dict[str, str]
) -> set[str]:
    path = _state_path(repo, feature, "archive-state.json")
    if not path.is_file():
        return set()
    state = _read_json_state(repo, feature, "archive-state.json", "Archive state")
    if (
        state.get("schema_version") != 1
        or state.get("feature") != feature
        or state.get("review_head") != evidence.get("review_head")
        or state.get("review_scope_sha256") != evidence.get("review_scope_sha256")
    ):
        raise WorkflowError("Archive state has invalid identity")
    paths = _knowledge_paths(json.dumps(state.get("knowledge_paths")))
    _validate_authorized_knowledge_files(repo, paths, require_exists=False)
    return set(paths)


def _review_acceptance_payload(
    feature: str, evidence: dict[str, str], entries: list[dict[str, Any]]
) -> dict[str, Any]:
    payload = {
        "feature": feature,
        "schema_version": 1,
        "entries": entries,
        "review_identity": {
            key: evidence[key]
            for key in (
                "review_fixed_point", "review_head", "review_scope_sha256",
                "spec_receipt_sha256", "tasks_receipt_sha256",
            )
        },
    }
    payload["acceptance_sha256"] = artifact_receipt(json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ))
    return payload


def _accepted_review_scope(
    repo: Path, feature: str, evidence: dict[str, str]
) -> dict[str, Any] | None:
    if evidence.get("archive_review_decision") != "user_accepted_changes":
        return None
    state = _read_json_state(
        repo, feature, "archive-review-acceptance.json", "Archive review acceptance"
    )
    entries = _validate_manifest(state.get("entries"), "Archive review acceptance")
    expected = _review_acceptance_payload(feature, evidence, entries)
    if (
        any(state.get(key) != value for key, value in expected.items())
        or evidence.get("archive_review_acceptance_sha256") != expected["acceptance_sha256"]
    ):
        raise WorkflowError("Archive review acceptance has invalid identity")
    return state


def _verify_review_scope(
    repo: Path,
    feature: str,
    evidence: dict[str, str],
    *,
    allow_knowledge_drift: bool,
    allow_review_acceptance: bool = True,
) -> dict[str, Any] | None:
    reviewed_head = evidence.get("review_head", "")
    sealed = _review_state(repo, feature)
    if (
        sealed.get("review_head") != reviewed_head
        or sealed.get("review_scope_sha256") != evidence.get("review_scope_sha256")
    ):
        raise WorkflowError("verify-report.md review scope is not sealed")
    authorized = (
        _authorized_knowledge_paths(repo, feature, evidence) if allow_knowledge_drift else set()
    )
    current = _review_scope_details(
        repo, feature, review_head=reviewed_head,
        authorized_knowledge_paths=frozenset(authorized),
    )

    def comparable(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [item for item in entries if str(item["path"]) not in authorized]

    if comparable(sealed["entries"]) == comparable(current["entries"]):
        return None
    accepted = _accepted_review_scope(repo, feature, evidence) if allow_review_acceptance else None
    if accepted and comparable(accepted["entries"]) == comparable(current["entries"]):
        return accepted
    before = {item["path"]: item for item in comparable(sealed["entries"])}
    after = {item["path"]: item for item in comparable(current["entries"])}
    payload = _review_acceptance_payload(feature, evidence, current["entries"])
    raise ReviewScopeDrift({
        "changed_since_review": sorted(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        ),
        "current_scope_sha256": payload["acceptance_sha256"],
        "decision_needed": "review_scope_changed",
        "feature": feature,
        "ready": False,
        "recommended_action": "review",
        "actions": ["review", "archive_without_review"],
    }, finalizing=allow_knowledge_drift)


def archive_preflight(
    repo: Path, feature: str, *, required_archive_state: str | None = None,
    allow_review_acceptance: bool = True,
) -> dict[str, Any]:
    completed = repo / "docs" / "specs" / "completed" / feature_name(feature)
    if os.path.lexists(completed):
        raise WorkflowError("completed feature already exists")
    task_result = validate_tasks(repo, feature, "archive")
    directory = feature_dir(repo, feature)
    evidence_text = read_regular(directory / "verify-report.md", "verify-report.md")
    evidence = nested_frontmatter(evidence_text, "verify-report.md")
    if not artifact_body(evidence_text, "verify-report.md").strip():
        raise WorkflowError("verify-report.md body is empty")

    spec_text = read_regular(directory / "spec.md", "spec.md")
    spec = spec_contract(spec_text)

    required = {
        "feature": feature,
        "review_mode": "parallel_subagents",
        "standards_verdict": "APPROVE",
        "spec_verdict": "APPROVE",
        "stage_trace.propose": "completed",
        "stage_trace.apply": "completed",
        "spec_receipt_sha256": task_result["spec_receipt_sha256"],
        "tasks_receipt_sha256": task_result["tasks_receipt_sha256"],
    }
    for key, expected in required.items():
        if evidence.get(key) != expected:
            raise WorkflowError(f"verify-report.md has invalid {key}")

    for key in ("review_fixed_point", "review_head"):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", evidence.get(key, "")):
            raise WorkflowError(f"verify-report.md has invalid {key}")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence.get("review_scope_sha256", "")):
        raise WorkflowError("verify-report.md has invalid review_scope_sha256")
    rounds = evidence.get("must_fix_rounds", "")
    if not re.fullmatch(r"0|[1-9][0-9]*", rounds):
        raise WorkflowError("verify-report.md has invalid must_fix_rounds")

    archive_state = None
    if spec.get("status") == "implemented" and evidence.get("stage_trace.archive") == "pending":
        archive_state = "ready"
    elif spec.get("status") == "completed" and evidence.get("stage_trace.archive") == "completed":
        archive_state = "finalizing"
    if archive_state is None:
        raise WorkflowError("spec.md and verify-report.md have inconsistent archive state")
    if required_archive_state is not None and archive_state != required_archive_state:
        raise WorkflowError("finalize-preflight requires finalizing state")

    if any(
        values.get("trigger") == "bug-fix" or values.get("bug_signature")
        for values in (spec, evidence)
    ):
        raise WorkflowError("artifacts contain deprecated bug-fix identity")
    accepted = _verify_review_scope(
        repo,
        feature,
        evidence,
        allow_knowledge_drift=archive_state == "finalizing",
        allow_review_acceptance=allow_review_acceptance,
    )
    return {
        "archive_state": archive_state,
        "feature": feature,
        "ready": True,
        "review_accepted_changes": accepted is not None,
        "task_count": task_result["task_count"],
    }


def command_validate_tasks(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    return validate_tasks(repo, args.feature, args.stage, repair_key=args.repair_key)


def command_archive_preflight(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    try:
        return archive_preflight(repo, args.feature, allow_review_acceptance=args.review_choice)
    except ReviewScopeDrift as exc:
        if not args.review_choice or exc.finalizing:
            raise
        return exc.choice


def command_accept_review_drift(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Record an explicit decision for exactly the content presented to the user."""
    if not args.user_confirmed:
        raise WorkflowError("Archive review acceptance requires user confirmation")
    git_state = operation_state(repo)
    if git_state["operations"] or git_state["unmerged_paths"]:
        raise WorkflowError("Git operation is in progress")
    try:
        archive_preflight(repo, args.feature, required_archive_state="ready", allow_review_acceptance=False)
    except ReviewScopeDrift as exc:
        if args.expected_scope_sha256 != exc.choice["current_scope_sha256"]:
            raise WorkflowError("Archive content changed since the user choice; ask again") from exc
        changed_paths = exc.choice["changed_since_review"]
    else:
        raise WorkflowError("Archive content is already reviewed; no acceptance is needed")
    path = feature_dir(repo, args.feature) / "verify-report.md"
    text = read_regular(path, "verify-report.md")
    evidence = nested_frontmatter(text, "verify-report.md")
    current = _review_scope_details(repo, args.feature, review_head=evidence["review_head"])
    payload = _review_acceptance_payload(args.feature, evidence, current["entries"])
    if payload["acceptance_sha256"] != args.expected_scope_sha256:
        raise WorkflowError("Archive content changed since the user choice; ask again")
    for key, value in {
        "archive_review_decision": "user_accepted_changes",
        "archive_review_acceptance_sha256": payload["acceptance_sha256"],
    }.items():
        if key in evidence:
            text = _replace_frontmatter_value(text, "verify-report.md", key, value)
        else:
            _, lines, end = frontmatter(text, "verify-report.md")
            lines.insert(end, f"{key}: {value}\n")
            text = "".join(lines)
    if evidence.get("archive_review_acceptance_sha256") != payload["acceptance_sha256"]:
        text += (
            "\n## Archive review decision\n\n"
            "用户确认内容已变化，选择直接归档；这些变化未重新 Review。\n\n"
            f"Acceptance SHA-256: {payload['acceptance_sha256']}\n\n"
            "Changed paths: " + json.dumps(changed_paths, ensure_ascii=True) + "\n"
        )
    _write_json_state(repo, args.feature, "archive-review-acceptance.json", payload, "Archive review acceptance")
    try:
        atomic_write_text(path, text)
    except (OSError, AtomicWriteError) as exc:
        raise WorkflowError("cannot write Archive review decision") from exc
    return archive_preflight(repo, args.feature, required_archive_state="ready")


def completed_archive_preflight(repo: Path, feature: str) -> dict[str, Any]:
    """Validate an already moved Lite feature for multi-repo archive recovery."""
    directory = repo / "docs" / "specs" / "completed" / feature_name(feature)
    if directory.is_symlink() or not directory.is_dir():
        raise WorkflowError("completed feature directory is missing or unsafe")
    spec_text = read_regular(directory / "spec.md", "spec.md")
    tasks_text = read_regular(directory / "tasks.md", "tasks.md")
    spec = spec_contract(spec_text)
    tasks, _, _ = frontmatter(tasks_text, "tasks.md")
    rows = task_rows(tasks_text)
    if (
        spec.get("feature") != feature
        or spec.get("status") != "completed"
        or tasks.get("feature") != feature
        or tasks.get("status") != "completed"
        or not rows
        or any(row["status"] != "completed" for row in rows)
    ):
        raise WorkflowError("completed Lite artifacts have invalid identity")
    evidence_text = read_regular(directory / "verify-report.md", "verify-report.md")
    if not artifact_body(evidence_text, "verify-report.md").strip():
        raise WorkflowError("completed verify-report.md body is empty")
    evidence = nested_frontmatter(evidence_text, "verify-report.md")
    if (
        evidence.get("feature") != feature
        or evidence.get("standards_verdict") != "APPROVE"
        or evidence.get("spec_verdict") != "APPROVE"
        or evidence.get("stage_trace.archive") != "completed"
        or evidence.get("spec_receipt_sha256") != spec_receipt(spec_text)
        or evidence.get("tasks_receipt_sha256") != artifact_receipt(tasks_text)
    ):
        raise WorkflowError("completed verify-report.md has invalid identity")
    _verify_review_scope(repo, feature, evidence, allow_knowledge_drift=True)
    return {"archive_state": "completed", "feature": feature, "ready": True}


def command_archive_code_scope(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    """Return the already sealed Apply Review Scope without resealing it."""
    if args.completed:
        completed_archive_preflight(repo, args.feature)
        directory = repo / "docs" / "specs" / "completed" / feature_name(args.feature)
        spec_text = read_regular(directory / "spec.md", "spec.md")
        tasks_text = read_regular(directory / "tasks.md", "tasks.md")
    else:
        archive_preflight(repo, args.feature)
        directory = feature_dir(repo, args.feature)
    sealed = _review_state(repo, args.feature)
    evidence = nested_frontmatter(
        read_regular(directory / "verify-report.md", "verify-report.md"),
        "verify-report.md",
    )
    accepted = _verify_review_scope(
        repo, args.feature, evidence,
        allow_knowledge_drift=evidence.get("stage_trace.archive") == "completed",
    )
    fixed_point = evidence["review_fixed_point"]
    _git_bytes(repo, "merge-base", "--is-ancestor", fixed_point, str(sealed["review_head"]))
    reviewed_commit_raw = _git_bytes(
        repo,
        "diff",
        "--name-only",
        "-z",
        fixed_point,
        str(sealed["review_head"]),
        "--",
    )
    packaged_commit_raw = _git_bytes(
        repo,
        "diff",
        "--name-only",
        "-z",
        str(sealed["review_head"]),
        "HEAD",
        "--",
    )
    committed_paths = sorted(
        {
            os.fsdecode(item)
            for raw in (reviewed_commit_raw, packaged_commit_raw)
            for item in raw.split(b"\0")
            if item and not _review_scope_excluded(args.feature, os.fsdecode(item))
        }
    )
    reviewed_paths = sorted({
        os.fsdecode(item) for item in reviewed_commit_raw.split(b"\0")
        if item and not _review_scope_excluded(args.feature, os.fsdecode(item))
    } | {str(item["path"]) for item in sealed["entries"]})
    sealed_delta_paths = {
        str(item["path"]) for item in (accepted or sealed)["entries"]
    }
    current_uncommitted = {
        os.fsdecode(item)
        for raw in (
            _git_bytes(repo, "diff", "--name-only", "-z", "HEAD", "--"),
            _git_bytes(repo, "ls-files", "--others", "--exclude-standard", "-z"),
        )
        for item in raw.split(b"\0")
        if item
    }
    working_tree_paths = sorted(sealed_delta_paths & current_uncommitted)
    changed_paths = sorted(set(committed_paths) | set(working_tree_paths))
    current = _review_scope_details(
        repo, args.feature, review_head=str(sealed["review_head"]),
        authorized_knowledge_paths=frozenset(
            _authorized_knowledge_paths(repo, args.feature, evidence)
            if evidence.get("stage_trace.archive") == "completed" else ()
        ),
    )
    archive_scope_sha256 = artifact_receipt(
        json.dumps(
            {
                "committed_paths": committed_paths,
                "review_fixed_point": fixed_point,
                "review_head": sealed["review_head"],
                "review_scope_sha256": sealed["review_scope_sha256"],
                "working_tree_paths": working_tree_paths,
                **({"review_acceptance_sha256": accepted["acceptance_sha256"]} if accepted else {}),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return {
        "archive_scope_sha256": archive_scope_sha256,
        "changed_paths": changed_paths,
        "committed_paths": committed_paths,
        "excluded_preexisting_paths": current["excluded_preexisting_paths"],
        "feature": args.feature,
        "review_fixed_point": fixed_point,
        "review_head": sealed["review_head"],
        "review_scope_sha256": sealed["review_scope_sha256"],
        "review_accepted_changes": accepted is not None,
        "reviewed_paths": reviewed_paths,
        "sealed": True,
        "working_tree_paths": working_tree_paths,
    }


def command_finalize_preflight(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    return archive_preflight(
        repo, args.feature, required_archive_state="finalizing"
    )


def command_authorize_knowledge(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    paths = _knowledge_paths(args.paths_json)
    _validate_authorized_knowledge_files(repo, paths, require_exists=True)
    try:
        archive_preflight(repo, args.feature, required_archive_state="ready")
    except WorkflowError as exc:
        if str(exc) == "verify-report.md review scope drift":
            raise WorkflowError(
                "authorize-knowledge requires an unchanged review scope; "
                "authorize before editing knowledge files"
            ) from exc
        raise
    evidence = nested_frontmatter(
        read_regular(feature_dir(repo, args.feature) / "verify-report.md", "verify-report.md"),
        "verify-report.md",
    )
    _write_json_state(
        repo,
        args.feature,
        "archive-state.json",
        {
            "feature": args.feature,
            "knowledge_paths": paths,
            "review_head": evidence["review_head"],
            "review_scope_sha256": evidence["review_scope_sha256"],
            "schema_version": 1,
        },
        "Archive state",
    )
    return {"feature": args.feature, "knowledge_paths": paths, "ready": True}


def command_bind_apply(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    return _bind_apply_receipt(repo, args.feature)


def command_review_scope(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    return seal_review_scope(repo, args.feature)


def command_repair_preflight(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    with _repair_lock(repo, args.feature):
        result = _repair_preflight(repo, args.feature)
        state = _repair_state(repo, args.feature)
        result["repair_state"] = (
            {
                "repair_key": state["repair_key"],
                "repair_kind": state["repair_kind"],
                "repair_revision": state["repair_revision"],
                "source_sha256": state["source_sha256"],
                "status": state["status"],
            }
            if state is not None
            else None
        )
        return result


def command_begin_repair(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    return begin_repair(
        repo,
        args.feature,
        repair_key=args.repair_key,
        source_sha256=args.source_sha256,
        repair_kind=args.repair_kind,
        expected_spec_receipt=args.expected_spec_receipt,
        expected_tasks_receipt=args.expected_tasks_receipt,
    )


def command_repair_rebind(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    return rebind_repair(
        repo,
        args.feature,
        repair_key=args.repair_key,
        expected_spec_receipt=args.expected_spec_receipt,
        expected_tasks_receipt=args.expected_tasks_receipt,
    )


def command_archive_git_state(repo: Path, args: argparse.Namespace) -> dict[str, Any]:
    target_branch, target_commit = resolve_target_branch(repo)
    head = git_text(repo, "rev-parse", "HEAD") or None
    source_branch = git_text(repo, "symbolic-ref", "--quiet", "--short", "HEAD") or None
    state = operation_state(repo)
    merge_base = (
        git_text(repo, "merge-base", str(head), str(target_commit))
        if head and target_commit
        else ""
    ) or None
    return {
        "head": head,
        "merge_base_commit": merge_base,
        "operations": state["operations"],
        "source_branch": source_branch,
        "target_branch": target_branch,
        "target_commit": target_commit,
        "unmerged_paths": state["unmerged_paths"],
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--repo-root", required=True)
    commands = root.add_subparsers(dest="command", required=True)

    task_check = commands.add_parser("validate-tasks")
    task_check.add_argument("--feature", required=True)
    task_check.add_argument("--stage", choices=("propose", "apply", "archive"), required=True)
    task_check.add_argument("--repair-key")
    task_check.set_defaults(handler=command_validate_tasks)

    archive = commands.add_parser("archive-preflight")
    archive.add_argument("--feature", required=True)
    archive.add_argument("--review-choice", action="store_true")
    archive.set_defaults(handler=command_archive_preflight)

    accept_review = commands.add_parser("accept-review-drift")
    accept_review.add_argument("--feature", required=True)
    accept_review.add_argument("--expected-scope-sha256", required=True)
    accept_review.add_argument("--user-confirmed", action="store_true", required=True)
    accept_review.set_defaults(handler=command_accept_review_drift)

    archive_scope = commands.add_parser("archive-code-scope")
    archive_scope.add_argument("--feature", required=True)
    archive_scope.add_argument("--completed", action="store_true")
    archive_scope.set_defaults(handler=command_archive_code_scope)

    finalize = commands.add_parser("finalize-preflight")
    finalize.add_argument("--feature", required=True)
    finalize.set_defaults(handler=command_finalize_preflight)

    authorize = commands.add_parser("authorize-knowledge")
    authorize.add_argument("--feature", required=True)
    authorize.add_argument("--paths-json", required=True)
    authorize.set_defaults(handler=command_authorize_knowledge)

    bind_apply = commands.add_parser("bind-apply")
    bind_apply.add_argument("--feature", required=True)
    bind_apply.set_defaults(handler=command_bind_apply)

    scope = commands.add_parser("review-scope")
    scope.add_argument("--feature", required=True)
    scope.set_defaults(handler=command_review_scope)

    repair_preflight = commands.add_parser("repair-preflight")
    repair_preflight.add_argument("--feature", required=True)
    repair_preflight.set_defaults(handler=command_repair_preflight)

    begin = commands.add_parser("begin-repair")
    begin.add_argument("--feature", required=True)
    begin.add_argument("--repair-key", required=True)
    begin.add_argument("--source-sha256", required=True)
    begin.add_argument("--repair-kind", choices=sorted(REPAIR_KINDS), required=True)
    begin.add_argument("--expected-spec-receipt", required=True)
    begin.add_argument("--expected-tasks-receipt", required=True)
    begin.set_defaults(handler=command_begin_repair)

    rebind = commands.add_parser("repair-rebind")
    rebind.add_argument("--feature", required=True)
    rebind.add_argument("--repair-key", required=True)
    rebind.add_argument("--expected-spec-receipt", required=True)
    rebind.add_argument("--expected-tasks-receipt", required=True)
    rebind.set_defaults(handler=command_repair_rebind)

    git_state = commands.add_parser("archive-git-state")
    git_state.set_defaults(handler=command_archive_git_state)
    return root


def main(argv: list[str] | None = None) -> int:
    guard_source(Path(__file__).resolve().parent.parent)
    args = parser().parse_args(argv)
    try:
        payload = args.handler(repo_root(args.repo_root), args)
    except WorkflowError as exc:
        print(f"invalid Lite workflow: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
