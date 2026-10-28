#!/usr/bin/env python3
"""Exact-window collector that creates sanitized stage supplements."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import telemetry_async as telemetry_async
import telemetry_consent_state as telemetry_consent
import telemetry_ledger as telemetry_ledger
import telemetry_protocol as telemetry_protocol
import telemetry_trace as telemetry_trace
from telemetry_hosts import get_host_adapter


MAX_TOOL_EVENTS = 20_000
COLLECTION_STATE_SCHEMA = "helixharness-lite-collection-state/1.0"


class UnrecoverableCollectionError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _context_kind(value: Any) -> str | None:
    text = str(value or "").replace("\\", "/").lower()
    for kind, markers in {
        "harness": ("docs/harness/",),
        "knowledge": ("docs/knowledge/",),
        "specs": ("docs/specs/",),
        "hooks": ("docs/hooks/",),
    }.items():
        if any(marker in text for marker in markers):
            return kind
    return None


def _execution_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "tool_call_count": 0,
        "tool_error_count": 0,
        "subagent_spawn_count": 0,
        "context_reads": {"harness": 0, "knowledge": 0, "specs": 0, "hooks": 0},
        "tools": [],
        "tools_truncated": False,
    }
    for event in events:
        if event.get("type") != "tool":
            continue
        summary["tool_call_count"] += 1
        if str(event.get("status") or "").lower() in {"failed", "error", "blocked"}:
            summary["tool_error_count"] += 1
        name = str(event.get("name") or "")
        started = _timestamp(event.get("timestamp"))
        ended = _timestamp(event.get("ended_at"))
        duration_ms = max(0.0, (ended - started).total_seconds() * 1000) if started and ended else 0.0
        if len(summary["tools"]) < MAX_TOOL_EVENTS:
            summary["tools"].append(
                {
                    "name": name[:128] or "unknown",
                    "status": str(event.get("status") or "unknown")[:32],
                    "duration_ms": round(duration_ms, 3),
                }
            )
        else:
            summary["tools_truncated"] = True
        if name in {"spawn_agent", "create_thread", "fork_thread", "Agent", "Task"}:
            summary["subagent_spawn_count"] += 1
        if name in {"Read", "read_file", "read_mcp_resource", "open"}:
            arguments = _tool_arguments(event.get("input"))
            kind = _context_kind(arguments.get("file_path") or arguments.get("path") or arguments.get("uri"))
            if kind:
                summary["context_reads"][kind] += 1
    return summary


def _timestamp(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _ledger_entry(repo: Path, locator: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    path = telemetry_ledger.ledger_path(repo, str(locator["feature"]))
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if ledger.get("schema") != telemetry_ledger.LEDGER_SCHEMA:
        raise ValueError("Lite fixed ledger unavailable")
    entry = next((item for item in ledger.get("entries", []) if isinstance(item, dict) and item.get("entry_id") == locator.get("entry_id")), None)
    if entry is None:
        raise ValueError("Lite fixed ledger entry unavailable")
    return ledger, entry


def _sealed_boundary(
    repo: Path, ledger: dict[str, Any], entry_id: str
) -> dict[str, Any] | None:
    if ledger.get("status") != "archived":
        return None
    seal = telemetry_ledger.load_collection_seal(repo, str(ledger["feature"]))
    if seal.get("requirement_id") != ledger.get("requirement", {}).get("id"):
        raise ValueError("Lite collection seal requirement does not match")
    boundary = next(
        (
            item
            for item in seal.get("entries", [])
            if isinstance(item, dict) and item.get("entry_id") == entry_id
        ),
        None,
    )
    if boundary is None:
        raise ValueError("Lite collection seal does not contain stage entry")
    return _validate_sealed_boundary(boundary)


def _validate_sealed_boundary(boundary: dict[str, Any]) -> dict[str, Any]:
    if boundary.get("source_status") != "recorded":
        raise ValueError("session source was unavailable when requirement was archived")
    limit = boundary.get("source_limit_bytes")
    fingerprint = boundary.get("source_fingerprint")
    started_at = boundary.get("started_at")
    ended_at = boundary.get("ended_at")
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 0
        or not isinstance(fingerprint, str)
        or not fingerprint.startswith("sealed-")
        or _timestamp(started_at) is None
        or _timestamp(ended_at) is None
    ):
        raise ValueError("Lite collection seal source boundary is invalid")
    return boundary


def _collection_context(
    repo: Path,
    locator: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    entry_id = str(locator["entry_id"])
    try:
        frozen = telemetry_ledger.load_collection_input(repo, entry_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise UnrecoverableCollectionError("collection_input_invalid") from exc
    if frozen is None:
        try:
            ledger, entry = _ledger_entry(repo, locator)
            boundary = _sealed_boundary(repo, ledger, entry_id)
        except FileNotFoundError as exc:
            raise UnrecoverableCollectionError("fixed_ledger_unavailable") from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise UnrecoverableCollectionError("fixed_ledger_invalid") from exc
        return ledger, entry, boundary
    if (
        frozen.get("feature") != locator.get("feature")
        or frozen.get("requirement", {}).get("id") != locator.get("requirement_id")
    ):
        raise UnrecoverableCollectionError("collection_input_locator_mismatch")
    entry = dict(frozen["entry"])
    ledger = {
        "schema": telemetry_ledger.LEDGER_SCHEMA,
        "feature": frozen["feature"],
        "requirement": dict(frozen["requirement"]),
        "status": frozen["ledger_status"],
        "entries": [entry],
    }
    boundary = frozen.get("sealed_boundary")
    try:
        validated_boundary = (
            _validate_sealed_boundary(boundary)
            if isinstance(boundary, dict)
            else None
        )
    except ValueError as exc:
        raise UnrecoverableCollectionError("sealed_boundary_invalid") from exc
    return ledger, entry, validated_boundary


def _collect_entry(
    repo: Path,
    entry_id: str,
    *,
    home: str | Path | None = None,
    endpoint: str | None = None,
    previous: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool, str, str | None, bool]:
    endpoint = endpoint or telemetry_async.configured_endpoint(home)
    repo = Path(repo).expanduser().resolve()
    try:
        locator = telemetry_ledger.load_locator(repo, entry_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise UnrecoverableCollectionError("stage_locator_unavailable") from exc
    ledger, entry, boundary = _collection_context(repo, locator)
    workflow_trace = telemetry_trace.load_for_supplement(repo, entry_id)
    trace_fingerprint = hashlib.sha256(
        json.dumps(
            workflow_trace,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    platform = str(locator.get("platform") or entry.get("platform") or "")
    consent_epoch = str(locator["consent_epoch"])
    with telemetry_consent.consent_lease(repo, platform, consent_epoch):
        source_limit_bytes = (
            int(boundary["source_limit_bytes"]) if boundary is not None else None
        )
        transcript_fingerprint = (
            str(boundary["source_fingerprint"])
            if boundary is not None
            else None
        )
        source_fingerprint = (
            hashlib.sha256(
                f"{transcript_fingerprint}\0{trace_fingerprint}".encode("utf-8")
            ).hexdigest()
            if transcript_fingerprint
            else None
        )
        if previous and source_fingerprint and previous.get("source_fingerprint") == source_fingerprint:
            return previous, False, str(previous.get("content_id") or ""), source_fingerprint, boundary is not None
        session_path = Path(str(locator.get("session_log_path") or ""))
        adapter = get_host_adapter(platform)
        if adapter is None:
            raise UnrecoverableCollectionError("unsupported_telemetry_host")
        validated_source = adapter.validate_session_source(session_path, repo) if adapter is not None else None
        if validated_source is None or Path(validated_source).resolve() != session_path.expanduser().resolve():
            raise UnrecoverableCollectionError("session_source_unavailable")
        transcript_fingerprint = transcript_fingerprint or _source_fingerprint(session_path)
        source_fingerprint = (
            hashlib.sha256(
                f"{transcript_fingerprint}\0{trace_fingerprint}".encode("utf-8")
            ).hexdigest()
            if transcript_fingerprint
            else None
        )
        if previous and source_fingerprint and previous.get("source_fingerprint") == source_fingerprint:
            return previous, False, str(previous.get("content_id") or ""), source_fingerprint, boundary is not None
        started_at = str(
            boundary["started_at"] if boundary is not None else locator["started_at"]
        )
        ended_at = str(
            boundary["ended_at"] if boundary is not None else locator["ended_at"]
        )
        try:
            window = adapter.collect_supplement_window(
                session_path,
                started_at=started_at,
                ended_at=ended_at,
                max_bytes=source_limit_bytes,
            )
        except FileNotFoundError as exc:
            raise UnrecoverableCollectionError("session_source_unavailable") from exc
        events, usage = window.events, window.usage
        evidence = telemetry_protocol.build_evolution_evidence(events, started_at=started_at, ended_at=ended_at)
        fixed = {
            "entry_id": entry_id,
            "requirement_id": ledger["requirement"]["id"],
            "requirement_name": ledger["requirement"]["name"],
            "feature": ledger["feature"],
            "platform": platform,
            "session_id": (
                None
                if str(locator.get("raw_session_id") or "") in {"", "unknown"}
                else str(locator["raw_session_id"])
            ),
        }
        execution = _execution_summary(events)
        candidate = telemetry_protocol.build_stage_supplement(
            fixed,
            session_hash=str(entry.get("session_hash") or "session-unknown"),
            usage=usage,
            execution=execution,
            evidence=evidence,
            workflow_trace=workflow_trace,
        )
        content_id = str(candidate["supplement_id"])
        if previous and previous.get("content_id") == content_id:
            return candidate, False, content_id, source_fingerprint, boundary is not None
        revision = int(previous.get("revision") or 1) + 1 if previous else 1
        supersedes = str(previous.get("supplement_id") or "") or None if previous else None
        supplement = candidate if revision == 1 else telemetry_protocol.build_stage_supplement(
            {
                **fixed,
            },
            session_hash=str(entry.get("session_hash") or "session-unknown"),
            usage=usage,
            execution=execution,
            evidence=evidence,
            workflow_trace=workflow_trace,
            revision=revision,
            supersedes=supersedes,
        )
        supplement["collected_at"] = telemetry_async._now()
        telemetry_async.Outbox(home).enqueue(
            supplement,
            repo=repo,
            platform=platform,
            consent_epoch=consent_epoch,
            endpoint=endpoint or telemetry_async.configured_endpoint(home),
            source_ended_at=ended_at,
            final_for_entry=boundary is not None,
        )
    return supplement, True, content_id, source_fingerprint, boundary is not None


def collect_entry(
    repo: Path,
    entry_id: str,
    *,
    home: str | Path | None = None,
    endpoint: str | None = None,
) -> dict[str, Any]:
    if endpoint is None:
        skipped = telemetry_async.endpoint_skip(home)
        if skipped:
            return skipped
    supplement, _, _, _, _ = _collect_entry(repo, entry_id, home=home, endpoint=endpoint)
    return supplement


def _collection_marker(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, dict):
        return value
    return {"supplement_id": raw, "content_id": raw, "revision": 1} if raw else None


def _source_fingerprint(session_path: Any) -> str | None:
    try:
        path = Path(str(session_path)).expanduser()
        stat = path.stat()
    except (OSError, ValueError):
        return None
    value = f"{path.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_quarantine_marker(
    path: Path,
    *,
    entry_id: str,
    reason_code: str,
) -> None:
    telemetry_async._atomic_bytes(
        path.with_suffix(".quarantined"),
        json.dumps(
            {
                "schema": COLLECTION_STATE_SCHEMA,
                "state": "quarantined",
                "entry_id": entry_id,
                "reason_code": reason_code,
                "recorded_at": telemetry_async._now(),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8"),
    )


def collect_finished(
    repo: Path,
    *,
    home: str | Path | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    skipped = telemetry_async.endpoint_skip(home)
    if skipped:
        return {**skipped, "collected": 0, "skipped": 0, "failed": 0}
    repo = Path(repo).expanduser().resolve()
    state_root = repo / telemetry_ledger.PRIVATE_ROOT / "stages"
    report = {"collected": 0, "skipped": 0, "failed": 0}
    if not state_root.is_dir():
        return report
    for path in sorted(state_root.glob("entry-*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _write_quarantine_marker(
                path,
                entry_id=path.stem,
                reason_code="stage_state_invalid",
            )
            report["skipped"] += 1
            continue
        try:
            if not state.get("finished"):
                report["skipped"] += 1
                continue
            if platform is not None and state.get("platform") != platform:
                continue
            marker = path.with_suffix(".collected")
            quarantine_marker = path.with_suffix(".quarantined")
            if quarantine_marker.is_file():
                report["skipped"] += 1
                continue
            previous = _collection_marker(marker)
            if previous and previous.get("state") == "sealed_collected":
                report["skipped"] += 1
                continue
            supplement, changed, content_id, source_fingerprint, sealed = _collect_entry(
                repo,
                str(state["entry_id"]),
                home=home,
                previous=previous,
            )
            if changed:
                report["collected"] += 1
            else:
                report["skipped"] += 1
            marker_state = "sealed_collected" if sealed else "collected"
            if (
                changed
                or not previous
                or previous.get("source_fingerprint") != source_fingerprint
                or previous.get("state") != marker_state
            ):
                latest = supplement if changed or not previous else previous
                telemetry_async._atomic_bytes(
                    marker,
                    json.dumps(
                        {
                            "state": marker_state,
                            "supplement_id": latest["supplement_id"],
                            "content_id": content_id,
                            "revision": latest["revision"],
                            "source_fingerprint": source_fingerprint,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ).encode("utf-8"),
                )
        except UnrecoverableCollectionError as exc:
            _write_quarantine_marker(
                path,
                entry_id=str(state.get("entry_id") or path.stem),
                reason_code=exc.reason_code,
            )
            report["skipped"] += 1
        except Exception:
            report["failed"] += 1
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--entry-id")
    parser.add_argument("--home")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.entry_id:
        result: dict[str, Any] = collect_entry(Path(args.repo_root), args.entry_id, home=args.home)
    else:
        result = collect_finished(Path(args.repo_root), home=args.home)
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
