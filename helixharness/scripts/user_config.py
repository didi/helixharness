#!/usr/bin/env python3
"""Inspect or update supported user settings through the existing authorities."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import reporting_config
import telemetry_consent
from atomic_files import atomic_write_text
from telemetry_hosts import get_host_adapter, normalize_host_id


class ConfigError(ValueError):
    """A setting cannot be safely read or updated."""


TELEMETRY_NOTICE_ID = 'helixharness-eval-data-notice-v1'
EVAL_PROTOCOL = Path(__file__).resolve().parents[1] / 'reference' / 'eval-protocol.md'


def telemetry_enable_notice() -> dict[str, str]:
    """Read the canonical notice; its digest is a version check, not UI attestation."""
    source = EVAL_PROTOCOL.read_text(encoding='utf-8')
    start = f'<!-- {TELEMETRY_NOTICE_ID}:start -->'
    end = f'<!-- {TELEMETRY_NOTICE_ID}:end -->'
    if source.count(start) != 1 or source.count(end) != 1:
        raise ConfigError('canonical telemetry data notice is missing or ambiguous')
    before, _, remaining = source.partition(start)
    section, _, _ = remaining.partition(end)
    if end in before:
        raise ConfigError('canonical telemetry data notice markers are out of order')
    lines = section.strip().splitlines()
    if len(lines) < 3 or lines[0] != '```text' or lines[-1] != '```':
        raise ConfigError('canonical telemetry data notice has an invalid format')
    text = '\n'.join(lines[1:-1])
    if not text.strip():
        raise ConfigError('canonical telemetry data notice is empty')
    return {'id': TELEMETRY_NOTICE_ID, 'text': text,
            'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()}


def channel_home(channel: str) -> Path:
    if channel not in {'telemetry', 'feedback'}:
        raise ConfigError('unsupported configuration channel')
    override = os.environ.get(f'HELIXHARNESS_{channel.upper()}_HOME')
    return Path(override).expanduser().absolute() if override else Path.home() / '.helixharness' / channel


def _safe_path(path: Path) -> None:
    for current in (*reversed(path.parents), path):
        if current.is_symlink():
            raise ConfigError('configuration path must not traverse a symlink')
    if path.exists() and not path.is_file():
        raise ConfigError('configuration target must be a regular file')


def _config(home: Path) -> dict[str, Any]:
    path = home / 'config.json'
    _safe_path(path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError('configuration file is unreadable or invalid JSON; original preserved') from exc
    if not isinstance(value, dict):
        raise ConfigError('configuration file must be an object; original preserved')
    return value


def describe_endpoint(channel: str) -> dict[str, Any]:
    home = channel_home(channel)
    default = (reporting_config.DEFAULT_REPORT_ENDPOINT if channel == 'telemetry'
               else reporting_config.DEFAULT_FEEDBACK_ENDPOINT)
    try:
        config = _config(home)
        resolved = reporting_config.endpoint_status(home, default)
        raw = config.get('endpoint', '')
        source = 'user' if isinstance(raw, str) and raw.strip() else 'published_default'
        if not resolved.get('ready'):
            source = 'invalid' if resolved['reason'] == 'endpoint_invalid' else 'unconfigured'
        return {**resolved, 'source': source, 'config_file': str(home / 'config.json')}
    except ConfigError as exc:
        return {'ready': False, 'reason': 'endpoint_invalid', 'source': 'invalid',
                'config_file': str(home / 'config.json'), 'error': str(exc)}


def set_endpoint(channel: str, endpoint: str | None, *, user_confirmed: bool) -> dict[str, Any]:
    if not user_confirmed:
        raise ConfigError('configuration change requires an explicit user request or selection')
    if endpoint is not None:
        endpoint = endpoint.strip()
        if not endpoint or not reporting_config.valid_endpoint(endpoint):
            raise ConfigError('endpoint must be HTTP(S) without credentials, query, fragment or whitespace')
    home = channel_home(channel)
    config = _config(home)
    if endpoint is None:
        config.pop('endpoint', None)
    else:
        config['endpoint'] = endpoint
    path = home / 'config.json'
    if endpoint is not None or path.exists():
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        _safe_path(path)
        atomic_write_text(path, json.dumps(config, ensure_ascii=False, indent=2) + '\n', mode=0o600)
    return {'updated': True, **describe_endpoint(channel)}


def consent_summary(repo: Path, platform: str) -> dict[str, Any]:
    # The pure API does not run the CLI resolve privacy-cleanup side effect.
    result = telemetry_consent.resolve_for_platform(repo, platform)
    keys = ('effective_mode', 'effective_scope', 'state', 'prompt_due',
            'allowed_actions', 'warning', 'reason')
    return {key: result[key] for key in keys if key in result}


def set_consent(repo: Path, platform: str, scope: str, mode: str, *, user_confirmed: bool) -> dict[str, Any]:
    if not user_confirmed:
        raise ConfigError('configuration change requires an explicit user request or selection')
    platform = normalize_host_id(platform)
    adapter = get_host_adapter(platform)
    if adapter is None:
        raise ConfigError('telemetry host is not supported')
    # Reuse project-root identity, consent epochs, scope precedence and cleanup.
    result = telemetry_consent.set_mode(
        telemetry_consent._repo_root(str(repo)), platform, adapter.consent_cache_path(None),
        scope, mode, consent_source='explicit-command', user_confirmed=True,
    )
    return {key: result[key] for key in ('updated', 'warning', 'effective_mode', 'effective_scope')}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', required=True)
    parser.add_argument('--platform', required=True)
    parser.add_argument('--scope', choices=('project', 'user'))
    parser.add_argument('--user-confirmed', action='store_true')
    parser.add_argument('--telemetry-notice-shown', metavar='SHA256',
                        help='internal: current notice digest, only after displaying its text to the user')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--telemetry', choices=('enabled', 'disabled'))
    action.add_argument('--report-endpoint')
    action.add_argument('--feedback-endpoint')
    action.add_argument('--reset-report-endpoint', action='store_true')
    action.add_argument('--reset-feedback-endpoint', action='store_true')
    args = parser.parse_args(argv)
    try:
        repo = Path(args.repo_root).expanduser().resolve()
        if not repo.is_dir():
            raise ConfigError('repo root is not a directory')
        if bool(args.scope) != bool(args.telemetry):
            raise ConfigError('--telemetry and --scope must be specified together')
        if args.telemetry_notice_shown is not None and args.telemetry != 'enabled':
            raise ConfigError('--telemetry-notice-shown is only valid when enabling telemetry')
        mutation = None
        notice_required = None
        if args.telemetry:
            if not args.user_confirmed:
                raise ConfigError('configuration change requires an explicit user request or selection')
            if args.telemetry == 'enabled':
                if get_host_adapter(normalize_host_id(args.platform)) is None:
                    raise ConfigError('telemetry host is not supported')
                notice = telemetry_enable_notice()
                if args.telemetry_notice_shown is None:
                    notice_required = notice
                elif args.telemetry_notice_shown != notice['sha256']:
                    raise ConfigError('telemetry data notice acknowledgment is invalid or stale; display the current notice first')
            if notice_required is None:
                mutation = set_consent(repo, args.platform, args.scope, args.telemetry,
                                       user_confirmed=args.user_confirmed)
        for channel, value, reset in (
            ('telemetry', args.report_endpoint, args.reset_report_endpoint),
            ('feedback', args.feedback_endpoint, args.reset_feedback_endpoint),
        ):
            if value is not None or reset:
                mutation = set_endpoint(channel, None if reset else value,
                                        user_confirmed=args.user_confirmed)
        # Completion describes this CLI request, not whether a setting changed.
        result: dict[str, Any] = {'read_only': mutation is None, 'request_complete': notice_required is None}
        if notice_required is not None:
            result.update({'change': {'updated': False, 'reason': 'telemetry_notice_required'},
                           'notice_required': True,
                           'next_action': 'display_notice_before_enable',
                           'notice': notice_required,
                           'continuation': {
                               'after': 'display_notice_to_user',
                               'action': 'repeat_same_request',
                               'same_turn': True,
                               'new_user_confirmation_required': False,
                               'append_arguments': ['--telemetry-notice-shown', notice_required['sha256']],
                           }})
        if mutation is not None:
            result['change'] = mutation
        # Report channels independently: a corrupt consent cache must not hide
        # the result of a successful endpoint edit or the other channel.
        try:
            result['telemetry'] = consent_summary(repo, args.platform)
        except (ValueError, OSError) as exc:
            result['telemetry'] = {'error': str(exc), 'effective_mode': 'unavailable'}
        result['report'] = describe_endpoint('telemetry')
        result['feedback'] = describe_endpoint('feedback')
        code = 0
    except (ValueError, OSError) as exc:
        result, code = {'error': str(exc)}, 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
