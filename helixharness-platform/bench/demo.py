"""Build a synthetic Case and Mock Run with the current public contracts."""
from __future__ import annotations

from pathlib import Path

from bench.evaluation_contracts import ArtifactSource, ExecutionStrategy, StrategyStage, freeze_evaluation_contract
from bench.scenario_runtime import (
    MockScenarioExecutionAdapter,
    ScenarioRunCreateRequest,
    ScenarioRunService,
    ScenarioWorker,
)
from bench.scenario_storage import ScenarioStore
from bench.scenarios import load_scenario_source


def _strategy(side: str) -> ExecutionStrategy:
    commands = tuple(f"/helix:{command} demo-task-filter" for command in ("propose", "apply", "archive"))
    return ExecutionStrategy(
        strategy_id=f"demo-{side}", version="1", adapter_id="known-helixharness", adapter_version="1",
        source_commit=("a" if side == "base" else "b") * 40,
        source_document_hashes=(("commands/propose.md", "c" * 64),),
        commands=commands,
        stages=tuple(StrategyStage(name.upper(), name.upper(), command, "COMPLETED", True)
                     for name, command in zip(("propose", "apply", "archive"), commands)),
        completion_states=("COMPLETED",),
        artifact_sources=(ArtifactSource("completed", "docs/specs/completed/demo-task-filter"),),
        finding_policy="NONE", finding_closures=(),
    )


def seed_demo(database: Path) -> None:
    """Generate inspectable sample data without fetching repositories or calling models."""
    store = ScenarioStore(database)
    store.initialize()
    if store.list_current():
        return
    case = store.publish(load_scenario_source(Path(__file__).parent / "sample_data/task-filter"))
    contract = freeze_evaluation_contract(base_strategy=_strategy("base"), candidate_strategy=_strategy("candidate"))
    service = ScenarioRunService(store)
    request = ScenarioRunCreateRequest(
        scenario_ids=(case.case_id,), run_kind="DEVELOPMENT", execution_adapter="MOCK",
        evaluation_contract=contract, created_by="Synthetic Demo",
    )
    preview = service.preview(request)
    run = service.create(request, expected_preview_hash=preview.preview_hash)
    worker = ScenarioWorker(store, MockScenarioExecutionAdapter())
    for _ in range(4):
        if worker.run_once(run_id=run.run_id).status == "FINALIZED":
            return
    raise RuntimeError("Synthetic demo Run did not finalize")
