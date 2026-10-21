"""The public demo must expose inspectable synthetic examples without real execution."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from web_ui.demo import configure_demo
from web_ui.main import create_app


@pytest.mark.parametrize("external_catalog", [False, True])
def test_demo_catalog_save_is_isolated_and_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external_catalog: bool,
) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    source = Path(__file__).resolve().parents[2] / "evolution/catalog/capabilities.yaml"
    original = source.read_bytes()
    external = tmp_path / "external.yaml"
    external.write_bytes(original)
    if external_catalog:
        monkeypatch.setenv("HELIXHARNESS_PLATFORM_EVOLUTION_CATALOG_PATH", str(external))
    else:
        monkeypatch.delenv("HELIXHARNESS_PLATFORM_EVOLUTION_CATALOG_PATH", raising=False)
    data_dir = tmp_path / "demo"
    configure_demo(data_dir)
    app = create_app()
    app.state.demo_mode = True
    catalog = app.state.evolution_catalog_path
    assert catalog == data_dir / "evolution/capabilities.yaml"
    assert catalog.read_bytes() == original
    edited = json.dumps({
        "schema_version": "evolution-capability-catalog.v1",
        "capabilities": [{
            "id": "demo-capability", "name": "Demo capability", "description": "Synthetic example",
            "selectors": [], "owned_paths": [], "protected_paths": [], "case_version_ids": [],
        }],
    }) + "\n"
    with TestClient(app) as client:
        response = client.post("/evolution/config/catalog", data={"catalog_yaml": edited},
                               follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/evolution/config?saved=catalog"
        assert len(app.state.evolution_service.capabilities) == 1
        assert str(catalog) in client.get("/evolution/config").text
    assert catalog.read_text() == edited
    assert source.read_bytes() == original
    assert external.read_bytes() == original
    configure_demo(data_dir)
    restarted = create_app()
    assert restarted.state.evolution_catalog_path == catalog
    assert len(restarted.state.evolution_service.capabilities) == 1
    assert catalog.read_text() == edited
    assert source.read_bytes() == original


def test_demo_lists_synthetic_cases_without_changing_normal_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "environ", os.environ.copy())
    configure_demo(tmp_path / "demo")
    ledger_path = tmp_path / "demo/demo-project/docs/eval/example-feature/eval-log.json"
    ledger = json.loads(ledger_path.read_text())
    assert set(ledger["requirement"]) == {"id", "name"}
    app = create_app()
    app.state.demo_mode = True
    with TestClient(app) as client:
        cases = client.get("/framework-evaluation/cases")
        assert cases.status_code == 200
        assert 'data-scenario-case-id=' in cases.text
        assert "合成示例 · 仅供查看" in cases.text
        case = app.state.scenario_store.list_current()[0]
        detail = client.get(f"/framework-evaluation/cases/{case.case_id}")
        assert detail.status_code == 200
        assert "合成示例 · 仅供查看" in detail.text
        app.state.demo_mode = False
        normal = client.get("/framework-evaluation/cases")
        assert 'data-scenario-case-id=' not in normal.text
