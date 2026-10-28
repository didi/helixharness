"""Shared Claude/Codex provider loading for server and background workers."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_PROVIDER_ENV_FILES = ("claude.env", "codex.env")
_DEFAULT_CLAUDE_BASE_URL = "https://api.anthropic.com"
_ENV_REFERENCE_PATTERN = re.compile(
    r"\$(?:\{[A-Za-z_][A-Za-z0-9_]*\}|[A-Za-z_][A-Za-z0-9_]*)"
)


@dataclass(frozen=True)
class EvaluationProviderConfig:
    """One platform provider shared by execution and final evaluation."""

    platform: str
    provider: str
    base_url: str
    api_key: str
    model: str
    api_key_env: str


def codex_responses_base_url(base_url: str) -> str:
    """Normalize a configured Codex proxy URL for the Responses API."""
    normalized = base_url.strip().rstrip("/")
    if not normalized or normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def load_runtime_secrets(secrets_dir: Path | None = None) -> None:
    """Load Claude/Codex providers without overriding explicit process values."""
    directory = secrets_dir or Path(__file__).resolve().parent / ".secrets"
    if not directory.is_dir():
        return
    for filename in _PROVIDER_ENV_FILES:
        env_file = directory / filename
        if not env_file.is_file():
            continue
        for raw_line in env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                expanded_value = os.path.expandvars(value.strip())
                if _ENV_REFERENCE_PATTERN.search(expanded_value):
                    raise ValueError(
                        f"unresolved environment reference in {filename}: {key}"
                    )
                os.environ[key] = expanded_value


def _configured_value(*keys: str, default: str = "") -> tuple[str, str]:
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value, key
    return default, keys[0]


def evaluation_provider_config(platform: str) -> EvaluationProviderConfig:
    """Resolve the Claude or Codex provider through one stable interface."""
    normalized = (platform or "claude-code").strip().lower()
    if normalized in {"claude", "claude-code", "anthropic"}:
        base_url, _ = _configured_value(
            "HELIXHARNESS_PLATFORM_EVALUATION_CLAUDE_BASE_URL",
            default=_DEFAULT_CLAUDE_BASE_URL,
        )
        api_key, api_key_env = _configured_value(
            "HELIXHARNESS_PLATFORM_EVALUATION_CLAUDE_API_KEY",
        )
        model, _ = _configured_value(
            "HELIXHARNESS_PLATFORM_EVALUATION_CLAUDE_MODEL",
            default="claude-sonnet-4-20250514",
        )
        return EvaluationProviderConfig(
            platform="claude-code",
            provider="claude",
            base_url=base_url,
            api_key=api_key,
            model=model,
            api_key_env=api_key_env,
        )
    if normalized == "codex":
        base_url, _ = _configured_value(
            "HELIXHARNESS_PLATFORM_EVALUATION_CODEX_BASE_URL",
        )
        api_key, api_key_env = _configured_value(
            "HELIXHARNESS_PLATFORM_EVALUATION_CODEX_API_KEY",
        )
        model, _ = _configured_value(
            "HELIXHARNESS_PLATFORM_EVALUATION_CODEX_MODEL",
        )
        return EvaluationProviderConfig(
            platform="codex",
            provider="codex",
            base_url=base_url,
            api_key=api_key,
            model=model,
            api_key_env=api_key_env,
        )
    raise ValueError(f"unsupported evaluation provider platform: {platform}")
