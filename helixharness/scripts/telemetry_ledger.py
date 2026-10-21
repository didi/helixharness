#!/usr/bin/env python3
"""Fixed-evidence ledger for the Lite HelixHarness workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from telemetry_hosts import get_host_adapter, normalize_host_id
from git_target import resolve_target_branch


def _plugin_version() -> str:
    root = Path(__file__).resolve().parent.parent
    for path, field in (
        (root / ".codex-plugin" / "plugin.json", "version"),
        (root / "runtime-manifest.json", "version"),
    ):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        version = str(value.get(field) or "") if isinstance(value, dict) else ""
        if version:
            return version
    raise RuntimeError("HelixHarness plugin version is unavailable")


SCHEMA_VERSION = "1.0"
LEDGER_SCHEMA = f"helixharness-eval-ledger/{SCHEMA_VERSION}"
COLLECTION_SEAL_SCHEMA = "helixharness-lite-collection-seal/1.0"
COLLECTION_INPUT_SCHEMA = "helixharness-lite-collection-input/1.0"
PLUGIN_VERSION = _plugin_version()
PRIVATE_ROOT = Path(".helixharness") / "telemetry-v2"
REQUIREMENT_ID = re.compile(r"^req_[0-9a-f]{24}$")
ENTRY_ID = re.compile(r"^entry-[0-9a-f]{16}$")
STATUSES = {"in_progress", "continued", "completed", "blocked", "partial", "decision_needed", "failed"}
STAGE_KINDS = {"propose": "requirement", "apply": "coding", "archive": "archive"}
LOCK_TIMEOUT_SECONDS = 5.0
RUNTIME_GITIGNORE_RULES = ("!docs/eval/", "!docs/eval/**", "/.helixharness/")

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LedgerError(ValueError):
    """Raised when fixed evidence cannot be validated or persisted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _feature(value: Any) -> str:
    feature = str(value or "")
    windows_forbidden = '<>:"|?*'
    reserved_stem = feature.split(".", 1)[0].casefold()
    windows_reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if (
        not feature
        or feature != feature.strip()
        or feature.endswith(".")
        or feature in {".", ".."}
        or feature.casefold() == ".git"
        or "/" in feature
        or "\\" in feature
        or any(character in feature for character in windows_forbidden)
        or reserved_stem in windows_reserved
        or len(feature.encode("utf-8")) > 255
        or any(ord(char) <= 0x1F or ord(char) == 0x7F for char in feature)
    ):
        raise LedgerError("invalid feature")
    return feature


def validate_feature(value: Any) -> str:
    """Return one canonical portable feature name or fail before path use."""
    return _feature(value)


def _managed(repo: Path, relative: Path) -> Path:
    root = repo.resolve()
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise LedgerError("telemetry path escapes repository") from exc
    current = root
    for part in relative.parts:
        current /= part
        if os.path.lexists(current) and current.is_symlink():
            raise LedgerError("telemetry path contains symlink")
    return candidate


def ledger_path(repo: Path, feature: str) -> Path:
    return _managed(repo, Path("docs") / "eval" / _feature(feature) / "eval-log.json")


def _locator_path(repo: Path, entry_id: str) -> Path:
    if not ENTRY_ID.fullmatch(str(entry_id or "")):
        raise LedgerError("invalid entry id")
    return _managed(repo, PRIVATE_ROOT / "locators" / f"{entry_id}.json")


def _state_path(repo: Path, entry_id: str) -> Path:
    if not ENTRY_ID.fullmatch(str(entry_id or "")):
        raise LedgerError("invalid entry id")
    return _managed(repo, PRIVATE_ROOT / "stages" / f"{entry_id}.json")


def _collection_seal_path(repo: Path, feature: str) -> Path:
    return _managed(repo, PRIVATE_ROOT / "seals" / _feature(feature) / "collection.json")


def _collection_input_path(repo: Path, entry_id: str) -> Path:
    if not ENTRY_ID.fullmatch(str(entry_id or "")):
        raise LedgerError("invalid entry id")
    return _managed(repo, PRIVATE_ROOT / "inputs" / f"{entry_id}.json")


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        private = ".helixharness" in path.parts and "telemetry-v2" in path.parts
        os.chmod(temporary, 0o600 if private else 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _lock(repo: Path) -> Iterator[None]:
    path = _managed(repo, PRIVATE_ROOT / "ledger.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
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
                        raise LedgerError("telemetry ledger lock acquisition timed out") from exc
                    time.sleep(0.01)
        else:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise LedgerError("telemetry ledger lock acquisition timed out") from exc
                    time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"invalid telemetry JSON: {path}") from exc
    if not isinstance(value, dict):
        raise LedgerError(f"telemetry JSON must be an object: {path}")
    return value


def _load(path: Path) -> dict[str, Any] | None:
    value = _load_json(path)
    if value is not None and not isinstance(value.get("entries"), list):
        raise LedgerError("eval-log must contain entries[]")
    return value


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _working_tree_dirty(repo: Path) -> bool | None:
    """Report business dirtiness without counting Lite telemetry's own files."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        value = line[3:] if len(line) >= 4 else line
        paths = value.split(" -> ")
        if all(
            (path == ".gitignore" and _gitignore_has_only_runtime_rules(repo))
            or path.startswith("docs/eval/")
            or path.startswith(".helixharness/")
            for path in paths
        ):
            continue
        return True
    return False


def _gitignore_has_only_runtime_rules(repo: Path) -> bool:
    path = repo / ".gitignore"
    try:
        current = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    try:
        baseline_result = subprocess.run(
            ["git", "-C", str(repo), "show", "HEAD:.gitignore"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    baseline = baseline_result.stdout.splitlines() if baseline_result.returncode == 0 else []
    without_runtime_rules = [
        line for line in current if line not in RUNTIME_GITIGNORE_RULES
    ]
    baseline_without_runtime_rules = [
        line for line in baseline if line not in RUNTIME_GITIGNORE_RULES
    ]
    return without_runtime_rules == baseline_without_runtime_rules


def _code_checkpoint(repo: Path) -> dict[str, Any]:
    head = _git(repo, "rev-parse", "HEAD") or None
    source_branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD") or None
    target_branch, target_commit = resolve_target_branch(repo)
    merge_base = (
        _git(repo, "merge-base", str(head), str(target_commit))
        if head and target_commit
        else ""
    ) or None
    return {
        "base_commit": head,
        "head_commit": None,
        "source_branch": source_branch,
        "target_branch": target_branch,
        "merge_base_commit": merge_base,
        "implementation_commit_id": None,
        "code_resolution": "pending",
        "working_tree_dirty": _working_tree_dirty(repo),
    }


def _validated_commit(repo: Path, value: str | None) -> str | None:
    if not value:
        return None
    resolved = _git(repo, "rev-parse", "--verify", f"{value}^{{commit}}") or None
    if resolved is None:
        raise LedgerError("implementation commit is unavailable")
    try:
        ancestor = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", resolved, "HEAD"],
            check=False,
            capture_output=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LedgerError("implementation commit cannot be verified") from exc
    if ancestor.returncode != 0:
        raise LedgerError("implementation commit is not on the current branch")
    return resolved


def _validated_review_scope(
    repo: Path,
    *,
    review_fixed_point: str | None,
    review_head: str | None,
    review_scope_sha256: str | None,
    archive_scope_sha256: str | None,
    reviewed_paths: list[str] | None,
) -> dict[str, Any] | None:
    identity_values = (
        review_fixed_point,
        review_head,
        review_scope_sha256,
        archive_scope_sha256,
    )
    if all(value is None for value in identity_values) and reviewed_paths is None:
        return None
    if not all(value is not None for value in identity_values):
        raise LedgerError("archive review scope is incomplete")
    fixed = _validated_commit(repo, review_fixed_point)
    head = _validated_commit(repo, review_head)
    if not fixed or not head:
        raise LedgerError("archive review scope commits are unavailable")
    if not re.fullmatch(r"[0-9a-f]{64}", str(review_scope_sha256)):
        raise LedgerError("archive review scope hash is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(archive_scope_sha256)):
        raise LedgerError("archive scope hash is invalid")
    paths = [str(item) for item in (reviewed_paths or [])]
    if (
        paths != sorted(set(paths))
        or any(
            not path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or "\\" in path
            for path in paths
        )
    ):
        raise LedgerError("archive reviewed paths are invalid")
    return {
        "archive_scope_sha256": archive_scope_sha256,
        "review_fixed_point": fixed,
        "review_head": head,
        "review_scope_sha256": review_scope_sha256,
        "reviewed_paths": paths,
    }


def _git_actor(repo: Path) -> dict[str, str | None]:
    local = _git(repo, "config", "--local", "user.name")
    if local:
        return {
            "user_name": local,
            "user_name_source": "git_config_local_user_name",
        }
    global_name = _git(repo, "config", "--global", "user.name")
    if global_name:
        return {
            "user_name": global_name,
            "user_name_source": "git_config_global_user_name",
        }
    return {"user_name": None, "user_name_source": "unavailable"}


def _new_requirement_id() -> str:
    return "req_" + secrets.token_hex(12)


def _new_ledger(repo: Path, feature: str, now: str) -> dict[str, Any]:
    return {
        "schema": LEDGER_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "feature": feature,
        "project": {"name": repo.name},
        "requirement": {"id": _new_requirement_id(), "name": feature},
        "created_at": now,
        "last_updated": now,
        "status": "in_progress",
        "entries": [],
    }


def _ensure_ledger_locked(repo: Path, feature: str, *, now: str) -> dict[str, Any]:
    path = ledger_path(repo, feature)
    current = _load(path)
    if current is None:
        return _new_ledger(repo, feature, now)
    if current.get("schema") == LEDGER_SCHEMA and current.get("schema_version") == SCHEMA_VERSION:
        requirement = current.get("requirement") if isinstance(current.get("requirement"), dict) else {}
        if not REQUIREMENT_ID.fullmatch(str(requirement.get("id") or "")):
            raise LedgerError("ledger requirement id is invalid")
        if current.get("feature") != feature:
            raise LedgerError("ledger feature does not match directory")
        return current
    raise LedgerError("unsupported Lite eval-log schema")


def ensure_ledger(repo: Path, feature: str) -> dict[str, Any]:
    repo = Path(repo).resolve()
    name = _feature(feature)
    with _lock(repo):
        ledger = _ensure_ledger_locked(repo, name, now=_now())
        _atomic_write(ledger_path(repo, name), ledger)
        return ledger


def _session_hash(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value or value == "unknown":
        return "session-unavailable"
    return "session-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _entry_id(identity: dict[str, Any]) -> str:
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "entry-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def latest_finished_stage(
    repo: Path,
    *,
    feature: str,
    command: str,
    platform: str,
    raw_session_id: str | None,
    consent_epoch: str,
) -> dict[str, Any] | None:
    """Return the latest terminal entry owned by this exact private session."""
    if raw_session_id is None:
        return None
    repo = Path(repo).resolve()
    name = _feature(feature)
    try:
        canonical_platform = normalize_host_id(platform)
    except ValueError:
        return None
    ledger = _load(ledger_path(repo, name))
    if not isinstance(ledger, dict):
        return None
    expected_session_hash = _session_hash(raw_session_id)
    for entry in reversed(ledger.get("entries", [])):
        if not isinstance(entry, dict):
            continue
        if (
            entry.get("command") != command
            or entry.get("platform") != canonical_platform
            or entry.get("session_hash") != expected_session_hash
        ):
            continue
        if entry.get("status") == "in_progress":
            return None
        state = _load_json(_state_path(repo, str(entry.get("entry_id") or "")))
        if not isinstance(state, dict):
            return None
        if (
            state.get("finished") is not True
            or state.get("consent_epoch") != consent_epoch
            or state.get("platform") != canonical_platform
            or state.get("raw_session_id") != raw_session_id
            or state.get("stage_status") != entry.get("status")
        ):
            return None
        return dict(entry)
    return None


def start_stage(
    repo: Path,
    *,
    feature: str,
    command: str,
    platform: str,
    trigger: str,
    consent_epoch: str,
    raw_session_id: str | None = None,
    session_log_path: str | None = None,
    started_at: str | None = None,
) -> dict[str, Any]:
    repo = Path(repo).resolve()
    name = _feature(feature)
    try:
        platform = normalize_host_id(platform)
    except ValueError as exc:
        raise LedgerError("telemetry platform must be canonical") from exc
    if get_host_adapter(platform) is None:
        raise LedgerError("telemetry platform has no registered adapter")
    stage_kind = STAGE_KINDS.get(str(command))
    if stage_kind is None:
        raise LedgerError("unsupported Lite telemetry command")
    started = started_at or _now()
    session_hash = _session_hash(raw_session_id)
    code_context = _code_checkpoint(repo)
    checkpoint = {
        "base_commit": code_context["base_commit"],
        "working_tree_dirty": code_context["working_tree_dirty"],
    }
    identity = {
        "feature": name,
        "command": str(command),
        "stage_kind": str(stage_kind),
        "started_at": started,
        "platform": str(platform),
        "session_hash": session_hash,
        "trigger": str(trigger),
        "base_commit": checkpoint["base_commit"],
    }
    entry_id = _entry_id(identity)
    with _lock(repo):
        ledger = _ensure_ledger_locked(repo, name, now=started)
        if ledger.get("status") == "archived":
            raise LedgerError("archived Lite requirement telemetry is sealed")
        existing = next((item for item in ledger["entries"] if item.get("entry_id") == entry_id), None)
        if existing is None:
            attempt = 1 + sum(
                1
                for item in ledger["entries"]
                if isinstance(item, dict)
                and item.get("command") == command
            )
            entry = {
                "entry_id": entry_id,
                "helixharness_version": PLUGIN_VERSION,
                "command": str(command),
                "stage_kind": stage_kind,
                "attempt": attempt,
                "trigger": str(trigger),
                "platform": str(platform),
                "session_hash": session_hash,
                "started_at": started,
                "ended_at": started,
                "status": "in_progress",
                "end_reason": None,
                "actor": _git_actor(repo),
                "checkpoint": checkpoint,
                "artifacts": [],
                "quality": {},
            }
            ledger["entries"].append(entry)
        else:
            entry = existing
        ledger["last_updated"] = started
        _atomic_write(ledger_path(repo, name), ledger)
        locator = {
            "entry_id": entry_id,
            "feature": name,
            "requirement_id": ledger["requirement"]["id"],
            "requirement_name": ledger["requirement"]["name"],
            "platform": str(platform),
            "raw_session_id": str(raw_session_id or "unknown"),
            "session_log_path": str(session_log_path) if session_log_path else None,
            "started_at": started,
            "ended_at": started,
            "consent_epoch": str(consent_epoch),
            "code_context": code_context,
        }
        locator_status = "recorded"
        try:
            _atomic_write(_locator_path(repo, entry_id), locator)
            _atomic_write(_state_path(repo, entry_id), {**locator, "finished": False})
            _ensure_gitignore(repo)
        except (OSError, LedgerError):
            locator_status = "unavailable"
    return {
        "path": str(ledger_path(repo, name)),
        "entry_id": entry_id,
        "requirement_id": ledger["requirement"]["id"],
        "stage_status": entry["status"],
        "idempotent": existing is not None,
        "session_locator_status": locator_status,
    }


def _ensure_gitignore(repo: Path) -> None:
    path = repo / ".gitignore"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    changed = False
    for rule in RUNTIME_GITIGNORE_RULES:
        if rule not in lines:
            lines.append(rule)
            changed = True
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stage_artifacts(repo: Path, feature: str, stage_kind: str) -> list[str]:
    if stage_kind == "requirement":
        root = Path("docs") / "specs" / "active" / feature
        names = ("spec.md", "tasks.md")
    elif stage_kind == "coding":
        root = Path("docs") / "specs" / "active" / feature
        names = ("verify-report.md",)
    elif stage_kind == "archive":
        root = Path("docs") / "specs" / "completed" / feature
        names = ("spec.md", "tasks.md", "verify-report.md")
    else:
        return []
    return [
        (root / name).as_posix()
        for name in names
        if (repo / root / name).is_file() and not (repo / root / name).is_symlink()
    ]


def finish_stage(
    repo: Path,
    entry_id: str,
    *,
    status: str,
    ended_at: str | None = None,
    failure_code: str | None = None,
    quality: dict[str, Any] | None = None,
    implementation_commit_id: str | None = None,
    review_fixed_point: str | None = None,
    review_head: str | None = None,
    review_scope_sha256: str | None = None,
    archive_scope_sha256: str | None = None,
    reviewed_paths: list[str] | None = None,
) -> dict[str, Any]:
    if status not in STATUSES - {"in_progress"}:
        raise LedgerError("invalid terminal status")
    repo = Path(repo).resolve()
    confirmed_commit = _validated_commit(repo, implementation_commit_id)
    confirmed_scope = _validated_review_scope(
        repo,
        review_fixed_point=review_fixed_point,
        review_head=review_head,
        review_scope_sha256=review_scope_sha256,
        archive_scope_sha256=archive_scope_sha256,
        reviewed_paths=reviewed_paths,
    )
    state = _load_json(_state_path(repo, entry_id))
    if state is None:
        raise LedgerError("stage state not found")
    feature = _feature(state.get("feature"))
    ended = ended_at or _now()
    with _lock(repo):
        ledger = _ensure_ledger_locked(repo, feature, now=ended)
        entry = next((item for item in ledger["entries"] if item.get("entry_id") == entry_id), None)
        if entry is None:
            raise LedgerError("ledger entry not found")
        archive_completion = entry.get("stage_kind") == "archive" and status == "completed"
        if ledger.get("status") == "archived":
            if archive_completion and entry.get("status") == "completed":
                load_collection_seal(repo, feature)
                try:
                    _write_collection_inputs(repo, ledger)
                except (OSError, LedgerError):
                    pass
                return {
                    "path": str(ledger_path(repo, feature)),
                    "entry_id": entry_id,
                    "stage_status": entry["status"],
                    "idempotent": True,
                }
            raise LedgerError("archived Lite requirement telemetry is sealed")
        if entry.get("status") != "in_progress":
            return {"path": str(ledger_path(repo, feature)), "entry_id": entry_id, "stage_status": entry["status"], "idempotent": True}
        entry.update(
            {
                "ended_at": ended,
                "status": status,
                "end_reason": failure_code or ("workflow_completed" if status == "completed" else f"workflow_{status}"),
                "artifacts": _stage_artifacts(repo, feature, str(entry.get("stage_kind") or "")),
                "quality": dict(quality or {}),
            }
        )
        checkpoint = entry.get("checkpoint") if isinstance(entry.get("checkpoint"), dict) else {}
        observed = _code_checkpoint(repo)
        private_context = state.get("code_context") if isinstance(state.get("code_context"), dict) else {}
        source_branch = (
            private_context.get("source_branch")
            or checkpoint.get("source_branch")
            or observed["source_branch"]
        )
        target_branch = (
            observed["target_branch"]
            or private_context.get("target_branch")
            or checkpoint.get("target_branch")
        )
        merge_base_commit = (
            private_context.get("merge_base_commit")
            or checkpoint.get("merge_base_commit")
            or observed["merge_base_commit"]
        )
        checkpoint["head_commit"] = observed["base_commit"]
        checkpoint["working_tree_dirty"] = observed["working_tree_dirty"]
        for optional in (
            "source_branch",
            "target_branch",
            "merge_base_commit",
            "implementation_commit_id",
            "code_resolution",
        ):
            checkpoint.pop(optional, None)
        if confirmed_scope is not None:
            checkpoint.update(confirmed_scope)
        if confirmed_commit:
            checkpoint["implementation_commit_id"] = confirmed_commit
            checkpoint["code_resolution"] = "commit"
        elif observed["working_tree_dirty"] is False and observed["base_commit"]:
            checkpoint["implementation_commit_id"] = observed["base_commit"]
            checkpoint["code_resolution"] = "commit"
        elif (
            source_branch
            and source_branch != target_branch
            and merge_base_commit
        ):
            checkpoint["source_branch"] = source_branch
            checkpoint["target_branch"] = target_branch
            checkpoint["merge_base_commit"] = merge_base_commit
            checkpoint["code_resolution"] = "branch"
        else:
            checkpoint["code_resolution"] = "unavailable"
        entry["checkpoint"] = checkpoint
        ledger["last_updated"] = ended
        if archive_completion:
            ledger["status"] = "archived"
            _write_collection_seal(
                repo,
                ledger,
                archive_entry_id=entry_id,
                sealed_at=ended,
            )
        _atomic_write(ledger_path(repo, feature), ledger)
        locator_status = "recorded"
        try:
            _write_collection_inputs(
                repo,
                ledger,
                entry_ids=None if archive_completion else {entry_id},
            )
            locator = _load_json(_locator_path(repo, entry_id)) or {}
            locator["ended_at"] = ended
            _atomic_write(_locator_path(repo, entry_id), locator)
            _atomic_write(_state_path(repo, entry_id), {**locator, "finished": True, "stage_status": status})
        except (OSError, LedgerError):
            locator_status = "unavailable"
    return {
        "path": str(ledger_path(repo, feature)),
        "entry_id": entry_id,
        "stage_status": status,
        "idempotent": False,
        "session_locator_status": locator_status,
}


def _source_boundary(
    entry: dict[str, Any], locator: dict[str, Any] | None
) -> dict[str, Any]:
    entry_id = str(entry.get("entry_id") or "")
    boundary: dict[str, Any] = {
        "entry_id": entry_id,
        "started_at": entry.get("started_at"),
        "ended_at": entry.get("ended_at"),
        "source_status": "unavailable_at_archive",
        "source_limit_bytes": None,
        "source_fingerprint": None,
    }
    if not locator:
        return boundary
    try:
        path = Path(str(locator.get("session_log_path") or "")).expanduser()
        if path.is_symlink() or not path.is_file():
            return boundary
        stat = path.stat()
    except (OSError, ValueError):
        return boundary
    snapshot = f"{entry_id}\0{stat.st_size}\0{stat.st_mtime_ns}"
    boundary.update(
        {
            "source_status": "recorded",
            "source_limit_bytes": stat.st_size,
            "source_fingerprint": "sealed-" + hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
        }
    )
    return boundary


def _write_collection_seal(
    repo: Path,
    ledger: dict[str, Any],
    *,
    archive_entry_id: str,
    sealed_at: str,
) -> None:
    path = _collection_seal_path(repo, str(ledger["feature"]))
    existing = _load_json(path)
    if existing is not None:
        if (
            existing.get("schema") != COLLECTION_SEAL_SCHEMA
            or existing.get("feature") != ledger.get("feature")
            or existing.get("requirement_id") != ledger.get("requirement", {}).get("id")
            or existing.get("archive_entry_id") != archive_entry_id
        ):
            raise LedgerError("existing Lite collection seal does not match archive")
        return
    entries = [
        _source_boundary(
            entry,
            _load_json(_locator_path(repo, str(entry.get("entry_id") or ""))),
        )
        for entry in ledger.get("entries", [])
        if isinstance(entry, dict) and ENTRY_ID.fullmatch(str(entry.get("entry_id") or ""))
    ]
    seal = {
        "schema": COLLECTION_SEAL_SCHEMA,
        "feature": ledger["feature"],
        "requirement_id": ledger["requirement"]["id"],
        "archive_entry_id": archive_entry_id,
        "sealed_at": sealed_at,
        "entries": entries,
    }
    _atomic_write(path, seal)


def _write_collection_inputs(
    repo: Path,
    ledger: dict[str, Any],
    *,
    entry_ids: set[str] | None = None,
) -> None:
    boundaries: dict[str, dict[str, Any]] = {}
    if ledger.get("status") == "archived":
        seal = load_collection_seal(repo, str(ledger["feature"]))
        boundaries = {
            str(item.get("entry_id")): dict(item)
            for item in seal.get("entries", [])
            if isinstance(item, dict) and ENTRY_ID.fullmatch(str(item.get("entry_id") or ""))
        }
    requirement = ledger.get("requirement")
    if not isinstance(requirement, dict):
        raise LedgerError("Lite collection input requirement is unavailable")
    for entry in ledger.get("entries", []):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("entry_id") or "")
        if not ENTRY_ID.fullmatch(entry_id) or (
            entry_ids is not None and entry_id not in entry_ids
        ):
            continue
        payload = {
            "schema": COLLECTION_INPUT_SCHEMA,
            "feature": ledger["feature"],
            "requirement": {
                "id": requirement.get("id"),
                "name": requirement.get("name"),
            },
            "ledger_status": ledger.get("status"),
            "entry": dict(entry),
            "sealed_boundary": boundaries.get(entry_id),
        }
        _atomic_write(_collection_input_path(repo, entry_id), payload)


def load_collection_seal(repo: Path, feature: str) -> dict[str, Any]:
    seal = _load_json(_collection_seal_path(Path(repo).resolve(), feature))
    if seal is None or seal.get("schema") != COLLECTION_SEAL_SCHEMA:
        raise LedgerError("archived Lite collection seal is unavailable")
    if seal.get("feature") != _feature(feature):
        raise LedgerError("Lite collection seal feature does not match")
    return seal


def load_collection_input(repo: Path, entry_id: str) -> dict[str, Any] | None:
    value = _load_json(_collection_input_path(Path(repo).resolve(), entry_id))
    if value is None:
        return None
    if value.get("schema") != COLLECTION_INPUT_SCHEMA:
        raise LedgerError("Lite collection input schema is invalid")
    feature = _feature(value.get("feature"))
    requirement = value.get("requirement")
    entry = value.get("entry")
    if (
        not isinstance(requirement, dict)
        or not REQUIREMENT_ID.fullmatch(str(requirement.get("id") or ""))
        or not isinstance(requirement.get("name"), str)
        or not requirement.get("name")
    ):
        raise LedgerError("Lite collection input requirement is invalid")
    if (
        not isinstance(entry, dict)
        or entry.get("entry_id") != entry_id
    ):
        raise LedgerError("Lite collection input entry is invalid")
    if value.get("ledger_status") not in {"in_progress", "archived"}:
        raise LedgerError("Lite collection input ledger status is invalid")
    boundary = value.get("sealed_boundary")
    if value.get("ledger_status") == "archived":
        if not isinstance(boundary, dict) or boundary.get("entry_id") != entry_id:
            raise LedgerError("Lite collection input seal boundary is invalid")
    elif boundary is not None:
        raise LedgerError("unarchived Lite collection input cannot be sealed")
    return {
        **value,
        "feature": feature,
        "requirement": dict(requirement),
        "entry": dict(entry),
        "sealed_boundary": dict(boundary) if isinstance(boundary, dict) else None,
    }


def load_locator(repo: Path, entry_id: str) -> dict[str, Any]:
    locator = _load_json(_locator_path(Path(repo).resolve(), entry_id))
    if locator is None:
        raise LedgerError("stage locator not found")
    return locator


def cleanup_delivered_entry(repo: Path, entry_id: str, *, feature: str) -> int:
    repo = Path(repo).resolve()
    name = _feature(feature)
    if not ENTRY_ID.fullmatch(str(entry_id or "")):
        raise LedgerError("invalid entry id")
    removed = 0
    with _lock(repo):
        state = _state_path(repo, entry_id)
        for path in (
            _locator_path(repo, entry_id),
            state,
            state.with_suffix(".collected"),
            state.with_suffix(".quarantined"),
            _collection_input_path(repo, entry_id),
            _managed(repo, PRIVATE_ROOT / "traces" / f"{entry_id}.json"),
        ):
            if path.is_file():
                path.unlink()
                removed += 1
        input_root = _managed(repo, PRIVATE_ROOT / "inputs")
        feature_has_inputs = False
        if input_root.is_dir():
            for path in input_root.glob("entry-*.json"):
                try:
                    value = _load_json(path) or {}
                except LedgerError:
                    continue
                if value.get("feature") == name:
                    feature_has_inputs = True
                    break
        if not feature_has_inputs:
            seal = _collection_seal_path(repo, name)
            if seal.is_file():
                seal.unlink()
                removed += 1
            try:
                seal.parent.rmdir()
            except OSError:
                pass
    return removed


def purge_private(repo: Path, *, platform: str | None = None) -> int:
    repo = Path(repo).resolve()
    removed = 0
    trace_entry_ids: set[str] = set()
    with _lock(repo):
        for directory_name in ("locators", "stages", "inputs"):
            directory = _managed(repo, PRIVATE_ROOT / directory_name)
            if not directory.is_dir():
                continue
            for path in directory.glob("entry-*.json"):
                try:
                    value = _load_json(path) or {}
                except LedgerError:
                    value = {}
                entry = value.get("entry") if isinstance(value.get("entry"), dict) else {}
                item_platform = value.get("platform") or entry.get("platform")
                if platform and item_platform not in {None, platform}:
                    continue
                trace_entry_ids.add(path.stem)
                path.unlink(missing_ok=True)
                path.with_suffix(".collected").unlink(missing_ok=True)
                path.with_suffix(".quarantined").unlink(missing_ok=True)
                removed += 1
        trace_directory = _managed(repo, PRIVATE_ROOT / "traces")
        if trace_directory.is_dir():
            trace_paths = (
                [trace_directory / f"{entry_id}.json" for entry_id in trace_entry_ids]
                if platform
                else list(trace_directory.glob("entry-*.json"))
            )
            for path in trace_paths:
                path.unlink(missing_ok=True)
    return removed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    for name in ("feature", "command-name", "platform", "trigger", "consent-epoch"):
        start.add_argument(f"--{name}", required=True)
    start.add_argument("--session-id")
    start.add_argument("--session-log-path")
    finish = sub.add_parser("finish")
    finish.add_argument("--entry-id", required=True)
    finish.add_argument("--status", required=True)
    finish.add_argument("--failure-code")
    finish.add_argument("--implementation-commit")
    return parser


def main() -> int:
    args = _parser().parse_args()
    repo = Path(args.repo_root).expanduser().resolve()
    try:
        if args.command == "start":
            result = start_stage(
                repo,
                feature=args.feature,
                command=args.command_name,
                platform=args.platform,
                trigger=args.trigger,
                consent_epoch=args.consent_epoch,
                raw_session_id=args.session_id,
                session_log_path=args.session_log_path,
            )
        else:
            result = finish_stage(
                repo,
                args.entry_id,
                status=args.status,
                failure_code=args.failure_code,
                implementation_commit_id=args.implementation_commit,
            )
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
        return 0
    except (LedgerError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
