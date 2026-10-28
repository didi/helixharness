#!/usr/bin/env python3
"""Deterministically resolve and persist HelixHarness telemetry consent."""

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

from telemetry_hosts import get_host_adapter, normalize_host_id, supported_host_ids
import eval_json
from telemetry_async import endpoint_skip


SCHEMA_VERSION = "1.0"
PROJECT_PREFIX = "prj_"
DISABLED_REMINDER_DAYS = 30
ENABLED_REMINDER_DAYS = 180
VALID_MODES = {"enabled", "disabled"}
VALID_CONSENT_SOURCES = {"interactive-user", "explicit-command"}
VALID_INTERACTIVE_ACTIONS = {
    "enable_project",
    "keep_disabled",
    "enable_user",
    "keep_enabled",
    "disable_project",
    "disable_user",
}
PROJECT_KEY_PATTERN = re.compile(r"^prj_[0-9a-f]{16}$")
CONSENT_EPOCH_PATTERN = re.compile(r"^cep_[0-9a-f]{24}$")
SCOPE_TIME_FIELDS = {"enabled_at", "disabled_at", "last_prompted_at", "next_prompt_after"}

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class ConsentError(ValueError):
    """Raised when consent state cannot be resolved or persisted safely."""


class ConsentLeaseUnavailable(ConsentError):
    """Raised when an enabled consent epoch cannot be leased atomically."""


class ConsentCacheUnreadable(ConsentError):
    """Raised when an existing consent cache cannot be trusted as v1 state."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip()


def _repo_root(value: str) -> Path:
    candidate = Path(value).expanduser().resolve()
    root = _git(candidate, "rev-parse", "--show-toplevel")
    if root:
        return Path(root).resolve()
    for current in (candidate, *candidate.parents):
        if (current / "helixharness.workspace.yaml").is_file():
            return current
    raise ConsentError(f"not a git repository or HelixHarness workspace: {candidate}")


def _normalized_remote(value: str) -> str:
    remote = value.strip()
    if not remote:
        return ""

    if "://" not in remote and ":" in remote and not remote.startswith(("/", "./", "../")):
        authority, path = remote.split(":", 1)
        host = authority.rsplit("@", 1)[-1].lower()
        normalized_path = path
    else:
        parsed = urlparse(remote)
        if parsed.scheme and parsed.hostname:
            host = parsed.hostname.lower()
            default_port = {"http": 80, "https": 443, "ssh": 22, "git": 9418}.get(parsed.scheme.lower())
            try:
                port = parsed.port
            except ValueError:
                return f"opaque:{remote}"
            if port and port != default_port:
                host = f"{host}:{port}"
            normalized_path = unquote(parsed.path)
        else:
            return f"local:{Path(remote).expanduser().resolve()}"

    normalized_path = normalized_path.strip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    return f"remote:{host}/{normalized_path}"


def project_fingerprint(repo: Path) -> str:
    remote = _git(repo, "remote", "get-url", "origin")
    identity = _normalized_remote(remote) if remote else f"local:{repo.resolve()}"
    digest = hashlib.sha256(f"helixharness-project-v1\0{identity}".encode()).hexdigest()[:16]
    return f"{PROJECT_PREFIX}{digest}"


def _platform(value: str | None) -> str:
    raw = value or os.environ.get("HELIXHARNESS_PLATFORM")
    if not raw:
        raise ConsentError("platform is required")
    try:
        return normalize_host_id(raw)
    except ValueError as exc:
        raise ConsentError(str(exc)) from exc


def _cache_path(platform: str, override: str | None) -> Path:
    adapter = get_host_adapter(platform)
    if adapter is None:
        raise ConsentError(f"telemetry host is not supported: {platform}")
    return adapter.consent_cache_path(override)


def _unsupported_result(repo: Path, platform: str) -> dict[str, Any]:
    """Return a non-prompting, non-writing result for an unregistered host."""
    del repo
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "cache_path": None,
        "project_fingerprint": None,
        "consent_epoch": None,
        "enabled_at": None,
        "disabled_at": None,
        "effective_mode": "disabled",
        "effective_scope": "unsupported_host",
        "state": "unsupported-host",
        "last_prompted_at": None,
        "next_prompt_after": None,
        "prompt_due": False,
        "prompt_kind": None,
        "allowed_actions": [],
        "reason": "unsupported_host",
        "warning": "WARN:telemetry_host_unsupported",
        "supported_hosts": list(supported_host_ids()),
    }


def _empty_cache(now: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": _timestamp(now),
        "user": {},
        "projects": {},
    }


def _normalized_scope(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("mode") not in VALID_MODES:
        return {}
    normalized = {"mode": data["mode"]}
    if data["mode"] == "enabled":
        source = data.get("consent_source")
        if source not in VALID_CONSENT_SOURCES:
            return {}
        normalized["consent_source"] = source
        epoch = data.get("consent_epoch")
        if not isinstance(epoch, str) or not CONSENT_EPOCH_PATTERN.fullmatch(epoch):
            return {}
        normalized["consent_epoch"] = epoch
    for field in SCOPE_TIME_FIELDS:
        if _parse_timestamp(data.get(field)) is not None:
            normalized[field] = data[field]
    return normalized


def _validated_cached_scope(data: Any, label: str, *, allow_empty: bool) -> dict[str, Any]:
    if allow_empty and data == {}:
        return {}
    if not isinstance(data, dict):
        raise ConsentError(f"invalid consent cache scope: {label}")
    mode = data.get("mode")
    required = {
        "enabled": {
            "mode",
            "consent_source",
            "consent_epoch",
            "enabled_at",
            "last_prompted_at",
            "next_prompt_after",
        },
        "disabled": {
            "mode",
            "disabled_at",
            "last_prompted_at",
            "next_prompt_after",
        },
    }.get(mode)
    if required is None or set(data) != required:
        raise ConsentError(f"invalid consent cache scope fields: {label}")
    normalized = _normalized_scope(data)
    if not normalized or any(
        _parse_timestamp(data.get(field)) is None
        for field in required & SCOPE_TIME_FIELDS
    ):
        raise ConsentError(f"invalid consent cache scope values: {label}")
    return normalized


def _load_cache(path: Path, now: datetime) -> dict[str, Any]:
    if not path.exists():
        return _empty_cache(now)
    try:
        data = eval_json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsentCacheUnreadable(f"invalid consent cache: {path}") from exc
    if not isinstance(data, dict):
        raise ConsentCacheUnreadable(f"invalid consent cache: {path}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ConsentCacheUnreadable(
            f"unsupported consent cache schema: {data.get('schema_version')}"
        )
    if set(data) != {"schema_version", "updated_at", "user", "projects"}:
        raise ConsentCacheUnreadable(f"invalid consent cache fields: {path}")
    if _parse_timestamp(data.get("updated_at")) is None:
        raise ConsentCacheUnreadable(f"invalid consent cache updated_at: {path}")
    if not isinstance(data.get("user"), dict) or not isinstance(data.get("projects"), dict):
        raise ConsentCacheUnreadable(f"invalid consent cache structure: {path}")
    projects: dict[str, dict[str, Any]] = {}
    for key, value in data["projects"].items():
        if not isinstance(key, str) or not PROJECT_KEY_PATTERN.fullmatch(key):
            raise ConsentCacheUnreadable(f"invalid consent cache project key: {key}")
        try:
            projects[key] = _validated_cached_scope(
                value, f"projects.{key}", allow_empty=False
            )
        except ConsentError as exc:
            raise ConsentCacheUnreadable(str(exc)) from exc
    try:
        user = _validated_cached_scope(data["user"], "user", allow_empty=True)
    except ConsentError as exc:
        raise ConsentCacheUnreadable(str(exc)) from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": data["updated_at"],
        "user": user,
        "projects": projects,
    }


def _purge_current_repo_private_state(repo: Path, platform: str) -> None:
    from metrics import purge_private_states
    purge_private_states(repo, platform)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _acquire_posix_flock(
    file_descriptor: int,
    *,
    shared: bool,
    deadline: float | None,
) -> None:
    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    if deadline is not None:
        operation |= fcntl.LOCK_NB
    while True:
        try:
            fcntl.flock(file_descriptor, operation)
            return
        except (BlockingIOError, OSError) as exc:
            if deadline is None:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConsentError("consent cache lock timed out") from exc
            time.sleep(min(0.05, remaining))


@contextmanager
def _cache_lock(
    path: Path,
    *,
    shared: bool = False,
    timeout_sec: float | None = None,
) -> Iterator[None]:
    """Lock the cache, optionally bounding interactive hook wait time."""
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    deadline = (
        time.monotonic() + timeout_sec
        if timeout_sec is not None
        else None
    )

    if os.name != "nt" and not lock_path.is_file():
        # The parent of the host config root is a stable, read-only anchor for
        # the first reader/writer race and a cache-without-lock recovery.
        anchor_path = path.parent.parent.parent
        anchor_fd = os.open(anchor_path, os.O_RDONLY)
        try:
            _acquire_posix_flock(anchor_fd, shared=shared, deadline=deadline)
            try:
                if lock_path.is_file():
                    with _cache_lock(
                        path,
                        shared=shared,
                        timeout_sec=(
                            max(0.0, deadline - time.monotonic())
                            if deadline is not None
                            else None
                        ),
                    ):
                        yield
                elif shared:
                    # Keep the anchor lease through the caller's repo privacy
                    # barrier. A first writer waits until that barrier ends.
                    yield
                else:
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    with lock_path.open("a+") as handle:
                        _acquire_posix_flock(
                            handle.fileno(), shared=False, deadline=deadline
                        )
                        try:
                            yield
                        finally:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                fcntl.flock(anchor_fd, fcntl.LOCK_UN)
        finally:
            os.close(anchor_fd)
        return

    read_existing = shared and os.name != "nt"
    if not lock_path.parent.exists():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    # A POSIX shared flock only needs a readable descriptor. Opening an
    # existing lock with a write-capable mode makes otherwise read-only
    # consent resolution fail in sandboxed hosts.
    with lock_path.open("r" if read_existing else "a+") as handle:
        if os.name == "nt":
            while True:
                try:
                    handle.seek(0)
                    if not handle.read(1):
                        handle.write("0")
                        handle.flush()
                    handle.seek(0)
                    mode = msvcrt.LK_NBLCK if deadline is not None else msvcrt.LK_LOCK
                    msvcrt.locking(handle.fileno(), mode, 1)
                    break
                except OSError as exc:
                    if deadline is None:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ConsentError("consent cache lock timed out") from exc
                    time.sleep(min(0.05, remaining))
        else:
            _acquire_posix_flock(
                handle.fileno(), shared=shared, deadline=deadline
            )
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _valid_scope(data: Any) -> dict[str, Any] | None:
    normalized = _normalized_scope(data)
    return normalized or None


def _allowed_actions(
    effective_mode: str,
    effective_scope: str,
    prompt_due: bool,
) -> tuple[str | None, list[dict[str, Any]]]:
    if not prompt_due:
        return None, []
    if effective_mode == "enabled":
        return "renew", [
            {
                "id": "keep_enabled",
                "label": "保持当前统计范围开启",
                "description": "继续按当前生效范围记录需求级使用统计。",
                "scope": effective_scope,
                "mode": "enabled",
                "recommended": True,
            },
            {
                "id": "disable_project",
                "label": "关闭当前项目统计",
                "description": "仅当前项目停止写入新的 docs/eval/** 使用记录。",
                "scope": "project",
                "mode": "disabled",
                "recommended": False,
            },
            {
                "id": "disable_user",
                "label": "关闭我本人统计",
                "description": "本机所有 HelixHarness 项目停止写入新的使用记录。",
                "scope": "user",
                "mode": "disabled",
                "recommended": False,
            },
        ]
    if effective_scope == "user":
        return "enable", [
            {
                "id": "enable_user",
                "label": "开启我本人统计（推荐，本机所有项目）",
                "description": "为本机所有 HelixHarness 项目默认开启需求级使用统计。",
                "scope": "user",
                "mode": "enabled",
                "recommended": True,
            },
            {
                "id": "keep_disabled",
                "label": "保持关闭",
                "description": "不写使用记录，业务流程正常继续。",
                "scope": "user",
                "mode": "disabled",
                "recommended": False,
            },
        ]
    return "enable", [
        {
            "id": "enable_project",
            "label": "开启当前项目统计（推荐）",
            "description": "仅为当前项目记录 docs/eval/** 需求级使用统计。",
            "scope": "project",
            "mode": "enabled",
            "recommended": True,
        },
        {
            "id": "enable_user",
            "label": "开启我本人统计（本机所有项目）",
            "description": "为本机所有 HelixHarness 项目默认开启需求级使用统计。",
            "scope": "user",
            "mode": "enabled",
            "recommended": False,
        },
        {
            "id": "keep_disabled",
            "label": "保持关闭",
            "description": "不写使用记录，业务流程正常继续。",
            "scope": "project",
            "mode": "disabled",
            "recommended": False,
        },
    ]


def resolve(repo: Path, platform: str, cache_path: Path, now: datetime | None = None) -> dict[str, Any]:
    if get_host_adapter(platform) is None:
        return _unsupported_result(repo, platform)
    current = now or _now()
    cache = _load_cache(cache_path, current)
    fingerprint = project_fingerprint(repo)
    user = _valid_scope(cache.get("user"))
    project = _valid_scope(cache.get("projects", {}).get(fingerprint))

    if user and user["mode"] == "disabled":
        selected = user
        effective_mode = "disabled"
        effective_scope = "user"
        state = "disabled"
    elif project:
        selected = project
        effective_mode = project["mode"]
        effective_scope = "project"
        state = effective_mode
    elif user:
        selected = user
        effective_mode = user["mode"]
        effective_scope = "user"
        state = effective_mode
    else:
        selected = {}
        effective_mode = "disabled"
        effective_scope = "default"
        state = "default-disabled"

    next_prompt = _parse_timestamp(selected.get("next_prompt_after"))
    endpoint_gate = endpoint_skip()
    # Consent covers the local ledger too; endpoint readiness only gates async work.
    prompt_due = next_prompt is None or current >= next_prompt
    prompt_kind, allowed_actions = _allowed_actions(
        effective_mode,
        effective_scope,
        prompt_due,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "cache_path": str(cache_path),
        "project_fingerprint": fingerprint,
        "consent_epoch": selected.get("consent_epoch") if effective_mode == "enabled" else None,
        "enabled_at": selected.get("enabled_at"),
        "disabled_at": selected.get("disabled_at"),
        "effective_mode": effective_mode,
        "effective_scope": effective_scope,
        "state": state,
        "last_prompted_at": selected.get("last_prompted_at"),
        "next_prompt_after": selected.get("next_prompt_after"),
        "prompt_due": prompt_due,
        **({key: value for key, value in endpoint_gate.items() if key != "status"} if endpoint_gate else {}),
        "prompt_kind": prompt_kind,
        "allowed_actions": allowed_actions,
    }


def resolve_for_platform(
    repo: Path,
    platform: str,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve consent, default-disabled without I/O for an unregistered host."""
    normalized_platform = _platform(platform)
    if get_host_adapter(normalized_platform) is None:
        return _unsupported_result(Path(repo), normalized_platform)
    normalized_repo = _repo_root(str(repo))
    path = cache_path or _cache_path(normalized_platform, None)
    return resolve(normalized_repo, normalized_platform, path, now)


@contextmanager
def consent_snapshot(
    repo: Path,
    platform: str,
    cache_path: Path | str | None = None,
    *,
    lock_timeout_sec: float | None = None,
) -> Iterator[dict[str, Any]]:
    """Resolve and hold one immutable consent-cache snapshot during local I/O.

    POSIX snapshots are shared readers and ``set_mode`` is the exclusive writer.
    Windows has no shared primitive here and conservatively serializes both.
    """
    normalized_platform = _platform(platform)
    if get_host_adapter(normalized_platform) is None:
        raise ConsentError(f"telemetry host is not supported: {normalized_platform}")
    normalized_repo = _repo_root(str(repo))
    path = (
        Path(cache_path).expanduser().resolve()
        if cache_path is not None
        else _cache_path(normalized_platform, None)
    )
    # POSIX readers share this lease, so an archive does not serialize normal
    # stage writes in unrelated repositories. Windows file locking has no
    # shared equivalent here and intentionally remains conservative/exclusive.
    try:
        with _cache_lock(path, shared=True, timeout_sec=lock_timeout_sec):
            yield resolve(normalized_repo, normalized_platform, path)
    except OSError as exc:
        raise ConsentError("consent snapshot lock is unavailable") from exc


@contextmanager
def consent_lease(
    repo: Path,
    platform: str,
    expected_consent_epoch: str,
    cache_path: Path | str | None = None,
) -> Iterator[dict[str, Any]]:
    """Hold an enabled exact-epoch snapshot for telemetry reads and writes.

    Readers share the POSIX cache lock while consent changes are exclusive, so
    either a change wins first and rejects this lease before telemetry I/O, or
    it waits until every current reader has stopped. Windows conservatively
    serializes readers and writers.
    """
    epoch = str(expected_consent_epoch or "").strip()
    if not CONSENT_EPOCH_PATTERN.fullmatch(epoch):
        raise ConsentLeaseUnavailable("valid expected consent epoch is required")
    with consent_snapshot(repo, platform, cache_path) as state:
        if (
            state.get("effective_mode") != "enabled"
            or state.get("consent_epoch") != epoch
        ):
            raise ConsentLeaseUnavailable("consent epoch is no longer enabled")
        yield state


def _mutate_mode_locked(
    repo: Path,
    platform: str,
    cache_path: Path,
    cache: dict[str, Any],
    fingerprint: str,
    scope: str,
    mode: str,
    consent_source: str | None,
    current: datetime,
) -> str | None:
    """Mutate one validated cache while its exclusive lock is held."""
    user = _valid_scope(cache.get("user"))
    if scope == "project" and mode == "enabled" and user and user["mode"] == "disabled":
        _purge_current_repo_private_state(repo, platform)
        return "WARN:telemetry_user_scope_disabled"

    reminder_days = ENABLED_REMINDER_DAYS if mode == "enabled" else DISABLED_REMINDER_DAYS
    state = {
        "mode": mode,
        "last_prompted_at": _timestamp(current),
        "next_prompt_after": _timestamp(current + timedelta(days=reminder_days)),
        f"{mode}_at": _timestamp(current),
    }
    if mode == "enabled":
        state["consent_source"] = consent_source
        state["consent_epoch"] = "cep_" + secrets.token_hex(12)
    if scope == "user":
        # A user-wide choice starts a new global consent generation.
        projects = cache.get("projects") if isinstance(cache.get("projects"), dict) else {}
        cache["projects"] = {
            key: value
            for key, value in projects.items()
            if _valid_scope(value) and value.get("mode") == "disabled"
        }
        cache["user"] = state
    else:
        cache.setdefault("projects", {})[fingerprint] = state
    cache["schema_version"] = SCHEMA_VERSION
    cache["updated_at"] = _timestamp(current)
    _atomic_write(cache_path, cache)
    if mode == "disabled":
        _purge_current_repo_private_state(repo, platform)
    return None


def set_mode(
    repo: Path,
    platform: str,
    cache_path: Path,
    scope: str,
    mode: str,
    consent_source: str | None = None,
    user_confirmed: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    try:
        canonical_platform = normalize_host_id(platform)
    except ValueError as exc:
        raise ConsentError(str(exc)) from exc
    if str(platform).strip() != canonical_platform:
        raise ConsentError("telemetry platform must be canonical")
    if get_host_adapter(canonical_platform) is None:
        raise ConsentError(f"telemetry host is not supported: {canonical_platform}")
    platform = canonical_platform
    if scope not in {"project", "user"}:
        raise ConsentError(f"invalid telemetry consent scope: {scope}")
    if mode not in VALID_MODES:
        raise ConsentError(f"invalid telemetry consent mode: {mode}")
    current = now or _now()
    fingerprint = project_fingerprint(repo)
    if mode == "enabled":
        if not user_confirmed:
            raise ConsentError("telemetry enable requires explicit user confirmation")
        if consent_source not in VALID_CONSENT_SOURCES:
            raise ConsentError("telemetry enable requires a valid consent source")
    with _cache_lock(cache_path):
        try:
            cache = _load_cache(cache_path, current)
        except ConsentCacheUnreadable:
            _purge_current_repo_private_state(repo, platform)
            raise
        warning = _mutate_mode_locked(
            repo, platform, cache_path, cache, fingerprint, scope, mode,
            consent_source, current,
        )
    return {
        **resolve(repo, platform, cache_path, current),
        "updated": warning is None,
        "warning": warning,
    }


def apply_interactive_choice(
    repo: Path,
    platform: str,
    cache_path: Path,
    action: str,
    user_confirmed: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    if get_host_adapter(platform) is None:
        raise ConsentError(f"telemetry host is not supported: {platform}")
    if not user_confirmed:
        raise ConsentError("interactive consent choice requires explicit user confirmation")
    if action not in VALID_INTERACTIVE_ACTIONS:
        raise ConsentError(f"invalid interactive consent action: {action}")
    current = now or _now()
    fingerprint = project_fingerprint(repo)
    with _cache_lock(cache_path):
        try:
            cache = _load_cache(cache_path, current)
            state = resolve(repo, platform, cache_path, current)
        except ConsentCacheUnreadable:
            _purge_current_repo_private_state(repo, platform)
            raise
        allowed_ids = {item["id"] for item in state["allowed_actions"]}
        if action not in allowed_ids:
            raise ConsentError(f"interactive consent action is not allowed: {action}")

        warning = None
        if action == "keep_enabled":
            scope = state.get("effective_scope")
            selected = (
                cache.get("user")
                if scope == "user"
                else cache.get("projects", {}).get(fingerprint)
                if scope == "project"
                else None
            )
            selected = _valid_scope(selected)
            if not selected or selected.get("mode") != "enabled":
                raise ConsentError("keep_enabled requires a currently enabled scope")
            refreshed = dict(selected)
            refreshed["last_prompted_at"] = _timestamp(current)
            refreshed["next_prompt_after"] = _timestamp(
                current + timedelta(days=ENABLED_REMINDER_DAYS)
            )
            if scope == "user":
                cache["user"] = refreshed
            else:
                cache.setdefault("projects", {})[fingerprint] = refreshed
            cache["updated_at"] = _timestamp(current)
            _atomic_write(cache_path, cache)
        else:
            if action == "enable_project":
                scope, mode = "project", "enabled"
            elif action == "enable_user":
                scope, mode = "user", "enabled"
            elif action == "disable_user":
                scope, mode = "user", "disabled"
            elif action == "disable_project":
                scope, mode = "project", "disabled"
            else:
                scope = "user" if state["effective_scope"] == "user" else "project"
                mode = "disabled"
            warning = _mutate_mode_locked(
                repo,
                platform,
                cache_path,
                cache,
                fingerprint,
                scope,
                mode,
                "interactive-user" if mode == "enabled" else None,
                current,
            )
    return {
        **resolve(repo, platform, cache_path, current),
        "updated": warning is None,
        "warning": warning,
        "action": action,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=os.getcwd())
    parser.add_argument("--platform", required=True)
    parser.add_argument("--cache-path", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("resolve", help="resolve effective telemetry consent")
    setter = subparsers.add_parser("set", help="persist telemetry consent")
    setter.add_argument("--scope", choices=("project", "user"), required=True)
    setter.add_argument("--mode", choices=sorted(VALID_MODES), required=True)
    setter.add_argument("--source", choices=sorted(VALID_CONSENT_SOURCES))
    setter.add_argument("--user-confirmed", action="store_true")
    chooser = subparsers.add_parser("choose", help="persist an explicit interactive choice")
    chooser.add_argument("--action", choices=sorted(VALID_INTERACTIVE_ACTIONS), required=True)
    chooser.add_argument("--user-confirmed", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    repo: Path | None = None
    try:
        platform = _platform(args.platform)
        if get_host_adapter(platform) is None:
            result = _unsupported_result(Path(args.repo_root), platform)
            if args.command != "resolve":
                result["updated"] = False
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        repo = _repo_root(args.repo_root)
        cache_path = _cache_path(platform, args.cache_path)
        if args.command == "resolve":
            # CLI resolve is an operational repo touch (status/init/propose),
            # unlike the pure resolve() API.  Apply the repo-wide privacy
            # barrier under the same consent snapshot before returning even
            # when effective consent is disabled.
            with consent_snapshot(repo, platform, cache_path) as snapshot:
                if snapshot.get("effective_mode") != "enabled":
                    _purge_current_repo_private_state(repo, platform)
                result = snapshot
        elif args.command == "set":
            result = set_mode(
                repo,
                platform,
                cache_path,
                args.scope,
                args.mode,
                consent_source=args.source,
                user_confirmed=args.user_confirmed,
            )
        else:
            result = apply_interactive_choice(
                repo,
                platform,
                cache_path,
                args.action,
                args.user_confirmed,
            )
    except (ConsentError, OSError) as exc:
        if repo is not None and isinstance(exc, ConsentCacheUnreadable):
            try:
                _purge_current_repo_private_state(repo, platform)
            except ConsentError as cleanup_exc:
                exc = ConsentError(f"{exc}; {cleanup_exc}")
        print(json.dumps({"error": str(exc), "effective_mode": "disabled"}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
