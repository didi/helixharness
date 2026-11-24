#!/usr/bin/env python3
"""Ground UT observation counts and publish bounded UT artifacts atomically.

Reference checks establish provenance, not a proof of business semantics. Commands
in reproducer records are data only and are never executed by this module.
"""
from __future__ import annotations
import argparse
import hashlib
import stat
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from atomic_files import atomic_write_text

STATE_NAME = re.compile(r'(?:unittest-(?:queue|session|results|summary|experience)\.md|worker-[1-9][0-9]*-result\.md)\Z')
KINDS = ('confirmed_defect', 'unconfirmed_risk', 'optional_improvement')
LABELS = dict(zip(KINDS, ('已证实缺陷', '待确认风险', '可选改进')))

def _safe_file(root: Path, path: Path) -> Path:
    if not path.is_absolute():
        path = root / path
    if '..' in path.parts:
        raise ValueError('UT artifact reference cannot traverse parents')
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError('UT artifact reference is outside its authorized root') from None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('UT artifact/reference cannot follow a symbolic link')
    if not path.is_file():
        raise ValueError('UT artifact/reference must be a regular file')
    return path


def _directory(repo: Path, feature: str) -> Path:
    repo = repo.resolve(strict=True)
    if not repo.is_dir() or not feature or feature in ('.', '..') or any(c in feature for c in ('/', '\\', '\0')):
        raise ValueError('Invalid UT repository or feature')
    current = repo
    for part in ('docs', 'specs', 'active', feature):
        current = current / part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ValueError('Unsafe UT state directory')
        current.mkdir(exist_ok=True)
    return current


def _publish(target: Path, text: str) -> None:
    if os.path.lexists(target) and (target.is_symlink() or not target.is_file()):
        raise ValueError('Unsafe UT artifact target')
    atomic_write_text(target, text, mode=stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644)


def _staged(directory: Path, name: str, source: Path) -> Path:
    expected = directory / (name + '.tmp')
    if source.name != expected.name or source.parent.resolve() != directory:
        raise ValueError('Use the target same-directory <name>.tmp staging file')
    return _safe_file(directory, expected)


def publish_state(repo: Path, feature: str, name: str, source: Path) -> dict[str, Any]:
    if not STATE_NAME.fullmatch(name):
        raise ValueError('Unsupported UT state filename')
    directory = _directory(repo, feature)
    staged = _staged(directory, name, source)
    _publish(directory / name, staged.read_text(encoding='utf-8'))
    staged.unlink()
    return {'status': 'written', 'atomic': True, 'path': str(directory / name)}


def _reference(repo: Path, ref: Any, *, normative: bool = False) -> bool:
    if not isinstance(ref, dict) or not isinstance(ref.get('path'), str) or not isinstance(ref.get('quote'), str) or not ref['quote'].strip():
        return False
    try:
        file = _safe_file(repo, Path(ref['path']))
        relative = file.relative_to(repo)
        if not normative and file.suffix.lower() not in ('.java', '.kt', '.groovy', '.py', '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.go'):
            return False
        if normative and (file.suffix.lower() != '.md' or not (relative.parts[0] in ('docs',) or relative.name in ('README.md', 'AGENTS.md'))):
            return False
        return ref['quote'] in file.read_text(encoding='utf-8')
    except (ValueError, OSError, UnicodeError):
        return False


def _reproducer(repo: Path, worker: int, value: Any) -> bool:
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k].strip() for k in ('command', 'expected', 'actual', 'evidence_path', 'evidence_quote')):
        return False
    if value['expected'].strip() == value['actual'].strip():
        return False
    path = Path(value['evidence_path'])
    temporary_project = Path(tempfile.gettempdir()) / re.sub(r'[^a-z0-9._-]', '-', repo.name.lower())
    temporary_worker = temporary_project / f'worker-{worker}'
    roots = [repo]
    # Resolve the platform /var alias only after rejecting links in owned suffixes.
    if not temporary_project.is_symlink() and not temporary_worker.is_symlink() and temporary_worker.is_dir():
        roots.extend((temporary_worker, temporary_worker.resolve()))
    if path.suffix.lower() not in ('.log', '.txt', '.xml'):
        return False
    for root in roots:
        try:
            file = _safe_file(root, path)
            if value['evidence_quote'] in file.read_text(encoding='utf-8'):
                return True
        except (ValueError, OSError, UnicodeError):
            pass
    return False


def normalize_findings(repo: Path, worker: int, document: Any) -> dict[str, Any]:
    repo = repo.resolve(strict=True)
    if not isinstance(document, dict) or type(document.get('worker_id')) is not int or document.get('worker_id') != worker or not isinstance(document.get('class_name'), str) or not document['class_name'].strip() or not isinstance(document.get('observations'), list):
        raise ValueError('Invalid worker observation document')
    observations = []
    ids: set[str] = set()
    for item in document['observations']:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ('id', 'title', 'actual_behavior')) or item.get('classification') not in KINDS or item['id'] in ids:
            raise ValueError('Invalid or duplicate UT observation')
        ids.add(item['id'])
        item = dict(item)
        category = item['classification']
        if category == 'confirmed_defect':
            supported = _reproducer(repo, worker, item.get('reproducer'))
            basis = item.get('basis')
            if basis == 'contract':
                supported = supported and _reference(repo, item.get('normative_ref'), normative=True)
            elif basis == 'mechanical':
                exception = item.get('observed_exception')
                kind = item.get('mechanical_kind')
                supported = supported and _reference(repo, item.get('code_ref')) and kind in ('inevitable_exception', 'nontermination', 'data_corruption', 'control_flow_failure', 'security_violation', 'misleading_success')
                if kind == 'inevitable_exception':
                    supported = supported and isinstance(exception, str) and bool(exception.strip()) and exception in str(item.get('reproducer', {}).get('evidence_quote', ''))
            else:
                supported = False
            if not supported:
                item['proposed_classification'] = category
                item['proposed_severity'] = item.get('severity')
                item['classification'] = 'unconfirmed_risk'
                item['classification_reason'] = '缺少可核验的规范来源或实际反例；保留观察，不计入已证实缺陷'
                item['severity'] = None
            elif item.get('severity') not in ('High', 'Medium', 'Low'):
                raise ValueError('Confirmed defect needs High/Medium/Low severity')
            else:
                item['classification_reason'] = '规范/机械失败依据与反例引用校验通过；语义仍由评审负责'
        else:
            item['severity'] = None
            item['classification_reason'] = item.get('classification_reason') or '未确认为违反已确认要求，不计入缺陷'
        observations.append(item)
    return _seal({'schema_version': 2, 'worker_id': worker, 'class_name': document['class_name'], 'observations': observations, 'counts': {k: sum(x['classification'] == k for x in observations) for k in KINDS}})


def _digest(document: dict[str, Any]) -> str:
    # Integrity checksum only, not authentication or a semantic correctness proof.
    payload = {k: v for k, v in document.items() if k != 'integrity_sha256'}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _seal(document: dict[str, Any]) -> dict[str, Any]:
    document['integrity_sha256'] = _digest(document)
    return document


def _snapshot(file: Path, worker: int, *, legacy: bool = False) -> dict[str, Any]:
    document = json.loads(file.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or type(document.get('worker_id')) is not int or document['worker_id'] != worker or not isinstance(document.get('class_name'), str) or not document['class_name'].strip() or not isinstance(document.get('observations'), list):
        raise ValueError('Invalid saved worker classification')
    if not legacy and (document.get('schema_version') != 2 or document.get('integrity_sha256') != _digest(document)):
        raise ValueError('Saved classification version/integrity is invalid; keep original evidence and revalidate explicitly')
    ids = set()
    for item in document['observations']:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ('id', 'title', 'actual_behavior', 'classification_reason')) or item['id'] in ids or item.get('classification') not in KINDS:
            raise ValueError('Invalid saved observation')
        ids.add(item['id'])
        if item['classification'] == 'confirmed_defect' and item.get('severity') not in ('High', 'Medium', 'Low'):
            raise ValueError('Invalid saved severity')
    counts = {k: sum(x['classification'] == k for x in document['observations']) for k in KINDS}
    if document.get('counts') != counts or any(type(v) is not int for v in document['counts'].values()):
        raise ValueError('Saved classification counts disagree with observations')
    return document


def _worker_file(directory: Path, worker: int, suffix: str, prefix: str) -> Path | None:
    file = directory / f'{prefix}worker-{worker}-{suffix}'
    return _safe_file(directory, file) if os.path.lexists(file) else None


def _cell(value: Any) -> str:
    return str(value).replace('|', '\\|').replace('\n', '<br>')


def _render(documents: list[dict[str, Any]], legacy: list[dict[str, Any]] | None = None) -> str:
    counts = {k: sum(d['counts'][k] for d in documents) for k in KINDS}
    lines = ['# UT 观察分类报告', '', '> 统计由 ut_artifacts.py 生成；规范引用存在不等于机械证明全部业务语义。', '', '| 已证实缺陷 | 待确认风险 | 可选改进 |', '|---|---|---|', '| ' + ' | '.join(('未核验' if legacy else str(counts[k])) for k in KINDS) + ' |', '']
    for kind in KINDS:
        lines += [f'## {LABELS[kind]}', '', '| Worker / Class | ID | 观察 | 实际行为 | 严重程度 | 依据/反例 | 分级依据 |', '|---|---|---|---|---|---|---|']
        for doc in documents:
            for item in doc['observations']:
                if item['classification'] == kind:
                    lines.append('| ' + ' | '.join(_cell(x) for x in (f"worker-{doc['worker_id']} / {doc['class_name']}", item['id'], item['title'], item['actual_behavior'], item.get('severity') or '—', json.dumps({'normative':item.get('normative_ref'),'code':item.get('code_ref'),'reproducer':item.get('reproducer')},ensure_ascii=False), item['classification_reason'])) + ' |')
        lines.append('')
    if legacy:
        lines += ['## 分类未核验的历史/未完成 Worker', '', '以下原记录保持不变；全量分类总数未知。上方明细只包含已校验记录，不能将缺失分类算作零。', '']
        for entry in legacy:
            lines.append(f"- worker-{entry['worker_id']}：{entry['status']}；原记录：" + ', '.join(entry['paths']))
    return '\n'.join(lines) + '\n'


def publish_findings(repo: Path, feature: str, worker: int, source: Path) -> dict[str, Any]:
    if type(worker) is not int or worker < 1:
        raise ValueError('Invalid worker ID')
    directory = _directory(repo, feature)
    name = f'worker-{worker}-findings.json'
    staged = _staged(directory, name, source)
    document = normalize_findings(repo, worker, json.loads(staged.read_text(encoding='utf-8')))
    _publish(directory / name, json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    _publish(directory / f'worker-{worker}-defects.md', _render([document]))
    staged.unlink()
    return {'status': 'validated', 'worker_id': worker, 'counts': document['counts'], 'atomic': True}


def aggregate_findings(repo: Path, feature: str, workers: list[int]) -> dict[str, Any]:
    if not workers or len(set(workers)) != len(workers) or any(type(w) is not int or w < 1 for w in workers):
        raise ValueError('Select explicit unique final worker IDs')
    directory = _directory(repo, feature)
    documents = []
    legacy = []
    for worker in workers:
        # Never splice archived evidence into a newly reused active Worker ID.
        prefix = '' if any(os.path.lexists(directory / f'worker-{worker}-{suffix}') for suffix in ('result.md', 'findings.json', 'defects.md')) else 'archived-'
        file = _worker_file(directory, worker, 'findings.json', prefix)
        if file:
            raw = json.loads(file.read_text(encoding='utf-8'))
            if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw['schema_version'] not in (1, 2):
                raise ValueError('Invalid saved classification schema')
            if raw['schema_version'] == 2:
                documents.append(_snapshot(file, worker))
                continue
            _snapshot(file, worker, legacy=True)
        result = _worker_file(directory, worker, 'result.md', prefix)
        if result is None:
            raise ValueError('Missing worker result/classification; cannot assume zero findings')
        first = result.read_text(encoding='utf-8').splitlines()
        if not first or first[0].strip() not in ('状态: 成功', '状态: 失败', '状态: 超时终止'):
            raise ValueError('Invalid legacy worker result status')
        defects = _worker_file(directory, worker, 'defects.md', prefix)
        legacy.append({'worker_id': worker, 'status': first[0].split(':', 1)[1].strip(), 'paths': [p.name for p in (result, defects, file) if p]})
    known = {k: sum(d['counts'][k] for d in documents) for k in KINDS}
    _publish(directory / 'unittest-defects.md', _render(documents, legacy))
    return {'status': 'aggregated', 'worker_ids': workers, 'classification_complete': not legacy, 'counts': {k: None for k in KINDS} if legacy else known, 'known_counts': known, 'legacy_workers': legacy, 'atomic': True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', type=Path, required=True)
    parser.add_argument('--feature', required=True)
    sub = parser.add_subparsers(dest='action', required=True)
    write = sub.add_parser('write');write.add_argument('--name', required=True);write.add_argument('--source', type=Path, required=True)
    findings = sub.add_parser('findings');findings.add_argument('--worker', type=int, required=True);findings.add_argument('--source', type=Path, required=True)
    aggregate = sub.add_parser('aggregate');aggregate.add_argument('--workers', required=True, help='comma-separated final worker IDs after retry deduplication')
    args = parser.parse_args()
    try:
        if args.action == 'write':
            result = publish_state(args.repo_root, args.feature, args.name, args.source)
        elif args.action == 'findings':
            result = publish_findings(args.repo_root, args.feature, args.worker, args.source)
        else:
            result = aggregate_findings(args.repo_root, args.feature, [int(x) for x in args.workers.split(',')])
    except (ValueError, OSError, UnicodeError) as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc)}, ensure_ascii=False));return 2
    print(json.dumps(result, ensure_ascii=False));return 0

if __name__ == '__main__':
    raise SystemExit(main())
