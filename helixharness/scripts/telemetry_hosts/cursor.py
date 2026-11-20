"""Cursor telemetry from consented native hooks, never private transcripts.

The journal is our own normalized, private source. All writes require the same
consent lease as the recorder. Raw tool arguments/results, email, attachments,
transcript paths and thinking are never retained. Optional turn token snapshots
are deduplicated; only complete turns inside a stage are numerically attributed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telemetry_hosts import SupplementWindow
from telemetry_session import SessionContextUnavailable


SCHEMA = "helixharness-cursor-hook-journal/1.0"
RELATIVE_ROOT = Path(".helixharness/telemetry-v2/cursor-events")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
EPOCH = re.compile(r"^cep_[0-9a-f]{24}$")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
HOOK_EVENTS = frozenset({"beforeSubmitPrompt", "afterAgentResponse", "postToolUse", "postToolUseFailure", "subagentStart", "subagentStop", "stop"})
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_JOURNAL_BYTES = 128 * 1024 * 1024
HOOK_BUDGET_SECONDS = 1.5


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise ValueError("Cursor optional telemetry budget exhausted")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def native_identity_fields(payload: dict[str, Any], *, deadline: float | None = None) -> tuple[str, list[str]] | None:
    """Validate native syntax only; prompt responses must not resolve the filesystem."""
    parent = payload.get("parent_conversation_id") if payload.get("hook_event_name") in {"subagentStart", "subagentStop"} else None
    conversation = _identifier(parent or payload.get("conversation_id") or payload.get("session_id"))
    if payload.get("conversation_id") and payload.get("session_id") and payload["conversation_id"] != payload["session_id"]:
        return None
    roots = payload.get("workspace_roots")
    if not conversation or not isinstance(roots, list) or not roots:
        return None
    for root in roots:
        _check_deadline(deadline)
        if not isinstance(root, str) or "\x00" in root or not Path(root).is_absolute():
            return None
    return conversation, list(roots)


def native_identity(payload: dict[str, Any], *, deadline: float | None = None) -> tuple[str, list[str]] | None:
    identity = native_identity_fields(payload, deadline=deadline)
    if identity is None:
        return None
    conversation, roots = identity
    resolved = set()
    for root in roots:
        _check_deadline(deadline)
        resolved.add(str(Path(root).resolve()))
    return conversation, sorted(resolved)


def _within_workspace(repo: Path, roots: list[str]) -> bool:
    return any(repo == Path(root) or Path(root) in repo.parents for root in roots)


def _managed(repo: Path, relative: Path) -> Path:
    # Reuse the public runtime's symlink/escape checks for all owned files.
    from telemetry_ledger import _managed as managed
    return managed(repo, relative)


def _source(repo: Path, conversation: str, epoch: str) -> Path:
    if not _identifier(conversation) or not EPOCH.fullmatch(epoch):
        raise ValueError("invalid Cursor journal identity")
    digest = hashlib.sha256(conversation.encode()).hexdigest()
    return _managed(repo, RELATIVE_ROOT / f"{digest}-{epoch}.jsonl")


@contextmanager
def _journal_lock(repo: Path, *, deadline: float | None = None):
    # Callers always acquire consent first; never acquire consent in this lock.
    _check_deadline(deadline)
    path = _managed(repo, RELATIVE_ROOT / ".lock")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
        else:
            import fcntl
        deadline = min(time.monotonic() + 0.5, deadline) if deadline is not None else time.monotonic() + 0.5
        while True:
            _check_deadline(deadline)
            try:
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise ValueError("Cursor journal lock unavailable")
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _header(repo: Path, conversation: str, epoch: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "conversation_id": conversation, "consent_epoch": epoch,
            "repo_hash": hashlib.sha256(str(repo.resolve()).encode()).hexdigest()}


def _read_header(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.readline(MAX_LINE_BYTES + 1)
    if len(raw) > MAX_LINE_BYTES:
        raise ValueError("invalid Cursor journal header")
    result = json.loads(raw)
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        raise ValueError("invalid Cursor journal header")
    return result


def _active_source(repo: Path, conversation: str, epoch: str, source: Path, *, deadline: float | None = None) -> bool:
    _check_deadline(deadline)
    stages = _managed(repo, Path(".helixharness/telemetry-v2/stages"))
    if not stages.is_dir():
        return False
    for path in stages.glob("entry-*.json"):
        _check_deadline(deadline)
        if path.is_symlink():
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_LINE_BYTES + 1)
            if len(raw) > MAX_LINE_BYTES:
                continue
            state = json.loads(raw)
        except (OSError, ValueError):
            continue
        if (isinstance(state, dict) and state.get("platform") == "cursor"
                and state.get("raw_session_id") == conversation
                and state.get("consent_epoch") == epoch
                and state.get("session_log_path") == str(source) and not state.get("finished")):
            return True
    return False


def _normalized_event(payload: dict[str, Any], observed_at: str) -> dict[str, Any] | None:
    from telemetry_protocol import sanitize_text
    event = payload.get("hook_event_name")
    if event not in HOOK_EVENTS:
        return None
    generation = _identifier(payload.get("generation_id"))
    record: dict[str, Any] = {"hook_event_name": event, "timestamp": observed_at, "generation_id": generation}
    if event == "beforeSubmitPrompt":
        if not generation:
            return None
        record["type"] = "user"
        record["text"] = sanitize_text(payload.get("prompt") if isinstance(payload.get("prompt"), str) else "")[0]
    elif event in {"afterAgentResponse", "stop"}:
        record["type"] = "assistant" if event == "afterAgentResponse" else "turn_end"
        if event == "afterAgentResponse":
            record["text"] = sanitize_text(payload.get("text") if isinstance(payload.get("text"), str) else "")[0]
        record["usage"] = {field: payload[field] for field in TOKEN_FIELDS
                           if type(payload.get(field)) is int and 0 <= payload[field] <= 2**53 - 1}
        version = payload.get("cursor_version")
        if isinstance(version, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?", version):
            record["cursor_version"] = version
    elif event in {"subagentStart", "subagentStop"}:
        subagent_id = _identifier(payload.get("subagent_id"))
        if not subagent_id:
            return None
        record.update(type="subagent", subagent_id=subagent_id,
                      phase="start" if event == "subagentStart" else "stop")
        agent_type = payload.get("subagent_type")
        if isinstance(agent_type, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", agent_type):
            record["name"] = agent_type
        if event == "subagentStop":
            status = payload.get("status")
            record["status"] = status if isinstance(status, str) and status in {"completed", "error", "aborted"} else "unknown"
            duration = payload.get("duration_ms")
            if type(duration) in {int, float} and 0 <= duration <= 86400000:
                record["duration_ms"] = duration
            loop = payload.get("loop_count")
            if type(loop) is int and 0 <= loop <= 100000:
                record["loop_count"] = loop
    else:
        call_id = _identifier(payload.get("tool_use_id"))
        if not call_id:
            return None
        name = payload.get("tool_name")
        record.update(type="tool", call_id=call_id,
                      name=name if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name) else "unknown",
                      status="succeeded" if event == "postToolUse" else "failed", ended_at=observed_at)
        duration = payload.get("duration")
        if type(duration) in {int, float} and 0 <= duration <= 86400000:
            record["timestamp"] = (_timestamp(observed_at) - timedelta(milliseconds=duration)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if event == "postToolUseFailure":
            failure = payload.get("failure_type")
            record["error"] = {"timeout": "timeout", "permission_denied": "permission denied"}.get(failure, "tool error") if isinstance(failure, str) else "tool error"
        if name == "Read" and isinstance(payload.get("tool_input"), dict):
            candidate = payload["tool_input"].get("file_path") or payload["tool_input"].get("path")
            if isinstance(candidate, str):
                normalized = candidate.replace("\\", "/")
                for category in ("harness", "knowledge", "specs", "hooks"):
                    if f"docs/{category}/" in normalized:
                        record["input"] = {"path": f"docs/{category}/[omitted]"}
                        break
    # Stable source identity excludes receipt time, so host redelivery is idempotent.
    identity = {key: value for key, value in record.items() if key not in {"timestamp", "ended_at"}}
    record["event_id"] = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return record


class CursorAdapter:
    host_id = "cursor"
    parser_version = "cursor-native-hooks/1.0"
    strict_private_cleanup = True

    def consent_cache_path(self, override: str | None = None) -> Path:
        if override:
            return Path(override).expanduser().resolve()
        root = Path(os.environ.get("HELIXHARNESS_CURSOR_HOME") or Path.home() / ".cursor").expanduser()
        return root / "helixharness/telemetry-consent-cache.json"

    def current_session_context(self, repo: Path) -> dict[str, str]:
        try:
            identity = native_identity({"conversation_id": os.environ.get("HELIXHARNESS_CURSOR_CONVERSATION_ID"),
                                        "workspace_roots": json.loads(os.environ.get("HELIXHARNESS_CURSOR_WORKSPACE_ROOTS", "null"))})
        except (ValueError, OSError):
            identity = None
        root = repo.resolve()
        if identity is None or not _within_workspace(root, identity[1]):
            raise SessionContextUnavailable("cursor_session_context_unavailable")
        return {"session_id": identity[0], "session_cwd": str(root)}

    def prepare_session_source(self, repo: Path, context: dict[str, str], consent_epoch: str) -> dict[str, str]:
        """Recorder holds consent then ledger lock before preparing this source."""
        repo = repo.resolve()
        conversation = context["session_id"]
        source = _source(repo, conversation, consent_epoch)
        expected = _header(repo, conversation, consent_epoch)
        with _journal_lock(repo):
            if source.exists():
                if _read_header(source) != expected:
                    raise ValueError("Cursor journal identity mismatch")
            else:
                fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(json.dumps(expected, ensure_ascii=False) + "\n")
        return {**context, "session_log_path": str(source)}

    def validate_session_source(self, value: Any, repo: Path | None = None) -> str | None:
        if repo is None or not isinstance(value, (str, Path)) or not str(value):
            return None
        try:
            root = repo.resolve()
            path = Path(value)
            relative = path.relative_to(root)
            if relative.parent != RELATIVE_ROOT or path != _managed(root, relative) or not path.is_file():
                return None
            header = _read_header(path)
            if path != _source(root, header.get("conversation_id"), header.get("consent_epoch")) or header != _header(root, header["conversation_id"], header["consent_epoch"]):
                return None
            return str(path)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def purge_private_sources(self, repo: Path, *, deadline: float | None = None) -> int:
        _check_deadline(deadline)
        root = _managed(repo.resolve(), RELATIVE_ROOT)
        if not root.is_dir():
            return 0
        removed = 0
        with _journal_lock(repo.resolve(), deadline=deadline):
            for path in root.glob("*.jsonl"):
                _check_deadline(deadline)
                if not path.is_symlink():
                    path.unlink()
                    removed += 1
        return removed

    def release_session_source(self, repo: Path, source: str) -> int:
        """Remove an owned source only after its final outstanding entry leaves."""
        validated = self.validate_session_source(source, repo)
        if validated is None:
            return 0
        locators = _managed(repo, Path(".helixharness/telemetry-v2/locators"))
        with _journal_lock(repo):
            for locator in locators.glob("entry-*.json"):
                if locator.is_symlink():
                    continue
                try:
                    value = json.loads(locator.read_text())
                except (OSError, ValueError):
                    # Unreadable attribution is not evidence that a source is unused.
                    return 0
                if isinstance(value, dict) and value.get("session_log_path") == source:
                    return 0
            Path(validated).unlink(missing_ok=True)
        return 1

    def collect_supplement_window(self, source: Path, *, started_at: str, ended_at: str, max_bytes: int | None = None) -> SupplementWindow:
        start, end = _timestamp(started_at), _timestamp(ended_at)
        if start is None or end is None or end < start:
            raise ValueError("invalid Cursor stage window")
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        limit = min(MAX_JOURNAL_BYTES, max_bytes) if max_bytes is not None else MAX_JOURNAL_BYTES
        with source.open("rb") as stream:
            header_line = stream.readline(MAX_LINE_BYTES + 1)
            if len(header_line) > limit or json.loads(header_line).get("schema") != SCHEMA:
                raise ValueError("invalid Cursor journal header")
            consumed = len(header_line)
            while consumed < limit:
                raw = stream.readline(min(MAX_LINE_BYTES + 1, limit - consumed))
                if not raw:
                    break
                consumed += len(raw)
                if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
                    raise ValueError("invalid Cursor journal event boundary")
                record = json.loads(raw)
                if not isinstance(record, dict) or not isinstance(record.get("event_id"), str):
                    raise ValueError("invalid Cursor journal event")
                if record["event_id"] not in seen:
                    seen.add(record["event_id"])
                    records.append(record)
        events = [record for record in records if record.get("type") in {"user", "assistant", "tool", "subagent", "collection_gap"}
                  and (when := _timestamp(record.get("timestamp"))) is not None and start <= when <= end]
        # Completion of one tool may be redelivered with a changed duration; its
        # native call id, rather than the callback count, is the tool identity.
        tool_ids: set[str] = set()
        filtered = []
        for event in events:
            if event.get("type") == "tool":
                key = str(event.get("generation_id")) + ":" + str(event.get("call_id"))
                if key in tool_ids:
                    continue
                tool_ids.add(key)
            filtered.append(event)
        return SupplementWindow(events=filtered, usage=_usage(records, start, end))


def _usage(records: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    generations: dict[str, dict[str, Any]] = {}
    ambiguous = any(record.get("type") == "collection_gap" and record.get("reason") == "ambiguous_workspace"
                    and (when := _timestamp(record.get("timestamp"))) is not None and start <= when <= end for record in records)
    for record in records:
        if record.get("type") in {"subagent", "collection_gap"}:
            # Some native child callbacks use the parent conversation id in
            # generation_id. This is not a new parent turn or a token sample.
            continue
        generation = record.get("generation_id")
        if not isinstance(generation, str):
            continue
        slot = generations.setdefault(generation, {"starts": [], "snapshots": [], "observations": []})
        when = _timestamp(record.get("timestamp"))
        if when:
            slot["observations"].append(when)
        if record.get("hook_event_name") == "beforeSubmitPrompt" and when:
            slot["starts"].append(when)
        elif record.get("hook_event_name") in {"afterAgentResponse", "stop"} and when:
            slot["snapshots"].append((when, record.get("usage", {})))
    totals: dict[str, int] = {}
    counted = 0
    unavailable = int(ambiguous)
    cache_complete = {key: True for key in TOKEN_FIELDS[2:]}
    for slot in generations.values():
        snapshots = [(when, value) for when, value in slot["snapshots"] if start <= when <= end]
        if not snapshots:
            unavailable += int(any(start <= when <= end for when in slot["observations"]))
            continue
        complete = [(when, value) for when, value in snapshots if isinstance(value, dict)
                    and all(type(value.get(key)) is int and 0 <= value[key] <= 2**53 - 1 for key in ("input_tokens", "output_tokens"))]
        if not slot["starts"] or not start <= min(slot["starts"]) <= end or not complete:
            unavailable += 1
            continue
        # Both native callbacks repeat a generation total. Contradictory totals
        # are not guessed, and missing optional cache fields stay missing.
        distinct = {(value["input_tokens"], value["output_tokens"]) for _, value in complete}
        cache_conflict = any(len({value[key] for _, value in complete if key in value}) > 1 for key in TOKEN_FIELDS[2:])
        if len(distinct) != 1 or cache_conflict:
            unavailable += 1
            continue
        values = complete[-1][1]
        if (any(field in values and (type(values[field]) is not int or not 0 <= values[field] <= values["input_tokens"]) for field in TOKEN_FIELDS[2:])
                or sum(values.get(field, 0) for field in TOKEN_FIELDS[2:]) > values["input_tokens"]):
            unavailable += 1
            continue
        for key in ("input_tokens", "output_tokens"):
            totals[key] = totals.get(key, 0) + values[key]
        for key in TOKEN_FIELDS[2:]:
            if all(type(value.get(key)) is int and 0 <= value[key] <= 2**53 - 1 for _, value in complete):
                totals[key] = totals.get(key, 0) + values[key]
            else:
                cache_complete[key] = False
        counted += 1
    for key, complete in cache_complete.items():
        if not complete:
            totals.pop(key, None)
    result: dict[str, Any] = {"collection_status": "unavailable", "unavailable_reason": "no_complete_generation_in_stage_window",
                              "scope": "parent_only", "attribution": "complete_generations_within_stage",
                              "input_tokens_include_cache": True}
    if counted:
        result.update(totals, collection_status="partial" if unavailable else "collected", generations=counted,
                      total_tokens=totals["input_tokens"] + totals["output_tokens"])
        if unavailable:
            result["unavailable_reason"] = "some_generations_unattributable"
        else:
            result.pop("unavailable_reason", None)
            result["reason"] = "native_generation_snapshots"
    if ambiguous:
        result["unavailable_reason"] = "ambiguous_workspace"
    return result


def record_hook(payload: dict[str, Any], *, deadline: float | None = None) -> int:
    """Record only actual events for an authorized, active Helix stage."""
    identity = native_identity(payload, deadline=deadline)
    if identity is None or not isinstance(payload.get("hook_event_name"), str) or payload["hook_event_name"] not in HOOK_EVENTS:
        return 0
    conversation, roots = identity
    # Most native tool callbacks have nothing to collect. Check the canonical
    # endpoint policy before importing the delivery runtime and consent stack.
    # Keep the same home/default/override semantics as telemetry_async; the
    # ready path still rechecks below before using any authorized sources.
    import reporting_config
    configured_home = os.environ.get("HELIXHARNESS_TELEMETRY_HOME")
    home = (Path(configured_home).expanduser().resolve() if configured_home
            else (Path.home() / ".helixharness" / "telemetry").resolve())
    if not reporting_config.endpoint_status(home, reporting_config.DEFAULT_REPORT_ENDPOINT)["ready"]:
        return 0
    import telemetry_async
    import telemetry_consent_state as consent

    # No endpoint means no private event collection, even with ledger consent.
    if telemetry_async.endpoint_skip():
        return 0
    _check_deadline(deadline)
    options = {"deadline": deadline} if deadline is not None else {}
    # Desktop tool callbacks can carry cwd="" for an unspecified directory.
    # Reuse workspace/active-stage attribution only for that exact sentinel;
    # malformed values and nonempty relative paths must still fail closed.
    if "cwd" in payload and payload["cwd"] != "":
        # An explicit native cwd is authoritative. Never fan a tool event out
        # into other registered repositories sharing the conversation.
        cwd = payload["cwd"]
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return 0
        candidates = [cwd]
    else:
        candidates = list(roots)
        # Prompt/response callbacks may omit cwd in a multi-repository workspace.
        # Revisit only repositories registered by a prior authorized recorder.
        for registered in telemetry_async.registered_repositories():
            _check_deadline(deadline)
            platforms = registered.get("platforms", [])
            if registered.get("platform") != "cursor" and (not isinstance(platforms, list) or "cursor" not in platforms):
                continue
            candidate = registered.get("path")
            if isinstance(candidate, str) and Path(candidate).is_absolute() and _within_workspace(Path(candidate).resolve(), roots):
                candidates.append(candidate)
    repos: set[Path] = set()
    for candidate in candidates:
        _check_deadline(deadline)
        try:
            repo = consent._repo_root(candidate, **options)
        except consent.ConsentDeadlineExceeded:
            raise
        except (OSError, ValueError):
            continue
        if _within_workspace(repo, roots):
            repos.add(repo)
    active = []
    for repo in sorted(repos):
        _check_deadline(deadline)
        with consent.consent_snapshot(repo, "cursor", **options) as state:
            if state.get("effective_mode") != "enabled":
                CursorAdapter().purge_private_sources(repo, **options)
                continue
            epoch = str(state["consent_epoch"])
            source = _source(repo, conversation, epoch)
            if not source.exists() or not _active_source(repo, conversation, epoch, source, **options):
                continue
            active.append((repo, epoch, source))
    observed_at = _now()
    ambiguous = len(active) > 1
    count = 0
    for repo, epoch, source in active:
        _check_deadline(deadline)
        with consent.consent_snapshot(repo, "cursor", **options) as state:
            if state.get("effective_mode") != "enabled" or state.get("consent_epoch") != epoch or not _active_source(repo, conversation, epoch, source, **options):
                continue
            event = _normalized_event(payload, observed_at)
            if event is None:
                continue
            if ambiguous:
                event = {"type": "collection_gap", "hook_event_name": "attributionGap", "reason": "ambiguous_workspace",
                         "timestamp": observed_at, "event_id": "gap-" + event["event_id"]}
            raw = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(raw) > MAX_LINE_BYTES:
                continue
            with _journal_lock(repo, **options):
                if _read_header(source) != _header(repo, conversation, epoch):
                    raise ValueError("Cursor hook journal identity mismatch")
                if source.stat().st_size + len(raw) > MAX_JOURNAL_BYTES:
                    continue
                _check_deadline(deadline)
                fd = os.open(source, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(fd, "ab") as stream:
                    stream.write(raw)
            count += int(not ambiguous)
    return count
