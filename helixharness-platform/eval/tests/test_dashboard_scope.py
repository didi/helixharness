"""Dashboard and AI use identical admitted facts and workflow filters."""

import copy
import json

from eval.tests.test_usage_loader import _workflow_log
from eval.usage_ai import _build_dataset, usage_data_state_hash
from eval.usage_loader import (
    aggregate_all,
    discover_projects,
    render_usage_global_overview_html,
)


def _write(root, name, payload):
    payload = copy.deepcopy(payload)
    payload["feature"] = name
    path = root / "docs" / "eval" / name / "eval-log.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return str(path)


def test_ai_applies_same_quality_gate_as_homepage(tmp_path):
    root = tmp_path / "repo"
    good = _workflow_log()
    bad = copy.deepcopy(good)
    bad["entries"].append(copy.deepcopy(bad["entries"][0]))
    _write(root, "good", good)
    rejected = _write(root, "duplicate", bad)
    dataset = _build_dataset([root], linked_supplements=[])
    page_facts = aggregate_all(discover_projects([str(root)], {rejected}, []))
    assert dataset["data_state_hash"] == usage_data_state_hash(page_facts)
    assert len(dataset["features"]) == 1




def test_generated_and_team_refreshed_cache_remains_visible(tmp_path, monkeypatch):
    from eval import usage_ai
    from eval.usage_loader import _load_ai_insights

    root = tmp_path / "repo"
    good = _workflow_log()
    _write(root, "admitted", good)
    bad = copy.deepcopy(good)
    bad["entries"].append(copy.deepcopy(bad["entries"][0]))
    rejected = _write(root, "rejected", bad)
    calls = []

    def fake_api(*args, **kwargs):
        calls.append(True)
        return {"answer": "结论：合格项目的解读。"}

    monkeypatch.setattr(usage_ai, "_call_api", fake_api)
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_USAGE_AI_ENABLED", "true")
    config = {"ai_analysis": {"enabled": True}}
    result = usage_ai.generate_usage_ai_insights(config, [root], tmp_path, linked_supplements=[])
    assert result["status"] == "success"
    assert len(calls) == 2  # One admitted feature and one global analysis.
    assert len(result["features"]) == 1
    refreshed = usage_ai.regenerate_usage_team_ai_insight(config, [root], tmp_path, linked_supplements=[])
    assert refreshed["features"] == result["features"]
    assert len(calls) == 3
    html = render_usage_global_overview_html([str(root)], {rejected}, [])
    assert "合格项目的解读" in html
    assert "数据已更新，解读待刷新" not in html
    # A real subsequent change is still hidden and accurately described.
    _write(root, "newly-arrived", good)
    html = render_usage_global_overview_html([str(root)], {rejected}, [])
    assert "数据已更新，解读待刷新" in html
    assert "合格项目的解读" not in html
    assert _load_ai_insights([str(root)])["status"] == "success"  # Disk history remains intact.


def test_platform_share_counts_each_user_once_across_projects():
    from eval.usage_loader import _runtime_distribution_html

    older = _workflow_log()["entries"][0]
    later = {**older, "started_at": "2026-09-10T01:00:00Z", "ended_at": "2026-09-10T01:10:00Z"}
    second_user = {**older, "actor": {"user_name": "Another"}}
    html = _runtime_distribution_html(
        [
            {"feature": "earlier", "entries": [older, second_user]},
            {"feature": "later", "entries": [later]},
        ],
        "platform",
    )
    assert 'data-label="Codex" data-count="2"' in html
    assert 'data-label="Legacy"' not in html
    assert "共 2 人" in html
    assert "<small>2 人</small><b>100.0%</b>" in html
