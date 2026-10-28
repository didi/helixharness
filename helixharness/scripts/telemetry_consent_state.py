#!/usr/bin/env python3
"""Read and lease consent for HelixHarness Lite telemetry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

from telemetry_hosts import (
    get_host_adapter,
    normalize_host_id,
    supported_host_ids,
)


SCHEMA_VERSION = "1.0"
PROJECT_KEY_PATTERN = re.compile(r"^prj_[0-9a-f]{16}$")
CONSENT_EPOCH_PATTERN = re.compile(r"^cep_[0-9a-f]{24}$")

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class ConsentError(ValueError):
    pass


class ConsentLeaseUnavailable(ConsentError):
    pass


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.0,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def _repo_root(value: str | Path) -> Path:
    candidate = Path(value).expanduser().resolve()
    root = _git(candidate, "rev-parse", "--show-toplevel")
    if root:
        return Path(root).resolve()
    raise ConsentError(f"not a Git repository: {candidate}")


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
        if not parsed.scheme or not parsed.hostname:
            return f"local:{Path(remote).expanduser().resolve()}"
        host = parsed.hostname.lower()
        normalized_path = unquote(parsed.path)
    normalized_path = normalized_path.strip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    return f"remote:{host}/{normalized_path}"


def project_fingerprint(repo: Path) -> str:
    remote = _git(repo, "remote", "get-url", "origin")
    identity = _normalized_remote(remote) if remote else f"local:{repo.resolve()}"
    digest = hashlib.sha256(f"helixharness-project-v1\0{identity}".encode()).hexdigest()[:16]
    return f"prj_{digest}"


def _scope(value: Any, label: str) -> dict[str, Any] | None:
    if value == {}:
        return None
    if not isinstance(value, dict):
        raise ConsentError(f"invalid consent scope: {label}")
    mode = value.get("mode")
    if mode == "enabled":
        epoch = value.get("consent_epoch")
        if not isinstance(epoch, str) or not CONSENT_EPOCH_PATTERN.fullmatch(epoch):
            raise ConsentError(f"invalid consent epoch: {label}")
    elif mode != "disabled":
        raise ConsentError(f"invalid consent mode: {label}")
    return dict(value)


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "user": {}, "projects": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsentError(f"invalid consent cache: {path}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or not isinstance(value.get("user"), dict)
        or not isinstance(value.get("projects"), dict)
        or _timestamp(value.get("updated_at")) is None
    ):
        raise ConsentError(f"invalid consent cache: {path}")
    projects: dict[str, dict[str, Any]] = {}
    for key, raw in value["projects"].items():
        if not isinstance(key, str) or not PROJECT_KEY_PATTERN.fullmatch(key):
            raise ConsentError(f"invalid consent project key: {key}")
        selected = _scope(raw, f"projects.{key}")
        if selected is None:
            raise ConsentError(f"empty consent project scope: {key}")
        projects[key] = selected
    return {
        "schema_version": SCHEMA_VERSION,
        "user": _scope(value["user"], "user") or {},
        "projects": projects,
    }


def _unsupported(platform: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "effective_mode": "disabled",
        "effective_scope": "unsupported_host",
        "consent_epoch": None,
        "reason": "unsupported_host",
        "supported_hosts": list(supported_host_ids()),
    }


def _resolve(repo: Path, platform: str, path: Path) -> dict[str, Any]:
    cache = _load_cache(path)
    fingerprint = project_fingerprint(repo)
    user = cache["user"] or None
    project = cache["projects"].get(fingerprint)
    if user and user.get("mode") == "disabled":
        selected, scope = user, "user"
    elif project:
        selected, scope = project, "project"
    elif user:
        selected, scope = user, "user"
    else:
        selected, scope = {"mode": "disabled"}, "default"
    enabled = selected.get("mode") == "enabled"
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": platform,
        "cache_path": str(path),
        "project_fingerprint": fingerprint,
        "effective_mode": "enabled" if enabled else "disabled",
        "effective_scope": scope,
        "consent_epoch": selected.get("consent_epoch") if enabled else None,
        "reason": None if enabled else "telemetry_disabled",
    }


def resolve_for_platform(
    repo: Path,
    platform: str,
    cache_path: Path | None = None,
) -> dict[str, Any]:
    canonical = normalize_host_id(platform)
    adapter = get_host_adapter(canonical)
    if adapter is None:
        return _unsupported(canonical)
    root = _repo_root(repo)
    return _resolve(root, canonical, cache_path or adapter.consent_cache_path())


@contextmanager
def _cache_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    if os.name != "nt" and not lock_path.is_file():
        # Coordinate the first reader/writer race without requiring a write
        # inside a sandbox-owned host config directory.
        anchor_path = path.parent.parent.parent
        anchor_fd = os.open(anchor_path, os.O_RDONLY)
        try:
            fcntl.flock(anchor_fd, fcntl.LOCK_SH)
            try:
                if lock_path.is_file():
                    with _cache_lock(path):
                        yield
                else:
                    yield
            finally:
                fcntl.flock(anchor_fd, fcntl.LOCK_UN)
        finally:
            os.close(anchor_fd)
        return

    if not lock_path.parent.exists():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    read_existing = os.name != "nt"
    with lock_path.open("r" if read_existing else "a+") as handle:
        try:
            if os.name == "nt":
                handle.seek(0)
                if not handle.read(1):
                    handle.write("0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def consent_snapshot(repo: Path, platform: str) -> Iterator[dict[str, Any]]:
    canonical = normalize_host_id(platform)
    adapter = get_host_adapter(canonical)
    if adapter is None:
        raise ConsentError(f"Lite telemetry host unsupported: {canonical}")
    root = _repo_root(repo)
    path = adapter.consent_cache_path()
    with _cache_lock(path):
        yield _resolve(root, canonical, path)


@contextmanager
def consent_lease(
    repo: Path, platform: str, expected_consent_epoch: str
) -> Iterator[dict[str, Any]]:
    epoch = str(expected_consent_epoch or "").strip()
    if not CONSENT_EPOCH_PATTERN.fullmatch(epoch):
        raise ConsentLeaseUnavailable("valid Lite consent epoch is required")
    with consent_snapshot(repo, platform) as state:
        if state.get("effective_mode") != "enabled" or state.get("consent_epoch") != epoch:
            raise ConsentLeaseUnavailable("Lite consent epoch is no longer enabled")
        yield state
