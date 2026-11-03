"""Qoder session adapter for Lite telemetry only.

Qoder 的会话记录结构与 Claude Code 同构（`type` / `message` / `content` blocks），
本适配器只处理三处差异：

1. 配置根为 `~/.qoder-cn`（中文版）或 `~/.qoder`（国际版）；
2. 会话文件位于 `<config>/projects/{escaped-cwd}/{session-id}.jsonl`；
3. 记录中不含 token 用量，因此 `usage` 保持为空而不做估算。

会话上下文优先由本 Host Kit 的 SessionStart hook 写入
`.helixharness/host-session/qoder.json`；缺失时再按已安装环境变量或会话目录回退。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SESSION_MARKER = Path(".helixharness") / "host-session" / "qoder.json"


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


class QoderAdapter:
    host_id = "qoder"
    parser_version = "lite-qoder/1.0"

    @staticmethod
    def _config_home() -> Path:
        for name in ("QODERCN_CONFIG_DIR", "QODER_CONFIG_DIR"):
            value = _identity(os.environ.get(name))
            if value:
                return Path(value).expanduser()
        home = Path.home()
        for name in (".qoder-cn", ".qoder"):
            candidate = home / name
            if candidate.is_dir():
                return candidate
        return home / ".qoder-cn"

    def consent_cache_path(self, override: str | None = None) -> Path:
        if override:
            return Path(override).expanduser().resolve()
        return self._config_home() / "helixharness" / "telemetry-consent-cache.json"

    # -- session context -------------------------------------------------

    @staticmethod
    def _marker_path(repo: Path) -> Path:
        return repo.expanduser().resolve() / SESSION_MARKER

    def _marker_context(self, repo: Path) -> dict[str, str]:
        path = self._marker_path(repo)
        try:
            if path.is_symlink() or not path.is_file():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        session_id = _identity(data.get("session_id"))
        session_cwd = _identity(data.get("session_cwd"))
        if not session_id or not _belongs_to_repo(session_cwd, repo):
            return {}
        context = {"session_id": session_id, "session_cwd": session_cwd}
        transcript = _identity(data.get("transcript_path"))
        validated = self.validate_session_source(transcript, repo)
        if validated is None and _belongs_to_repo(data.get("session_cwd"), repo):
            # The host may omit transcript_path; locate the session file for
            # this repo and id instead of scanning arbitrary directories.
            validated = self._session_path(repo, session_id, session_cwd)
        if validated:
            context["session_log_path"] = validated
        return context

    def _session_path(
        self, repo: Path, session_id: str, session_cwd: str | None = None
    ) -> str | None:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            return None
        roots = [self._config_home() / "projects"]
        for root in roots:
            try:
                if not root.is_dir():
                    continue
            except OSError:
                continue
            for candidate in root.glob(f"*/{session_id}.jsonl"):
                validated = self.validate_session_source(candidate, repo, session_cwd)
                if validated:
                    return validated
        return None

    def current_session_context(self, repo: Path) -> dict[str, str] | None:
        context = self._marker_context(repo)
        if context:
            return context
        session_id = _identity(os.environ.get("HELIXHARNESS_LITE_SESSION_ID"))
        session_cwd = _identity(os.environ.get("HELIXHARNESS_LITE_SESSION_CWD"))
        if not session_id or not _belongs_to_repo(session_cwd, repo):
            return None
        result = {"session_id": session_id, "session_cwd": session_cwd}
        validated = self.validate_session_source(
            os.environ.get("HELIXHARNESS_LITE_SESSION_LOG_PATH"), repo, session_cwd
        ) or self._session_path(repo, session_id, session_cwd)
        if validated:
            result["session_log_path"] = validated
        return result

    # -- validation ------------------------------------------------------

    def validate_session_source(
        self, value: Any, repo: Path | None = None, session_cwd: str | None = None
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
        if session_cwd:
            cwd = _identity(self._recorded_cwd(path))
            if cwd and not _belongs_to_repo(cwd, Path(session_cwd)):
                return None
        return str(path)

    @staticmethod
    def _recorded_cwd(path: Path) -> str:
        """Read the first record carrying ``cwd``; never load the whole file."""

        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for ordinal, line in enumerate(stream):
                    if ordinal > 200:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        value = _identity(record.get("cwd"))
                        if value:
                            return value
        except OSError:
            return ""
        return ""

    # -- collection ------------------------------------------------------

    def collect_supplement_window(
        self,
        source: Path,
        *,
        started_at: str,
        ended_at: str,
        max_bytes: int | None = None,
    ):
        from .supplement_parsers import collect_qoder_window

        return collect_qoder_window(
            source,
            started_at=started_at,
            ended_at=ended_at,
            max_bytes=max_bytes,
        )
