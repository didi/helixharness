#!/usr/bin/env python3
"""Persistent asynchronous delivery for HelixHarness stage supplements."""

from __future__ import annotations

import argparse
import getpass
import gzip
import hashlib
import json
import os
import platform as system_platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as element_tree
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from xml.sax.saxutils import escape


import reporting_config


DEFAULT_INTERVAL_SECONDS = 600
MAX_WORKER_SECONDS = 15 * 60
WINDOWS_TASK_NAME = "HelixHarnessTelemetry"
CIRCUIT_FAILURE_THRESHOLD = 6
CIRCUIT_COOLDOWN_SECONDS = 60 * 60
HEALTH_REPORT_RETRY_SECONDS = 60 * 60
RUNTIME_HEALTH_SCHEMA = "helixharness-telemetry-runtime-health/2.0"
RUNTIME_STATE_SCHEMA = "helixharness-telemetry-runtime-state/1.0"
REGISTRY_LOCK_TIMEOUT_SECONDS = 5.0
MAX_OUTBOX_BYTES = 500 * 1024 * 1024
PENDING_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_BATCH_COMPRESSED_BYTES = 1024 * 1024
MAX_BATCH_ITEMS = 100
BATCH_PAUSE_SECONDS = 0.5
RUNTIME_FILES = (
    "reporting_config.py",
    "git_target.py",
    "telemetry_async.py",
    "telemetryctl.py",
    "telemetry_collector.py",
    "telemetry_ledger.py",
    "telemetry_protocol.py",
    "telemetry_trace.py",
    "telemetry_consent_state.py",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _runtime_version(plugin_root: Path) -> str:
    root = Path(plugin_root).resolve()
    for path in (
        root / ".codex-plugin" / "plugin.json",
        root / "runtime-manifest.json",
    ):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        version = str(value.get("version") or "") if isinstance(value, dict) else ""
        if version:
            return version
    raise ValueError("HelixHarness runtime version is unavailable")


def telemetry_home(override: str | Path | None = None) -> Path:
    configured = override or os.environ.get("HELIXHARNESS_TELEMETRY_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".helixharness" / "telemetry").resolve()


def endpoint_status(home: str | Path | None = None) -> dict[str, Any]:
    """Check configuration only; never probe the service or change consent."""
    result = reporting_config.endpoint_status(telemetry_home(home), reporting_config.DEFAULT_REPORT_ENDPOINT)
    if result.get("reason") == "endpoint_invalid":
        result["warning"] = "WARN:telemetry_endpoint_invalid"
    return result


def configured_endpoint(home: str | Path | None = None) -> str:
    status = endpoint_status(home)
    if not status["ready"]:
        raise ValueError(status["reason"])
    return str(status["endpoint"])


def endpoint_skip(home: str | Path | None = None) -> dict[str, Any] | None:
    status = endpoint_status(home)
    if status["ready"]:
        return None
    return {"status": "skipped", **{key: value for key, value in status.items() if key != "ready"}}


def _is_workflow_ledger(path: Path) -> bool:
    """Recognize the current public ledger; only enroll the current ledger."""
    from telemetry_ledger import LEDGER_SCHEMA, SCHEMA_VERSION

    if path.is_symlink():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    entries = value.get("entries") if isinstance(value, dict) else None
    return bool(
        isinstance(value, dict)
        and value.get("schema") == LEDGER_SCHEMA
        and value.get("schema_version") == SCHEMA_VERSION
        and isinstance(entries, list)
        and all(
            isinstance(entry, dict)
            for entry in entries
        )
    )


def _repo_contains_telemetry(repo: Path) -> bool:
    input_root = Path(repo).expanduser().resolve() / ".helixharness" / "telemetry-v2" / "inputs"
    if input_root.is_dir() and any(
        path.is_file() for path in input_root.glob("entry-*.json")
    ):
        return True
    root = Path(repo).expanduser().resolve() / "docs" / "eval"
    return root.is_dir() and any(
        path.is_file() and _is_workflow_ledger(path)
        for path in root.glob("*/eval-log.json")
    )


def _atomic_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _runtime_state_path(home: str | Path | None = None) -> Path:
    return telemetry_home(home) / "runtime-health.json"


def _new_runtime_state(runtime_id: str | None = None) -> dict[str, Any]:
    return {
        "schema": RUNTIME_STATE_SCHEMA,
        "runtime_id": runtime_id or f"runtime_{uuid.uuid4().hex}",
        "consecutive_failures": 0,
        "circuit_open": False,
        "circuit_opened_at": None,
        "circuit_resume_at": 0,
        "reason_code": None,
        "active_run": None,
        "health_report_delivered": False,
        "health_report_last_attempt_at": 0,
        "health_report_last_status": None,
        "collection_circuits": {},
        "delivery_circuits": {},
    }


def _load_runtime_state(home: str | Path | None = None) -> dict[str, Any]:
    path = _runtime_state_path(home)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _new_runtime_state()
    if not isinstance(value, dict) or value.get("schema") != RUNTIME_STATE_SCHEMA:
        return _new_runtime_state()
    defaults = _new_runtime_state(str(value.get("runtime_id") or "") or None)
    defaults.update(value)
    for name in ("collection_circuits", "delivery_circuits"):
        if not isinstance(defaults.get(name), dict):
            defaults[name] = {}
    return defaults


def _save_runtime_state(home: str | Path | None, state: dict[str, Any]) -> None:
    _atomic_bytes(
        _runtime_state_path(home),
        (json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )


def runtime_health_status(home: str | Path | None = None) -> dict[str, Any]:
    state = _load_runtime_state(home)
    result = {
        key: value
        for key, value in state.items()
        if key not in {"runtime_id", "active_run"}
    }
    result["collection_circuits_open"] = _open_scoped_circuit_count(
        state, "collection_circuits"
    )
    result["delivery_circuits_open"] = _open_scoped_circuit_count(
        state, "delivery_circuits"
    )
    return result


def reset_runtime_health(home: str | Path | None = None) -> dict[str, Any]:
    current = _load_runtime_state(home)
    state = _new_runtime_state(str(current.get("runtime_id") or "") or None)
    _save_runtime_state(home, state)
    return runtime_health_status(home)


def _mark_runtime_failure(
    state: dict[str, Any], reason_code: str, *, now: float
) -> bool:
    state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    state["reason_code"] = reason_code
    if int(state["consecutive_failures"]) < CIRCUIT_FAILURE_THRESHOLD:
        return False
    was_open = bool(state.get("circuit_open"))
    state["circuit_open"] = True
    state["circuit_opened_at"] = _now()
    state["circuit_resume_at"] = now + CIRCUIT_COOLDOWN_SECONDS
    state["health_report_delivered"] = False
    state["health_report_last_attempt_at"] = 0
    state["health_report_last_status"] = None
    return not was_open


def _reset_runtime_failures(state: dict[str, Any]) -> None:
    state.update(
        {
            "consecutive_failures": 0,
            "circuit_open": False,
            "circuit_opened_at": None,
            "circuit_resume_at": 0,
            "reason_code": None,
            "health_report_delivered": False,
            "health_report_last_attempt_at": 0,
            "health_report_last_status": None,
        }
    )


def _scoped_circuit_key(kind: str, *parts: str) -> str:
    identity = "\0".join((kind, *parts))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _scoped_circuits(state: dict[str, Any], name: str) -> dict[str, dict[str, Any]]:
    value = state.get(name)
    if not isinstance(value, dict):
        value = {}
        state[name] = value
    return value


def _scoped_circuit_allows(
    state: dict[str, Any], name: str, key: str, *, now: float
) -> bool:
    circuit = _scoped_circuits(state, name).get(key)
    if not isinstance(circuit, dict) or not circuit.get("circuit_open"):
        return True
    if now < float(circuit.get("circuit_resume_at") or 0):
        return False
    circuit["circuit_open"] = False
    return True


def _mark_scoped_failure(
    state: dict[str, Any],
    name: str,
    key: str,
    reason_code: str,
    *,
    now: float,
    metadata: dict[str, Any],
) -> None:
    circuits = _scoped_circuits(state, name)
    current = circuits.get(key)
    circuit = dict(current) if isinstance(current, dict) else {}
    circuit.update(metadata)
    circuit["consecutive_failures"] = int(circuit.get("consecutive_failures") or 0) + 1
    circuit["reason_code"] = reason_code
    if int(circuit["consecutive_failures"]) >= CIRCUIT_FAILURE_THRESHOLD:
        circuit["circuit_open"] = True
        circuit["circuit_opened_at"] = _now()
        circuit["circuit_resume_at"] = now + CIRCUIT_COOLDOWN_SECONDS
    else:
        circuit.setdefault("circuit_open", False)
        circuit.setdefault("circuit_opened_at", None)
        circuit.setdefault("circuit_resume_at", 0)
    circuits[key] = circuit


def _reset_scoped_failures(state: dict[str, Any], name: str, key: str) -> None:
    _scoped_circuits(state, name).pop(key, None)


def _clear_collection_scope(
    state: dict[str, Any], *, repo: Path, platform: str
) -> None:
    circuits = _scoped_circuits(state, "collection_circuits")
    target = str(Path(repo).expanduser().resolve())
    for key, value in list(circuits.items()):
        if (
            isinstance(value, dict)
            and value.get("path") == target
            and value.get("platform") == platform
        ):
            circuits.pop(key, None)


def _open_scoped_circuit_count(state: dict[str, Any], name: str) -> int:
    return sum(
        1
        for value in _scoped_circuits(state, name).values()
        if isinstance(value, dict) and value.get("circuit_open")
    )


@contextmanager
def _worker_lease(home: str | Path | None = None) -> Iterator[bool]:
    """Allow only one collector/sender per user runtime, with crash-safe OS release."""
    path = telemetry_home(home) / "worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
        acquired = True
        handle.seek(1)
        handle.truncate()
        handle.write(
            json.dumps({"pid": os.getpid(), "started_at": _now()}).encode("utf-8")
        )
        handle.flush()
        os.fsync(handle.fileno())
        yield True
    finally:
        if acquired:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def _worker_watchdog(seconds: int = MAX_WORKER_SECONDS) -> Iterator[None]:
    """Hard-stop a wedged one-shot worker; durable files make the next run resumable."""
    timer = threading.Timer(seconds, os._exit, args=(124,))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def _load_gzip(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("outbox item must be an object")
    return value


class Outbox:
    """Acknowledge-by-item durable queue with quarantine and bounded retention."""

    def __init__(self, home: str | Path | None = None) -> None:
        self.home = telemetry_home(home)
        self.outbox = self.home / "outbox"
        self.quarantine = self.home / "quarantine"
        self.last_corrupt_count = 0

    def enqueue(
        self,
        supplement: dict[str, Any],
        *,
        repo: Path,
        platform: str,
        consent_epoch: str,
        endpoint: str = "",
        source_ended_at: str | None = None,
        final_for_entry: bool = False,
    ) -> Path:
        identifier = str(supplement.get("supplement_id") or "")
        if not identifier.startswith("supp_"):
            raise ValueError("supplement_id is required")
        envelope = {
            "queued_at": _now(),
            "next_attempt_at": 0,
            "attempt_count": 0,
            "repo": str(Path(repo).expanduser().resolve()),
            "platform": str(platform),
            "consent_epoch": str(consent_epoch),
            "endpoint": str(endpoint),
            "source_ended_at": str(source_ended_at or supplement.get("collected_at") or _now()),
            "final_for_entry": bool(final_for_entry),
            "supplement": supplement,
        }
        content = gzip.compress(json.dumps(envelope, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        path = self.outbox / f"{identifier}.json.gz"
        if not path.exists():
            self._reserve(len(content))
            _atomic_bytes(path, content)
        return path

    def _reserve(self, incoming: int) -> None:
        existing = sorted(
            self.outbox.glob("*.json.gz"),
            key=lambda path: path.stat().st_mtime,
        ) if self.outbox.is_dir() else []
        total = sum(path.stat().st_size for path in existing)
        if total + incoming <= MAX_OUTBOX_BYTES:
            return
        raise OSError("telemetry outbox size limit reached; pending evidence is retained")

    def records(self) -> list[tuple[Path, dict[str, Any]]]:
        records: list[tuple[Path, dict[str, Any]]] = []
        self.last_corrupt_count = 0
        if not self.outbox.is_dir():
            return records
        for path in sorted(self.outbox.glob("*.json.gz")):
            try:
                records.append((path, _load_gzip(path)))
            except (OSError, ValueError, json.JSONDecodeError, gzip.BadGzipFile):
                self.quarantine.mkdir(parents=True, exist_ok=True)
                os.replace(path, self.quarantine / path.name)
                self.last_corrupt_count += 1
        return records

    def quarantine_count(self) -> int:
        return len(list(self.quarantine.glob("*.json.gz"))) if self.quarantine.is_dir() else 0

    def pending(self) -> list[dict[str, Any]]:
        return [record["supplement"] for _, record in self.records()]

    def acknowledge(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def reject(self, path: Path) -> None:
        self.quarantine.mkdir(parents=True, exist_ok=True)
        os.replace(path, self.quarantine / path.name)

    def retry(self, path: Path, record: dict[str, Any]) -> None:
        attempts = int(record.get("attempt_count") or 0) + 1
        record["attempt_count"] = attempts
        record["next_attempt_at"] = time.time() + min(3600, 2 ** min(attempts, 11))
        _atomic_bytes(path, gzip.compress(json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")))

    def purge_private(self) -> int:
        removed = 0
        for directory in (self.outbox, self.quarantine):
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json.gz"):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def purge_repo(self, repo: Path, *, platform: str | None = None) -> int:
        target = str(Path(repo).expanduser().resolve())
        removed = 0
        for path, record in self.records():
            if str(record.get("repo") or "") != target:
                continue
            if platform and str(record.get("platform") or "") != platform:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def cleanup(self) -> None:
        now = time.time()
        for directory, retention in ((self.quarantine, PENDING_RETENTION_SECONDS),):
            if not directory.is_dir():
                continue
            for path in directory.glob("*.json.gz"):
                if now - path.stat().st_mtime > retention:
                    path.unlink(missing_ok=True)


def _encoded_report(items: list[dict[str, Any]]) -> bytes:
    return gzip.compress(json.dumps({"items": items}, ensure_ascii=False).encode("utf-8"))


def _delivery_batches(
    records: list[tuple[Path, dict[str, Any]]],
    *,
    max_compressed_bytes: int,
    max_items: int,
) -> list[list[tuple[Path, dict[str, Any]]]]:
    """Keep every Supplement while bounding ordinary request size and item count."""
    if max_compressed_bytes < 1 or max_items < 1:
        raise ValueError("telemetry delivery batch limits must be positive")
    batches: list[list[tuple[Path, dict[str, Any]]]] = []
    current: list[tuple[Path, dict[str, Any]]] = []
    for record in records:
        candidate = [*current, record]
        items = [item["supplement"] for _, item in candidate]
        if current and (
            len(candidate) > max_items
            or len(_encoded_report(items)) > max_compressed_bytes
        ):
            batches.append(current)
            current = [record]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def _http_sender(endpoint: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = _encoded_report(items)
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read().decode("utf-8"))
    acknowledgements = value.get("items") if isinstance(value, dict) else None
    if not isinstance(acknowledgements, list):
        raise ValueError("report response must contain items[]")
    return [item for item in acknowledgements if isinstance(item, dict)]


def _http_health_sender(endpoint: str, event: dict[str, Any]) -> str:
    payload = _encoded_report([event])
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read().decode("utf-8"))
    acknowledgements = value.get("items") if isinstance(value, dict) else None
    if not isinstance(acknowledgements, list):
        raise ValueError("report response must contain items[]")
    identifier = str(event.get("event_id") or "")
    for item in acknowledgements:
        if not isinstance(item, dict):
            continue
        returned_id = str(item.get("event_id") or item.get("supplement_id") or "")
        if returned_id == identifier or (not returned_id and len(acknowledgements) == 1):
            return str(item.get("status") or "retryable")
    return "retryable"


def _enabled_runtime_consent(
    home: str | Path | None,
    consent_resolver: Callable[[Path, str], dict[str, Any]],
) -> tuple[Path, str, str] | None:
    for repository in registered_repositories(home):
        repo = Path(str(repository.get("path") or "")).expanduser().resolve()
        platforms = repository.get("platforms") or []
        if isinstance(repository.get("platform"), str):
            platforms = [*platforms, repository["platform"]]
        for platform in {str(value) for value in platforms if value}:
            try:
                consent = consent_resolver(repo, platform)
            except Exception:
                continue
            epoch = consent.get("consent_epoch")
            if consent.get("effective_mode") == "enabled" and isinstance(epoch, str) and epoch:
                return repo, platform, epoch
    return None


def _runtime_health_event(
    home: str | Path | None, state: dict[str, Any]
) -> dict[str, Any]:
    root = telemetry_home(home)
    opened_at = str(state.get("circuit_opened_at") or _now())
    reason = str(state.get("reason_code") or "worker_failed")
    identity = f"{state.get('runtime_id')}|{opened_at}|{reason}"
    try:
        manifest = json.loads((root / "runtime-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    return {
        "schema": RUNTIME_HEALTH_SCHEMA,
        "event_id": "health_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16],
        "event": "circuit_open",
        "runtime_id": state.get("runtime_id"),
        "observed_at": opened_at,
        "scope_kind": "runtime",
        "scope_id": "runtime",
        "circuit_open": True,
        "reason_code": reason,
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "circuit_opened_at": opened_at,
        "circuit_resume_at": datetime.fromtimestamp(
            float(state.get("circuit_resume_at") or 0), timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "host_os": system_platform.system().lower(),
        "runtime_version": str(manifest.get("version") or "unknown"),
    }


def _maybe_report_runtime_health(
    home: str | Path | None,
    state: dict[str, Any],
    *,
    consent_resolver: Callable[[Path, str], dict[str, Any]],
    consent_lease_factory: Callable[..., Any] | None,
    health_sender: Callable[[str, dict[str, Any]], str],
    now: float,
) -> None:
    if state.get("health_report_delivered"):
        return
    last_attempt = float(state.get("health_report_last_attempt_at") or 0)
    if last_attempt and now - last_attempt < HEALTH_REPORT_RETRY_SECONDS:
        return
    consent = _enabled_runtime_consent(home, consent_resolver)
    if consent is None or consent_lease_factory is None:
        state["health_report_last_status"] = "consent_unavailable"
        return
    state["health_report_last_attempt_at"] = now
    try:
        with consent_lease_factory(*consent):
            status = health_sender(
                configured_endpoint(home), _runtime_health_event(home, state)
            )
    except Exception:
        status = "retryable"
    state["health_report_last_status"] = status
    if status in {"accepted", "duplicate"}:
        state["health_report_delivered"] = True


def _default_consent_resolver(repo: Path, platform: str) -> dict[str, Any]:
    import telemetry_consent_state as telemetry_consent

    return telemetry_consent.resolve_for_platform(repo, platform)


def run_once(
    home: str | Path | None = None,
    *,
    sender: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]] = _http_sender,
    health_sender: Callable[[str, dict[str, Any]], str] = _http_health_sender,
    consent_resolver: Callable[[Path, str], dict[str, Any]] = _default_consent_resolver,
    repo_filter: set[Path] | None = None,
    source_since: str | None = None,
    consent_lease_factory: Callable[..., Any] | None = None,
    max_batch_compressed_bytes: int = MAX_BATCH_COMPRESSED_BYTES,
    max_batch_items: int = MAX_BATCH_ITEMS,
    batch_pause_seconds: float = BATCH_PAUSE_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    skipped = endpoint_skip(home)
    if skipped:
        return {**skipped, "collection_failed": 0, "accepted": 0, "retryable": 0}
    root = telemetry_home(home)
    if consent_lease_factory is None and consent_resolver is _default_consent_resolver:
        import telemetry_consent_state as telemetry_consent

        consent_lease_factory = telemetry_consent.consent_lease
    with _worker_lease(root) as acquired:
        if not acquired:
            return {
                "pending": 0,
                "accepted": 0,
                "duplicate": 0,
                "retryable": 0,
                "rejected": 0,
                "consent_removed": 0,
                "remaining": 0,
                "collection_failed": 0,
                "quarantined": Outbox(root).quarantine_count(),
                "skipped_existing_worker": 1,
                "circuit_open": 0,
                "collection_circuits_open": _open_scoped_circuit_count(
                    _load_runtime_state(root), "collection_circuits"
                ),
                "delivery_circuits_open": _open_scoped_circuit_count(
                    _load_runtime_state(root), "delivery_circuits"
                ),
            }
        now = time.time()
        state = _load_runtime_state(root)
        if state.get("active_run"):
            _mark_runtime_failure(state, "worker_abandoned", now=now)
            state["active_run"] = None
        if state.get("circuit_open") and now < float(state.get("circuit_resume_at") or 0):
            _maybe_report_runtime_health(
                root,
                state,
                consent_resolver=consent_resolver,
                consent_lease_factory=consent_lease_factory,
                health_sender=health_sender,
                now=now,
            )
            _save_runtime_state(root, state)
            return {
                "pending": 0,
                "accepted": 0,
                "duplicate": 0,
                "retryable": 0,
                "rejected": 0,
                "consent_removed": 0,
                "remaining": 0,
                "collection_failed": 0,
                "quarantined": Outbox(root).quarantine_count(),
                "skipped_existing_worker": 0,
                "circuit_open": 1,
                "consecutive_failures": int(state.get("consecutive_failures") or 0),
                "collection_circuits_open": _open_scoped_circuit_count(
                    state, "collection_circuits"
                ),
                "delivery_circuits_open": _open_scoped_circuit_count(
                    state, "delivery_circuits"
                ),
            }
        if state.get("circuit_open"):
            state["circuit_open"] = False
        state["active_run"] = {
            "run_id": f"run_{uuid.uuid4().hex}",
            "pid": os.getpid(),
            "started_at": _now(),
        }
        _save_runtime_state(root, state)
        try:
            with _worker_watchdog():
                result = _run_once_unlocked(
                    root,
                    state=state,
                    sender=sender,
                    consent_resolver=consent_resolver,
                    repo_filter=repo_filter,
                    source_since=source_since,
                    consent_lease_factory=consent_lease_factory,
                    max_batch_compressed_bytes=max_batch_compressed_bytes,
                    max_batch_items=max_batch_items,
                    batch_pause_seconds=batch_pause_seconds,
                    sleep_fn=sleep_fn,
                )
        except Exception:
            state["active_run"] = None
            _mark_runtime_failure(state, "worker_exception", now=time.time())
            if state.get("circuit_open"):
                _maybe_report_runtime_health(
                    root,
                    state,
                    consent_resolver=consent_resolver,
                    consent_lease_factory=consent_lease_factory,
                    health_sender=health_sender,
                    now=time.time(),
                )
            _save_runtime_state(root, state)
            raise
        state["active_run"] = None
        _reset_runtime_failures(state)
        _save_runtime_state(root, state)
        result["circuit_open"] = int(bool(state.get("circuit_open")))
        result["consecutive_failures"] = int(state.get("consecutive_failures") or 0)
        result.setdefault(
            "collection_circuits_open",
            _open_scoped_circuit_count(state, "collection_circuits"),
        )
        result.setdefault(
            "delivery_circuits_open",
            _open_scoped_circuit_count(state, "delivery_circuits"),
        )
        return result


def _run_once_unlocked(
    home: str | Path | None = None,
    *,
    state: dict[str, Any] | None = None,
    sender: Callable[[str, list[dict[str, Any]]], list[dict[str, Any]]] = _http_sender,
    consent_resolver: Callable[[Path, str], dict[str, Any]] = _default_consent_resolver,
    repo_filter: set[Path] | None = None,
    source_since: str | None = None,
    consent_lease_factory: Callable[..., Any] | None = None,
    max_batch_compressed_bytes: int = MAX_BATCH_COMPRESSED_BYTES,
    max_batch_items: int = MAX_BATCH_ITEMS,
    batch_pause_seconds: float = BATCH_PAUSE_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    root = telemetry_home(home)
    state = state if state is not None else _load_runtime_state(root)
    normalized_filter = {Path(path).expanduser().resolve() for path in repo_filter} if repo_filter else None
    collection_failed = 0
    repository_results: list[dict[str, Any]] = []
    active_collection_keys: set[str] = set()
    import telemetry_collector as telemetry_collector

    for repository in registered_repositories(root):
        repo_path = Path(str(repository["path"])).expanduser().resolve()
        if (
            not repo_path.is_dir()
            or not _repo_contains_telemetry(repo_path)
            or (normalized_filter is not None and repo_path not in normalized_filter)
        ):
            continue
        platforms = {
            str(value)
            for value in repository.get("platforms", [])
            if isinstance(value, str) and value
        }
        if isinstance(repository.get("platform"), str) and repository["platform"]:
            platforms.add(str(repository["platform"]))
        for platform in sorted(platforms):
            try:
                consent = consent_resolver(repo_path, platform)
            except Exception:
                consent = {}
                consent_failed = True
            else:
                consent_failed = False
            if not consent_failed and consent.get("effective_mode") != "enabled":
                _clear_collection_scope(state, repo=repo_path, platform=platform)
                repository_results.append(
                    {
                        "path": str(repo_path),
                        "platform": platform,
                        "collected": 0,
                        "skipped": 0,
                        "failed": 0,
                        "circuit_open": False,
                        "reason_code": "telemetry_disabled",
                    }
                )
                continue
            consent_epoch = str(consent.get("consent_epoch") or "unavailable")
            key = _scoped_circuit_key(
                "collection", str(repo_path), platform, consent_epoch
            )
            active_collection_keys.add(key)
            if not _scoped_circuit_allows(
                state, "collection_circuits", key, now=time.time()
            ):
                repository_results.append(
                    {
                        "path": str(repo_path),
                        "platform": platform,
                        "collected": 0,
                        "skipped": 0,
                        "failed": 0,
                        "circuit_open": True,
                    }
                )
                continue
            try:
                if consent_failed:
                    raise RuntimeError("telemetry consent unavailable")
                collection = telemetry_collector.collect_finished(
                    repo_path,
                    home=root,
                    platform=platform,
                )
            except Exception:
                collection = {"collected": 0, "skipped": 0, "failed": 1}
            failed = int(collection.get("failed") or 0)
            collection_failed += failed
            if failed:
                _mark_scoped_failure(
                    state,
                    "collection_circuits",
                    key,
                    "collection_failed",
                    now=time.time(),
                    metadata={"path": str(repo_path), "platform": platform},
                )
            else:
                _reset_scoped_failures(state, "collection_circuits", key)
            repository_results.append(
                {
                    "path": str(repo_path),
                    "platform": platform,
                    "collected": int(collection.get("collected") or 0),
                    "skipped": int(collection.get("skipped") or 0),
                    "failed": failed,
                    "circuit_open": bool(
                        _scoped_circuits(state, "collection_circuits")
                        .get(key, {})
                        .get("circuit_open")
                    ),
                }
            )
    if normalized_filter is None:
        circuits = _scoped_circuits(state, "collection_circuits")
        for key in set(circuits) - active_collection_keys:
            circuits.pop(key, None)
    outbox = Outbox(root)
    all_records = outbox.records()
    corrupt_quarantined = outbox.last_corrupt_count
    records = [
        (path, record)
        for path, record in all_records
        if float(record.get("next_attempt_at") or 0) <= time.time()
        and (normalized_filter is None or Path(str(record.get("repo") or "")).expanduser().resolve() in normalized_filter)
        and (source_since is None or str(record.get("source_ended_at") or "") >= source_since)
    ]
    report = {
        "pending": len(records),
        "accepted": 0,
        "duplicate": 0,
        "retryable": 0,
        "rejected": 0,
        "consent_removed": 0,
        "remaining": 0,
        "collection_failed": collection_failed,
        "corrupt_quarantined": corrupt_quarantined,
        "repository_results": repository_results,
        "skipped_existing_worker": 0,
    }
    grouped: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
    for path, record in records:
        try:
            consent = consent_resolver(Path(str(record["repo"])), str(record["platform"]))
        except Exception:
            outbox.retry(path, record)
            report["retryable"] += 1
            continue
        if consent.get("effective_mode") != "enabled" or consent.get("consent_epoch") != record.get("consent_epoch"):
            path.unlink(missing_ok=True)
            report["consent_removed"] += 1
            continue
        grouped.setdefault(str(record["endpoint"]), []).append((path, record))
    if normalized_filter is None and source_since is None:
        active_delivery_keys = {
            _scoped_circuit_key("delivery", endpoint) for endpoint in grouped
        }
        circuits = _scoped_circuits(state, "delivery_circuits")
        for key in set(circuits) - active_delivery_keys:
            circuits.pop(key, None)
    if consent_lease_factory is None:
        if consent_resolver is _default_consent_resolver:
            import telemetry_consent_state as telemetry_consent

            consent_lease_factory = telemetry_consent.consent_lease
        else:
            consent_lease_factory = lambda *_args, **_kwargs: nullcontext({})
    for endpoint, endpoint_records in grouped.items():
        endpoint_key = _scoped_circuit_key("delivery", endpoint)
        endpoint_state = _scoped_circuits(state, "delivery_circuits").get(
            endpoint_key, {}
        )
        half_open = bool(
            isinstance(endpoint_state, dict)
            and endpoint_state.get("circuit_open")
            and time.time() >= float(endpoint_state.get("circuit_resume_at") or 0)
        )
        if not _scoped_circuit_allows(
            state, "delivery_circuits", endpoint_key, now=time.time()
        ):
            continue
        endpoint_failed = False
        batches = _delivery_batches(
            endpoint_records,
            max_compressed_bytes=max_batch_compressed_bytes,
            max_items=max_batch_items,
        )
        if half_open:
            batches = batches[:1]
        for batch_index, batch in enumerate(batches):
            try:
                with ExitStack() as leases:
                    lease_keys: set[tuple[str, str, str]] = set()
                    for _, record in batch:
                        lease_key = (
                            str(Path(str(record["repo"])).expanduser().resolve()),
                            str(record["platform"]),
                            str(record["consent_epoch"]),
                        )
                        if lease_key in lease_keys:
                            continue
                        lease_keys.add(lease_key)
                        leases.enter_context(
                            consent_lease_factory(
                                Path(lease_key[0]),
                                lease_key[1],
                                lease_key[2],
                            )
                        )
                    acknowledgements = sender(
                        endpoint,
                        [record["supplement"] for _, record in batch],
                    )
            except Exception:
                endpoint_failed = True
                for path, record in batch:
                    try:
                        consent = consent_resolver(
                            Path(str(record["repo"])), str(record["platform"])
                        )
                    except Exception:
                        consent = {}
                    if (
                        consent.get("effective_mode") != "enabled"
                        or consent.get("consent_epoch") != record.get("consent_epoch")
                    ):
                        path.unlink(missing_ok=True)
                        report["consent_removed"] += 1
                    else:
                        outbox.retry(path, record)
                        report["retryable"] += 1
            else:
                by_id = {
                    str(item.get("supplement_id") or ""): item
                    for item in acknowledgements
                }
                for path, record in batch:
                    identifier = str(record["supplement"]["supplement_id"])
                    status = str(
                        (by_id.get(identifier) or {}).get("status") or "retryable"
                    )
                    if status in {"accepted", "duplicate"}:
                        if record.get("final_for_entry") is True:
                            try:
                                import telemetry_ledger as telemetry_ledger

                                telemetry_ledger.cleanup_delivered_entry(
                                    Path(str(record["repo"])),
                                    str(record["supplement"]["entry_id"]),
                                    feature=str(record["supplement"]["feature"]),
                                )
                            except Exception:
                                outbox.retry(path, record)
                                report["retryable"] += 1
                                continue
                        outbox.acknowledge(path)
                        report[status] += 1
                    elif status == "rejected":
                        outbox.reject(path)
                        report["rejected"] += 1
                    else:
                        endpoint_failed = True
                        outbox.retry(path, record)
                        report["retryable"] += 1
            if batch_index + 1 < len(batches) and batch_pause_seconds > 0:
                sleep_fn(batch_pause_seconds)
        if endpoint_failed:
            _mark_scoped_failure(
                state,
                "delivery_circuits",
                endpoint_key,
                "report_retryable",
                now=time.time(),
                metadata={"endpoint_hash": endpoint_key},
            )
        else:
            _reset_scoped_failures(state, "delivery_circuits", endpoint_key)
    outbox.cleanup()
    report["remaining"] = sum(
        1
        for _, record in outbox.records()
        if (normalized_filter is None or Path(str(record.get("repo") or "")).expanduser().resolve() in normalized_filter)
        and (source_since is None or str(record.get("source_ended_at") or "") >= source_since)
    )
    report["quarantined"] = outbox.quarantine_count()
    report["collection_circuits_open"] = _open_scoped_circuit_count(
        state, "collection_circuits"
    )
    report["delivery_circuits_open"] = _open_scoped_circuit_count(
        state, "delivery_circuits"
    )
    return report


def registry_path(home: str | Path | None = None) -> Path:
    return telemetry_home(home) / "registered-repositories.json"


@contextmanager
def _registry_lock(home: str | Path | None = None) -> Iterator[None]:
    path = telemetry_home(home) / "registered-repositories.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + REGISTRY_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError) as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError("telemetry repository registry lock timed out") from exc
                time.sleep(0.01)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def registered_repositories(home: str | Path | None = None) -> list[dict[str, Any]]:
    path = registry_path(home)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    repositories = value.get("repositories") if isinstance(value, dict) else None
    return [item for item in (repositories or []) if isinstance(item, dict) and item.get("path")]


def register_repository(repo: Path, platform: str, home: str | Path | None = None) -> None:
    path = registry_path(home)
    with _registry_lock(home):
        current = registered_repositories(home)
        resolved = str(Path(repo).expanduser().resolve())
        by_path = {str(item["path"]): item for item in current}
        existing = by_path.get(resolved, {})
        platforms = {
            str(value)
            for value in existing.get("platforms", [])
            if isinstance(value, str) and value
        }
        if isinstance(existing.get("platform"), str) and existing["platform"]:
            platforms.add(str(existing["platform"]))
        platforms.add(str(platform))
        by_path[resolved] = {
            "path": resolved,
            "platforms": sorted(platforms),
            "updated_at": _now(),
        }
        _atomic_bytes(
            path,
            (
                json.dumps(
                    {
                        "repositories": sorted(
                            by_path.values(), key=lambda item: item["path"]
                        )
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8"),
        )


def install_runtime(plugin_root: Path, home: str | Path | None = None) -> Path:
    root = telemetry_home(home)
    version = _runtime_version(Path(plugin_root))
    source = Path(plugin_root) / "scripts"
    target = root / "runtime"
    target.mkdir(parents=True, exist_ok=True)
    for filename in RUNTIME_FILES:
        candidate = source / filename
        if candidate.is_file():
            shutil.copy2(candidate, target / filename)
    source_hosts = source / "telemetry_hosts"
    target_hosts = target / "telemetry_hosts"
    if source_hosts.is_dir():
        if target_hosts.exists():
            shutil.rmtree(target_hosts)
        shutil.copytree(source_hosts, target_hosts, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    wrapper = root / "bin" / "telemetryctl"
    command = f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(target / 'telemetryctl.py')!r}, run_name='__main__')\n"
    _atomic_bytes(wrapper, command.encode("utf-8"), 0o700)
    windows_wrapper = root / "bin" / "telemetryctl.cmd"
    windows_command = f'@"{sys.executable}" "{target / "telemetryctl.py"}" %*\r\n'
    _atomic_bytes(windows_wrapper, windows_command.encode("utf-8"), 0o700)
    _atomic_bytes(root / "runtime-manifest.json", (json.dumps({"version": version, "installed_at": _now(), "plugin_root": str(Path(plugin_root).resolve())}) + "\n").encode("utf-8"))
    return wrapper


def _runtime_files_current(plugin_root: Path, home: str | Path | None = None) -> bool:
    source = Path(plugin_root).resolve() / "scripts"
    target = telemetry_home(home) / "runtime"
    try:
        for filename in RUNTIME_FILES:
            candidate = source / filename
            if candidate.is_file() and (target / filename).read_bytes() != candidate.read_bytes():
                return False
        source_hosts = source / "telemetry_hosts"
        target_hosts = target / "telemetry_hosts"
        source_files = {
            path.relative_to(source_hosts)
            for path in source_hosts.rglob("*.py")
            if path.is_file()
        }
        target_files = {
            path.relative_to(target_hosts)
            for path in target_hosts.rglob("*.py")
            if path.is_file()
        }
        return source_files == target_files and all(
            (target_hosts / relative).read_bytes()
            == (source_hosts / relative).read_bytes()
            for relative in source_files
        )
    except OSError:
        return False


def ensure_runtime_ready(plugin_root: Path, repo: Path, platform: str, home: str | Path | None = None) -> dict[str, Any]:
    skipped = endpoint_skip(home)
    if skipped:
        return skipped
    root = telemetry_home(home)
    expected_version = _runtime_version(Path(plugin_root))
    manifest_path = root / "runtime-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    if (
        manifest.get("version") != expected_version
        or not (root / "bin" / "telemetryctl").is_file()
        or not _runtime_files_current(plugin_root, root)
    ):
        install_runtime(plugin_root, root)
    register_repository(repo, platform, root)
    scheduler_disabled = os.environ.get("HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER") == "1"
    if scheduler_disabled:
        scheduler = {"scheduler": "disabled-by-environment", "path": None}
    else:
        scheduler = scheduler_status(root)
        if not scheduler.get("healthy") and system_platform.system() in {"Darwin", "Windows"}:
            scheduler = ensure_scheduler(root)
    return {"runtime": str(root), **scheduler}


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.helixharness.telemetry.plist"


def _windows_task_xml_path(home: str | Path | None = None) -> Path:
    return telemetry_home(home) / "scheduler" / "helixharness-telemetry.xml"


def _windows_task_xml(home: str | Path | None = None) -> str:
    root = telemetry_home(home)
    executable = escape(str(Path(sys.executable).resolve()))
    script = str((root / "runtime" / "telemetryctl.py").resolve())
    arguments = escape(f'"{script}" run --all')
    working_directory = escape(str((root / "runtime").resolve()))
    username = str(os.environ.get("USERNAME") or getpass.getuser()).strip()
    domain = str(os.environ.get("USERDOMAIN") or "").strip()
    user_id = escape(f"{domain}\\{username}" if domain else username)
    start = datetime.now().replace(microsecond=0).isoformat()
    return f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>HelixHarness Lite telemetry delivery</Description></RegistrationInfo>
  <Triggers><TimeTrigger>
    <StartBoundary>{start}</StartBoundary><Enabled>true</Enabled>
    <Repetition><Interval>PT10M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
  </TimeTrigger><LogonTrigger><Enabled>true</Enabled><UserId>{user_id}</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{user_id}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <Enabled>true</Enabled>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT{MAX_WORKER_SECONDS // 60}M</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author"><Exec>
    <Command>{executable}</Command><Arguments>{arguments}</Arguments><WorkingDirectory>{working_directory}</WorkingDirectory>
  </Exec></Actions>
</Task>'''


def _windows_task_is_current(home: str | Path | None, payload: Any) -> bool:
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-16")
        except UnicodeDecodeError:
            payload = payload.decode("utf-8", errors="replace")
    if not isinstance(payload, str) or not payload.strip():
        return False
    try:
        root = element_tree.fromstring(payload)
    except element_tree.ParseError:
        return False
    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

    def value(path: str) -> str:
        node = root.find(path, namespace)
        return str(node.text or "").strip() if node is not None else ""

    runtime = telemetry_home(home) / "runtime"
    enabled = value("task:Settings/task:Enabled").lower()
    return (
        enabled not in {"false", "0"}
        and value("task:Settings/task:MultipleInstancesPolicy") == "IgnoreNew"
        and value("task:Settings/task:StartWhenAvailable").lower() == "true"
        and value("task:Settings/task:ExecutionTimeLimit")
        == f"PT{MAX_WORKER_SECONDS // 60}M"
        and value("task:Actions/task:Exec/task:Command")
        == str(Path(sys.executable).resolve())
        and value("task:Actions/task:Exec/task:Arguments")
        == f'"{(runtime / "telemetryctl.py").resolve()}" run --all'
        and value("task:Actions/task:Exec/task:WorkingDirectory")
        == str(runtime.resolve())
    )


def scheduler_status(home: str | Path | None = None) -> dict[str, Any]:
    root = telemetry_home(home)
    host = system_platform.system()
    executable = (
        root / "runtime" / "telemetryctl.py"
        if host == "Windows"
        else root / "bin" / "telemetryctl"
    )
    if not executable.is_file():
        return {"scheduler": "missing", "path": None, "healthy": False}
    if os.environ.get("HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER") == "1":
        return {"scheduler": "disabled-by-environment", "path": None, "healthy": True}
    if host == "Darwin":
        path = _launchd_path()
        if not path.is_file():
            return {"scheduler": "missing", "path": str(path), "healthy": False}
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/com.helixharness.telemetry"],
                check=False,
                capture_output=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return {"scheduler": "launchd", "path": str(path), "healthy": False}
        return {"scheduler": "launchd", "path": str(path), "healthy": result.returncode == 0}
    if host == "Windows":
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME, "/XML"],
                check=False,
                capture_output=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            return {"scheduler": "windows-task-scheduler", "path": WINDOWS_TASK_NAME, "healthy": False}
        return {
            "scheduler": "windows-task-scheduler",
            "path": WINDOWS_TASK_NAME,
            "healthy": result.returncode == 0
            and _windows_task_is_current(root, result.stdout),
        }
    return {"scheduler": "session-only", "path": None, "healthy": True}


def ensure_scheduler(home: str | Path | None = None) -> dict[str, Any]:
    root = telemetry_home(home)
    executable = root / "bin" / "telemetryctl"
    if system_platform.system() == "Darwin":
        path = _launchd_path()
        escaped_executable = escape(str(executable))
        content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.helixharness.telemetry</string>
<key>ProgramArguments</key><array><string>{escaped_executable}</string><string>run</string><string>--all</string></array>
<key>RunAtLoad</key><true/><key>StartInterval</key><integer>{DEFAULT_INTERVAL_SECONDS}</integer>
<key>ProcessType</key><string>Background</string>
</dict></plist>'''
        _atomic_bytes(path, content.encode("utf-8"), 0o644)
        result = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], check=False, capture_output=True, timeout=3)
        health = scheduler_status(root)
        if result.returncode != 0 and not health.get("healthy"):
            return {"scheduler": "launchd", "path": str(path), "healthy": False, "error": "launchd_bootstrap_failed"}
        return {"scheduler": "launchd", "path": str(path), "healthy": bool(health.get("healthy"))}
    if system_platform.system() == "Windows":
        path = _windows_task_xml_path(root)
        _atomic_bytes(path, _windows_task_xml(root).encode("utf-16"), 0o600)
        result = subprocess.run(
            ["schtasks", "/Create", "/TN", WINDOWS_TASK_NAME, "/XML", str(path), "/F"],
            check=False,
            capture_output=True,
            timeout=3,
        )
        health = scheduler_status(root)
        healthy = result.returncode == 0 and bool(health.get("healthy"))
        return {
            "scheduler": "windows-task-scheduler",
            "path": WINDOWS_TASK_NAME,
            "healthy": healthy,
            **({"error": "windows_task_create_failed"} if not healthy else {}),
        }
    return {"scheduler": "session-only", "path": None, "healthy": True}


def remove_scheduler(home: str | Path | None = None) -> dict[str, Any]:
    if os.environ.get("HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER") == "1":
        return {"scheduler": "disabled-by-environment", "removed": True}
    if system_platform.system() == "Darwin":
        path = _launchd_path()
        if not path.exists():
            return {"scheduler": "launchd", "removed": True}
        result = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False, capture_output=True, timeout=3)
        if result.returncode != 0:
            probe = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/com.helixharness.telemetry"],
                check=False,
                capture_output=True,
                timeout=3,
            )
            if probe.returncode == 0:
                raise OSError("failed to stop HelixHarness launchd scheduler")
        path.unlink(missing_ok=True)
        return {"scheduler": "launchd", "removed": True}
    elif system_platform.system() == "Windows":
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", WINDOWS_TASK_NAME, "/F"],
            check=False,
            capture_output=True,
            timeout=3,
        )
        if result.returncode != 0:
            probe = subprocess.run(
                ["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME],
                check=False,
                capture_output=True,
                timeout=3,
            )
            if probe.returncode == 0:
                raise OSError("failed to stop HelixHarness Windows scheduler")
        _windows_task_xml_path(home).unlink(missing_ok=True)
        return {"scheduler": "windows-task-scheduler", "removed": True}
    return {"scheduler": "session-only", "removed": True}


def wake_detached(home: str | Path | None = None) -> bool:
    if endpoint_skip(home):
        return False
    if os.environ.get("HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER") == "1":
        return False
    root = telemetry_home(home)
    health = _load_runtime_state(root)
    if health.get("circuit_open") and time.time() < float(health.get("circuit_resume_at") or 0):
        return False
    host = system_platform.system()
    executable = root / "bin" / "telemetryctl"
    runtime_script = root / "runtime" / "telemetryctl.py"
    if host == "Darwin":
        command = [
            "launchctl",
            "kickstart",
            f"gui/{os.getuid()}/com.helixharness.telemetry",
        ]
    elif host == "Windows":
        command = ["schtasks", "/Run", "/TN", WINDOWS_TASK_NAME]
    elif runtime_script.is_file():
        command = [sys.executable, str(runtime_script), "run", "--all"]
    elif executable.is_file():
        command = [str(executable), "run", "--all"]
    else:
        return False
    try:
        options: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            options["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:
            options["start_new_session"] = True
        subprocess.Popen(command, **options)
        return True
    except OSError:
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home")
    parser.add_argument("--plugin-root", default=str(Path(__file__).resolve().parent.parent))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    repair = sub.add_parser("repair")
    repair.add_argument("--repo-root")
    repair.add_argument("--platform")
    repair.add_argument("--require-ledger", action="store_true")
    sub.add_parser("disable")
    return parser


def main() -> int:
    args = _parser().parse_args()
    exit_code = 0
    if args.command == "run":
        result = run_once(args.home)
        exit_code = 1 if result["collection_failed"] or result["remaining"] or result["rejected"] or result.get("circuit_open") or result.get("collection_circuits_open") or result.get("delivery_circuits_open") else 0
    elif args.command == "repair":
        if args.repo_root and args.platform:
            repo = Path(args.repo_root).expanduser().resolve()
            consent = _default_consent_resolver(repo, args.platform)
            if consent.get("effective_mode") != "enabled":
                result = {"skipped": True, "reason": "telemetry_disabled"}
            elif args.require_ledger and not _repo_contains_telemetry(repo):
                result = {"skipped": True, "reason": "no_lite_telemetry"}
            else:
                result = ensure_runtime_ready(Path(args.plugin_root), repo, args.platform, args.home)
        else:
            install_runtime(Path(args.plugin_root), args.home)
            result = ensure_scheduler(args.home)
        exit_code = 1 if result.get("healthy") is False else 0
    else:
        scheduler = remove_scheduler(args.home)
        result = {**scheduler, "private_items_removed": Outbox(args.home).purge_private()}
    if args.command == "run" and result.get("quarantined"):
        exit_code = 1
    print(json.dumps({"ok": exit_code == 0, **result}, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
