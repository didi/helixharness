#!/usr/bin/env python3
"""Deterministic HelixHarness version policy and guarded native updates."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import tempfile
import time
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from source_binding import guard_source
from typing import Any, Iterator

import eval_json
from update_hosts import (
    INSTALL_TIMEOUT_SECONDS,
    MARKETPLACE,
    PLUGIN_ID,
    PLUGIN_NAME,
    BoundedCommandRunner,
    UpdateHostAdapter,
    UpdateHostError,
    build_update_host_adapters,
    detect_update_host_id,
    supported_update_host_ids,
)


CACHE_SCHEMA_VERSION = "1.0"
AUTH_SCHEMA_VERSION = "1.0"
POLICY_SCHEMA_VERSION = "1.0"
SUPPORTED_PLATFORMS = set(supported_update_host_ids())
VALID_CONFIRMATION_SOURCES = {"interactive-user"}
VALID_UPDATE_ACTIONS = {"upgrade_now", "skip_update"}
VALID_UPDATE_STATUSES = {"latest", "recommended", "required", "current-newer"}
AUTH_TTL_MINUTES = 10
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_POLL_SECONDS = 0.02
VOLATILE_NEGATIVE_TTL_MINUTES = 15
SEMVER_PATTERN = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")
CHALLENGE_PATTERN = re.compile(r"^upd_[0-9a-f]{24}$")
PLUGIN_ROOT = Path(__file__).resolve().parent.parent

POLICY_KEYS = {
    "schema_version",
    "plugin",
    "marketplace",
    "latest_version",
    "minimum_supported_version",
    "force_update_below",
    "force_update_message",
    "releases",
}
RELEASE_KEYS = {
    "version",
    "date",
    "update_type",
    "title",
    "changes",
    "restart_required",
}
CACHE_KEYS = {
    "schema_version",
    "platform",
    "plugin_id",
    "checked_at",
    "marketplace",
    "current_version",
    "current_scope",
    "current_source",
    "latest_version",
    "minimum_supported_version",
    "force_update_below",
    "update_status",
    "required_reason",
    "releases",
}
AUTH_KEYS = {
    "schema_version",
    "challenge_id",
    "platform",
    "scope",
    "current_version",
    "target_version",
    "update_status",
    "state",
    "created_at",
    "expires_at",
    "prompt",
    "completed_steps",
}

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class UpdateError(ValueError):
    """Raised when version state or update authorization is invalid."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _semver(value: Any) -> tuple[int, int, int]:
    text = str(value or "").strip()
    match = SEMVER_PATTERN.fullmatch(text)
    if not match:
        raise UpdateError(f"invalid semantic version: {text or '<empty>'}")
    major, minor, patch = match.groups()
    return (
        int(major),
        int(minor),
        int(patch or 0),
    )


def _version_text(value: Any) -> str:
    major, minor, patch = _semver(value)
    return f"{major}.{minor}.{patch}"


def _optional_version(value: Any, field: str) -> str | None:
    if value is None:
        return None
    try:
        return _version_text(value)
    except UpdateError as exc:
        raise UpdateError(f"invalid {field}") from exc


def _platform(value: str | None) -> str:
    try:
        return detect_update_host_id(
            value or os.environ.get("HELIXHARNESS_PLATFORM")
        )
    except UpdateHostError as exc:
        raise UpdateError(str(exc)) from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = eval_json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"invalid JSON file: {path}") from exc
    if not isinstance(value, dict):
        raise UpdateError(f"invalid JSON object: {path}")
    return value


_HOST_ADAPTERS = build_update_host_adapters(_version_text, _read_json)


def _adapter(platform: str) -> UpdateHostAdapter:
    try:
        return _HOST_ADAPTERS[platform]
    except KeyError as exc:
        raise UpdateError(f"unsupported update host: {platform}") from exc


def _unsupported_result(platform: str) -> dict[str, Any]:
    return {
        "state": "unsupported",
        "platform": platform,
        "update_status": "unknown",
        "next_action": "continue_silently",
        "reason": "unsupported_host",
        "supported_hosts": sorted(SUPPORTED_PLATFORMS),
    }


def _cache_path(platform: str, override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return _adapter(platform).state_home() / "helixharness" / "version-check-cache.json"


def _authorization_path(platform: str, override: str | None = None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return _adapter(platform).state_home() / "helixharness" / "version-update-authorization.json"


def _expect_exact_keys(
    value: dict[str, Any],
    required: set[str],
    *,
    optional: set[str] | None = None,
    label: str,
) -> None:
    allowed = required | (optional or set())
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise UpdateError(f"invalid {label}: missing {', '.join(missing)}")
    if unknown:
        raise UpdateError(f"invalid {label}: unknown {', '.join(unknown)}")


def _nonempty_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UpdateError(f"invalid {field}")
    return value.strip()


def _normalized_release(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UpdateError("invalid version policy release")
    _expect_exact_keys(
        value,
        RELEASE_KEYS,
        optional={"required_reason"},
        label="version policy release",
    )
    version = _version_text(value["version"])
    date_text = _nonempty_text(value["date"], "release date")
    try:
        if date.fromisoformat(date_text).isoformat() != date_text:
            raise ValueError
    except ValueError as exc:
        raise UpdateError("invalid release date") from exc
    update_type = value["update_type"]
    if update_type not in {"recommended", "required"}:
        raise UpdateError("invalid release update_type")
    title = _nonempty_text(value["title"], "release title")
    changes = value["changes"]
    if (
        not isinstance(changes, list)
        or not changes
        or any(not isinstance(item, str) or not item.strip() for item in changes)
    ):
        raise UpdateError("invalid release changes")
    if not isinstance(value["restart_required"], bool):
        raise UpdateError("invalid release restart_required")
    reason = value.get("required_reason")
    if update_type == "required":
        reason = _nonempty_text(reason, "release required_reason")
    elif reason is not None:
        raise UpdateError("recommended release cannot define required_reason")
    result: dict[str, Any] = {
        "version": version,
        "date": date_text,
        "update_type": update_type,
        "title": title,
        "changes": [item.strip() for item in changes],
        "restart_required": value["restart_required"],
    }
    if reason is not None:
        result["required_reason"] = reason
    return result


def _load_policy(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    _expect_exact_keys(data, POLICY_KEYS, label="version policy")
    if data["schema_version"] != POLICY_SCHEMA_VERSION:
        raise UpdateError(f"unsupported version policy schema: {data['schema_version']}")
    if data["plugin"] != PLUGIN_NAME or data["marketplace"] != MARKETPLACE:
        raise UpdateError("version policy identity mismatch")
    latest = _version_text(data["latest_version"])
    minimum = _optional_version(data["minimum_supported_version"], "minimum_supported_version")
    force_below = _optional_version(data["force_update_below"], "force_update_below")
    force_message = data["force_update_message"]
    if force_below is not None:
        force_message = _nonempty_text(force_message, "force_update_message")
    elif force_message is not None:
        raise UpdateError("force_update_message requires force_update_below")
    if minimum and _semver(minimum) > _semver(latest):
        raise UpdateError("minimum_supported_version exceeds latest_version")
    if force_below and _semver(force_below) > _semver(latest):
        raise UpdateError("force_update_below exceeds latest_version")
    releases_value = data["releases"]
    if not isinstance(releases_value, list) or not releases_value:
        raise UpdateError("version policy releases must be a non-empty list")
    releases = [_normalized_release(item) for item in releases_value]
    versions = [item["version"] for item in releases]
    if len(versions) != len(set(versions)):
        raise UpdateError("version policy release versions must be unique")
    if versions != sorted(versions, key=_semver, reverse=True):
        raise UpdateError("version policy releases must be newest first")
    if versions[0] != latest:
        raise UpdateError("latest_version must match the first release")
    return {
        "schema_version": POLICY_SCHEMA_VERSION,
        "plugin": PLUGIN_NAME,
        "marketplace": MARKETPLACE,
        "latest_version": latest,
        "minimum_supported_version": minimum,
        "force_update_below": force_below,
        "force_update_message": force_message,
        "releases": releases,
    }


def inspect_formal_installation(
    platform: str,
    repo_root: Path,
    plugin_root: Path = PLUGIN_ROOT,
    plugin_list_data: Any = None,
    *,
    runner: BoundedCommandRunner | None = None,
) -> dict[str, Any]:
    try:
        return _adapter(platform).inspect_installation(
            repo_root,
            plugin_root,
            runner or BoundedCommandRunner(),
            plugin_list_data,
        )
    except UpdateHostError as exc:
        raise UpdateError(str(exc)) from exc


def locate_formal_policy(
    platform: str,
    marketplace_list_data: Any = None,
    *,
    snapshot_root: Path | None = None,
    runner: BoundedCommandRunner | None = None,
) -> Path:
    adapter = _adapter(platform)
    try:
        root = snapshot_root or adapter.marketplace_root(
            runner or BoundedCommandRunner(),
            marketplace_list_data,
        )
    except UpdateHostError as exc:
        raise UpdateError(str(exc)) from exc
    marketplace = _read_json(root / ".claude-plugin" / "marketplace.json")
    plugins = marketplace.get("plugins") if isinstance(marketplace.get("plugins"), list) else []
    matches = [item for item in plugins if isinstance(item, dict) and item.get("name") == PLUGIN_NAME]
    if marketplace.get("name") != MARKETPLACE or len(matches) != 1:
        raise UpdateError("invalid formal HelixHarness marketplace manifest")
    source = matches[0].get("source")
    if not isinstance(source, str) or not source.strip():
        raise UpdateError("formal HelixHarness marketplace source is invalid")
    plugin_root = (root / source).resolve()
    if root != plugin_root and root not in plugin_root.parents:
        raise UpdateError("formal HelixHarness marketplace source escapes its root")
    policy_path = plugin_root / "reference" / "version-policy.json"
    policy = _load_policy(policy_path)
    marketplace_version = _version_text(matches[0].get("version"))
    declared_version = _version_text(adapter.declared_version(plugin_root))
    if len({policy["latest_version"], marketplace_version, declared_version}) != 1:
        raise UpdateError("formal HelixHarness marketplace versions are inconsistent")
    return policy_path


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
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _durable_unlink(path: Path) -> None:
    path.unlink()
    if os.name != "nt":
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


@contextmanager
def _lock(path: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    lock_path = path.with_suffix(f"{path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with lock_path.open("a+") as handle:
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    if not handle.read(1):
                        handle.write("0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    raise UpdateError(f"timed out waiting for update state lock: {lock_path}") from exc
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_cached_releases(
    value: Any,
    current_version: str,
    latest_version: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise UpdateError("invalid version cache releases")
    releases = [_normalized_release(item) for item in value]
    versions = [item["version"] for item in releases]
    if len(versions) != len(set(versions)):
        raise UpdateError("invalid version cache duplicate release")
    if versions != sorted(versions, key=_semver):
        raise UpdateError("invalid version cache release order")
    current_semver = _semver(current_version)
    latest_semver = _semver(latest_version)
    if any(not (current_semver < _semver(version) <= latest_semver) for version in versions):
        raise UpdateError("invalid version cache release range")
    return releases


def _load_cache(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    _expect_exact_keys(
        data, CACHE_KEYS, optional={"skip"}, label="version cache"
    )
    if data["schema_version"] != CACHE_SCHEMA_VERSION:
        raise UpdateError(f"unsupported version cache schema: {data['schema_version']}")
    platform = str(data["platform"])
    adapter = _adapter(platform)
    if data["plugin_id"] != PLUGIN_ID or data["marketplace"] != MARKETPLACE:
        raise UpdateError("invalid version cache identity")
    if _parse_timestamp(data["checked_at"]) is None:
        raise UpdateError("invalid version cache checked_at")
    current_version = _version_text(data["current_version"])
    latest_version = _version_text(data["latest_version"])
    scope = str(data["current_scope"])
    adapter.validate_scope(scope)
    if scope == "all":
        raise UpdateError("version cache cannot use aggregate install scope")
    if data["current_source"] != MARKETPLACE:
        raise UpdateError("invalid version cache current_source")
    status = data["update_status"]
    if status not in VALID_UPDATE_STATUSES:
        raise UpdateError("invalid version cache update_status")
    current_semver = _semver(current_version)
    latest_semver = _semver(latest_version)
    if (
        (status == "latest" and current_semver != latest_semver)
        or (status == "current-newer" and current_semver <= latest_semver)
        or (status in {"recommended", "required"} and current_semver >= latest_semver)
    ):
        raise UpdateError("invalid version cache status for version range")
    minimum = _optional_version(data["minimum_supported_version"], "minimum_supported_version")
    force_below = _optional_version(data["force_update_below"], "force_update_below")
    required_reason = data["required_reason"]
    if status == "required":
        required_reason = _nonempty_text(required_reason, "version cache required_reason")
    elif required_reason is not None:
        raise UpdateError("non-required version cache cannot define required_reason")
    releases = _validate_cached_releases(data["releases"], current_version, latest_version)
    normalized: dict[str, Any] = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "platform": platform,
        "plugin_id": PLUGIN_ID,
        "checked_at": data["checked_at"],
        "marketplace": MARKETPLACE,
        "current_version": current_version,
        "current_scope": scope,
        "current_source": MARKETPLACE,
        "latest_version": latest_version,
        "minimum_supported_version": minimum,
        "force_update_below": force_below,
        "update_status": status,
        "required_reason": required_reason,
        "releases": releases,
    }
    skip = data.get("skip")
    if skip is not None:
        if not isinstance(skip, dict):
            raise UpdateError("invalid version cache skip")
        _expect_exact_keys(
            skip,
            {"version", "skipped_at", "source"},
            label="version cache skip",
        )
        skip_version = _version_text(skip["version"])
        if (
            skip_version != latest_version
            or status != "recommended"
            or skip["source"] != "interactive-user"
            or _parse_timestamp(skip["skipped_at"]) is None
        ):
            raise UpdateError("invalid version cache skip")
        normalized["skip"] = {
            "version": skip_version,
            "skipped_at": skip["skipped_at"],
            "source": "interactive-user",
        }
    return normalized


def resolve(cache_path: Path, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now()
    if not cache_path.exists():
        return {
            "state": "missing",
            "update_status": "unknown",
            "next_action": "refresh_policy",
            "cache_path": str(cache_path),
        }
    cache = _load_cache(cache_path)
    checked_at = _parse_timestamp(cache["checked_at"])
    skip_effective = bool(cache.get("skip")) and cache["update_status"] == "recommended"
    if cache["update_status"] == "required":
        next_action = "block_for_update"
    elif cache["update_status"] == "recommended" and not skip_effective:
        next_action = "prompt_update"
    else:
        next_action = "continue_silently"
    return {
        **cache,
        "state": "ready",
        "cache_path": str(cache_path),
        "stale": checked_at is None or current - checked_at >= timedelta(hours=24),
        "skip_effective": skip_effective,
        "next_action": next_action,
    }


def refresh(
    policy_path: Path,
    current_version: str,
    current_scope: str,
    cache_path: Path,
    *,
    platform: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    adapter = _adapter(platform)
    adapter.validate_scope(current_scope)
    if current_scope == "all":
        raise UpdateError("formal installation scope cannot be all")
    policy = _load_policy(policy_path)
    current_text = _version_text(current_version)
    latest_text = policy["latest_version"]
    current_semver = _semver(current_text)
    latest_semver = _semver(latest_text)
    path_releases = [
        item
        for item in reversed(policy["releases"])
        if current_semver < _semver(item["version"]) <= latest_semver
    ]
    required = next((item for item in path_releases if item["update_type"] == "required"), None)
    required_reason = required.get("required_reason") if required else None
    force_below = policy["force_update_below"]
    minimum = policy["minimum_supported_version"]
    if force_below and current_semver < _semver(force_below):
        required_reason = policy["force_update_message"]
    elif minimum and current_semver < _semver(minimum):
        required_reason = f"current version is below minimum supported version {minimum}"

    if current_semver > latest_semver:
        status = "current-newer"
    elif current_semver == latest_semver:
        status = "latest"
    elif required_reason:
        status = "required"
    else:
        status = "recommended"

    data: dict[str, Any] = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "platform": platform,
        "plugin_id": PLUGIN_ID,
        "checked_at": _timestamp(current),
        "marketplace": MARKETPLACE,
        "current_version": current_text,
        "current_scope": current_scope,
        "current_source": MARKETPLACE,
        "latest_version": latest_text,
        "minimum_supported_version": minimum,
        "force_update_below": force_below,
        "update_status": status,
        "required_reason": required_reason,
        "releases": path_releases,
    }
    with _lock(cache_path):
        if cache_path.exists():
            try:
                existing = _load_cache(cache_path)
            except UpdateError:
                existing = None
            if (
                status == "recommended"
                and existing
                and existing.get("platform") == platform
                and existing.get("skip", {}).get("version") == latest_text
            ):
                data["skip"] = existing["skip"]
        _atomic_write(cache_path, data)
    return resolve(cache_path, current)


def _unavailable_result(reason: str, warning: str) -> dict[str, Any]:
    return {
        "state": "unavailable",
        "update_status": "unknown",
        "next_action": "continue_with_warning",
        "reason": reason,
        "warning": warning,
    }


def _negative_cache_result(
    path: Path | None,
    installation: dict[str, Any],
    current: datetime,
) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        value = _read_json(path)
        checked_at = _parse_timestamp(value.get("checked_at"))
        expires_at = _parse_timestamp(value.get("expires_at"))
        required_keys = {
            "schema_version",
            "platform",
            "plugin_id",
            "current_version",
            "checked_at",
            "expires_at",
            "reason",
            "warning",
        }
        if (
            not required_keys.issubset(value)
            or set(value) - required_keys
            or value["schema_version"] != "1.0"
            or value["platform"] != installation["platform"]
            or value["plugin_id"] != installation["plugin_id"]
            or value["current_version"] != installation["current_version"]
            or checked_at is None
            or expires_at is None
            or not checked_at <= current < expires_at
            or value["warning"] != "WARN:helixharness_version_check_unavailable"
            or not isinstance(value["reason"], str)
            or not value["reason"]
        ):
            return None
    except (UpdateError, OSError, TypeError):
        return None
    return {
        **_unavailable_result(value["reason"], value["warning"]),
        "decision_source": "negative-cache",
        "installation": installation,
    }


def _write_negative_cache(
    path: Path | None,
    installation: dict[str, Any],
    reason_category: str,
    current: datetime,
) -> None:
    if path is None:
        return
    try:
        _atomic_write(
            path,
            {
                "schema_version": "1.0",
                "platform": installation["platform"],
                "plugin_id": installation["plugin_id"],
                "current_version": installation["current_version"],
                "checked_at": _timestamp(current),
                "expires_at": _timestamp(
                    current + timedelta(minutes=VOLATILE_NEGATIVE_TTL_MINUTES)
                ),
                "reason": reason_category,
                "warning": "WARN:helixharness_version_check_unavailable",
            },
        )
    except OSError:
        return


def _resolved_cache(cache_path: Path, current: datetime) -> dict[str, Any] | None:
    try:
        return resolve(cache_path, current)
    except (UpdateError, OSError):
        return None


def _cache_matches_installation(
    cached: dict[str, Any] | None,
    installation: dict[str, Any],
) -> bool:
    if not cached or not all(
        cached.get(key) == installation[key]
        for key in ("platform", "plugin_id", "current_version", "current_scope", "current_source")
    ):
        return False
    return True


def check_for_update(
    platform: str,
    repo_root: Path,
    cache_path: Path,
    *,
    plugin_root: Path = PLUGIN_ROOT,
    plugin_list_data: Any = None,
    marketplace_list_data: Any = None,
    force_refresh: bool = False,
    now: datetime | None = None,
    runner: BoundedCommandRunner | None = None,
    volatile_cache_path: Path | None = None,
    negative_cache_path: Path | None = None,
) -> dict[str, Any]:
    if platform not in SUPPORTED_PLATFORMS:
        return _unsupported_result(platform)
    current = now or _now()
    command_runner = runner or BoundedCommandRunner()
    try:
        installation = inspect_formal_installation(
            platform,
            repo_root,
            plugin_root=plugin_root,
            plugin_list_data=plugin_list_data,
            runner=command_runner,
        )
    except UpdateError as exc:
        return _unavailable_result(str(exc), "WARN:helixharness_version_unknown")

    cached = _resolved_cache(cache_path, current)
    cache_matches = _cache_matches_installation(cached, installation)
    refresh_due = (
        force_refresh
        or not cached
        or cached.get("state") != "ready"
        or bool(cached.get("stale"))
        or not cache_matches
    )
    if not refresh_due and cached:
        return {**cached, "decision_source": "cache", "installation": installation}

    if not force_refresh and volatile_cache_path is not None:
        volatile = _resolved_cache(volatile_cache_path, current)
        if (
            volatile
            and not volatile.get("stale")
            and _cache_matches_installation(volatile, installation)
        ):
            return {
                **volatile,
                "decision_source": "volatile-cache",
                "installation": installation,
            }
    if not force_refresh:
        negative = _negative_cache_result(negative_cache_path, installation, current)
        if negative is not None:
            return negative

    def refresh_into(target: Path) -> dict[str, Any]:
        refresh_guard = target.with_name(f"{target.name}.refresh")
        with _lock(refresh_guard):
            if not force_refresh:
                refreshed_by_peer = _resolved_cache(target, current)
                if (
                    refreshed_by_peer
                    and not refreshed_by_peer.get("stale")
                    and _cache_matches_installation(refreshed_by_peer, installation)
                ):
                    return {
                        **refreshed_by_peer,
                        "decision_source": "cache",
                        "refresh_status": "refreshed_by_peer",
                        "installation": installation,
                    }
            adapter = _adapter(platform)
            try:
                snapshot = (
                    adapter.refresh_marketplace(command_runner)
                    if marketplace_list_data is None else nullcontext(None)
                )
                with snapshot as snapshot_root:
                    policy_path = locate_formal_policy(
                        platform, marketplace_list_data, runner=command_runner,
                        snapshot_root=snapshot_root,
                    )
                    refreshed = refresh(
                        policy_path,
                        installation["current_version"],
                        installation["current_scope"],
                        target,
                        platform=platform,
                        now=current,
                    )
            except UpdateHostError as exc:
                raise UpdateError(str(exc)) from exc
            return {
                **refreshed,
                "decision_source": "marketplace-policy",
                "installation": installation,
            }

    try:
        return refresh_into(cache_path)
    except OSError as primary_error:
        if volatile_cache_path is not None:
            try:
                refreshed = refresh_into(volatile_cache_path)
                return {**refreshed, "decision_source": "volatile-cache-fallback"}
            except (UpdateError, OSError) as fallback_error:
                error: Exception = fallback_error
        else:
            error = primary_error
    except (UpdateError, OSError) as exc:
        error = exc

    try:
        cached = _resolved_cache(cache_path, current)
        if cached and _cache_matches_installation(cached, installation):
            return {
                **cached,
                "decision_source": "cache",
                "refresh_status": "failed_using_cache",
                "installation": installation,
            }
        if volatile_cache_path is not None:
            volatile = _resolved_cache(volatile_cache_path, current)
            if volatile and _cache_matches_installation(volatile, installation):
                return {
                    **volatile,
                    "decision_source": "volatile-cache",
                    "refresh_status": "failed_using_cache",
                    "installation": installation,
                }
        if isinstance(error, UpdateError):
            _write_negative_cache(
                negative_cache_path,
                installation,
                "marketplace_refresh_failed",
                current,
            )
        return _unavailable_result(
            str(error),
            (
                "WARN:version_check_cache_unwritable"
                if isinstance(error, OSError)
                else "WARN:helixharness_version_check_unavailable"
            ),
        )
    except OSError as cache_error:
        return _unavailable_result(
            str(cache_error),
            "WARN:version_check_cache_unwritable",
        )


def record_skip(
    cache_path: Path,
    target_version: str,
    source: str | None,
    user_confirmed: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not user_confirmed or source not in VALID_CONFIRMATION_SOURCES:
        raise UpdateError("skipping an update requires explicit interactive user confirmation")
    current = now or _now()
    with _lock(cache_path):
        cache = _load_cache(cache_path)
        target = _version_text(target_version)
        if cache["update_status"] != "recommended" or target != cache["latest_version"]:
            raise UpdateError("only the current recommended version can be skipped")
        cache["skip"] = {
            "version": target,
            "skipped_at": _timestamp(current),
            "source": source,
        }
        _atomic_write(cache_path, cache)
    return resolve(cache_path, current)


def _prompt_for(cache: dict[str, Any]) -> dict[str, Any]:
    release_title = next(
        (item["title"] for item in reversed(cache["releases"]) if item.get("title")),
        "",
    )
    if cache["update_status"] == "required":
        return {
            "kind": "required",
            "title": "HelixHarness 当前版本必须升级后才能继续",
            "message": (
                f"当前版本：{cache['current_version']}\n"
                f"最新版本：{cache['latest_version']}\n"
                f"强制原因：{cache['required_reason']}\n\n"
                "是否立即升级？选择跳过后，本次 HelixHarness 流程将停止。"
            ),
            "allowed_actions": [
                {
                    "id": "upgrade_now",
                    "label": "立即升级",
                    "description": "确认后执行升级，完成后需要重启或新开会话。",
                    "recommended": True,
                },
                {
                    "id": "skip_update",
                    "label": "跳过本次升级",
                    "description": "不安装更新，并停止本次 HelixHarness 流程。",
                    "recommended": False,
                },
            ],
        }
    return {
        "kind": "recommended",
        "title": "检测到 HelixHarness 有推荐更新",
        "message": (
            f"当前版本：{cache['current_version']}\n"
            f"最新版本：{cache['latest_version']}\n"
            f"更新摘要：{release_title}\n\n"
            "是否现在升级？升级后需要重启或新开会话，本次 HelixHarness 流程会先暂停。"
        ),
        "allowed_actions": [
            {
                "id": "upgrade_now",
                "label": "现在升级",
                "description": "确认后执行升级，完成后需要重启或新开会话。",
                "recommended": True,
            },
            {
                "id": "skip_update",
                "label": "跳过本次更新（出现新版本前不再提示）",
                "description": "本次继续使用当前版本，出现更高版本时再提醒。",
                "recommended": False,
            },
        ],
    }


def _validate_prompt(value: Any, update_status: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UpdateError("invalid update authorization prompt")
    _expect_exact_keys(
        value,
        {"kind", "title", "message", "allowed_actions"},
        label="update authorization prompt",
    )
    expected_kind = "required" if update_status == "required" else "recommended"
    if value["kind"] != expected_kind:
        raise UpdateError("invalid update authorization prompt kind")
    _nonempty_text(value["title"], "authorization prompt title")
    _nonempty_text(value["message"], "authorization prompt message")
    actions = value["allowed_actions"]
    if not isinstance(actions, list) or len(actions) != 2:
        raise UpdateError("invalid update authorization actions")
    ids: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            raise UpdateError("invalid update authorization action")
        _expect_exact_keys(
            action,
            {"id", "label", "description", "recommended"},
            label="update authorization action",
        )
        if action["id"] not in VALID_UPDATE_ACTIONS:
            raise UpdateError("invalid update authorization action id")
        _nonempty_text(action["label"], "authorization action label")
        _nonempty_text(action["description"], "authorization action description")
        if not isinstance(action["recommended"], bool):
            raise UpdateError("invalid update authorization recommendation")
        ids.append(action["id"])
    if ids != ["upgrade_now", "skip_update"]:
        raise UpdateError("invalid update authorization action order")
    return value


def prepare_authorization(
    platform: str,
    scope: str,
    cache_path: Path,
    authorization_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    adapter = _adapter(platform)
    try:
        adapter.validate_scope(scope)
    except UpdateHostError as exc:
        raise UpdateError(str(exc)) from exc
    with _lock(cache_path):
        cache = _load_cache(cache_path)
    if cache["platform"] != platform:
        raise UpdateError("version cache platform mismatch")
    if cache["update_status"] not in {"recommended", "required"}:
        raise UpdateError("no installable update is available")
    challenge_id = "upd_" + secrets.token_hex(12)
    data = {
        "schema_version": AUTH_SCHEMA_VERSION,
        "challenge_id": challenge_id,
        "platform": platform,
        "scope": scope,
        "current_version": cache["current_version"],
        "target_version": cache["latest_version"],
        "update_status": cache["update_status"],
        "state": "pending",
        "created_at": _timestamp(current),
        "expires_at": _timestamp(current + timedelta(minutes=AUTH_TTL_MINUTES)),
        "prompt": _prompt_for(cache),
        "completed_steps": [],
    }
    with _lock(authorization_path):
        if authorization_path.exists():
            try:
                existing_raw = _read_json(authorization_path)
                existing_id = str(existing_raw.get("challenge_id") or "")
                _load_authorization(authorization_path, existing_id, current)
            except UpdateError:
                pass
            else:
                raise UpdateError(
                    "an active update authorization must be decided or cancelled before prepare"
                )
        _atomic_write(authorization_path, data)
    return data


def _load_authorization(path: Path, challenge_id: str, now: datetime) -> dict[str, Any]:
    data = _read_json(path)
    state = data.get("state")
    optional_by_state = {
        "pending": set(),
        "authorized": {"confirmation_source", "authorized_at"},
        "installing": {
            "confirmation_source",
            "authorized_at",
            "install_started_at",
            "last_error",
        },
    }
    if state not in optional_by_state:
        raise UpdateError("invalid update authorization state")
    optional = optional_by_state[state]
    required = AUTH_KEYS | (
        {"confirmation_source", "authorized_at"}
        if state in {"authorized", "installing"}
        else set()
    ) | ({"install_started_at"} if state == "installing" else set())
    _expect_exact_keys(data, required, optional=optional, label="update authorization")
    if data["schema_version"] != AUTH_SCHEMA_VERSION:
        raise UpdateError("invalid update authorization schema")
    if data["challenge_id"] != challenge_id or not CHALLENGE_PATTERN.fullmatch(str(challenge_id)):
        raise UpdateError("invalid update authorization challenge")
    platform = str(data["platform"])
    adapter = _adapter(platform)
    try:
        adapter.validate_scope(str(data["scope"]))
    except UpdateHostError as exc:
        raise UpdateError(str(exc)) from exc
    _version_text(data["current_version"])
    _version_text(data["target_version"])
    if data["update_status"] not in {"recommended", "required"}:
        raise UpdateError("invalid update authorization policy")
    created_at = _parse_timestamp(data["created_at"])
    expires_at = _parse_timestamp(data["expires_at"])
    if created_at is None or expires_at is None or expires_at <= created_at:
        raise UpdateError("invalid update authorization lifetime")
    if now >= expires_at:
        raise UpdateError("update authorization challenge expired")
    _validate_prompt(data["prompt"], data["update_status"])
    completed_steps = data["completed_steps"]
    if (
        not isinstance(completed_steps, list)
        or any(not isinstance(item, str) or not item for item in completed_steps)
        or len(completed_steps) != len(set(completed_steps))
    ):
        raise UpdateError("invalid update authorization completed_steps")
    if state == "pending" and completed_steps:
        raise UpdateError("pending update authorization cannot have completed steps")
    if state in {"authorized", "installing"}:
        if data["confirmation_source"] not in VALID_CONFIRMATION_SOURCES:
            raise UpdateError("update authorization has no valid interactive confirmation")
        authorized_at = _parse_timestamp(data["authorized_at"])
        if authorized_at is None or authorized_at < created_at or authorized_at >= expires_at:
            raise UpdateError("invalid update authorization confirmation time")
    if state == "authorized" and completed_steps:
        raise UpdateError("authorized update cannot have completed steps before install")
    if state == "installing":
        started_at = _parse_timestamp(data["install_started_at"])
        if started_at is None or started_at < _parse_timestamp(data["authorized_at"]):
            raise UpdateError("invalid update install start time")
        if "last_error" in data:
            _nonempty_text(data["last_error"], "update install last_error")
    return data


def cancel_authorization(
    authorization_path: Path,
    challenge_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or _now()
    with _lock(authorization_path):
        data = _load_authorization(authorization_path, challenge_id, current)
        if data["state"] == "installing":
            raise UpdateError("an update already in progress cannot be cancelled")
        _durable_unlink(authorization_path)
    return {"cancelled": True, "challenge_id": challenge_id}


def apply_update_choice(
    cache_path: Path,
    authorization_path: Path,
    challenge_id: str,
    action: str,
    user_confirmed: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not user_confirmed:
        raise UpdateError("update decisions require explicit interactive user confirmation")
    if action not in VALID_UPDATE_ACTIONS:
        raise UpdateError(f"invalid update action: {action}")
    current = now or _now()
    with _lock(authorization_path):
        authorization = _load_authorization(authorization_path, challenge_id, current)
        if authorization["state"] != "pending":
            raise UpdateError("update authorization is not pending")
        allowed_ids = [item["id"] for item in authorization["prompt"]["allowed_actions"]]
        if action not in allowed_ids:
            raise UpdateError(f"update action is not allowed: {action}")
        if action == "upgrade_now":
            authorization["state"] = "authorized"
            authorization["confirmation_source"] = "interactive-user"
            authorization["authorized_at"] = _timestamp(current)
            _atomic_write(authorization_path, authorization)
            return {**authorization, "action": action}
        _durable_unlink(authorization_path)

    if authorization["update_status"] == "recommended":
        skipped = record_skip(
            cache_path,
            authorization["target_version"],
            "interactive-user",
            True,
            now=current,
        )
        return {
            **skipped,
            "action": action,
            "blocked": False,
            "authorization_cancelled": True,
        }
    return {
        "action": action,
        "blocked": True,
        "block_reason": "BLOCKED:helixharness_update_required",
        "authorization_cancelled": True,
        "target_version": authorization["target_version"],
    }


def _validate_install_against_cache(
    authorization: dict[str, Any],
    cache: dict[str, Any],
) -> None:
    comparisons = {
        "platform": "platform",
        "target_version": "latest_version",
        "current_version": "current_version",
        "update_status": "update_status",
    }
    for authorization_key, cache_key in comparisons.items():
        if authorization[authorization_key] != cache[cache_key]:
            raise UpdateError(f"update authorization {authorization_key} no longer matches cache")


def install(
    platform: str,
    cache_path: Path,
    authorization_path: Path,
    challenge_id: str,
    now: datetime | None = None,
    *,
    repo_root: Path | None = None,
    plugin_root: Path = PLUGIN_ROOT,
    plugin_list_data: Any = None,
    runner: BoundedCommandRunner | None = None,
) -> dict[str, Any]:
    current = now or _now()
    adapter = _adapter(platform)
    command_runner = runner or BoundedCommandRunner()
    install_guard = authorization_path.with_name(f"{authorization_path.name}.install")
    executed: list[list[str]] = []
    with _lock(install_guard):
        with _lock(authorization_path):
            authorization = _load_authorization(authorization_path, challenge_id, current)
            if authorization["state"] not in {"authorized", "installing"}:
                raise UpdateError("update authorization has not been confirmed by the user")
            if authorization["platform"] != platform:
                raise UpdateError("update authorization platform mismatch")
            cache = _load_cache(cache_path)
            _validate_install_against_cache(authorization, cache)
            target_release = next(
                (
                    item
                    for item in cache["releases"]
                    if item["version"] == authorization["target_version"]
                ),
                None,
            )
            if target_release is None:
                raise UpdateError("target release is missing from version cache")
            restart_required = any(
                item["restart_required"] for item in cache["releases"]
            )
            try:
                steps = adapter.install_steps(str(authorization["scope"]), command_runner)
            except UpdateHostError as exc:
                raise UpdateError(str(exc)) from exc
            step_ids = {step.step_id for step in steps}
            if any(item not in step_ids for item in authorization["completed_steps"]):
                raise UpdateError("update authorization contains an unknown completed step")
            initial_state = authorization["state"]

        if initial_state == "authorized":
            installation = inspect_formal_installation(
                platform,
                (repo_root or Path.cwd()).expanduser().resolve(),
                plugin_root=plugin_root,
                plugin_list_data=plugin_list_data,
                runner=command_runner,
            )
            if (
                installation["current_version"] != authorization["current_version"]
                or installation["current_source"] != MARKETPLACE
                or (
                    authorization["scope"] != "all"
                    and installation["current_scope"] != authorization["scope"]
                )
            ):
                raise UpdateError("live formal installation no longer matches authorization")
            try:
                adapter.validate_live_scope(str(authorization["scope"]), installation)
            except UpdateHostError as exc:
                raise UpdateError(str(exc)) from exc
            with _lock(authorization_path):
                authorization = _load_authorization(authorization_path, challenge_id, current)
                if authorization["state"] != "authorized":
                    raise UpdateError("update authorization state changed before install")
                authorization["state"] = "installing"
                authorization["install_started_at"] = _timestamp(current)
                _atomic_write(authorization_path, authorization)

        resumed = initial_state == "installing"
        for step in steps:
            with _lock(authorization_path):
                authorization = _load_authorization(authorization_path, challenge_id, current)
                if step.step_id in authorization["completed_steps"]:
                    continue
            try:
                command_runner.run(step.command, timeout=INSTALL_TIMEOUT_SECONDS)
            except UpdateHostError as exc:
                with _lock(authorization_path):
                    failed = _load_authorization(authorization_path, challenge_id, current)
                    failed["last_error"] = f"{step.step_id}: {exc}"
                    _atomic_write(authorization_path, failed)
                completed = authorization["completed_steps"]
                raise UpdateError(
                    f"plugin update step failed: {step.step_id}; completed steps: {completed}"
                ) from exc
            executed.append(list(step.command))
            with _lock(authorization_path):
                authorization = _load_authorization(authorization_path, challenge_id, current)
                if step.step_id not in authorization["completed_steps"]:
                    authorization["completed_steps"].append(step.step_id)
                authorization.pop("last_error", None)
                _atomic_write(authorization_path, authorization)

        with _lock(authorization_path):
            authorization = _load_authorization(authorization_path, challenge_id, current)
            if set(authorization["completed_steps"]) != {step.step_id for step in steps}:
                raise UpdateError("update install journal is incomplete")
            _durable_unlink(authorization_path)
        return {
            "updated": True,
            "platform": platform,
            "target_version": authorization["target_version"],
            "commands": executed,
            "completed_steps": [step.step_id for step in steps],
            "resumed": resumed,
            "authorization_consumed": True,
            "restart_required": restart_required,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--cache-path", help=argparse.SUPPRESS)
    parser.add_argument("--authorization-path", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--force-refresh", action="store_true")
    subparsers.add_parser("resolve")
    refresh_parser = subparsers.add_parser("refresh")
    refresh_parser.add_argument("--policy-file", required=True)
    refresh_parser.add_argument("--current-version", required=True)
    refresh_parser.add_argument("--scope", required=True)
    skip_parser = subparsers.add_parser("skip")
    skip_parser.add_argument("--target-version", required=True)
    skip_parser.add_argument("--source", choices=sorted(VALID_CONFIRMATION_SOURCES))
    skip_parser.add_argument("--user-confirmed", action="store_true")
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--scope", required=True)
    decide_parser = subparsers.add_parser("decide")
    decide_parser.add_argument("--challenge-id", required=True)
    decide_parser.add_argument("--action", choices=sorted(VALID_UPDATE_ACTIONS), required=True)
    decide_parser.add_argument("--user-confirmed", action="store_true")
    cancel_parser = subparsers.add_parser("cancel")
    cancel_parser.add_argument("--challenge-id", required=True)
    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--challenge-id", required=True)
    return parser


def main() -> int:
    guard_source(Path(__file__).resolve().parent.parent)
    args = _parser().parse_args()
    try:
        platform = _platform(args.platform)
        if platform not in SUPPORTED_PLATFORMS:
            print(json.dumps(_unsupported_result(platform), ensure_ascii=False, sort_keys=True))
            return 0
        repo_root = Path(args.repo_root).expanduser().resolve()
        cache_path = _cache_path(platform, args.cache_path)
        authorization_path = _authorization_path(platform, args.authorization_path)
        if args.command == "check":
            volatile_cache_path, negative_cache_path = (
                (None, None)
                if args.cache_path is not None
                else _adapter(platform).volatile_check_cache_paths(PLUGIN_ROOT)
            )
            result = check_for_update(
                platform,
                repo_root,
                cache_path,
                force_refresh=args.force_refresh,
                volatile_cache_path=volatile_cache_path,
                negative_cache_path=negative_cache_path,
            )
        elif args.command == "resolve":
            result = resolve(cache_path)
        elif args.command == "refresh":
            result = refresh(
                Path(args.policy_file).expanduser().resolve(),
                args.current_version,
                args.scope,
                cache_path,
                platform=platform,
            )
        elif args.command == "skip":
            result = record_skip(
                cache_path,
                args.target_version,
                args.source,
                args.user_confirmed,
            )
        elif args.command == "prepare":
            result = prepare_authorization(
                platform,
                args.scope,
                cache_path,
                authorization_path,
            )
        elif args.command == "decide":
            result = apply_update_choice(
                cache_path,
                authorization_path,
                args.challenge_id,
                args.action,
                args.user_confirmed,
            )
        elif args.command == "cancel":
            result = cancel_authorization(authorization_path, args.challenge_id)
        else:
            result = install(
                platform,
                cache_path,
                authorization_path,
                args.challenge_id,
                repo_root=repo_root,
            )
    except (UpdateError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
