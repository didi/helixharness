#!/usr/bin/env python3
"""Route the single Lite lifecycle without inferring or migrating old artifacts."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from pathlib import Path
from source_binding import guard_source
from typing import Any

from workflow import ReviewScopeDrift, WorkflowError, archive_preflight, feature_name, frontmatter, read_regular
from workflow_target import WorkflowTargetError, resolve_workflow_target
from workspace import WorkspaceError, workspace_archive_preflight, workspace_revision_context

PUBLIC_COMMANDS = ('propose', 'apply', 'archive', 'bug-fix')


class RouterError(ValueError):
    """Invalid input or unsafe workflow state."""


def valid_feature_name(value: Any) -> bool:
    try:
        return isinstance(value, str) and feature_name(value) == value
    except WorkflowError:
        return False


def _repo_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise RouterError('repo_root_missing')
    return root


def _spec_root(repo: Path, root: str) -> Path:
    current = repo
    for part in ('docs', 'specs', root):
        current /= part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise RouterError('spec_root_unsafe')
    return current


def _feature_contract(repo: Path, feature: str) -> dict[str, Any]:
    if not valid_feature_name(feature):
        raise RouterError('feature_name_invalid')
    directory = _spec_root(repo, 'active') / feature
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise RouterError('feature_directory_unsafe')
    path = directory / 'spec.md'
    if not path.exists():
        if directory.exists() and any(directory.iterdir()):
            raise RouterError('workflow_artifact_conflict')
        return {'feature': feature, 'exists': False, 'status': None, 'execution_scope': None}
    fields, _, _ = frontmatter(read_regular(path, 'spec.md'), 'spec.md')
    if fields.get('feature') != feature:
        raise RouterError('workflow_artifact_conflict')
    scope = fields.get('execution_scope')
    target = resolve_workflow_target(repo, 'propose', scope)
    if target['execution_scope'] == 'single':
        tasks_path = directory / 'tasks.md'
        if tasks_path.exists():
            tasks, _, _ = frontmatter(read_regular(tasks_path, 'tasks.md'), 'tasks.md')
            if tasks.get('feature') != feature:
                raise RouterError('workflow_artifact_conflict')
    return {'feature': feature, 'exists': True, 'status': fields.get('status'),
            'execution_scope': target['execution_scope']}


def _archive_ready(
    repo: Path, contract: dict[str, Any], *, allow_review_choice: bool = False
) -> bool:
    if contract['status'] not in {'implemented', 'completed'}:
        return False
    try:
        if contract['execution_scope'] == 'workspace':
            workspace_archive_preflight(repo, contract['feature'], allow_review_choice=allow_review_choice)
        else:
            archive_preflight(repo, contract['feature'], allow_review_acceptance=allow_review_choice)
        return True
    except ReviewScopeDrift as exc:
        return allow_review_choice and not exc.finalizing
    except (WorkflowError, WorkspaceError):
        return False


def route(repo: Path, *, command: str, feature: str | None = None,
          arguments: str | None = None, arguments_mode: str = 'auto') -> tuple[int, dict[str, Any]]:
    """Return a single explicit workflow and stage; never write business artifacts."""
    base: dict[str, Any] = {'command': command, 'workflow': None,
                            'recommendation': None}
    try:
        if command not in PUBLIC_COMMANDS:
            raise RouterError('command_not_supported')
        raw_arguments = arguments or ''
        lexer = shlex.shlex(raw_arguments, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ''
        tokens = []
        quoted = []
        while True:
            offset = lexer.instream.tell()
            token = lexer.get_token()
            if token is None:
                break
            tokens.append(token)
            quoted.append(raw_arguments[offset:lexer.instream.tell()].lstrip().startswith(('"', "'")))
        positionals: list[str] = []
        commit_option = None
        loop = False
        revise = False
        marked_feature = None
        first_positional_quoted = False
        literal = False
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if literal:
                if not positionals:
                    first_positional_quoted = quoted[index]
                positionals.append(token)
            elif command == 'propose' and not quoted[index] and (
                re.match(r'^(?:feature\s*=|需求名[：:])', token)
                or (token == 'feature' and index + 1 < len(tokens) and tokens[index + 1] == '=')
            ):
                if token == 'feature':
                    index += 1
                    candidate = ''
                else:
                    candidate = re.sub(r'^(?:feature\s*=|需求名[：:])', '', token)
                if not candidate:
                    index += 1
                    if index >= len(tokens):
                        raise RouterError('feature_required')
                    candidate = tokens[index]
                if marked_feature is not None and marked_feature != candidate:
                    raise RouterError('feature_context_conflict')
                marked_feature = candidate
            elif token == '--':
                literal = True
            elif token == '--loop' and command == 'propose':
                if loop:
                    raise RouterError('duplicate_loop_option')
                loop = True
            elif token == '--revise' and command == 'propose':
                if revise:
                    raise RouterError('duplicate_revision_option')
                revise = True
            elif token in {'--commit', '--no-commit'} and command == 'archive':
                if commit_option is not None and commit_option != token:
                    raise RouterError('commit_options_conflict')
                commit_option = token
            elif token.startswith('--'):
                raise RouterError('option_not_supported')
            else:
                if not positionals:
                    first_positional_quoted = quoted[index]
                positionals.append(token)
            index += 1
        argument_feature = marked_feature
        if command == 'propose' and argument_feature is None and positionals:
            candidate = positionals[0]
            if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', candidate) or first_positional_quoted:
                argument_feature = candidate
        if feature:
            if command == 'propose' and argument_feature is not None and argument_feature != feature:
                raise RouterError('feature_context_conflict')
            if marked_feature is None and positionals and positionals[0] == feature:
                positionals.pop(0)
        elif command == 'propose' and argument_feature is not None:
            feature = argument_feature
            if marked_feature is None:
                positionals.pop(0)
        elif positionals:
            if command == 'propose':
                raise RouterError('feature_required')
            feature = positionals.pop(0)
        else:
            active = _spec_root(repo, 'active')
            choices = sorted(p.name for p in active.iterdir()) if active.exists() else []
            if len(choices) == 1:
                feature = choices[0]
            elif choices:
                return 0, {**base, 'action': 'select_feature', 'candidates': choices, 'reason': 'feature_required'}
            else:
                raise RouterError('feature_required')
        if not valid_feature_name(feature):
            raise RouterError('feature_name_invalid')
        if command == 'archive' and positionals:
            raise RouterError('archive_arguments_not_supported')
        base.update(feature=feature, loop_requested=loop)
        if os.path.lexists(_spec_root(repo, 'completed') / feature):
            raise RouterError('feature_already_archived')
        contract = _feature_contract(repo, feature)
        status = contract['status']
        base.update(status=status)
        if revise:
            if not contract['exists']:
                raise RouterError('feature_not_found')
            if contract['execution_scope'] != 'workspace':
                raise RouterError('workspace_revision_requires_workspace')
            if status not in {'planning', 'ready', 'applying', 'implemented'}:
                raise RouterError('workspace_revision_stage_invalid')
            if not positionals:
                raise RouterError('requirement_required')
            target = resolve_workflow_target(repo, 'propose', 'workspace')
            return 0, {**base, **target, 'action': 'load_workflow', 'stage': 'propose',
                       'revision_requested': True, 'reason': 'workspace_revision_requested',
                       'workflow_arguments': shlex.join([feature, *positionals])}
        if command == 'propose' and contract['exists'] and len(positionals) == 1:
            revision = workspace_revision_context(repo, feature, positionals[0])
            if revision is not None and revision['required']:
                target = resolve_workflow_target(repo, 'propose', 'single')
                return 0, {**base, **target, 'action': 'load_workflow', 'stage': 'propose',
                           'reason': 'workspace_contract_revision',
                           'workspace_revision': revision,
                           'workflow_arguments': shlex.join([feature, *positionals])}
        if not contract['exists']:
            if command != 'propose':
                raise RouterError('feature_not_found')
            if not positionals:
                raise RouterError('requirement_required')
            stage = 'propose'
        elif command == 'bug-fix':
            if contract['execution_scope'] == 'workspace':
                raise RouterError('bug_fix_workspace_not_supported')
            if status not in {'planning', 'ready', 'applying', 'implemented'}:
                raise RouterError('bug_fix_stage_invalid')
            # The workflow's repair-preflight decides whether to resume persisted
            # repair state or request a source. Missing source is not a route error.
            return 0, {**base, 'action': 'repair_feature', 'reason': 'bug_fix_active',
                       'execution_scope': 'single', 'workflow': 'workflows/single/bug-fix.md',
                       'workflow_arguments': shlex.join([feature, *positionals])}
        else:
            if status == 'planning':
                next_stage = 'propose'
            elif status in {'ready', 'applying'}:
                next_stage = 'apply'
            elif status in {'implemented', 'completed'}:
                next_stage = 'archive' if _archive_ready(
                    repo, contract, allow_review_choice=command != 'apply'
                ) else 'apply'
            else:
                raise RouterError('lite_stage_invalid')
            stage = next_stage if loop else command
            if stage != next_stage:
                return 0, {**base, 'action': 'recommend_command', 'reason': 'lite_stage_already_advanced',
                           'recommendation': f'/helix:{next_stage} {shlex.quote(feature)}'}
            if stage in {'apply', 'archive'} and contract['execution_scope'] == 'single':
                read_regular(_spec_root(repo, 'active') / feature / 'tasks.md', 'tasks.md')
        target = resolve_workflow_target(repo, stage, contract['execution_scope'])
        workflow_arguments = [feature, *positionals]
        if commit_option is not None:
            workflow_arguments.append(commit_option)
        return 0, {**base, **target, 'action': 'load_workflow', 'stage': stage,
                   'reason': 'loop_orchestrated' if loop else 'workflow_resolved',
                   'workflow_arguments': shlex.join(workflow_arguments)}
    except (ValueError, WorkflowError, WorkspaceError, WorkflowTargetError, OSError) as exc:
        return 2, {**base, 'action': 'blocked', 'reason': str(exc)}


def main(argv: list[str] | None = None) -> int:
    guard_source(Path(__file__).resolve().parent.parent)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    router = commands.add_parser('route')
    router.add_argument('--command', choices=PUBLIC_COMMANDS, required=True)
    router.add_argument('--feature')
    router.add_argument('--arguments', default='')
    router.add_argument('--arguments-mode', choices=('auto',), default='auto')
    args = parser.parse_args(argv)
    try:
        code, result = route(_repo_root(args.repo_root), command=args.command, feature=args.feature,
                             arguments=args.arguments, arguments_mode=args.arguments_mode)
    except RouterError as exc:
        code, result = 2, {'action': 'blocked', 'reason': str(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
