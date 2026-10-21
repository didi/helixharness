"""Check shared instructions consumed by current workflow entrypoints."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ReferenceContractsTest(unittest.TestCase):
    def test_init_notice_matches_shared_authority_and_current_lifecycle(self):
        protocol = (ROOT / 'reference/eval-protocol.md').read_text()
        init = (ROOT / 'commands/init.md').read_text()
        pattern = (r'<!-- helixharness-eval-data-notice-v1:start -->\n'
                   r'(.*?)\n<!-- helixharness-eval-data-notice-v1:end -->')
        source = re.findall(pattern, protocol, re.S)
        copy = re.findall(pattern, init, re.S)
        self.assertEqual(len(source), 1)
        self.assertEqual(source, copy)
        self.assertIn('## 2. 数据统计开关与提醒缓存', protocol)
        for retired_action in ('prepare-git', 'prepare_git(', 'resolve_consent(',
                               'choose_consent(', 'eval_telemetry.py', 'stage-start'):
            self.assertNotIn(retired_action, init)
        self.assertIn('异步 Runtime', source[0])
        self.assertIn('需先启动平台并配置地址', protocol)
        for stage in ('propose', 'apply', 'archive'):
            text = (ROOT / f'workflows/single/{stage}.md').read_text()
            self.assertIn('reference/eval-protocol.md', text)

    def test_telemetry_reference_chain_is_complete(self):
        host = (ROOT / 'hosts/shared/telemetry.md').read_text()
        command = (ROOT / 'reference/telemetry-command-protocol.md').read_text()
        protocol = (ROOT / 'reference/eval-protocol.md').read_text()
        for caller in (host, command, protocol):
            self.assertIn('reference/eval-supplement-protocol.md', caller)
        supplement = (ROOT / 'reference/eval-supplement-protocol.md').read_text()
        for contract in ('sealed_collected', 'final_for_entry=true',
                         'archive --no-commit', '默认接收地址为空'):
            self.assertIn(contract, supplement)
        self.assertIn('status=skipped', command)
        self.assertNotIn('先取得 start 返回的 entry_id，之后才开始业务', command)

    def test_loading_rules_referenced_by_workflows_are_defined(self):
        protocol = (ROOT / 'reference/loading-protocol.md').read_text()
        for section in ('## 3. 「必读」项', '## 4. 时间戳缓存',
                        '## 5. index.md 格式规范', '## 6. 边界与例外'):
            self.assertIn(section, protocol)
        for stage in ('apply', 'archive'):
            workflow = (ROOT / f'workflows/single/{stage}.md').read_text()
            self.assertIn('时间戳缓存', workflow)
        for retired_stage in ('propose Phase 1', 'propose Phase 2', '| verify |'):
            self.assertNotIn(retired_stage, protocol)


if __name__ == '__main__':
    unittest.main()
