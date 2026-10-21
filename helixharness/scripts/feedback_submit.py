#!/usr/bin/env python3
"""Prepare user-reviewed feedback and deliver it to an explicitly configured platform.

Feedback is independent from usage consent. Optional current-session excerpts
are redacted before review. Drafts and destination-bound retries live only in
the private feedback directory; the business repository is never modified.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform as platform_module
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import eval_schema
import reporting_config
import telemetry_consent_state
from telemetry_hosts import get_host_adapter, normalize_host_id
from telemetry_ledger import PLUGIN_VERSION, _git
from telemetry_ledger import LedgerError as TelemetryError
from telemetry_ledger import _feature as _feature_name
from telemetry_ledger import _managed as _managed_repo_path
from telemetry_protocol import (
    _FENCED_CODE,
    _HIGH_RISK_PATTERNS,
    _SECRET_PATTERNS,
    sanitize_text,
)

SCHEMA_VERSION = "1.0"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
FEEDBACK_SCHEMA_PATH = PLUGIN_ROOT / "reference" / "feedback-schema-v1.json"
FEEDBACK_ID = re.compile(r"^fbk-[0-9a-f]{16}$")
# Entry ids are opaque identifiers owned by eval-log (real protocol: entry-{16hex};
# demo/sample data may use readable ids) — accept both rather than hard-coding one shape.
ENTRY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
FEEDBACK_CATEGORIES = (
    "bug",
    "docs_unclear",
    "process_stuck",
    "suggestion",
    "uncategorized",
)
FEEDBACK_TEXT_MAX_LENGTH = 4000
MAX_RELATED_ENTRY_IDS = 5
FEEDBACK_REPORT_SCHEMA = "helixharness-feedback/1.0"
TRANSCRIPT_MAX_BYTES = 512 * 1024
TRANSCRIPT_RETENTION_SECONDS = 30 * 24 * 60 * 60
POST_TIMEOUT_SECONDS = 10


def _feedback_home() -> Path:
    override = os.environ.get("HELIXHARNESS_FEEDBACK_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".helixharness" / "feedback").resolve()


def _configured_endpoint() -> str:
    result = reporting_config.endpoint_status(_feedback_home(), reporting_config.DEFAULT_FEEDBACK_ENDPOINT)
    if result.get("reason") == "endpoint_unconfigured":
        raise FeedbackError("feedback endpoint is not configured; set the published default in scripts/reporting_config.py or endpoint in the private feedback config.json")
    if not result["ready"]:
        raise FeedbackError("feedback endpoint configuration is invalid")
    return str(result["endpoint"])


def _validate_endpoint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
                 and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                 and not any(ch.isspace() for ch in endpoint))
    except ValueError:
        valid = False
    if not valid:
        raise FeedbackError("feedback endpoint must be an http(s) URL without credentials, query or fragment")
    return endpoint


class FeedbackError(ValueError):
    """Raised when feedback input cannot be persisted safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _resolve_platform(value: Any) -> str:
    raw = str(value or os.environ.get("HELIXHARNESS_PLATFORM") or "").strip()
    if not raw:
        return "unknown"
    try:
        return normalize_host_id(raw)
    except ValueError:
        return "unknown"


def _feedback_id(text: str, created_at: str, project: str, session: str) -> str:
    identity = {
        "algorithm": "helixharness-feedback-v1",
        "text": text,
        "created_at": created_at,
        "project": project,
        "session": session,
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "fbk-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _plugin_version_or_none(platform: str) -> str:
    return PLUGIN_VERSION


def _git_head_or_none(repo: Path) -> str | None:
    head = _git(repo, "rev-parse", "--short=12", "HEAD")
    return head if re.fullmatch(r"[0-9a-f]{4,40}", head) else None


def _related_entry_ids(repo: Path, feature: str | None) -> list[str]:
    if not feature:
        return []
    log_path = _managed_repo_path(repo, Path("docs") / "eval" / feature / "eval-log.json")
    try:
        log = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        return []
    result: list[str] = []
    for item in reversed(entries):
        entry_id = str(item.get("entry_id") or "") if isinstance(item, dict) else ""
        if ENTRY_ID.fullmatch(entry_id) and entry_id not in result:
            result.append(entry_id)
        if len(result) >= MAX_RELATED_ENTRY_IDS:
            break
    return result


# ── Session transcript capture ─────────────────────────────────────────────
# Chat text plus a one-line tool-call trace.  Raw tool outputs, thinking
# blocks and host-injected command documents are excluded: they carry file
# contents and internal paths, and they dominate the file size.

TOOL_ARG_KEYS = (
    "file_path", "path", "command", "cmd", "pattern", "query",
    "url", "description", "prompt", "name",
)
_USER_STR_SKIP_PREFIXES = (
    "<command-message", "<command-name", "<local-command",
    "<bash-", "<task-notification",
)


def _tool_arg_summary(tool_input: Any) -> str:
    if not isinstance(tool_input, dict) or not tool_input:
        return ""
    for key in TOOL_ARG_KEYS:
        if key in tool_input:
            value = str(tool_input[key]).replace("\n", " ").strip()
            return value[:120]
    first = next(iter(tool_input))
    return str(tool_input[first]).replace("\n", " ").strip()[:120]


def _local_clock(ts: str) -> str:
    """UTC ISO timestamp ("2026-09-02T06:11:46.545Z") → 本地时区 HH:MM:SS。

    Session jsonl 的 timestamp 是 UTC，直接切片会把下午显示成凌晨，
    所以这里转本地时区后再取钟点；解析失败退回原始切片。
    """
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[11:19] if len(ts) >= 19 else "--:--:--"
    return parsed.astimezone().strftime("%H:%M:%S")


def _render_claude_transcript(path: Path) -> tuple[str, dict[str, Any]]:
    """Render a Claude Code session jsonl as chat text + tool-call trace."""
    body: list[str] = []
    stats = {"user_messages": 0, "assistant_messages": 0, "tool_calls": 0}
    first_ts = last_ts = ""
    pending_command_document = False
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            ts = str(record.get("timestamp") or "")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts
            moment = _local_clock(ts)
            content = (record.get("message") or {}).get("content")
            if record.get("type") == "user":
                if isinstance(content, str):
                    texts, is_string = [content], True
                elif isinstance(content, list):
                    texts = [
                        block.get("text") or ""
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    ]
                    is_string = False
                else:
                    texts, is_string = [], False
                text = "\n".join(item for item in texts if item.strip())
                if is_string and text.startswith(_USER_STR_SKIP_PREFIXES):
                    pending_command_document = True
                    continue
                if pending_command_document:
                    pending_command_document = False
                    if not is_string:
                        continue
                if text:
                    stats["user_messages"] += 1
                    body.append(f"\n## {moment} user\n\n{text}\n")
            elif record.get("type") == "assistant" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and (block.get("text") or "").strip():
                        stats["assistant_messages"] += 1
                        body.append(f"\n## {moment} assistant\n\n{block['text']}\n")
                    elif block.get("type") == "tool_use":
                        stats["tool_calls"] += 1
                        argument = _tool_arg_summary(block.get("input"))
                        body.append(f"  → {block.get('name')}({argument})")
    return "".join(body), {**stats, "first_seen": first_ts, "last_seen": last_ts}


def _locate_claude_session(session_id: str, hint_path: str | None) -> Path | None:
    if not hint_path:
        return None
    candidate = Path(hint_path).expanduser()
    if candidate.is_file() and not candidate.is_symlink() and candidate.stem == session_id:
        return candidate
    return None


def _redact_transcript(body: str) -> tuple[str, int]:
    """Local-only redaction for a user-confirmed transcript.

    Unlike ``sanitize_text`` (built for unconfirmed tool-output summaries and
    therefore drops whole texts that merely look like code), the feedback
    transcript has been explicitly confirmed by the user: conversation code is
    expected content.  Keep the local replacements — fenced code blocks,
    secrets, paths — and never omit the whole text.
    """
    text = body.replace(str(Path.home()), "~")
    text, private_keys = _HIGH_RISK_PATTERNS[0].subn("[OMITTED_HIGH_RISK]", text)
    text, code_blocks = _FENCED_CODE.subn("[OMITTED_CODE]", text)
    redactions = code_blocks + private_keys
    for pattern, replacement in _SECRET_PATTERNS:
        text, count = pattern.subn(replacement, text)
        redactions += count
    return text, redactions


def _collect_transcript(platform: str, context: dict[str, str]) -> dict[str, Any]:
    """Capture the current session transcript, redacted and identifier-free."""
    if platform != "claude-code" or not context.get("session_id"):
        return {"supported": False,
                "reason": "transcript capture supports claude-code sessions only"}
    session_file = _locate_claude_session(
        context["session_id"], context.get("session_log_path")
    )
    if session_file is None:
        return {"supported": False, "reason": "current session file not found"}
    if session_file.stat().st_size > 8 * 1024 * 1024:
        return {"supported": False, "reason": "session source exceeds capture size limit"}
    body, stats = _render_claude_transcript(session_file)
    sanitized, redactions = _redact_transcript(body)
    if len(sanitized.encode("utf-8")) > TRANSCRIPT_MAX_BYTES:
        return {"supported": False, "reason": "transcript exceeds size limit"}
    return {
        "supported": True,
        "transcript_md": sanitized,
        "user_messages": stats["user_messages"],
        "assistant_messages": stats["assistant_messages"],
        "tool_calls": stats["tool_calls"],
        "first_seen": stats["first_seen"],
        "last_seen": stats["last_seen"],
        "redactions": redactions,
    }


def _normalized_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise FeedbackError("feedback text must not be empty")
    if len(text) > FEEDBACK_TEXT_MAX_LENGTH:
        raise FeedbackError(f"feedback text exceeds {FEEDBACK_TEXT_MAX_LENGTH} characters")
    return sanitize_text(text)[0]


def _normalized_category(value: Any) -> str:
    category = str(value or "").strip()
    if category not in FEEDBACK_CATEGORIES:
        raise FeedbackError(
            "invalid feedback category: "
            + (category or "<empty>")
            + "; expected one of: "
            + ", ".join(FEEDBACK_CATEGORIES)
        )
    return category


def _normalized_feature(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return _feature_name(value)
    except TelemetryError as exc:
        raise FeedbackError(str(exc)) from exc


def _feedback_schema() -> dict[str, Any]:
    try:
        schema = json.loads(FEEDBACK_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeedbackError("invalid feedback schema") from exc
    if not isinstance(schema, dict):
        raise FeedbackError("invalid feedback schema")
    return schema


def _validate_feedback_document(document: dict[str, Any]) -> None:
    try:
        eval_schema.validate(document, _feedback_schema())
    except eval_schema.SchemaValidationError as exc:
        raise FeedbackError(f"feedback document violates schema: {exc}") from exc


def _build_document(repo: Path, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str] | None, str]:
    """Assemble and validate one feedback document; also return the host context."""
    text = _normalized_text(payload.get("text"))
    category = _normalized_category(payload.get("category") or "uncategorized")
    feature = _normalized_feature(payload.get("feature"))
    if feature and not _managed_repo_path(repo, Path("docs/specs/active") / feature).is_dir():
        raise FeedbackError("feature must exist under docs/specs/active")
    platform = _resolve_platform(payload.get("platform"))

    created_at = _now()
    project = telemetry_consent_state.project_fingerprint(repo)
    adapter = get_host_adapter(platform)
    if adapter is None:
        # Unregistered hosts still may submit; the schema only admits
        # registered host ids plus the explicit "unknown" marker.
        platform = "unknown"
    session = "unknown"
    context = None
    if adapter is not None:
        try:
            context = adapter.current_session_context(repo)
        except (OSError, ValueError):
            context = None
        if isinstance(context, dict):
            session = ("ses_" + hashlib.sha256(str(context["session_id"]).encode()).hexdigest()[:24]) if context.get("session_id") else "unknown"
    document = {
        "schema_version": SCHEMA_VERSION,
        "feedback_id": _feedback_id(text, created_at, project, session),
        "created_at": created_at,
        "text": text,
        "category": category,
        "plugin_version": _plugin_version_or_none(platform),
        "host": platform,
        "os": platform_module.system().lower() or "unknown",
        "python_version": platform_module.python_version(),
        "user_name": None,
        "project": project,
        "session": session,
        "feature": feature,
        "current_command": "feedback",
        "git_head": _git_head_or_none(repo),
        "related_entry_ids": _related_entry_ids(repo, feature),
    }
    _validate_feedback_document(document)
    return document, context, platform


# ── Realtime delivery: direct POST with a local outbox fallback ────────────

def _gzip_json(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    with BytesIO() as buffer:
        with gzip.GzipFile(fileobj=buffer, mode="wb") as compressed:
            compressed.write(raw)
        return buffer.getvalue()


def _post_item(endpoint: str, item: dict[str, Any]) -> str:
    """POST one item; return ``accepted``, ``rejected`` or ``unreachable``."""
    request = urllib.request.Request(
        endpoint,
        data=_gzip_json({"items": [item]}),
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=POST_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError):
        return "unreachable"
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unreachable"
    if response.status != 200:
        return "rejected"
    acknowledgements = body.get("items") if isinstance(body, dict) else None
    first = acknowledgements[0] if isinstance(acknowledgements, list) and acknowledgements else {}
    status = str(first.get("status") or "") if isinstance(first, dict) else ""
    if not isinstance(first, dict) or first.get("feedback_id") != item["feedback_id"]:
        return "unreachable"
    if status in {"accepted", "duplicate"}:
        return "accepted"
    if status == "retryable":
        return "unreachable"
    return "rejected"


def _private_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False)


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def prepare(repo: Path, payload: dict[str, Any], *, include_transcript: bool = False) -> dict[str, Any]:
    """Prepare an exact, redacted report for review without contacting the platform."""
    endpoint = _configured_endpoint()
    document, context, platform = _build_document(repo, payload)
    capture = (_collect_transcript(platform, context or {}) if include_transcript
               else {"supported": False, "reason": "not requested"})
    item = {"schema": FEEDBACK_REPORT_SCHEMA, "feedback_id": document["feedback_id"],
            "document": document, "transcript_stats": {k: v for k, v in capture.items() if k != "transcript_md"}}
    if capture.get("supported"):
        item["transcript_md"] = capture["transcript_md"]
    draft = {"endpoint": endpoint, "item": item}
    path = _feedback_home() / "drafts" / (document["feedback_id"] + ".json")
    _private_write(path, draft)
    return {"status": "prepared", "draft_path": str(path), "digest": _digest(draft), **draft}


def _read_draft(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    if path.is_symlink() or path.resolve().parent != (_feedback_home() / "drafts").resolve():
        raise FeedbackError("draft must be a regular file in the feedback drafts directory")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise FeedbackError("draft exceeds size limit")
    draft = json.loads(path.read_text(encoding="utf-8"))
    _validate_endpoint(draft["endpoint"])
    _validate_feedback_document(draft["item"]["document"])
    return draft


def _flush_outbox(endpoint: str) -> dict[str, int]:
    """Retry only reports previously approved for this exact destination."""
    counts = {"flushed": 0, "pending": 0, "expired": 0}
    for path in sorted((_feedback_home() / "outbox").glob("*.json")):
        if path.is_symlink():
            continue
        try:
            if datetime.now(timezone.utc).timestamp() - path.stat().st_mtime > TRANSCRIPT_RETENTION_SECONDS:
                path.unlink()
                counts["expired"] += 1
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("endpoint") != endpoint:
                counts["pending"] += 1
                continue
            if _post_item(endpoint, record["item"]) == "accepted":
                path.unlink()
                counts["flushed"] += 1
            else:
                counts["pending"] += 1
        except (ValueError, KeyError, OSError):
            counts["pending"] += 1
    return counts


def send(path: Path, digest: str, *, confirmed: bool = False) -> dict[str, Any]:
    if not confirmed:
        raise FeedbackError("sending requires explicit confirmation of the prepared content and destination")
    draft = _read_draft(path)
    if _digest(draft) != digest:
        raise FeedbackError("draft changed; prepare and confirm it again")
    endpoint, item = draft["endpoint"], draft["item"]
    flushed = _flush_outbox(endpoint)
    delivery = _post_item(endpoint, item)
    result = {"feedback_id": item["feedback_id"], "endpoint": endpoint, "delivery": delivery,
              "queue_flushed": flushed}
    if delivery == "accepted":
        path.unlink()
        return {"status": "delivered", **result}
    if delivery == "rejected":
        return {"status": "rejected", "draft_path": str(path), **result}
    queue = _feedback_home() / "outbox" / (item["feedback_id"] + ".json")
    if not queue.exists():
        _private_write(queue, draft)
    path.unlink()
    return {"status": "queued", "queue_path": str(queue), **result,
            "notice": "Start the configured platform. Pending feedback is retried on the next send to this address or explicit retry; retention is 30 days."}


def main() -> int:
    parser = argparse.ArgumentParser(description="HelixHarness feedback: use the published destination or a private config.json override")
    actions = parser.add_subparsers(dest="action", required=True)
    draft = actions.add_parser("prepare", help="prepare a local redacted draft; does not send")
    draft.add_argument("--repo-root", required=True)
    draft.add_argument("--platform", default="unknown")
    draft.add_argument("--text", required=True)
    draft.add_argument("--category", choices=FEEDBACK_CATEGORIES, default="uncategorized")
    draft.add_argument("--feature")
    draft.add_argument("--include-transcript", action="store_true")
    submit = actions.add_parser("send", help="send the exact confirmed draft")
    submit.add_argument("--draft", type=Path, required=True)
    submit.add_argument("--digest", required=True)
    submit.add_argument("--confirmed", action="store_true")
    retry = actions.add_parser("retry", help="retry approved queued reports to the currently configured address")
    retry.add_argument("--confirmed", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(Path(args.repo_root).expanduser().resolve(), vars(args), include_transcript=args.include_transcript)
        elif args.action == "send":
            result = send(args.draft, args.digest, confirmed=args.confirmed)
        else:
            if not args.confirmed:
                raise FeedbackError("retry requires explicit confirmation")
            result = {"status": "retried", **_flush_outbox(_configured_endpoint())}
    except (FeedbackError, TelemetryError, ValueError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("status") == "rejected" else 0


if __name__ == "__main__":
    sys.exit(main())
