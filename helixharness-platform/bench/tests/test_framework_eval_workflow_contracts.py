"""Current HelixHarness workflow contracts freeze explicit actions and reject retired commands."""
import pytest

from bench.workflow_contracts import WorkflowContractError, resolve_workflow_contract


def _closure() -> dict[str, object]:
    return {
        "schema_version": "framework-eval-helixharness-closure.v1",
        "max_stage_rework_cycles": 3,
        "stage_findings_prompt": "repair the failed native stage",
        "transitions": [],
    }

def resolve(commands):
    return resolve_workflow_contract(commands, profile_name="UNIFIED_EVALUATION", side="BASE", source_commit="a"*40, workflow_closure=_closure())

def test_workflow_chain_freezes_exact_commands_and_archive():
    commands=["/helix:init", "/helix:propose demo request.md", "/helix:apply demo", "/helix:archive demo"]
    result=resolve(commands)
    assert [s["command"] for s in result["steps"]] == commands
    assert "variant" not in result
    assert result["terminal"]["evidence_contract"] == "HELIXHARNESS_ARCHIVE"
    assert "REVIEW" not in result["goal_control"]["actions"]

def test_loop_is_terminal_and_keeps_exact_invocation():
    result=resolve(["/helix:propose demo request.md --loop"])
    assert result["steps"][0]["artifact_contract"] == "HELIXHARNESS_ARCHIVE"
    assert result["steps"][0]["command"].endswith("--loop")


def test_literal_requirement_flags_do_not_turn_propose_into_archive():
    result = resolve(["/helix:propose demo -- --loop --example", "/helix:archive demo"])
    assert result["steps"][0]["artifact_contract"] == "HELIXHARNESS_PROPOSE"


def test_workspace_init_and_independent_unit_test_command_keep_feature_identity():
    result = resolve([
        "/helix:init --workspace --repos ../service-a ../service-b",
        "/helix:propose demo implement a requirement",
        "/helix:ut --packages sample.service --concurrency 2",
        "/helix:apply demo",
        "/helix:archive demo --no-commit",
    ])
    assert result["feature"] == "demo"
    assert result["steps"][2]["artifact_contract"] == "UT"

@pytest.mark.parametrize("commands", [
 ["/helix:review demo", "/helix:archive demo"],
 ["/helix:propose demo --workflow", "/helix:archive demo"],
 ["/helix:propose demo --loop", "/helix:archive demo"],
 ["/helix:apply one", "/helix:archive two"],
 ["/helix:apply demo"],
 ["/helix:archive demo && echo bad"],
 ["/helix:init --removed", "/helix:archive demo"],
 ["/helix:init --repos ../service", "/helix:archive demo"],
 ["/helix:propose demo --loop=false"],
 ["/helix:archive demo --commit --no-commit"],
])
def test_invalid_or_retired_chains_are_rejected(commands):
    with pytest.raises(WorkflowContractError):
        resolve(commands)
