"""Real plugin CLI artifacts must reach platform storage without invented sessions."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eval.telemetry_materialization import materialize_eval_log
from eval.usage_loader import _resource_usage, _user_interactions, render_feature_timeline_html
from web_ui import create_app


def _stage_artifacts(root: Path) -> dict:
    plugin = Path(__file__).resolve().parents[3] / "helixharness"
    if not (plugin / "scripts/metrics.py").is_file():
        pytest.skip("cross-package acceptance requires the sibling plugin checkout")
    git = shutil.which("git")
    if not git:
        pytest.skip("cross-package acceptance requires Git")
    root.mkdir()
    repo = root / "synthetic OpenCode project"
    repo.mkdir()
    code = r'''
import gzip,json,subprocess,sys
from pathlib import Path
plugin,repo,telemetry=json.load(sys.stdin)
plugin,repo,telemetry=Path(plugin),Path(repo),Path(telemetry)
telemetry.mkdir()
(telemetry/'config.json').write_text('{"endpoint":"disabled-for-synthetic-test"}')
def cli(name,*args):
    p=subprocess.run([sys.executable,str(plugin/'scripts'/name),*map(str,args)],cwd=repo,
        capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert p.returncode==0,p.stdout+p.stderr
    return json.loads(p.stdout)
subprocess.run(['git','init','-q',str(repo)],check=True)
notice=cli('user_config.py','--repo-root',repo,'--platform','opencode',
    '--telemetry','enabled','--scope','project','--user-confirmed')
assert notice['notice_required'] and not notice['change']['updated']
# Acknowledgment belongs only to this synthetic test profile, not a native UI claim.
cli('user_config.py','--repo-root',repo,'--platform','opencode','--telemetry','enabled',
    '--scope','project','--user-confirmed','--telemetry-notice-shown',notice['notice']['sha256'])
cli('user_config.py','--repo-root',repo,'--platform','opencode',
    '--report-endpoint','https://telemetry.example.test/report','--user-confirmed')
start=cli('metrics.py','--repo-root',repo,'--platform','opencode','start',
    '--feature','synthetic','--stage','propose','--trigger','user_command')
finish=cli('metrics.py','--repo-root',repo,'--platform','opencode','finish',
    '--feature','synthetic','--stage','propose','--status','completed')
assert finish['entry_id']==start['entry_id']
assert cli('telemetry_collector.py','--repo-root',repo)['collected']==1
outbox,=list((telemetry/'outbox').glob('*.json.gz'))
envelope=json.loads(gzip.decompress(outbox.read_bytes()))
ledger=json.loads((repo/'docs/eval/synthetic/eval-log.json').read_text())
print(json.dumps({'repo':str(repo),'ledger':ledger,'payload':envelope['supplement']}))
'''
    environment = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), str(Path(git).parent), os.defpath]),
        **{key: os.environ[key] for key in ("SystemRoot", "COMSPEC", "PATHEXT") if key in os.environ},
        "HOME": str(root), "USERPROFILE": str(root), "TMPDIR": str(root),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "HELIXHARNESS_OPENCODE_HOME": str(root / "opencode"),
        "HELIXHARNESS_TELEMETRY_HOME": str(root / "telemetry"),
        "HELIXHARNESS_TELEMETRY_DISABLE_SCHEDULER": "1",
        "HELIXHARNESS_SESSION_ID": "ses_synthetic_pipeline", "HELIXHARNESS_SESSION_CWD": str(repo.resolve()),
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": os.devnull,
    }
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=root, env=environment,
                            input=json.dumps([str(plugin), str(repo), str(root / "telemetry")]),
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_opencode_stage_cli_to_report_storage_and_dashboard(tmp_path, monkeypatch):
    from eval import usage_sync

    artifacts = _stage_artifacts(tmp_path / "plugin-cli")
    repo, payload, ledger = Path(artifacts["repo"]), artifacts["payload"], artifacts["ledger"]
    monkeypatch.setenv("HELIXHARNESS_PLATFORM_HELIXHARNESS_ROOTS", str(repo))
    monkeypatch.setattr(usage_sync, "schedule_usage_ai_regeneration", lambda roots: None)
    with TestClient(create_app()) as client:
        receipt = client.post("/report", json={"items": [payload]})
        assert receipt.status_code == 200
        assert receipt.json()["items"][0]["status"] == "accepted"
        assert client.post("/report", json={"items": [payload]}).json()["items"][0]["status"] == "duplicate"
        store = client.app.state.stage_supplement_store
        assert store.refresh_links([repo]) == 1
        record, = store.active_linked_records()
        assert record.payload["evidence"]["reason_code"] == "host_transcript_not_collected"
        projected = materialize_eval_log(ledger, {payload["entry_id"]: record})
        entry, = projected["entries"]
        assert entry["telemetry_status"] == "complete"
        assert entry["collection"]["semantic_status"] == "unavailable"
        assert entry["collection"]["scope"] == "none"
        assert entry["collection"]["confidence"] == "unknown"
        assert _resource_usage(entry)["total_tokens"] is None
        assert _user_interactions([entry])["total"] is None
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        for html in (render_feature_timeline_html(projected), dashboard.text):
            assert "会话未采集" in html
            assert "仅记录阶段信息" in html
            assert "用户交互轮次 <b>0 轮</b>" not in html
        assert payload["usage"] == {} and payload["execution"] == {}
