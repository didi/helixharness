"""Public Lite routing, safe topology selection and archive resume behavior."""
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_router import route
from workflow_target import resolve_workflow_target, WorkflowTargetError
from workflow import WorkflowError
from host_runtime import HostRegistry

ROOT = Path(__file__).resolve().parents[2]

class PublicRouterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)

    def feature(self, status, scope='single'):
        path=self.repo/'docs/specs/active/demo'; path.mkdir(parents=True, exist_ok=True)
        (path/'spec.md').write_text(f'---\nfeature: demo\nexecution_scope: {scope}\nstatus: {status}\n---\nDemo spec\n')
        (path/'tasks.md').write_text('---\nfeature: demo\n---\nDemo tasks\n')

    def run_route(self, args, command='propose'):
        code, value=route(self.repo, command=command, arguments=args)
        self.assertNotIn("variant", value)
        return code,value

    def test_new_loop_and_literal_requirement(self):
        code,r=self.run_route('demo --loop -- "a literal --lite request"')
        self.assertEqual(code,0);self.assertTrue(r['loop_requested'])
        self.assertEqual(r['workflow'],'workflows/single/propose.md')
        self.assertIn('a literal --lite request',r['workflow_arguments'])

    def test_removed_address_option_does_not_disable_url_requirement_input(self):
        code, result = self.run_route('demo request --requirement-url https://tracker.example/1')
        self.assertEqual((code, result['reason']), (2, 'option_not_supported'))
        code, result = self.run_route('demo https://docs.example/requirements/1 --loop')
        self.assertEqual(code, 0)
        self.assertEqual(result['workflow_arguments'], 'demo https://docs.example/requirements/1')
        self.assertTrue(result['loop_requested'])

    def test_loop_resumes_first_unfinished_stage(self):
        for status,stage in [('planning','propose'),('ready','apply'),('applying','apply')]:
            self.feature(status)
            code,r=self.run_route('demo --loop')
            self.assertEqual((code,r['stage']),(0,stage))
            self.assertEqual(r['execution_scope'],'single')
            self.assertEqual(r['workflow'],f'workflows/single/{stage}.md')
        self.feature('implemented')
        with patch('workflow_router.archive_preflight',return_value={}):
            self.assertEqual(self.run_route('demo --loop')[1]['stage'],'archive')
        with patch('workflow_router.archive_preflight',side_effect=WorkflowError('scope drift')):
            self.assertEqual(self.run_route('demo --loop')[1]['stage'],'apply')

    def test_workspace_loop_routes_to_workspace_workflows(self):
        (self.repo/'helixharness.workspace.yaml').write_text('repos: []\n')
        code,r=self.run_route('demo "multi repo request" --loop')
        self.assertEqual(code,0);self.assertEqual(r['execution_scope'],'workspace')
        self.assertEqual(r['workflow'],'workflows/workspace/propose.md')
        self.feature('ready','workspace')
        result=self.run_route('demo --loop')[1]
        self.assertEqual((result['workflow'],result['execution_scope']),('workflows/workspace/apply.md','workspace'))
        self.feature('implemented','workspace')
        with patch('workflow_router.workspace_archive_preflight',return_value={}):
            result=self.run_route('demo --loop')[1]
            self.assertEqual((result['workflow'],result['execution_scope']),('workflows/workspace/archive.md','workspace'))

    def test_retired_flags_and_unsafe_features_are_blocked(self):
        for args in ['../escape request','demo request --lite','demo request --fusion','demo request --continue']:
            self.assertEqual(self.run_route(args)[0],2)
        self.assertEqual(self.run_route('demo',command='verify')[0],2)
        outside=self.repo/'outside';outside.mkdir()
        docs=self.repo/'docs';docs.symlink_to(outside,target_is_directory=True)
        self.assertEqual(self.run_route('demo request')[0],2)

    def test_bug_fix_and_archived_identity(self):
        self.feature('ready')
        code,r=self.run_route('demo "fix edge case"','bug-fix')
        self.assertEqual((code,r['action']),(0,'repair_feature'))
        path=self.repo/'docs/specs/completed/demo';path.mkdir(parents=True)
        self.assertEqual(self.run_route('demo --loop')[1]['reason'],'feature_already_archived')

    def test_explicit_workspace_revision_routes_to_propose_without_writes(self):
        (self.repo / 'helixharness.workspace.yaml').write_text('repos: []\n')
        for status in ('planning', 'ready', 'applying', 'implemented'):
            with self.subTest(status=status):
                self.feature(status, 'workspace')
                before = {str(p): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
                code, result = self.run_route('demo revised-requirement.md --revise --loop')
                self.assertEqual(code, 0, result)
                self.assertEqual(result['workflow'], 'workflows/workspace/propose.md')
                self.assertTrue(result['revision_requested'])
                self.assertTrue(result['loop_requested'])
                self.assertEqual(result['workflow_arguments'], 'demo revised-requirement.md')
                self.assertEqual(before, {str(p): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()})

    def test_workspace_revision_requires_source_and_cannot_reopen_finalizing(self):
        (self.repo / 'helixharness.workspace.yaml').write_text('repos: []\n')
        self.feature('applying', 'workspace')
        self.assertEqual(self.run_route('demo --revise')[1]['reason'], 'requirement_required')
        self.feature('completed', 'workspace')
        self.assertEqual(self.run_route('demo req --revise')[1]['reason'], 'workspace_revision_stage_invalid')
        self.assertEqual(self.run_route('demo req --revise --revise')[0], 2)

    def test_revision_flag_does_not_change_single_or_literal_propose_routing(self):
        self.feature('ready')
        self.assertEqual(self.run_route('demo req --revise')[1]['reason'], 'workspace_revision_requires_workspace')
        result = self.run_route('demo -- --revise')[1]
        self.assertEqual(result['action'], 'recommend_command')

    def test_bug_fix_target_only_reaches_preflight_without_writing(self):
        for status in ('planning', 'ready', 'applying', 'implemented'):
            with self.subTest(status=status):
                self.feature(status)
                before = {str(p): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()}
                code, result = self.run_route('demo', 'bug-fix')
                self.assertEqual((code, result['action']), (0, 'repair_feature'))
                self.assertEqual(result['workflow_arguments'], 'demo')
                self.assertEqual(result['workflow'], 'workflows/single/bug-fix.md')
                self.assertEqual(before, {str(p): p.read_bytes() for p in self.repo.rglob('*') if p.is_file()})

    def test_bug_fix_target_only_preserves_workspace_and_finalizing_guards(self):
        self.feature('completed')
        self.assertEqual(self.run_route('demo', 'bug-fix')[1]['reason'], 'bug_fix_stage_invalid')
        self.feature('ready', 'workspace')
        (self.repo / 'helixharness.workspace.yaml').write_text('repos: []\n')
        self.assertEqual(self.run_route('demo', 'bug-fix')[1]['reason'], 'bug_fix_workspace_not_supported')

    def test_archive_commit_selection_is_preserved_for_both_scopes(self):
        for scope in ('single', 'workspace'):
            self.feature('implemented', scope)
            if scope == 'workspace':
                (self.repo/'helixharness.workspace.yaml').write_text('repos: []\n')
            check = 'archive_preflight' if scope == 'single' else 'workspace_archive_preflight'
            with patch(f'workflow_router.{check}', return_value={}):
                for flag in ('--commit', '--no-commit'):
                    with self.subTest(scope=scope, flag=flag):
                        code, result = self.run_route(f'demo {flag}', 'archive')
                        self.assertEqual(code, 0)
                        self.assertEqual(result['workflow'], f'workflows/{scope}/archive.md')
                        self.assertEqual(result['execution_scope'], scope)
                        self.assertEqual(result['workflow_arguments'], f'demo {flag}')

    def test_archive_commit_selection_with_inferred_feature(self):
        self.feature('implemented')
        with patch('workflow_router.archive_preflight', return_value={}):
            code, result = self.run_route('--no-commit', 'archive')
        self.assertEqual(code, 0)
        self.assertEqual(result['workflow_arguments'], 'demo --no-commit')

    def test_archive_conflicting_commit_flags_are_blocked(self):
        self.feature('implemented')
        for flags in ('--commit --no-commit', '--no-commit --commit'):
            code, result = self.run_route(f'demo {flags}', 'archive')
            self.assertEqual(code, 2)
            self.assertEqual(result['reason'], 'commit_options_conflict')

    def test_commit_flags_are_not_enabled_for_other_commands(self):
        self.feature('ready')
        for command in ('propose', 'apply', 'bug-fix'):
            for flag in ('--commit', '--no-commit'):
                with self.subTest(command=command, flag=flag):
                    code, result = self.run_route(f'demo {flag}', command)
                    self.assertEqual(code, 2)
                    self.assertEqual(result['reason'], 'option_not_supported')

    def test_archive_literal_text_cannot_become_commit_authorization(self):
        self.feature('implemented')
        for args in ('demo -- --commit', 'demo -- --no-commit', 'demo extra'):
            code, result = self.run_route(args, 'archive')
            self.assertEqual(code, 2)
            self.assertEqual(result['reason'], 'archive_arguments_not_supported')

    def test_propose_prose_requests_name_without_creating_artifacts(self):
        for requirement in ('增加批量模型对比功能', '为页面增加 batch model 对比'):
            code, result = self.run_route(requirement)
            self.assertEqual((code, result['reason']), (2, 'feature_required'))
            code, result = route(self.repo, command='propose', feature='batch-model', arguments=requirement)
            self.assertEqual(code, 0)
            self.assertEqual(shlex.split(result['workflow_arguments']), ['batch-model', *shlex.split(requirement)])
        self.assertEqual(list(self.repo.iterdir()), [])

    def test_propose_explicit_names_preserve_requirement(self):
        for arguments, expected in (
            ('demo request', ['demo', 'request']),
            ('demo "feature=other request"', ['demo', 'feature=other request']),
            ('demo "需求名：other"', ['demo', '需求名：other']),
            ('--loop "中文需求" request', ['中文需求', 'request']),
            ('"中文需求" request', ['中文需求', 'request']),
            ('feature=demo request', ['demo', 'request']),
            ('需求名：demo request', ['demo', 'request']),
            ('feature = "demo" request --loop', ['demo', 'request']),
            ('demo -- "literal --loop and feature=other"', ['demo', 'literal --loop and feature=other']),
        ):
            with self.subTest(arguments=arguments):
                code, result = self.run_route(arguments)
                self.assertEqual(code, 0, result)
                self.assertEqual(shlex.split(result['workflow_arguments']), expected)

    def test_propose_rejects_conflicting_feature_context(self):
        code, result = route(self.repo, command='propose', feature='bound', arguments='other request')
        self.assertEqual((code, result['reason']), (2, 'feature_context_conflict'))

    def test_propose_without_arguments_resumes_unique_active_feature(self):
        self.feature('planning')
        code, result = self.run_route('')
        self.assertEqual(code, 0, result)
        self.assertEqual(result['feature'], 'demo')
        second = self.repo / 'docs/specs/active/other'
        second.mkdir()
        code, result = self.run_route('')
        self.assertEqual(result['action'], 'select_feature')
        self.assertEqual(result['candidates'], ['demo', 'other'])

    def test_every_routed_target_exists_and_is_not_a_public_entry(self):
        for scope in ('single', 'workspace'):
            marker = self.repo / 'helixharness.workspace.yaml'
            if scope == 'workspace':
                marker.write_text('repos: []\n')
            for stage in ('propose', 'apply', 'archive'):
                target = resolve_workflow_target(self.repo, stage)
                self.assertEqual(target['workflow'], f'workflows/{scope}/{stage}.md')
                self.assertTrue((ROOT / target['workflow']).is_file())
        with self.assertRaises(WorkflowTargetError):
            resolve_workflow_target(self.repo, 'bug-fix')

    def test_hosts_expose_exact_public_command_set(self):
        commands={'init','propose','apply','archive','ut','bug-fix','update','status','config'}
        self.assertEqual({p.stem for p in (ROOT/'commands').glob('*.md')},commands)
        methods={p.name for p in (ROOT/'skills').iterdir() if (p/'SKILL.md').is_file()}
        self.assertEqual({p.name for p in (ROOT/'hosts/codex/skills').iterdir() if p.is_dir()},commands | methods)
        registry=HostRegistry.discover(ROOT/'hosts')
        for host in registry.manifests():
            for key in host.adapters:
                if host.adapter_reference(key).kind=='python':
                    host.load_python_adapter(key)
        self.assertEqual(set(registry.supported_ids()),{'codex','claude-code'})
