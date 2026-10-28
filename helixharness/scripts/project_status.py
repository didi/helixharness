#!/usr/bin/env python3
"""Read existing workflow artifacts without advancing or repairing a feature."""
from __future__ import annotations

import argparse
import json
import os
import shlex
from collections import Counter
from pathlib import Path
from typing import Any

from workflow import (
    WorkflowError, archive_preflight, completed_archive_preflight, feature_name,
    frontmatter, read_regular, spec_contract, task_rows, validate_task_graph,
    validate_tasks,
)
from workflow_target import resolve_workflow_target
from workspace import (
    WorkspaceError, validate_workspace_overview, workspace_archive_preflight,
    workspace_completed_preflight,
)


def spec_root(repo: Path, state: str) -> Path:
    current = repo
    for part in ('docs', 'specs', state):
        current /= part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise WorkflowError('spec root is missing or unsafe')
    return current


def inspect_feature(repo: Path, feature: str, state: str = 'active', *,
                    expected_repo: str | None = None,
                    workspace_receipt: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        'feature': feature, 'location': state, 'status': None,
        'tasks': None, 'issues': [], 'next_command': None,
        'archive_check': 'not_checked',
    }
    try:
        directory = spec_root(repo, state) / feature_name(feature)
        if directory.is_symlink() or not directory.is_dir():
            raise WorkflowError('feature directory is missing or unsafe')
        other = 'completed' if state == 'active' else 'active'
        if os.path.lexists(spec_root(repo, other) / feature):
            raise WorkflowError('feature exists in both active and completed')
        spec = spec_contract(read_regular(directory / 'spec.md', 'spec.md'))
        if spec['feature'] != feature:
            raise WorkflowError('spec.md feature does not match its directory')
        status = spec['status']
        result['status'] = status
        scope = resolve_workflow_target(repo, 'propose', spec.get('execution_scope'))['execution_scope']
        result['execution_scope'] = scope
        if expected_repo is not None and (
            scope != 'single' or spec.get('workspace_repo') != expected_repo
            or spec.get('workspace_contract_sha256') != workspace_receipt
        ):
            raise WorkflowError('workspace child identity or contract receipt is invalid')
        stage = 'propose' if status == 'planning' else 'apply'
        if status in {'implemented', 'completed'}:
            stage = 'archive'
        if state == 'active':
            result['next_command'] = f'/helix:{stage} {shlex.quote(feature)}'
        if scope == 'workspace':
            overview = validate_workspace_overview(
                repo, feature, 'completed' if state == 'completed' else stage, state=state,
            )
            children = []
            for item in overview['repositories']:
                child_root = Path(item['path'])
                child_state = 'completed' if os.path.lexists(spec_root(child_root, 'completed') / feature) else 'active'
                child = inspect_feature(child_root, feature, child_state,
                                        expected_repo=item['repo'],
                                        workspace_receipt=overview['spec_receipt_sha256'])
                child['repo'] = item['repo']
                child['depends_on'] = item['depends_on']
                children.append(child)
            result['repositories'] = children
            if any(child['issues'] for child in children):
                result['issues'].append('workspace repositories have unresolved issues; see repositories')
        else:
            tasks_path = directory / 'tasks.md'
            if status == 'planning' and not os.path.lexists(tasks_path):
                result['issues'].append('tasks.md not generated yet')
            else:
                tasks_text = read_regular(tasks_path, 'tasks.md')
                tasks, _, _ = frontmatter(tasks_text, 'tasks.md')
                if tasks.get('feature') != feature:
                    raise WorkflowError('tasks.md feature does not match its directory')
                rows = task_rows(tasks_text)
                counts = Counter(row['status'] for row in rows)
                result['tasks'] = {'total': len(rows), **{
                    key: counts[key] for key in ('pending', 'in_progress', 'blocked', 'completed')
                }}
                result['blocked_tasks'] = [row for row in rows if row['status'] == 'blocked']
                result['next_task'], result['in_progress_tasks'] = validate_task_graph(rows)
                if result['blocked_tasks']:
                    result['issues'].append('tasks.md contains blocked tasks')
                if state == 'active' and stage != 'archive':
                    validate_tasks(repo, feature, stage)
        if state == 'completed' or stage == 'archive':
            result['archive_check'] = 'failed'
            if scope == 'workspace':
                check = workspace_completed_preflight if state == 'completed' else workspace_archive_preflight
            else:
                check = completed_archive_preflight if state == 'completed' else archive_preflight
            check(repo, feature)
            result['archive_check'] = 'passed'
            if state == 'completed':
                result['status'] = 'archived'
    except (ValueError, OSError) as exc:
        result['issues'].append(str(exc))
        # A failed archive gate is a diagnostic, not permission to rerun Apply.
        if result['archive_check'] == 'failed':
            result['next_command'] = None
    return result


def snapshot(repo: Path, feature: str | None = None, *, completed: bool = False) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    if not repo.is_dir():
        raise WorkflowError('repo root is not a directory')
    states = ['completed'] if completed else ['active']
    if feature and not completed:
        feature_name(feature)
        if not os.path.lexists(spec_root(repo, 'active') / feature):
            states = ['completed']
    features = []
    for state in states:
        root = spec_root(repo, state)
        names = [feature] if feature else sorted(p.name for p in root.iterdir()) if root.exists() else []
        features.extend(inspect_feature(repo, name, state) for name in names)
    return {'read_only': True, 'features': features, 'empty': not features,
            'note': 'Existing artifact checks only; tests and model reviews were not rerun.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', required=True)
    parser.add_argument('--feature')
    parser.add_argument('--completed', action='store_true')
    args = parser.parse_args()
    try:
        result = snapshot(Path(args.repo_root), args.feature, completed=args.completed)
        code = 0
    except (WorkflowError, WorkspaceError, OSError) as exc:
        result, code = {'error': str(exc), 'read_only': True}, 2
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
