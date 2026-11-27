from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import ut_artifacts as ut
except ModuleNotFoundError:
    ut = None

class UtArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.directory = self.repo / 'docs/specs/active/sample'
        self.directory.mkdir(parents=True)
        (self.repo/'README.md').write_text('Invalid percentages must be rejected.\n',encoding='utf-8')
        (self.repo/'evidence.log').write_text('FAIL: expected rejection; returned -1\n',encoding='utf-8')
        self.assertIsNotNone(ut, 'UT artifacts implementation is required')
    def stage(self, name, text):
        p=self.directory/(name+'.tmp');p.write_text(text,encoding='utf-8');return p
    def observation(self, category='confirmed_defect', **kwargs):
        d=dict(id='D1',classification=category,title='Percentage is not rejected',actual_behavior='Returns -1',severity='Low',basis='contract',normative_ref={'path':'README.md','quote':'Invalid percentages must be rejected.'},reproducer={'command':'synthetic test','expected':'rejection','actual':'returned -1','evidence_path':'evidence.log','evidence_quote':'FAIL: expected rejection; returned -1'})
        d.update(kwargs);return d
    def findings(self, worker, observations):
        src=self.stage(f'worker-{worker}-findings.json',json.dumps({'worker_id':worker,'class_name':'synthetic.Rules','observations':observations}))
        return ut.publish_findings(self.repo,'sample',worker,src)
    def test_supported_contract_violation_counts(self):
        result=self.findings(1,[self.observation()]);self.assertEqual(result['counts']['confirmed_defect'],1)
        self.assertIn('已证实缺陷', (self.directory/'worker-1-defects.md').read_text())
    def test_unsupported_expectations_are_preserved_as_risks(self):
        obs=[self.observation(id=f'D{i}',title=title,normative_ref=None) for i,title in enumerate(('Unicode whitespace','Different messages','NPE instead of IAE','Different rounding','Javadoc'),1)]
        result=self.findings(1,obs);self.assertEqual(result['counts']['confirmed_defect'],0);self.assertEqual(result['counts']['unconfirmed_risk'],5)
        doc=json.loads((self.directory/'worker-1-findings.json').read_text());self.assertEqual(len(doc['observations']),5)
        self.assertTrue(all(x['classification_reason'] for x in doc['observations']))
    def test_misquoted_or_outside_contract_cannot_confirm(self):
        for ref in ({'path':'README.md','quote':'Must throw NPE'}, {'path':'../outside.md','quote':'x'}):
            with self.subTest(ref=ref):self.assertEqual(self.findings(1,[self.observation(normative_ref=ref)])['counts']['confirmed_defect'],0)
    def test_missing_or_identical_counterexample_cannot_confirm(self):
        for value in (None, {'expected':'x','actual':'x'}, {'expected':'x','actual':'y','evidence_path':'evidence.log','evidence_quote':'not in the log'}):
            with self.subTest(value=value):self.assertEqual(self.findings(1,[self.observation(reproducer=value)])['counts']['confirmed_defect'],0)
    def test_optional_improvement_never_counts_as_defect(self):
        r=self.findings(1,[self.observation('optional_improvement',normative_ref=None,reproducer=None)]);self.assertEqual(r['counts']['optional_improvement'],1);self.assertEqual(r['counts']['confirmed_defect'],0)
    def test_mechanical_failure_requires_reproduced_evidence(self):
        (self.repo/'Rules.java').write_text('return 1 / 0;')
        (self.repo/'exception.log').write_text('java.lang.ArithmeticException: / by zero')
        r=self.findings(1,[self.observation(basis='mechanical',mechanical_kind='inevitable_exception',observed_exception='ArithmeticException',code_ref={'path':'Rules.java','quote':'return 1 / 0;'},normative_ref=None,reproducer={'command':'synthetic runtime test','expected':'returns a result','actual':'ArithmeticException','evidence_path':'exception.log','evidence_quote':'java.lang.ArithmeticException: / by zero'})])
        self.assertEqual(r['counts']['confirmed_defect'],1)
        r=self.findings(1,[self.observation(basis='mechanical',mechanical_kind='style',normative_ref=None)])
        self.assertEqual(r['counts']['confirmed_defect'],0)
    def test_sanitized_project_name_can_supply_temp_evidence(self):
        renamed=self.repo.parent/'My App';self.repo.rename(renamed);self.repo=renamed;self.directory=self.repo/'docs/specs/active/sample'
        base=Path(self.tmp.name)/'os-temp';worker=base/'my-app/worker-1';worker.mkdir(parents=True)
        log=worker/'evidence.log';log.write_text('FAIL: expected rejection; returned -1')
        repro={'command':'test','expected':'rejection','actual':'returned -1','evidence_path':str(log.resolve()),'evidence_quote':'FAIL: expected rejection; returned -1'}
        with patch('ut_artifacts.tempfile.gettempdir',return_value=str(base)):
            self.assertEqual(self.findings(1,[self.observation(reproducer=repro)])['counts']['confirmed_defect'],1)
    def test_symlink_worker_evidence_root_cannot_escape(self):
        outside=Path(self.tmp.name)/'outside';outside.mkdir();(outside/'evidence.log').write_text('FAIL: expected rejection; returned -1')
        base=Path(self.tmp.name)/'os-temp';project=base/self.repo.name;project.mkdir(parents=True)
        (project/'worker-1').symlink_to(outside,target_is_directory=True)
        repro={'command':'test','expected':'rejection','actual':'returned -1','evidence_path':str((outside/'evidence.log').resolve()),'evidence_quote':'FAIL: expected rejection; returned -1'}
        with patch('ut_artifacts.tempfile.gettempdir',return_value=str(base)):
            self.assertEqual(self.findings(1,[self.observation(reproducer=repro)])['counts']['confirmed_defect'],0)
    def test_generated_classification_reports_cannot_be_overwritten_by_write(self):
        for name in ('unittest-defects.md','worker-1-defects.md'):
            with self.subTest(name=name),self.assertRaises(ValueError):ut.publish_state(self.repo,'sample',name,self.stage(name,'invented counts'))
    def test_atomic_write_publishes_unicode_and_consumes_staging(self):
        target=self.directory/'unittest-summary.md';target.write_text('old')
        src=self.stage(target.name,'新的报告\n');ut.publish_state(self.repo,'sample',target.name,src)
        self.assertEqual(target.read_text(),'新的报告\n');self.assertFalse(src.exists())
    def test_failed_replace_keeps_previous_file_and_staging(self):
        target=self.directory/'unittest-session.md';target.write_text('previous');src=self.stage(target.name,'next')
        with patch('atomic_files.os.replace',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):ut.publish_state(self.repo,'sample',target.name,src)
        self.assertEqual(target.read_text(),'previous');self.assertTrue(src.exists())
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()),sorted([target.name,src.name]))
    def test_rejects_path_traversal_arbitrary_source_and_symlink(self):
        src=self.stage('unittest-results.md','safe')
        for feature,name,source in (('../escape','unittest-results.md',src),('sample','spec.md',src),('sample','unittest-results.md',self.repo/'README.md')):
            with self.subTest(feature=feature,name=name),self.assertRaises(ValueError):ut.publish_state(self.repo,feature,name,source)
        outside=self.repo/'outside';outside.write_text('untouched');target=self.directory/'unittest-results.md';target.symlink_to(outside)
        with self.assertRaises((ValueError,OSError)):ut.publish_state(self.repo,'sample',target.name,src)
        self.assertEqual(outside.read_text(),'untouched')
    def test_rejects_symlink_ancestor_before_reading_staging(self):
        outside=Path(self.tmp.name)/'outside';outside.mkdir();(outside/'unittest-results.md.tmp').write_text('not authorized')
        (self.repo/'docs/specs/active/alias').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):ut.publish_state(self.repo,'alias','unittest-results.md',outside/'unittest-results.md.tmp')
    def test_aggregation_uses_only_explicit_terminal_workers(self):
        self.findings(1,[self.observation('optional_improvement')]);self.findings(2,[self.observation()]);self.findings(3,[self.observation()])
        result=ut.aggregate_findings(self.repo,'sample',[1,2]);self.assertEqual(result['counts']['confirmed_defect'],1);self.assertEqual(result['counts']['optional_improvement'],1)
        self.assertEqual(result['worker_ids'],[1,2]);self.assertNotIn('worker-3', (self.directory/'unittest-defects.md').read_text())
        with self.assertRaises(ValueError):ut.aggregate_findings(self.repo,'sample',[4])
    def test_rejects_duplicate_observations_and_worker_mismatch(self):
        with self.assertRaises(ValueError):self.findings(1,[self.observation(),self.observation()])
        src=self.stage('worker-1-findings.json',json.dumps({'worker_id':2,'class_name':'a','observations':[]}))
        with self.assertRaises(ValueError):ut.publish_findings(self.repo,'sample',1,src)
    def test_legacy_timeout_and_archived_workers_keep_unknown_totals(self):
        for worker,status,prefix in ((1,'成功',''),(2,'超时终止',''),(3,'失败','archived-')):
            result=self.directory/f'{prefix}worker-{worker}-result.md'
            result.write_text(f'状态: {status}\nlegacy High defect: preserve this report\n')
            defects=self.directory/f'{prefix}worker-{worker}-defects.md';defects.write_text('old detailed evidence')
            before={p:p.read_bytes() for p in (result,defects)}
            r=ut.aggregate_findings(self.repo,'sample',[worker])
            self.assertFalse(r['classification_complete']);self.assertIsNone(r['counts']['confirmed_defect'])
            self.assertEqual(r['legacy_workers'][0]['status'],status)
            self.assertIn(result.name,(self.directory/'unittest-defects.md').read_text())
            self.assertTrue(all(p.read_bytes()==v for p,v in before.items()))
    def test_mixed_legacy_and_current_does_not_claim_zero(self):
        self.findings(1,[self.observation()])
        (self.directory/'worker-2-result.md').write_text('状态: 超时终止\n')
        r=ut.aggregate_findings(self.repo,'sample',[1,2])
        self.assertFalse(r['classification_complete']);self.assertIsNone(r['counts']['confirmed_defect'])
        self.assertEqual(r['known_counts']['confirmed_defect'],1)
        self.assertIn('未核验',(self.directory/'unittest-defects.md').read_text())
    def test_published_findings_survive_evidence_cleanup_but_corruption_blocks(self):
        self.findings(1,[self.observation()]);(self.repo/'evidence.log').unlink()
        r=ut.aggregate_findings(self.repo,'sample',[1]);self.assertTrue(r['classification_complete'])
        self.assertEqual(r['counts']['confirmed_defect'],1)
        file=self.directory/'worker-1-findings.json';doc=json.loads(file.read_text());doc['counts']['confirmed_defect']=99;file.write_text(json.dumps(doc))
        with self.assertRaises(ValueError):ut.aggregate_findings(self.repo,'sample',[1])
    def test_class_cast_and_non_jvm_runtime_failures_remain_confirmed_high(self):
        for suffix,exception in (('.java','ClassCastException'),('.py','ZeroDivisionError'),('.ts','RangeError'),('.js','TypeError'),('.go','panic: runtime error')):
            with self.subTest(suffix=suffix):
                name='Rules'+suffix;(self.repo/name).write_text('synthetic failing expression')
                (self.repo/'exception.log').write_text(exception)
                obs=self.observation(basis='mechanical',severity='High',mechanical_kind='inevitable_exception',observed_exception=exception,normative_ref=None,code_ref={'path':name,'quote':'synthetic failing expression'},reproducer={'command':'runtime reproducer','expected':'returns safely','actual':exception,'evidence_path':'exception.log','evidence_quote':exception})
                self.assertEqual(self.findings(1,[obs])['counts']['confirmed_defect'],1)
                self.assertEqual(json.loads((self.directory/'worker-1-findings.json').read_text())['observations'][0]['severity'],'High')
    def test_other_reproduced_mechanical_failures_and_invalid_marker(self):
        (self.repo/'Rules.java').write_text('synthetic failing expression')
        for kind in ('nontermination','data_corruption','control_flow_failure','security_violation','misleading_success'):
            self.assertEqual(self.findings(1,[self.observation(basis='mechanical',mechanical_kind=kind,code_ref={'path':'Rules.java','quote':'synthetic failing expression'})])['counts']['confirmed_defect'],1)
        for marker in ([],None,''):
            self.assertEqual(self.findings(1,[self.observation(basis='mechanical',mechanical_kind='inevitable_exception',observed_exception=marker)])['counts']['confirmed_defect'],0)
        doc=json.loads((self.directory/'worker-1-findings.json').read_text())
        self.assertEqual(doc['observations'][0]['proposed_severity'],'Low')
    def test_configuration_is_not_a_mechanical_code_reference(self):
        (self.repo/'settings.json').write_text('synthetic setting')
        self.assertEqual(self.findings(1,[self.observation(basis='mechanical',mechanical_kind='data_corruption',code_ref={'path':'settings.json','quote':'synthetic setting'})])['counts']['confirmed_defect'],0)
    @unittest.skipIf(os.name == 'nt', 'Windows models only the read-only bit')
    def test_existing_posix_modes_survive_all_publication_paths(self):
        import stat
        for mode in (0o600,0o640,0o750):
            target=self.directory/'unittest-summary.md';target.write_text('old');target.chmod(mode)
            ut.publish_state(self.repo,'sample',target.name,self.stage(target.name,'new'))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode),mode)
        self.findings(1,[self.observation()])
        for name in ('worker-1-findings.json','worker-1-defects.md'):(self.directory/name).chmod(0o600)
        self.findings(1,[self.observation()])
        for name in ('worker-1-findings.json','worker-1-defects.md'):self.assertEqual(stat.S_IMODE((self.directory/name).stat().st_mode),0o600)
    def test_host_worker_contracts_allow_generated_json_and_staging(self):
        plugin=Path(__file__).resolve().parents[2]
        for host in ('codex','cursor','opencode'):
            text=(plugin/f'hosts/{host}/ut.md').read_text()
            self.assertIn('findings',text);self.assertIn('.tmp',text)
    def test_v1_snapshot_is_unverified_and_corrupt_json_cannot_fallback(self):
        result=self.directory/'worker-1-result.md';result.write_text('状态: 成功\n')
        findings=self.directory/'worker-1-findings.json'
        findings.write_text(json.dumps({'schema_version':1,'worker_id':1,'class_name':'Rules','observations':[],'counts':{k:0 for k in ut.KINDS}}))
        self.assertFalse(ut.aggregate_findings(self.repo,'sample',[1])['classification_complete'])
        for text in (json.dumps({'schema_version':True,'worker_id':1,'observations':[]}),json.dumps({'schema_version':1,'worker_id':1,'class_name':'Rules','observations':[None],'counts':{'confirmed_defect':999}}),'{broken',json.dumps({'schema_version':2,'worker_id':1}),json.dumps({'schema_version':1,'worker_id':2,'observations':[]})):
            findings.write_text(text)
            with self.assertRaises(ValueError):ut.aggregate_findings(self.repo,'sample',[1])
    def test_archived_valid_snapshot_keeps_counts_after_cleanup(self):
        self.findings(1,[self.observation()]);(self.repo/'evidence.log').unlink()
        for suffix in ('findings.json','defects.md'):
            file=self.directory/f'worker-1-{suffix}';file.rename(self.directory/('archived-'+file.name))
        self.assertEqual(ut.aggregate_findings(self.repo,'sample',[1])['counts']['confirmed_defect'],1)
    def test_replace_failure_preserves_private_mode_and_new_file_default(self):
        import stat
        target=self.directory/'unittest-session.md';target.write_text('private');target.chmod(0o600)
        with patch('atomic_files.os.replace',side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):ut.publish_state(self.repo,'sample',target.name,self.stage(target.name,'replacement'))
        self.assertEqual(target.read_text(),'private')
        name='unittest-summary.md';ut.publish_state(self.repo,'sample',name,self.stage(name,'new'))
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE((self.directory/name).stat().st_mode),0o644)
    def test_active_timeout_never_inherits_archived_worker_generation(self):
        self.findings(1,[])
        for suffix in ('findings.json','defects.md'):
            p=self.directory/f'worker-1-{suffix}';p.rename(self.directory/('archived-'+p.name))
        (self.directory/'archived-worker-1-result.md').write_text('状态: 成功\nold class')
        current=self.directory/'worker-1-result.md';current.write_text('状态: 超时终止\nnew class')
        r=ut.aggregate_findings(self.repo,'sample',[1])
        self.assertFalse(r['classification_complete']);self.assertIsNone(r['counts']['confirmed_defect'])
        self.assertEqual(r['legacy_workers'][0]['paths'],['worker-1-result.md'])
if __name__=='__main__':unittest.main()
