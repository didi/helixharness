"""Fake provider clients exercise advisory gates without model calls."""
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bench.hard_gate_analysis import (
    OUTPUT_SCHEMA_VERSION,
    HardGateAnalysisError,
    OpenAIHardGateAnalyzer,
    present_hard_gate_analysis,
    validate_hard_gate_analysis_output,
)
from bench.runtime_secrets import EvaluationProviderConfig
from bench.semantic_judging import JudgeContractError, OpenAISemanticJudgeAdapter, judge_with_retries


def request():
    return {'hard_gates': [{'gate_id': 'g1', 'side': 'CANDIDATE', 'case_id': 'c1'}],
            'evidence': [{'ref_id': 'e1', 'kind': 'synthetic-check'}]}


def output():
    return {'schema_version': OUTPUT_SCHEMA_VERSION, 'overall_summary': 'Synthetic',
            'decision_impact': 'ANALYSIS_ONLY', 'findings': [{'gate_id': 'g1', 'side': 'CANDIDATE', 'case_id': 'c1',
            'attribution': 'INSUFFICIENT_EVIDENCE', 'confidence': 'LOW', 'summary': 'Synthetic',
            'root_causes': ['Unknown'], 'evidence_refs': [{'ref_id': 'e1', 'claim': 'Synthetic fact'}],
            'counterevidence': [], 'recommended_actions': ['Inspect synthetic check']}]}


def provider():
    return EvaluationProviderConfig('codex', 'synthetic', 'https://example.invalid/v1', '', 'synthetic', 'SYNTHETIC_KEY')


def test_hard_gate_provider_uses_strict_schema_no_tools_and_exact_evidence():
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(output_text=json.dumps(output()))
    adapter = OpenAIHardGateAnalyzer(provider_config=provider(), client_factory=lambda: client)
    assert adapter.analyze(request(), model='synthetic', reasoning_effort='low') == output()
    call = client.responses.create.call_args.kwargs
    assert call['tools'] == [] and call['store'] is False
    schema = call['text']['format']
    assert schema['strict'] is True
    assert schema['schema']['properties']['findings']['minItems'] == 1
    assert json.loads(call['input'][1]['content'][0]['text']) == request()


@pytest.mark.parametrize('raw', [None, '', '[]', 'invalid JSON'])
def test_hard_gate_provider_rejects_malformed_output(raw):
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(output_text=raw)
    with pytest.raises(ValueError):
        OpenAIHardGateAnalyzer(provider_config=provider(), client_factory=lambda: client).analyze(request(), model='synthetic', reasoning_effort='low')


def test_no_gate_or_unconfigured_provider_fails_before_a_model_call():
    factory = Mock()
    with pytest.raises(ValueError, match='at least one'):
        OpenAIHardGateAnalyzer(provider_config=provider(), client_factory=factory).analyze({}, model='synthetic', reasoning_effort='low')
    factory.assert_not_called()
    with pytest.raises(ValueError, match='not configured'):
        OpenAIHardGateAnalyzer(provider_config=provider()).analyze(request(), model='synthetic', reasoning_effort='low')


@pytest.mark.parametrize('mutation', [{'schema_version': 'bad'}, {'decision_impact': 'CHANGE_VERDICT'}, {'findings': None}, {'findings': []}])
def test_advisory_output_cannot_change_verdict_or_omit_gates(mutation):
    with pytest.raises(HardGateAnalysisError):
        validate_hard_gate_analysis_output({**output(), **mutation}, request())


@pytest.mark.parametrize('mutation', [{'gate_id': 'unknown'}, {'gate_id': []}, {'attribution': 'unknown'},
    {'evidence_refs': []}, {'evidence_refs': ['invalid']}, {'evidence_refs': [{'ref_id': 'unknown'}]}, {'evidence_refs': [{'ref_id': []}]}])
def test_untrusted_findings_and_references_fail_closed(mutation):
    result = output()
    result['findings'][0].update(mutation)
    with pytest.raises(HardGateAnalysisError):
        validate_hard_gate_analysis_output(result, request())


def test_presentation_adds_labels_without_mutating_frozen_facts():
    stored = {'input': request(), 'output': output()}
    original = deepcopy(stored)
    assert validate_hard_gate_analysis_output(stored['output'], stored['input']) == output()
    shown = present_hard_gate_analysis(stored)
    assert shown['output']['findings'][0]['attribution_label'] == '证据不足'
    assert shown['output']['findings'][0]['evidence_refs'][0]['source_kind'] == 'synthetic-check'
    assert stored == original
    assert present_hard_gate_analysis(None) is None


@pytest.mark.parametrize('raw', [None, '', '[]', 'invalid JSON'])
def test_semantic_judge_rejects_malformed_provider_output(raw):
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(output_text=raw)
    with pytest.raises(JudgeContractError):
        OpenAISemanticJudgeAdapter(client_factory=lambda: client).judge({'axes': ['quality']}, model='synthetic', reasoning_effort='low')


def test_semantic_judge_uses_frozen_axes_and_no_tools():
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(output_text='{"status":"COMPLETED"}')
    adapter = OpenAISemanticJudgeAdapter(client_factory=lambda: client)
    assert adapter.judge({'axes': ['quality']}, model='synthetic', reasoning_effort='low')['status'] == 'COMPLETED'
    call = client.responses.create.call_args.kwargs
    assert call['tools'] == [] and call['store'] is False
    assert call['text']['format']['schema']['properties']['axis_scores']['items']['properties']['axis']['enum'] == ['quality']
    with pytest.raises(JudgeContractError):
        adapter.judge({'axes': []}, model='synthetic', reasoning_effort='low')


@pytest.mark.parametrize('name,error', [('APITimeoutError', TimeoutError), ('APIConnectionError', ConnectionError), ('InternalServerError', ConnectionError), ('RateLimitError', ConnectionError)])
def test_provider_transient_errors_are_mapped_for_retry(name, error):
    client = Mock()
    client.responses.create.side_effect = type(name, (Exception,), {})('synthetic failure')
    with pytest.raises(error):
        OpenAISemanticJudgeAdapter(client_factory=lambda: client).judge({'axes': ['quality']}, model='synthetic', reasoning_effort='low')


def test_retry_budget_is_bounded_and_contract_errors_are_not_retried():
    adapter = Mock()
    adapter.judge.side_effect = [TimeoutError(), ConnectionError(), {'status': 'COMPLETED'}]
    assert judge_with_retries(adapter, {}, model='synthetic', reasoning_effort='low', max_retries=2) == {'status': 'COMPLETED'}
    assert adapter.judge.call_count == 3
    adapter.judge.reset_mock(side_effect=True)
    adapter.judge.side_effect = JudgeContractError('invalid evidence')
    with pytest.raises(JudgeContractError):
        judge_with_retries(adapter, {}, model='synthetic', reasoning_effort='low', max_retries=5)
    assert adapter.judge.call_count == 1
