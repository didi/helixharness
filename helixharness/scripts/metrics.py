#!/usr/bin/env python3
"""Record Lite stages in the Lite-only schema-2 ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import telemetry_async as telemetry_async
import telemetry_consent_state as telemetry_consent
import telemetry_ledger as telemetry_ledger
import telemetry_trace as telemetry_trace
from git_target import operation_state
from telemetry_hosts import get_host_adapter, normalize_host_id


STAGES = ("propose", "apply", "archive")
VALID_QUALITY = {
    "verification": {"passed", "failed", "not_run"},
    "standards_verdict": {"APPROVE", "MUST_FIX", "DECISION_NEEDED"},
    "spec_verdict": {"APPROVE", "MUST_FIX", "DECISION_NEEDED"},
}
ACTIVE_LOCK_TIMEOUT_SECONDS = 5.0
TELEMETRY_DELIVERY_NOTICE = (
    "NOTICE:lite_telemetry_delivery_required: HelixHarness 数据统计已开启；"
    "请务必将最终 eval-log 提交并随功能分支 push，"
    "否则平台无法完成关联统计。"
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class MetricsError(ValueError):
    """Compatibility error type retained for consent cleanup callers."""


def purge_private_states(repo: Path, platform: str) -> int:
    root = Path(repo) / ".helixharness" / "telemetry-v2" / "lite-active"
    removed = 0
    with _active_lock(Path(repo)):
        if root.is_dir():
            for path in root.rglob("*.json"):
                active = _active_states(path)
                retained = {
                    key: state
                    for key, state in active.items()
                    if state.get("platform") != platform
                }
                removed += len(active) - len(retained)
                if retained:
                    _write_active_states(path, retained)
                else:
                    path.unlink(missing_ok=True)
    telemetry_ledger.purge_private(Path(repo), platform=platform)
    telemetry_async.Outbox().purge_repo(Path(repo), platform=platform)
    return removed


def _state_path(repo: Path, feature: str, stage: str) -> Path:
    return repo / ".helixharness" / "telemetry-v2" / "lite-active" / feature / f"{stage}.json"


def _atomic(path: Path, value: dict[str, Any]) -> None:
    telemetry_async._atomic_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


@contextmanager
def _active_lock(repo: Path) -> Iterator[None]:
    path = Path(repo) / ".helixharness" / "telemetry-v2" / "lite-active.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        deadline = time.monotonic() + ACTIVE_LOCK_TIMEOUT_SECONDS
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
                        raise MetricsError("active telemetry lock timed out") from exc
                    time.sleep(0.01)
        else:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise MetricsError("active telemetry lock timed out") from exc
                    time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _consent(repo: Path, platform: str) -> dict[str, Any]:
    return telemetry_consent.resolve_for_platform(repo, platform)


def _skip(reason: str) -> dict[str, Any]:
    return {"ok": True, "status": "skipped", "reason": reason}


def _quality(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only valid optional quality facts; never fail stage finalization for them."""
    result: dict[str, Any] = {}
    for field, allowed in VALID_QUALITY.items():
        if value.get(field) in allowed:
            result[field] = value[field]
    rounds = value.get("must_fix_rounds")
    if isinstance(rounds, int) and not isinstance(rounds, bool) and rounds >= 0:
        result["must_fix_rounds"] = rounds
    return result


def _session_key(platform: str, raw_session_id: Any) -> str:
    identity = f"{platform}\0{str(raw_session_id or 'unavailable').strip()}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _active_states(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    active = value.get("active") if isinstance(value, dict) else None
    if isinstance(active, dict):
        return {
            str(key): dict(state)
            for key, state in active.items()
            if isinstance(state, dict) and state.get("entry_id")
        }
    return {}


def _write_active_states(path: Path, active: dict[str, dict[str, Any]]) -> None:
    _atomic(path, {"version": 2, "active": active})


def _session_context(repo: Path, platform: str) -> tuple[dict[str, Any], str | None]:
    adapter = get_host_adapter(platform)
    context = adapter.current_session_context(repo) if adapter else None
    context = context if isinstance(context, dict) else {}
    raw_session_id = context.get("session_id") or os.environ.get("HELIXHARNESS_SESSION_ID")
    return context, str(raw_session_id) if raw_session_id else None


def start(
    repo: Path,
    platform: str,
    feature: str,
    stage: str,
    trigger: str,
    route_reason: str | None = None,
) -> dict[str, Any]:
    feature = telemetry_ledger.validate_feature(feature)
    if route_reason is not None:
        telemetry_trace.validate_identifier(route_reason)
    trace_status = "unavailable"
    with telemetry_consent.consent_snapshot(repo, platform) as consent:
        if consent.get("effective_mode") != "enabled":
            purge_private_states(repo, platform)
            return _skip("telemetry_disabled")
        context, raw_session_id = _session_context(repo, platform)
        state_path = _state_path(repo, feature, stage)
        with _active_lock(repo):
            active = _active_states(state_path)
            state_key = _session_key(platform, raw_session_id)
            previous = active.pop(state_key, None)
            if previous:
                telemetry_ledger.finish_stage(
                    repo,
                    str(previous["entry_id"]),
                    status="partial",
                    failure_code="stage_restarted",
                )
                try:
                    telemetry_trace.finish_trace(
                        repo,
                        str(previous["entry_id"]),
                        status="partial",
                        failure_code="stage_restarted",
                        quality={},
                    )
                except Exception:
                    pass
            try:
                result = telemetry_ledger.start_stage(
                    repo,
                    feature=feature,
                    command=stage,
                    platform=platform,
                    trigger=trigger,
                    consent_epoch=str(consent["consent_epoch"]),
                    raw_session_id=raw_session_id,
                    session_log_path=context.get("session_log_path") or os.environ.get("HELIXHARNESS_SESSION_LOG_PATH"),
                )
            except telemetry_ledger.LedgerError as exc:
                if str(exc) == "unsupported Lite eval-log schema":
                    return _skip("unsupported_ledger_schema")
                raise
            active[state_key] = {
                "entry_id": result["entry_id"],
                "platform": platform,
                "raw_session_id": raw_session_id,
                "consent_epoch": consent["consent_epoch"],
            }
            _write_active_states(state_path, active)
        try:
            telemetry_trace.start_trace(
                repo,
                entry_id=str(result["entry_id"]),
                command=stage,
                route_reason=route_reason,
                plugin_root=Path(__file__).resolve().parent.parent,
                helixharness_version=telemetry_ledger.PLUGIN_VERSION,
            )
            trace_status = "recorded"
        except Exception:
            pass
    try:
        telemetry_async.ensure_runtime_ready(Path(__file__).resolve().parent.parent, repo, platform)
    except Exception:
        pass
    return {
        "ok": True,
        "status": "started",
        "workflow_trace_status": trace_status,
        **result,
    }


def record_trace_event(
    repo: Path,
    platform: str,
    feature: str,
    stage: str,
    *,
    event_id: str,
    kind: str,
    status: str | None,
    reason_code: str | None,
    taken: bool | None,
) -> dict[str, Any]:
    feature = telemetry_ledger.validate_feature(feature)
    telemetry_trace.validate_identifier(event_id)
    if reason_code is not None:
        telemetry_trace.validate_identifier(reason_code)
    with telemetry_consent.consent_snapshot(repo, platform) as consent:
        if consent.get("effective_mode") != "enabled":
            purge_private_states(repo, platform)
            return _skip("telemetry_disabled")
        state_path = _state_path(repo, feature, stage)
        _, raw_session_id = _session_context(repo, platform)
        with _active_lock(repo):
            active = _active_states(state_path)
            state = active.get(_session_key(platform, raw_session_id))
            if state is None:
                candidates = [
                    candidate
                    for candidate in active.values()
                    if candidate.get("platform") == platform
                ]
                if raw_session_id is not None or len(candidates) != 1:
                    return _skip(
                        "stage_state_ambiguous"
                        if len(candidates) > 1
                        else "stage_state_missing"
                    )
                state = candidates[0]
            if state.get("consent_epoch") != consent.get("consent_epoch"):
                return _skip("consent_epoch_changed")
            telemetry_trace.record_event(
                repo,
                str(state["entry_id"]),
                event_id=event_id,
                kind=kind,
                status=status,
                reason_code=reason_code,
                taken=taken,
            )
    return {
        "ok": True,
        "status": "recorded",
        "entry_id": str(state["entry_id"]),
    }


def finish(
    repo: Path,
    platform: str,
    feature: str,
    stage: str,
    status: str,
    failure_code: str | None,
    metrics: dict[str, Any],
    implementation_commit_id: str | None = None,
    recover_stage: bool = False,
    review_fixed_point: str | None = None,
    review_head: str | None = None,
    review_scope_sha256: str | None = None,
    archive_scope_sha256: str | None = None,
    reviewed_paths: list[str] | None = None,
) -> dict[str, Any]:
    if recover_stage and (stage != "archive" or status != "completed"):
        raise MetricsError("stage recovery only supports completed Archive")
    feature = telemetry_ledger.validate_feature(feature)
    with telemetry_consent.consent_snapshot(repo, platform) as consent:
        if consent.get("effective_mode") != "enabled":
            purge_private_states(repo, platform)
            return _skip("telemetry_disabled")
        state_path = _state_path(repo, feature, stage)
        context, raw_session_id = _session_context(repo, platform)
        recovery: dict[str, str] | None = None
        with _active_lock(repo):
            active = _active_states(state_path)
            state_key = _session_key(platform, raw_session_id)
            state = active.get(state_key)
            if state is None:
                previous = telemetry_ledger.latest_finished_stage(
                    repo,
                    feature=feature,
                    command=stage,
                    platform=platform,
                    raw_session_id=raw_session_id,
                    consent_epoch=str(consent["consent_epoch"]),
                )
                if stage == "apply" and status == "completed" and previous is not None:
                    previous_status = str(previous.get("status") or "")
                    if previous_status == "completed":
                        result = {
                            "path": str(telemetry_ledger.ledger_path(repo, feature)),
                            "entry_id": str(previous["entry_id"]),
                            "stage_status": "completed",
                            "idempotent": True,
                        }
                        recovery = {"recovery": "idempotent_finish"}
                        state = None
                    elif previous_status in {"failed", "blocked"}:
                        started = telemetry_ledger.start_stage(
                            repo,
                            feature=feature,
                            command=stage,
                            platform=platform,
                            trigger=str(previous.get("trigger") or "user_command"),
                            consent_epoch=str(consent["consent_epoch"]),
                            raw_session_id=raw_session_id,
                            session_log_path=context.get("session_log_path")
                            or os.environ.get("HELIXHARNESS_SESSION_LOG_PATH"),
                            started_at=str(previous.get("ended_at") or "") or None,
                        )
                        state = {
                            "entry_id": started["entry_id"],
                            "platform": platform,
                            "raw_session_id": raw_session_id,
                            "consent_epoch": consent["consent_epoch"],
                        }
                        active[state_key] = state
                        _write_active_states(state_path, active)
                        recovery = {
                            "recovery": "successor_attempt",
                            "previous_entry_id": str(previous["entry_id"]),
                        }
                        try:
                            telemetry_trace.start_trace(
                                repo,
                                entry_id=str(started["entry_id"]),
                                command=stage,
                                route_reason="apply_finish_recovery",
                                plugin_root=Path(__file__).resolve().parent.parent,
                                helixharness_version=telemetry_ledger.PLUGIN_VERSION,
                            )
                        except Exception:
                            pass
                if state is None and recovery is None:
                    candidates = [
                        (key, candidate)
                        for key, candidate in active.items()
                        if candidate.get("platform") == platform
                    ]
                    if (raw_session_id is not None and not recover_stage) or len(candidates) != 1:
                        return _skip("stage_state_ambiguous" if len(candidates) > 1 else "stage_state_missing")
                    state_key, state = candidates[0]
            if state is None:
                response = {
                    "ok": True,
                    "status": "recorded",
                    "recorder_duration_ms": 0,
                    **result,
                    **(recovery or {}),
                }
                return response
            if state.get("consent_epoch") != consent.get("consent_epoch"):
                active.pop(state_key, None)
                _write_active_states(state_path, active)
                return _skip("consent_epoch_changed")
            quality = _quality({
                key: value
                for key, value in metrics.items()
                if key in {"verification", "standards_verdict", "spec_verdict", "must_fix_rounds"}
            })
            result = telemetry_ledger.finish_stage(
                repo,
                str(state["entry_id"]),
                status=status,
                failure_code=failure_code,
                quality=quality,
                implementation_commit_id=implementation_commit_id,
                review_fixed_point=review_fixed_point,
                review_head=review_head,
                review_scope_sha256=review_scope_sha256,
                archive_scope_sha256=archive_scope_sha256,
                reviewed_paths=reviewed_paths,
            )
            trace_status = "unavailable"
            try:
                telemetry_trace.finish_trace(
                    repo,
                    str(state["entry_id"]),
                    status=status,
                    failure_code=failure_code,
                    quality=quality,
                    recovery=(
                        "archive_finish_recovery"
                        if recover_stage
                        else "apply_finish_recovery"
                        if recovery and recovery.get("recovery") == "successor_attempt"
                        else None
                    ),
                )
                trace_status = "recorded"
            except Exception:
                pass
            active.pop(state_key, None)
            _write_active_states(state_path, active)
    try:
        telemetry_async.wake_detached()
    except Exception:
        pass
    response = {
        "ok": True,
        "status": "recorded",
        "recorder_duration_ms": 0,
        "workflow_trace_status": trace_status,
        **result,
        **(recovery or {}),
    }
    if stage == "archive" and status == "completed":
        response["notice"] = TELEMETRY_DELIVERY_NOTICE
    return response


def _git_command(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MetricsError("ledger git operation unavailable") from exc


def commit_ledger(
    repo: Path,
    platform: str,
    feature: str,
    *,
    commit_authorized: bool,
) -> dict[str, Any]:
    """Commit only the final Lite Ledger after explicit Archive commit authorization."""
    if not commit_authorized:
        return _skip("commit_not_authorized")
    feature = telemetry_ledger.validate_feature(feature)
    with telemetry_consent.consent_snapshot(repo, platform) as consent:
        if consent.get("effective_mode") != "enabled":
            return _skip("telemetry_disabled")
        path = telemetry_ledger.ledger_path(repo, feature)
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MetricsError("final Lite Ledger is unavailable") from exc
        archive_entries = [
            entry
            for entry in ledger.get("entries", [])
            if isinstance(entry, dict) and entry.get("command") == "archive"
        ]
        if (
            ledger.get("schema") != telemetry_ledger.LEDGER_SCHEMA
            or ledger.get("status") != "archived"
            or not archive_entries
            or archive_entries[-1].get("status") != "completed"
        ):
            raise MetricsError("Lite Ledger is not archived and complete")

        git_state = operation_state(repo)
        if git_state.get("operations") or git_state.get("unmerged_paths"):
            raise MetricsError("cannot commit Lite Ledger during a Git operation")
        relative = path.relative_to(repo).as_posix()
        branch = _git_command(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch.returncode != 0 or not branch.stdout.strip():
            raise MetricsError("Lite Ledger commit requires a branch")
        head_before = _git_command(repo, "rev-parse", "--verify", "HEAD")
        if head_before.returncode != 0:
            raise MetricsError("Lite Ledger commit requires an existing HEAD")
        path_status = _git_command(
            repo, "status", "--porcelain=v1", "--untracked-files=all", "--", relative
        )
        if path_status.returncode != 0:
            raise MetricsError("Lite Ledger Git status is unavailable")
        if not path_status.stdout.strip():
            existing = _git_command(repo, "log", "-1", "--format=%H", "--", relative)
            if existing.returncode == 0 and existing.stdout.strip():
                return {
                    **_skip("ledger_already_committed"),
                    "commit_id": existing.stdout.strip(),
                    "path": relative,
                }
            raise MetricsError("Lite Ledger has no committable change")

        staged_before = _git_command(repo, "diff", "--cached", "--quiet", "--", relative)
        if staged_before.returncode not in {0, 1}:
            raise MetricsError("Lite Ledger index state is unavailable")
        added = _git_command(repo, "add", "--", relative)
        if added.returncode != 0:
            raise MetricsError("failed to stage final Lite Ledger")
        committed = _git_command(
            repo,
            "commit",
            "--only",
            "-m",
            f"chore(eval): record {feature}",
            "--",
            relative,
        )
        if committed.returncode != 0:
            if staged_before.returncode == 0:
                _git_command(repo, "restore", "--staged", "--", relative)
            raise MetricsError("failed to commit final Lite Ledger")
        commit_id = _git_command(repo, "rev-parse", "HEAD")
        changed = _git_command(
            repo,
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "-r",
            commit_id.stdout.strip(),
        )
        if commit_id.returncode != 0 or changed.returncode != 0:
            raise MetricsError("final Lite Ledger commit cannot be verified")
        if changed.stdout.splitlines() != [relative]:
            raise MetricsError("final Lite Ledger commit contains unexpected paths")
        return {
            "ok": True,
            "status": "committed",
            "commit_id": commit_id.stdout.strip(),
            "path": relative,
            "pushed": False,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--platform", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    start_parser = sub.add_parser("start")
    finish_parser = sub.add_parser("finish")
    trace_parser = sub.add_parser("trace-event")
    commit_parser = sub.add_parser("commit-ledger")
    for child in (start_parser, finish_parser, trace_parser):
        child.add_argument("--feature", required=True)
        child.add_argument("--stage", choices=sorted(STAGES), required=True)
    start_parser.add_argument(
        "--trigger", choices=("user_command", "loop", "bug-fix"), required=True
    )
    start_parser.add_argument("--route-reason")
    trace_parser.add_argument("--event-id", required=True)
    trace_parser.add_argument("--kind", choices=sorted(telemetry_trace.EVENT_KINDS), required=True)
    trace_parser.add_argument("--status", choices=sorted(telemetry_trace.GATE_STATUSES))
    trace_parser.add_argument("--reason-code")
    trace_parser.add_argument("--taken", choices=("true", "false"))
    finish_parser.add_argument("--status", choices=("completed", "failed", "blocked"), required=True)
    finish_parser.add_argument("--failure-code")
    finish_parser.add_argument("--metrics-json", default="{}")
    finish_parser.add_argument("--implementation-commit")
    finish_parser.add_argument("--recover-stage", action="store_true")
    finish_parser.add_argument("--review-fixed-point")
    finish_parser.add_argument("--review-head")
    finish_parser.add_argument("--review-scope-sha256")
    finish_parser.add_argument("--archive-scope-sha256")
    finish_parser.add_argument("--reviewed-path", action="append")
    commit_parser.add_argument("--feature", required=True)
    commit_parser.add_argument("--commit-authorized", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    repo = Path(args.repo_root).expanduser().resolve()
    try:
        platform = normalize_host_id(args.platform)
        if get_host_adapter(platform) is None:
            result = _skip("unsupported_host")
        elif args.command == "start":
            result = start(
                repo,
                platform,
                args.feature,
                args.stage,
                args.trigger,
                args.route_reason,
            )
        elif args.command == "trace-event":
            result = record_trace_event(
                repo,
                platform,
                args.feature,
                args.stage,
                event_id=args.event_id,
                kind=args.kind,
                status=args.status,
                reason_code=args.reason_code,
                taken=None if args.taken is None else args.taken == "true",
            )
        elif args.command == "commit-ledger":
            result = commit_ledger(
                repo,
                platform,
                args.feature,
                commit_authorized=args.commit_authorized,
            )
        else:
            try:
                decoded_metrics = json.loads(args.metrics_json)
            except (json.JSONDecodeError, TypeError):
                decoded_metrics = {}
            metrics = decoded_metrics if isinstance(decoded_metrics, dict) else {}
            result = finish(
                repo,
                platform,
                args.feature,
                args.stage,
                args.status,
                args.failure_code,
                metrics,
                implementation_commit_id=args.implementation_commit,
                recover_stage=args.recover_stage,
                review_fixed_point=args.review_fixed_point,
                review_head=args.review_head,
                review_scope_sha256=args.review_scope_sha256,
                archive_scope_sha256=args.archive_scope_sha256,
                reviewed_paths=args.reviewed_path,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 2
    except Exception as exc:
        print(f"invalid lite metrics: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
