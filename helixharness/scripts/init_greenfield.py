#!/usr/bin/env python3
"""Classify a single Git checkout and manage its scanless Init state."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath

from atomic_files import atomic_write_text
from source_binding import guard_source


STATE_PATH = 'docs/.helixharness-init-state.json'
DOC_SUFFIXES = {'.md', '.markdown', '.rst', '.adoc', '.asciidoc'}
DOC_ASSET_SUFFIXES = {'.txt', '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'}
REPO_METADATA = {'.gitignore', '.gitattributes', '.gitkeep', '.editorconfig'}
DOCUMENT_NAMES = {'readme', 'license', 'licence', 'copying', 'notice', 'changelog'}
BUILD_TEXT_NAMES = {'cmakelists.txt', 'requirements.txt'}
DEPENDENCY_DIRS = {'node_modules', '.venv', 'venv', '.tox', '.nox', '__pycache__'}
INIT_FILES = {
    STATE_PATH, '.codex/hooks.json', '.claude/settings.json',
    '.cursor/hooks.json', '.cursor/settings.json',
    'docs/harness/linters/invariant-check.py', 'docs/harness/linters/invariant-check.sh',
    'docs/harness/linters/spec-sync-check.py', 'docs/harness/linters/spec-sync-check.sh',
}


class InitError(ValueError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ['git', '-C', str(root), *args], capture_output=True, timeout=15,
        env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'},
    )


def _require_git_root(root: Path) -> None:
    result = _git(root, 'rev-parse', '--show-toplevel')
    if result.returncode != 0:
        if os.path.lexists(root / '.git'):
            raise InitError('init_project_probe_failed', 'Git could not read this checkout. Check Git ownership or repository state.')
        raise InitError('init_git_repository_required', 'Use an existing Git checkout or run git init yourself in the project directory.')
    git_root = Path(os.fsdecode(result.stdout.rstrip(b'\r\n'))).resolve()
    if git_root != root:
        raise InitError('init_git_root_required', 'Run Init from the root of the intended Git checkout.')


def _paths(root: Path, *args: str) -> set[str]:
    result = _git(root, 'ls-files', '-z', *args)
    if result.returncode != 0:
        raise InitError('init_project_probe_failed', 'Could not list Git checkout files.')
    return {os.fsdecode(item) for item in result.stdout.split(b'\0') if item}


def _is_documentation_or_metadata(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1].casefold()
    suffix = PurePosixPath(name).suffix
    return (
        not path.endswith('/') and name not in BUILD_TEXT_NAMES and (
            path in INIT_FILES or path.startswith('docs/eval/') or name in REPO_METADATA
            or suffix in DOC_SUFFIXES
            or (name in DOCUMENT_NAMES or (suffix == '.txt' and name[:-4] in DOCUMENT_NAMES))
            or (parts[0].casefold() in {'docs', 'doc', 'documentation'} and suffix in DOC_ASSET_SUFFIXES)
        )
    )


def probe(root: Path) -> dict:
    tracked = _paths(root, '--cached')
    others = _paths(root, '--others', '--exclude-standard')
    # Consent resolution can create private lock anchors before bootstrap has
    # added any ignore rules. They are runtime data, not application signals.
    # Keep tracked files visible even under the reserved runtime directory.
    candidates = tracked | {
        path for path in others
        if not path.startswith('.helixharness/')
        and not DEPENDENCY_DIRS.intersection(PurePosixPath(path).parts)
    }
    signals = sorted(path for path in candidates if not _is_documentation_or_metadata(path))[:20]
    return {'empty': not signals, 'projectType': 'existing' if signals else 'empty', 'signals': signals}


def _state_path(root: Path) -> Path:
    state = root / STATE_PATH
    if state.parent.resolve() != state.parent or state.is_symlink():
        raise InitError('init_state_update_failed', 'Init state path must be inside the checkout and cannot be a link.')
    return state


def _write_state(path: Path, state: dict) -> dict:
    atomic_write_text(path, json.dumps(state, indent=2, ensure_ascii=False) + '\n')
    return state


def prepare(root: Path) -> dict:
    if not probe(root)['empty']:
        raise InitError('init_not_greenfield', 'Project files are present. Run the existing project detection flow.')
    path = _state_path(root)
    path.parent.mkdir(exist_ok=True)
    return _write_state(path, {
        'projectType': 'empty', 'detectionLevel': 'fast', 'confidence': 1.0,
        'techStack': {'hasBackend': False, 'hasFrontend': False},
        'scanPlan': [], 'userConfirmed': False, 'scanStatus': 'skipped',
        'bootstrapStatus': 'pending',
    })


def _verify_bootstrap(root: Path) -> None:
    directories = ['docs/hooks', 'docs/specs/active', 'docs/specs/completed']
    files = ['docs/harness/index.md', 'docs/knowledge/index.md', 'docs/knowledge/project-context.md']
    missing = []
    for relative in directories + files:
        target = root / relative
        if target.resolve() != target:
            missing.append(relative)
            continue
        valid = target.is_dir() if relative in directories else target.is_file() and target.stat().st_size > 0
        if not valid:
            missing.append(relative)
    if missing:
        raise InitError('init_required_artifact_missing', ', '.join(missing))
    context = (root / 'docs/knowledge/project-context.md').read_text(encoding='utf-8')
    if not re.search(r'^\s*(?:[-*] +)?(?:\*\*)?project-mode(?:\*\*)?:\s*greenfield\s*$', context, re.M):
        raise InitError('init_required_artifact_missing', 'project-context.md must declare project-mode: greenfield.')
    index = (root / 'docs/knowledge/index.md').read_text(encoding='utf-8')
    linked = re.search(r'\]\((?:\./)?project-context\.md\)', index)
    indexed = re.search(r'^\s*\|\s*(`?)(?:\./)?project-context\.md\1\s*\|', index, re.M)
    if not (linked or indexed):
        raise InitError('init_required_artifact_missing',
                        'knowledge/index.md must index project-context.md in a link or file-table entry.')


def complete(root: Path) -> dict:
    path = _state_path(root)
    try:
        if not path.is_file():
            raise ValueError('state is missing')
        state = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('projectType') != 'empty'
                or state.get('scanPlan') != [] or state.get('scanStatus') != 'skipped'
                or state.get('bootstrapStatus') not in ('pending', 'completed')):
            raise ValueError('state does not describe a scanless Init')
    except (ValueError, OSError) as exc:
        raise InitError('init_state_update_failed', str(exc)) from exc
    _verify_bootstrap(root)
    if state['bootstrapStatus'] == 'completed' and type(state.get('completedAt')) is int:
        return state
    state.update(bootstrapStatus='completed', completedAt=int(time.time()))
    return _write_state(path, state)


def main() -> int:
    guard_source(Path(__file__).resolve().parents[1])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', required=True, type=Path)
    parser.add_argument('action', choices=('probe', 'prepare', 'complete'))
    args = parser.parse_args()
    try:
        root = args.repo_root.resolve()
        _require_git_root(root)
        result = {'probe': probe, 'prepare': prepare, 'complete': complete}[args.action](root)
    except InitError as exc:
        print(json.dumps({'reason': exc.reason, 'detail': str(exc)}))
        return 2
    except (OSError, UnicodeError, subprocess.TimeoutExpired, RuntimeError) as exc:
        reason = 'init_project_probe_failed' if args.action == 'probe' else 'init_state_update_failed'
        print(json.dumps({'reason': reason, 'detail': str(exc)}))
        return 2
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
