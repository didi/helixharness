"""Consent-gated stage facts for OpenCode; no native transcript collection."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from telemetry_session import SessionContextUnavailable


class OpenCodeAdapter:
    host_id = "opencode"
    parser_version = "lite-opencode-stage/1.0"
    strict_private_cleanup = True
    stage_facts_only = True

    def consent_cache_path(self, override: str | None = None) -> Path:
        if override:
            return Path(override).expanduser().resolve()
        home = Path(os.environ.get("HELIXHARNESS_OPENCODE_HOME") or
                    Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "opencode")
        return home.expanduser() / "helixharness/telemetry-consent-cache.json"

    def current_session_context(self, repo: Path) -> dict[str, str]:
        identity = os.environ.get("HELIXHARNESS_SESSION_ID", "")
        directory = os.environ.get("HELIXHARNESS_SESSION_CWD", "")
        if not re.fullmatch(r"ses_[A-Za-z0-9_-]{1,160}", identity) or not Path(directory).is_absolute():
            raise SessionContextUnavailable("opencode_session_identity_unavailable")
        try:
            root = repo.resolve(strict=True)
            if not root.is_dir() or Path(directory).resolve(strict=True) != root:
                raise ValueError()
        except (OSError, ValueError):
            raise SessionContextUnavailable("opencode_session_identity_unavailable") from None
        return {"session_id": identity, "session_cwd": str(root)}

    def prepare_session_source(self, repo: Path, context: dict[str, Any], consent_epoch: str) -> dict[str, str]:
        del consent_epoch
        bound = self.current_session_context(repo)
        if context.get("session_id") != bound["session_id"]:
            raise SessionContextUnavailable("opencode_session_identity_unavailable")
        # Explicitly strip any other host's inherited source environment.
        return bound

    def validate_session_source(self, value: Any, repo: Path | None = None) -> None:
        return None

    def collect_supplement_window(self, source: Path, *, started_at: str, ended_at: str,
                                  max_bytes: int | None = None):
        raise SessionContextUnavailable("opencode_transcript_collection_unsupported")
