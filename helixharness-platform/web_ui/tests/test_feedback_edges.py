"""Receipt failures, filter pagination and bounded workflow association."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from eval import usage_loader, usage_sync
from web_ui.main import create_app
from web_ui.routes import feedback_report as routes
from web_ui.tests.test_feedback_report import _item


@pytest.mark.parametrize('payload', [[], {}, {'items': {}}, {'items': [{}] * 101}])
def test_feedback_receipt_rejects_non_batch_or_oversized_payload(payload):
    with TestClient(create_app()) as client:
        assert client.post('/feedback/report', json=payload).status_code == 400


def test_storage_failure_is_retryable_and_does_not_lose_successful_siblings():
    with TestClient(create_app()) as client:
        store = client.app.state.feedback_store
        with patch.object(store, 'ingest', side_effect=[sqlite3.OperationalError('synthetic unavailable'), {'status': 'accepted'}]):
            response = client.post('/feedback/report', json={'items': [_item(), _item()]})
        assert response.status_code == 200
        rows = response.json()['items']
        assert [row['status'] for row in rows] == ['retryable', 'accepted']
        assert rows[0]['error']['code'] == 'storage_unavailable'
        assert rows[0]['feedback_id'] == _item()['feedback_id']
        assert 'synthetic unavailable' not in response.text


def test_feedback_pagination_and_invalid_filter_values_are_normalized():
    with TestClient(create_app()) as client:
        items = []
        for index in range(25):
            item = _item()
            item['feedback_id'] = item['document']['feedback_id'] = f'fbk-{index:016x}'
            item['document']['text'] = f'Synthetic row {index}'
            items.append(item)
        assert all(row['status'] == 'accepted' for row in client.post('/feedback/report', json={'items': items}).json()['items'])
        page = client.get('/feedback/partial/table?page=999&page_size=10')
        assert page.status_code == 200
        assert page.text.count('class="helixharness-feedback-copy"') == 5
        default = client.get('/feedback/partial/table?page=-1&page_size=invalid&category=not-real')
        assert default.status_code == 200
        assert default.text.count('class="helixharness-feedback-copy"') == 20
        assert 'page_size=20' in default.text
        assert 'not-real' not in default.text


@pytest.mark.parametrize('document,ids,association', [
    ({'related_entry_ids': ['e3'], 'feature': 'f1', 'session': 's1'}, ['e3'], 'related_entry_ids'),
    ({'related_entry_ids': ['missing'], 'feature': 'f1', 'session': 's1'}, ['e1', 'e2'], 'feature'),
    ({'feature': 'missing', 'session': 's1'}, ['e1', 'e3'], 'session'),
    ({'feature': 'missing', 'session': 'missing'}, [], None),
])
def test_feedback_association_prioritizes_exact_entries_then_feature_then_session(monkeypatch, document, ids, association):
    data = SimpleNamespace(projects=[{'features': [
        {'feature': 'f1', 'entries': [{'entry_id': 'e1', 'session_hash': 's1'}, {'entry_id': 'e2', 'session_hash': 's2'}]},
        {'feature': 'f2', 'entries': [{'entry_id': 'e3', 'session_hash': 's1'}]},
    ]}])
    monkeypatch.setattr(routes, 'get_roots', lambda: [])
    monkeypatch.setattr(usage_sync, '_current_usage_roots', lambda roots: [])
    monkeypatch.setattr(usage_loader, 'load_usage_dashboard_data', lambda roots: data)
    rows, selected = routes._related_workflow_entries(None, document)
    assert [row['entry_id'] for row in rows] == ids
    assert selected == association


def test_association_is_optional_when_usage_data_is_unavailable(monkeypatch):
    monkeypatch.setattr(routes, 'get_roots', lambda: [])
    def unavailable(*args):
        raise RuntimeError('synthetic read failure')
    monkeypatch.setattr(usage_sync, '_current_usage_roots', unavailable)
    assert routes._related_workflow_entries(None, {'feature': 'f1'}) == ([], None)
    assert routes._related_workflow_entries(None, {'session': 'unknown'}) == ([], None)


def test_transcript_groups_ignore_preamble_and_blank_lines():
    assert routes._parse_transcript('preamble\n## 12:34:56 user\nHello\n\n## 12:35:00 assistant\nReply') == [
        {'time': '12:34:56', 'role': 'user', 'lines': ['Hello']},
        {'time': '12:35:00', 'role': 'assistant', 'lines': ['Reply']},
    ]
    assert routes._parse_transcript('preamble only') == []
    assert routes._page_window(10, 20) == [1, 2, '…', 9, 10, 11, '…', 19, 20]
