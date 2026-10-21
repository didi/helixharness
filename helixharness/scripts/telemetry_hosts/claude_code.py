"""Claude Code adapter for Lite telemetry only."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _identity(value: Any) -> str:
    return str(value or "").strip()


def _belongs_to_repo(value: Any, repo: Path) -> bool:
    text = _identity(value)
    if not text:
        return False
    try:
        candidate = Path(text).expanduser().resolve()
        root = repo.expanduser().resolve()
    except OSError:
        return False
    return candidate == root or root in candidate.parents


class ClaudeCodeAdapter:
    host_id = "claude-code"
    parser_version = "lite-claude-code/1.0"

    @staticmethod
    def _config_home() -> Path:
        return Path(
            os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
        ).expanduser()

    def consent_cache_path(self, override: str | None = None) -> Path:
        if override:
            return Path(override).expanduser().resolve()
        return self._config_home() / "helixharness" / "telemetry-consent-cache.json"

    def current_session_context(self, repo: Path) -> dict[str, str] | None:
        session_id = _identity(os.environ.get("HELIXHARNESS_LITE_SESSION_ID"))
        session_source = _identity(
            os.environ.get("HELIXHARNESS_LITE_SESSION_LOG_PATH")
        )
        session_cwd = _identity(os.environ.get("HELIXHARNESS_LITE_SESSION_CWD"))
        if not session_id or not _belongs_to_repo(session_cwd, repo):
            return None
        context = {"session_id": session_id, "session_cwd": session_cwd}
        validated = self.validate_session_source(session_source, repo)
        if validated:
            context["session_log_path"] = validated
        return context

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
            path.relative_to((self._config_home() / "projects").resolve(strict=True))
        except (OSError, ValueError):
            return None
        if not path.is_file() or path.is_symlink() or path.suffix != ".jsonl":
            return None
        return str(path)

    def collect_supplement_window(
        self,
        source: Path,
        *,
        started_at: str,
        ended_at: str,
        max_bytes: int | None = None,
    ):
        from .supplement_parsers import collect_claude_window

        return collect_claude_window(
            source,
            started_at=started_at,
            ended_at=ended_at,
            max_bytes=max_bytes,
        )
