"""Deleted experiments retain their decision state and offer a reviewed retry."""

import re

from evolution.candidate import CandidateBuildError
from evolution.tests.test_retry import _blocked
from evolution.tests.test_service import _Gateway
from web_ui.tests.test_server import client as _client

client = _client


def _attach(client, service):
    client.app.state.evolution_store = service.store
    client.app.state.evolution_service = service
    client.app.state.evolution_scheduler.service = service


def _dossier(response, hypothesis_id):
    return re.search(rf'<tbody id="hypothesis-{hypothesis_id}".*?</tbody>', response.text, re.S).group()


def test_deleted_blocked_experiment_has_visible_recovery_action(client, tmp_path):
    service, builder, hypothesis_id, experiment_id = _blocked(tmp_path)
    service.delete_experiment(experiment_id, "从列表移除")
    _attach(client, service)

    response = client.get("/evolution/hypotheses")

    assert response.status_code == 200
    dossier = _dossier(response, hypothesis_id)
    assert "已进入实验" in dossier
    assert "待确认" not in dossier
    assert "关联实验已从列表移除" in dossier
    assert f'href="/evolution/experiments#experiment-{experiment_id}"' not in dossier
    assert f'action="/evolution/experiments/{experiment_id}/retry"' in dossier
    assert "重新生成候选并审阅" in dossier
    assert "采纳并进入实验" not in dossier

    response = client.post(f"/evolution/experiments/{experiment_id}/retry", follow_redirects=False)
    assert response.status_code == 303
    retry_id = response.headers["location"].split("#hypothesis-")[1]
    assert client.app.state.evolution_scheduler.wait_for_candidate_preparation(retry_id, timeout=5)
    repeated = client.post(f"/evolution/experiments/{experiment_id}/retry", follow_redirects=False)
    assert repeated.headers["location"] == response.headers["location"]
    assert builder.calls == 2
    page = client.get("/evolution/hypotheses")
    assert "采纳并进入实验" in _dossier(page, retry_id)
    assert "查看重新生成记录" in _dossier(page, hypothesis_id)
    assert len(service.store.list_experiments(include_deleted=True)) == 1

    service.evaluation_gateway = _Gateway()
    assert client.post(f"/evolution/hypotheses/{retry_id}/select", follow_redirects=False).status_code == 303
    assert client.post(f"/evolution/hypotheses/{retry_id}/select", follow_redirects=False).status_code == 422
    assert len(service.store.list_experiments(include_deleted=True)) == 2
    assert service.store.get_experiment(experiment_id)["deleted_at"] is not None


def test_deleted_running_experiment_cannot_be_retried(client, tmp_path):
    service, _, hypothesis_id, experiment_id = _blocked(tmp_path)
    service.store.attach_benchmark_run(experiment_id, "run-existing")
    service.delete_experiment(experiment_id, "从列表移除")
    _attach(client, service)
    dossier = _dossier(client.get("/evolution/hypotheses"), hypothesis_id)
    assert "已进入实验" in dossier
    assert f'action="/evolution/experiments/{experiment_id}/retry"' not in dossier
    assert client.post(f"/evolution/experiments/{experiment_id}/retry").status_code == 422


def test_selected_hypothesis_with_missing_experiment_is_not_pending(client, tmp_path):
    service, _, hypothesis_id, _ = _blocked(tmp_path)
    with service.store.connect() as connection:
        connection.execute("DELETE FROM experiments")
    _attach(client, service)
    dossier = _dossier(client.get("/evolution/hypotheses"), hypothesis_id)
    assert "已进入实验" in dossier
    assert "待确认" not in dossier
    assert "假设已批准，但关联实验记录缺失" in dossier
    assert "采纳并进入实验" not in dossier


def test_failed_retry_generation_can_resume_without_using_old_candidate(client, tmp_path, monkeypatch):
    service, builder, hypothesis_id, experiment_id = _blocked(tmp_path)
    _attach(client, service)
    build = builder.build

    def fail(*args, **kwargs):
        raise CandidateBuildError("无法刷新远端 master")

    monkeypatch.setattr(builder, "build", fail)
    response = client.post(f"/evolution/experiments/{experiment_id}/retry", follow_redirects=False)
    retry_id = response.headers["location"].split("#hypothesis-")[1]
    assert client.app.state.evolution_scheduler.wait_for_candidate_preparation(retry_id, timeout=5)
    dossier = _dossier(client.get("/evolution/hypotheses"), retry_id)
    assert "无法刷新远端 master" in dossier
    assert "重新生成候选版本" in dossier
    assert "采纳并进入实验" not in dossier
    assert service.store.get_hypothesis_candidate(retry_id) is None
    assert service.store.get_hypothesis_candidate(hypothesis_id) is not None

    monkeypatch.setattr(builder, "build", build)
    client.post(f"/evolution/hypotheses/{retry_id}/prepare-candidate", follow_redirects=False)
    assert client.app.state.evolution_scheduler.wait_for_candidate_preparation(retry_id, timeout=5)
    assert "采纳并进入实验" in _dossier(client.get("/evolution/hypotheses"), retry_id)
    assert len(service.store.list_experiments(include_deleted=True)) == 1


def test_pending_candidate_after_web_restart_has_a_start_action(client, tmp_path):
    service, _, _, experiment_id = _blocked(tmp_path)
    retry_id = service.retry_blocked_experiment(experiment_id)
    _attach(client, service)
    dossier = _dossier(client.get("/evolution/hypotheses"), retry_id)
    assert "候选版本准备中" not in dossier
    assert f'action="/evolution/hypotheses/{retry_id}/prepare-candidate"' in dossier
    response = client.post(f"/evolution/hypotheses/{retry_id}/prepare-candidate", follow_redirects=False)
    assert response.status_code == 303
    assert client.app.state.evolution_scheduler.wait_for_candidate_preparation(retry_id, timeout=5)
    assert "采纳并进入实验" in _dossier(client.get("/evolution/hypotheses"), retry_id)
