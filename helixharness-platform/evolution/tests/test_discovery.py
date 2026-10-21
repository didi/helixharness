from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evolution.discovery import CodexSemanticAnalyzer, SemanticProposal, discover_hypotheses
from evolution.models import (
    BenchmarkRemediationEvidence,
    Capability,
    EvaluationFinding,
    EvolutionEvidence,
    HypothesisKind,
)


def _capability(**overrides: object) -> Capability:
    values = {
        "capability_id": "workflow",
        "name": "工作流",
        "description": "工作流纪律",
        "selectors": ("archive",),
        "owned_paths": ("commands/",),
        "protected_paths": ("scripts/eval_telemetry.py",),
        "case_version_ids": ("case-1",),
        "auto_repair": True,
        "growth_confirmed": False,
    }
    values.update(overrides)
    return Capability(**values)


def _evidence(**overrides: object) -> EvolutionEvidence:
    values = {
        "evidence_id": "hash:entry-1",
        "source_path": "/tmp/eval-log.json",
        "source_commit": "abc",
        "source_hash": "hash",
        "feature": "feature-a",
        "project_name": "demo",
        "helixharness_version": "2.0.0",
        "command": "/helix:archive",
        "stage_kind": "archive",
        "status": "blocked",
        "outcome_reason": "archive prerequisite is missing",
        "gate_result": "fail",
        "evidence_complete": False,
        "occurred_at": "2026-08-01T00:00:00Z",
    }
    values.update(overrides)
    return EvolutionEvidence(**values)


class _Analyzer:
    def analyze(self, capability, evidence, *, analysis_context=None):
        return (
            SemanticProposal(
                title="增加轻量归档能力",
                rationale="成功链路仍有重复步骤",
                expected_effect="减少一次非必要阶段",
                confidence=0.81,
            ),
        )


class _CapturingAnalyzer(_Analyzer):
    def __init__(self) -> None:
        self.evidence = ()

    def analyze(self, capability, evidence, *, analysis_context=None):
        self.evidence = tuple(evidence)
        return super().analyze(capability, evidence, analysis_context=analysis_context)


def test_current_contract_failure_is_always_repair() -> None:
    hypotheses = discover_hypotheses((_evidence(),), (_capability(),), analyzer=_Analyzer())

    assert len(hypotheses) == 1
    assert hypotheses[0].kind is HypothesisKind.REPAIR
    assert hypotheses[0].requires_human_selection is False
    assert hypotheses[0].evidence_ids == ("hash:entry-1",)
    assert "source_variant" not in hypotheses[0].__dict__


def test_full_snapshot_is_context_but_only_new_evidence_triggers_hypothesis() -> None:
    historical = _evidence(evidence_id="old:entry", status="blocked")
    changed = _evidence(evidence_id="new:entry", status="blocked")
    analyzer = _CapturingAnalyzer()

    hypotheses = discover_hypotheses(
        (historical, changed),
        (_capability(),),
        analyzer=analyzer,
        focus_evidence_ids={"new:entry"},
    )

    assert [item.analysis_scope for item in analyzer.evidence] == [
        "HISTORICAL_CONTEXT",
        "NEW_OR_CHANGED",
    ]
    assert hypotheses[0].evidence_ids == ("new:entry",)
    assert hypotheses[0].context_evidence_ids == ("new:entry", "old:entry")


def test_model_opportunity_without_contract_failure_is_growth() -> None:
    evidence = _evidence(status="completed", gate_result="pass", evidence_complete=True)

    hypotheses = discover_hypotheses((evidence,), (_capability(),), analyzer=_Analyzer())

    assert len(hypotheses) == 1
    assert hypotheses[0].kind is HypothesisKind.GROWTH
    assert hypotheses[0].requires_human_selection is True


def test_success_evidence_does_not_create_growth_without_semantic_analyzer() -> None:
    evidence = _evidence(status="completed", gate_result="pass", evidence_complete=True)

    assert discover_hypotheses((evidence,), (_capability(),)) == ()


def test_semantic_analyzer_uses_the_bundled_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bundled-codex"
    binary.write_text("runtime", encoding="utf-8")
    binary.chmod(0o755)
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = kwargs.get("input")
        captured["timeout"] = kwargs.get("timeout")
        captured["env"] = kwargs.get("env")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(json.dumps({"proposals": []}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("evolution.discovery.resolve_codex_executable", lambda: binary)
    monkeypatch.setattr("evolution.discovery.subprocess.run", fake_run)

    evidence = _evidence(
        fixed_evidence={"stage": {"status": "blocked"}},
        async_evidence={"tools": {"failed": 1}},
        code_evidence={"changed_files": [{"path": "workflow.md", "added": 2, "deleted": 1}]},
    )
    proposals = CodexSemanticAnalyzer(
        tmp_path,
        model="codex-evaluation-model",
        api_key="test-api-key",
        base_url="https://evaluation.example/v1/",
        timeout_seconds=17,
    ).analyze(_capability(), (evidence,))

    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == str(binary)
    assert "/Applications/ChatGPT.app" not in " ".join(command)
    assert "test-api-key" not in " ".join(command)
    assert 'model_provider="evaluation_proxy"' in command
    assert 'model_providers.evaluation_proxy.base_url="https://evaluation.example/v1"' in command
    assert 'model_providers.evaluation_proxy.env_key="OPENAI_API_KEY"' in command
    assert 'model_providers.evaluation_proxy.wire_api="responses"' in command
    assert command[command.index("--model") + 1] == "codex-evaluation-model"
    assert captured["timeout"] == 17
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["OPENAI_API_KEY"] == "test-api-key"
    prompt = str(captured["input"])
    assert '"fixed_evidence"' in prompt
    assert '"async_evidence"' in prompt
    assert '"code_evidence"' in prompt
    assert "checkpoint commit diff metadata" in prompt
    assert "plain Chinese" in prompt
    assert "Never copy raw errors" in prompt
    assert "Case or Run IDs" in prompt
    assert proposals == ()


def test_rule_repair_keeps_raw_failure_in_generation_basis_only() -> None:
    hypothesis = discover_hypotheses((_evidence(),), (_capability(),))[0]

    operator_copy = " ".join(
        (
            hypothesis.title,
            hypothesis.rationale,
            hypothesis.intervention,
            hypothesis.acceptance.expected_effect,
            hypothesis.validation_plan,
            hypothesis.falsification_condition,
        )
    )
    assert "archive prerequisite is missing" not in operator_copy
    assert hypothesis.generation_basis[0]["outcome_reason"] == "archive prerequisite is missing"
    assert hypothesis.generation_basis[0]["command"] == "/helix:archive"


def test_semantic_analyzer_has_a_hard_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bundled-codex"
    binary.write_text("runtime", encoding="utf-8")
    binary.chmod(0o755)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("evolution.discovery.resolve_codex_executable", lambda: binary)
    monkeypatch.setattr("evolution.discovery.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="timed out after 3 seconds"):
        CodexSemanticAnalyzer(tmp_path, timeout_seconds=3).analyze(
            _capability(),
            (_evidence(),),
        )


def test_semantic_analyzer_generates_plain_benchmark_narrative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "bundled-codex"
    binary.write_text("runtime", encoding="utf-8")
    binary.chmod(0o755)
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["input"] = kwargs.get("input")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "proposal": {
                        "title": "提升工作流的完成可靠性",
                        "rationale": "部分业务场景未能稳定完成，需要提高执行一致性。",
                        "intervention": "梳理关键执行环节，补齐容易中断或遗漏的处理。",
                        "expected_effect": "让同类任务稳定完成并产出可确认的结果。",
                        "validation_plan": "沿用原有业务场景重新对比修改前后的表现。",
                        "falsification_condition": "如果同类场景仍不稳定，则本次修改不成立。",
                        "confidence": 0.86,
                    }
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("evolution.discovery.resolve_codex_executable", lambda: binary)
    monkeypatch.setattr("evolution.discovery.subprocess.run", fake_run)
    evidence = BenchmarkRemediationEvidence(
        source_run_id="run-source",
        root_run_id="run-source",
        profile="WORKFLOW_REDESIGN",
        base_commit="base-commit",
        candidate_commit="candidate-commit",
        case_ids=("case-1",),
        decision_status="DO_NOT_RECOMMEND",
        decision_reason="Candidate triggered CORE_BUSINESS_BEHAVIOR_FAILURE",
        findings=(
            EvaluationFinding(
                finding_id="finding-1",
                case_id="case-1",
                category="CANDIDATE_HARD_GATE",
                summary="Candidate failed one business check",
                evidence_ids=("evidence-1",),
            ),
        ),
        created_at="2026-08-25T00:00:00Z",
    )

    proposal = CodexSemanticAnalyzer(tmp_path).analyze_benchmark_remediation(
        _capability(),
        evidence,
    )

    assert proposal is not None
    assert proposal.title == "提升工作流的完成可靠性"
    prompt = str(captured["input"])
    assert "Candidate triggered CORE_BUSINESS_BEHAVIOR_FAILURE" in prompt
    assert "Do not copy or paraphrase individual raw errors" in prompt
    assert "collapsed generation evidence" in prompt
