"""Management HTTP contracts connected to actual simulated business operations."""

import pytest
from app.main import create_app
from fastapi.testclient import TestClient
from test_tasks_api import QuietModel


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(
        f"sqlite:///{(tmp_path / 'management.sqlite').as_posix()}", model=QuietModel(),
        enable_tasks_actions=True, enable_workflow_actions=True, task_poll_seconds=3600,
    )) as value:
        yield value


def post(client, path, payload):
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def balance(client):
    return next(a['balance_minor'] for a in client.get('/api/overview').json()['accounts']
                if a['id'] == 'demo-checking-001')


def test_new_name_only_contact_aa_then_transfer_details_and_remove(client):
    before = balance(client)
    conversations = client.get('/api/conversations').json()
    body = {'name': '许宁', 'note': '同事', 'client_request_id': 'contact-api-1'}
    person = post(client, '/api/contacts', body)
    assert post(client, '/api/contacts', body)['id'] == person['id']
    assert not person['can_transfer']
    assert client.get('/api/conversations').json() == conversations
    choices = client.get('/api/tasks/recipients').json()['recipients']
    assert any(p['id'] == person['id'] and not p['can_transfer'] for p in choices)
    cid = post(client, '/api/conversations', {'scope': 'bills/payments'})['id']
    plan = post(client, '/api/tasks/preview', {
        'action': 'create_aa', 'conversation_id': cid, 'account_id': 'demo-checking-001',
        'total_minor': 200, 'participant_ids': ['self', person['id']],
    })
    post(client, f'/api/task-plans/{plan["id"]}/decision', {'decision': 'approve', 'conversation_id': cid})
    post(client, f'/api/tasks/{plan["task_id"]}/demo-payment', {'participant_id': person['id']})
    assert balance(client) == before + 100
    update = client.patch(f'/api/contacts/{person["id"]}', json={
        'version': person['version'], 'bank_name': '海岚银行', 'account_last4': '1128'})
    assert update.status_code == 200, update.text
    changed = update.json()
    assert changed['can_transfer']
    assert client.get('/api/contacts', params={'query': '1128'}).json()['contacts'][0]['id'] == person['id']
    assert client.patch(f'/api/contacts/{person["id"]}', json={
        'version': person['version'], 'name': '旧版本'}).status_code == 409
    removed = client.delete(f'/api/contacts/{person["id"]}', params={'version': changed['version']})
    assert removed.status_code == 200, removed.text
    assert removed.json()['removed']
    assert not client.get('/api/contacts', params={'query': '许宁'}).json()['contacts']
    aa = client.get('/api/tasks').json()['aa'][0]
    assert aa['collected_minor'] == 100
    assert balance(client) == before + 100


def test_custom_service_http_order_cancel_refund_and_catalog_removal(client):
    before = balance(client)
    item = post(client, '/api/workflow-services', {
        'name': '节日贺卡', 'merchant_name': '纸间小铺', 'price_minor': 2900,
        'unit': '张', 'description': '', 'cancel_supported': True, 'client_request_id': 'service-api-1',
    })
    assert item['unit'] == '张' and item['active']
    assert any(p['id'] == item['id'] for p in client.get('/api/workflows').json()['catalog'])
    cid = post(client, '/api/conversations', {'scope': 'life/arrangements'})['id']
    plan = post(client, '/api/workflows/preview', {
        'action': 'arrange', 'conversation_id': cid, 'title': '节日安排',
        'account_id': 'demo-checking-001', 'recipient_label': '家人', 'budget_minor': 10000,
        'items': [{'product_id': item['id'], 'quantity': 2}],
    })
    assert plan['total_minor'] == 5800
    post(client, f'/api/workflow-plans/{plan["id"]}/decision', {'decision': 'approve', 'conversation_id': cid})
    for _ in range(4):
        client.app.state.task_tick()
    task = client.get('/api/workflows').json()['tasks'][0]
    assert task['status'] == 'completed' and task['title'] == '节日安排'
    assert task['steps'][0]['unit'] == '张'
    assert balance(client) == before - 5800
    removed = client.delete(f'/api/workflow-services/{item["id"]}', params={'version': item['version']})
    assert removed.status_code == 200, removed.text
    assert removed.json()['removed']
    cancel = post(client, '/api/workflows/preview', {
        'action': 'cancel', 'conversation_id': cid, 'task_id': task['id'],
    })
    assert cancel['items'][0]['unit'] == '张'
    post(client, f'/api/workflow-plans/{cancel["id"]}/decision', {'decision': 'approve', 'conversation_id': cid})
    refunded = client.get('/api/workflows').json()['tasks'][0]
    assert refunded['refunded_minor'] == 5800 and refunded['net_spent_minor'] == 0
    assert balance(client) == before
    assert not client.get('/api/workflow-services', params={'query': '节日贺卡'}).json()['services']
