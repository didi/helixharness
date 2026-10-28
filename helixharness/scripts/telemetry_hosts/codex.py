"""Codex rollout adapter for Lite telemetry only."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


THREAD_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
MAX_META_LINE_BYTES = 8 * 1024 * 1024


def _identity(value: Any) -> str:
    return str(value or "").strip()


def _belongs_to_repo(value: Any, repo: Path) -> bool:
    text = _identity(value)
    if not text:
        return False
    try:
        candidate = Path(text).expanduser().resolve(strict=True)
        root = repo.expanduser().resolve(strict=True)
    except OSError:
        return False
    if not candidate.is_dir() or (candidate != root and root not in candidate.parents):
        return False
    for directory in (candidate, *candidate.parents):
        if directory == root.parent:
            break
        try:
            if (directory / ".git").exists():
                return directory == root
        except OSError:
            return False
    return True


def _rollout_relative_path(thread_id: str) -> Path:
    compact = thread_id.replace("-", "")
    local = time.localtime(int(compact[:12], 16) / 1000)
    return (
        Path(time.strftime("%Y/%m/%d", local))
        / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', local)}-{thread_id}.jsonl"
    )


class CodexAdapter:
    host_id = "codex"
    parser_version = "lite-codex/1.0"

    @staticmethod
    def _config_home() -> Path:
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()

    def consent_cache_path(self, override: str | None = None) -> Path:
        if override:
            return Path(override).expanduser().resolve()
        return self._config_home() / "helixharness" / "telemetry-consent-cache.json"

    def _source_for_thread(self, thread_id: str) -> Path | None:
        normalized = _identity(thread_id).lower()
        if not THREAD_ID_PATTERN.fullmatch(normalized):
            return None
        return self._config_home() / "sessions" / _rollout_relative_path(normalized)

    def validate_session_source(
        self, value: Any, repo: Path | None = None
    ) -> str | None:
        del repo
        text = _identity(value)
        if not text:
            return None
        candidate = Path(text).expanduser()
        try:
            if candidate.is_symlink():
                return None
            path = candidate.resolve(strict=True)
            path.relative_to((self._config_home() / "sessions").resolve(strict=True))
        except (OSError, ValueError):
            return None
        if not path.is_file() or path.is_symlink() or path.suffix != ".jsonl":
            return None
        return str(path)

    def _source_cwd(self, path: Path, thread_id: str, repo: Path) -> str | None:
        expected = self._source_for_thread(thread_id)
        try:
            if expected is None or path.resolve(strict=True) != expected.resolve(strict=True):
                return None
            with path.open("rb") as handle:
                for _ in range(64):
                    raw = handle.readline(MAX_META_LINE_BYTES + 1)
                    if not raw or len(raw) > MAX_META_LINE_BYTES:
                        break
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict) or event.get("type") != "session_meta":
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    raw_id = _identity(payload.get("id") or payload.get("session_id")).lower()
                    if raw_id != thread_id or not _belongs_to_repo(payload.get("cwd"), repo):
                        return None
                    return str(Path(str(payload["cwd"])).expanduser().resolve())
        except OSError:
            return None
        return None

    def current_session_context(self, repo: Path) -> dict[str, str] | None:
        thread_id = _identity(os.environ.get("CODEX_THREAD_ID")).lower()
        source = self._source_for_thread(thread_id)
        validated = self.validate_session_source(source, repo) if source else None
        if not validated:
            return None
        cwd = self._source_cwd(Path(validated), thread_id, repo)
        if not cwd:
            return None
        return {
            "session_id": thread_id,
            "session_log_path": validated,
            "session_cwd": cwd,
        }

    def collect_supplement_window(
        self,
        source: Path,
        *,
        started_at: str,
        ended_at: str,
        max_bytes: int | None = None,
    ):
        from .supplement_parsers import collect_codex_window

        return collect_codex_window(
            source,
            started_at=started_at,
            ended_at=ended_at,
            max_bytes=max_bytes,
        )
