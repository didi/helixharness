"""Test projection generation in a synthetic plugin, including rejected input."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import codex_skill_projection as projection


class CodexSkillProjectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'plugin'
        self.mapping = self.root / 'hosts/codex/command-projection.json'
        self.mapping.parent.mkdir(parents=True)
        self.mapping.write_text(json.dumps({'projection_version': '1.0', 'surface': 'skill',
            'entrypoints': {'init': ['hosts/codex/skills/init/SKILL.md', 'commands/init.md']}}), encoding='utf-8')
        self.command = self.root / 'commands/init.md'
        self.command.parent.mkdir()
        self.command.write_text('---\nname: init\ndescription: "Initialize synthetic project"\n---\nCanonical command\n', encoding='utf-8')
        self.target = self.root / 'hosts/codex/skills/init/SKILL.md'
        self.target.parent.mkdir(parents=True)
        self.target.write_text('---\nname: init\ndescription: "stale"\n---\nPreserve command routing body\n', encoding='utf-8')
        self.method = self.root / 'skills/tdd/SKILL.md'
        self.method.parent.mkdir(parents=True)
        self.method.write_text('---\nname: tdd\ndescription: "测试方法"\ndisable-model-invocation: true\n---\nCanonical method body\n', encoding='utf-8')

    def test_write_then_check_is_idempotent_and_preserves_canonical_content(self):
        originals = (self.command.read_bytes(), self.method.read_bytes())
        projection.write(self.root)
        projection.check(self.root)
        generated = {p.relative_to(self.root): p.read_bytes() for p in (self.root/'hosts/codex/skills').rglob('*') if p.is_file()}
        projection.write(self.root)
        self.assertEqual(generated, {p.relative_to(self.root): p.read_bytes() for p in (self.root/'hosts/codex/skills').rglob('*') if p.is_file()})
        self.assertEqual(originals, (self.command.read_bytes(), self.method.read_bytes()))
        self.assertIn('Preserve command routing body', self.target.read_text(encoding='utf-8'))
        self.assertIn('description: "Initialize synthetic project"', self.target.read_text(encoding='utf-8'))
        method_target = self.root/'hosts/codex/skills/tdd/SKILL.md'
        self.assertIn('skills/tdd/SKILL.md', method_target.read_text(encoding='utf-8'))
        self.assertNotIn('Canonical method body', method_target.read_text(encoding='utf-8'))
        metadata = method_target.parent/'agents/openai.yaml'
        self.assertIn('allow_implicit_invocation: false', metadata.read_text(encoding='utf-8'))
        self.assertIn('allow_implicit_invocation: true', (self.target.parent/'agents/openai.yaml').read_text(encoding='utf-8'))

    def test_frontmatter_supported_scalars_and_multiline_descriptions(self):
        cases = [('"JSON quoted"', 'JSON quoted'), ("'It''s quoted'", "It's quoted"),
                 ('plain description', 'plain description'), ('>\n  first line\n  second line', 'first line second line'),
                 ('|\n  first line\n  second line', 'first line second line')]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.method.write_text(f'---\nname: tdd\ndescription: {raw}\n---\n', encoding='utf-8')
                projection.write(self.root)
                projection.check(self.root)
                content = (self.root/'hosts/codex/skills/tdd/agents/openai.yaml').read_text(encoding='utf-8')
                self.assertIn(json.dumps(expected), content)

    def test_short_description_stays_bounded_and_keeps_sentence_boundary(self):
        self.assertEqual('short', projection._short_description('short'))
        first = 'a' * 30 + '. '
        self.assertEqual(first.strip(), projection._short_description(first + 'b' * 80))
        self.assertEqual(64, len(projection._short_description('x' * 200)))

    def test_method_with_command_name_is_not_projected_as_a_method(self):
        path = self.root/'skills/init/SKILL.md'
        path.parent.mkdir()
        path.write_text('invalid canonical method frontmatter', encoding='utf-8')
        projection.write(self.root)
        self.assertIn('Preserve command routing body', self.target.read_text(encoding='utf-8'))

    def test_stale_missing_and_unexpected_metadata_are_detected_without_writes(self):
        projection.write(self.root)
        method_target = self.root/'hosts/codex/skills/tdd/SKILL.md'
        for target in (method_target, self.target, method_target.parent/'agents/openai.yaml'):
            with self.subTest(target=target):
                original = target.read_bytes()
                target.write_text('stale', encoding='utf-8')
                with self.assertRaises(projection.ProjectionError):
                    projection.check(self.root)
                self.assertEqual('stale', target.read_text(encoding='utf-8'))
                target.write_bytes(original)
        metadata = method_target.parent/'agents/openai.yaml'
        metadata.unlink()
        with self.assertRaisesRegex(projection.ProjectionError, 'stale'):
            projection.check(self.root)
        projection.write(self.root)
        orphan = self.root/'hosts/codex/skills/orphan/agents/openai.yaml'
        orphan.parent.mkdir(parents=True)
        orphan.write_text('synthetic orphan', encoding='utf-8')
        with self.assertRaisesRegex(projection.ProjectionError, 'unexpected.*orphan'):
            projection.check(self.root)
        self.assertEqual('synthetic orphan', orphan.read_text(encoding='utf-8'))

    def test_bad_method_frontmatter_fails_before_any_projection_is_modified(self):
        for frontmatter in ('missing', '---\nname: other\ndescription: ok\n---\n',
                            '---\nname: tdd\n---\n',
                            '---\nname: tdd\ndescription: a\ndescription: b\n---\n',
                            '---\nname: tdd\ndescription: "invalid\n---\n',
                            '---\nname: tdd\ndescription: ""\n---\n',
                            '---\nname: tdd\ndescription: ok\ndisable-model-invocation: maybe\n---\n'):
            with self.subTest(frontmatter=frontmatter):
                self.target.write_text('---\nname: init\ndescription: "stale"\n---\nOriginal body\n', encoding='utf-8')
                self.method.write_text(frontmatter, encoding='utf-8')
                original = self.target.read_bytes()
                with self.assertRaises(projection.ProjectionError):
                    projection.write(self.root)
                self.assertEqual(original, self.target.read_bytes())

    def test_unsafe_or_malformed_command_mapping_is_rejected(self):
        for entries in ([], {'../escape': []}, {'init': ['commands/wrong.md']}, {'init': None}):
            with self.subTest(entries=entries):
                self.mapping.write_text(json.dumps({'entrypoints': entries}), encoding='utf-8')
                with self.assertRaises(projection.ProjectionError):
                    projection.write(self.root)

    def test_missing_or_malformed_command_projection_fails_with_domain_error(self):
        self.target.unlink()
        with self.assertRaises(projection.ProjectionError):
            projection.write(self.root)
        self.target.write_text('missing frontmatter', encoding='utf-8')
        with self.assertRaises(projection.ProjectionError):
            projection.write(self.root)

    def test_command_description_must_be_a_unique_nonempty_json_string(self):
        for description in ('description: unquoted', 'description: 123', 'description: ""',
                            'description: "a"\ndescription: "b"'):
            with self.subTest(description=description):
                self.command.write_text(f'---\nname: init\n{description}\n---\n', encoding='utf-8')
                with self.assertRaises(projection.ProjectionError):
                    projection.write(self.root)

    def test_cli_check_reports_errors_and_write_is_explicit(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(1, projection.main(['--root', str(self.root), '--check']))
            self.assertIn('FAIL', output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, projection.main(['--root', str(self.root), '--write']))
            self.assertEqual(0, projection.main(['--root', str(self.root), '--check']))
            self.assertIn('PASS', output.getvalue())


if __name__ == '__main__':
    unittest.main()
