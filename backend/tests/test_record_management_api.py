"""Focused checks for user records, batch ownership, feedback and informational reminders."""

from app.main import create_app
from fastapi.testclient import TestClient
from test_api import ScriptedModel


def post(client, path, data):
    response = client.post(path, json=data)
    assert response.status_code == 200, response.text
    return response.json()


def test_bank_annotations_and_manual_records_share_report_without_mutating_cash(tmp_path):
    url = f"sqlite:///{(tmp_path / 'records.sqlite').as_posix()}"
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        before = client.get('/api/overview').json()
        entry = next(e for e in client.get('/api/billing/entries').json()['entries']
                     if e['source'] == 'bank' and e['amount_minor'] < 0)
        changed = client.patch(f'/api/billing/entries/{entry["id"]}', json={
            'version': entry['version'], 'category': '个人分类', 'tags': ['已核对'], 'note': '会议支出'})
        assert changed.status_code == 200, changed.text
        edited = changed.json()
        assert edited['category'] == '个人分类'
        assert client.patch(f'/api/billing/entries/{entry["id"]}', json={
            'version': edited['version'], 'amount_minor': -1}).status_code in (409, 422)
        assert client.get('/api/overview').json() == before
        body = {'client_request_id': 'm' * 120, 'date': '2026-10-22', 'merchant': '手工餐费',
                'amount_minor': 1250, 'transaction_kind': 'expense', 'category': '餐饮', 'note': '自记'}
        manual = post(client, '/api/billing/manual', body)
        assert len(manual['id']) <= 80
        assert post(client, '/api/billing/manual', body)['id'] == manual['id']
        assert manual['amount_minor'] == -1250
        report = client.get('/api/billing/report', params={'source': 'manual', 'query': '自记'}).json()
        assert report['net_expense_minor'] == 1250 and report['entry_count'] == 1
        assert client.get('/api/overview').json() == before
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        rows = client.get('/api/billing/entries', params={'category': '个人分类'}).json()['entries']
        assert len(rows) == 1 and rows[0]['note'] == '会议支出'
        assert rows[0]['tags'] == ['已核对']
        assert client.delete(f'/api/billing/entries/{manual["id"]}', params={'version': manual['version']}).status_code == 200
        assert client.get('/api/billing/report', params={'source': 'manual'}).json()['entry_count'] == 0


def test_import_undo_owns_only_new_rows_and_allows_reimport(tmp_path):
    url = f"sqlite:///{(tmp_path / 'imports.sqlite').as_posix()}"
    header = 'transaction_id,date,merchant,amount,currency,transaction_type\n'
    a = 'own-a,2026-10-22,咖啡,-10.00,CNY,expense\n'
    b = 'own-b,2026-10-22,文具,-20.00,CNY,expense\n'
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        first = post(client, '/api/billing/import', {'filename': 'a.csv', 'content': header+a, 'client_request_id': 'import-a'})
        second = post(client, '/api/billing/import', {'filename': 'b.csv', 'content': header+a+b, 'client_request_id': 'import-b'})
        assert (second['imported'], second['duplicates']) == (1, 1)
        batches = client.get('/api/billing/import-batches').json()['batches']
        assert all(b['created_at'].endswith('+00:00') for b in batches)
        post(client, f'/api/billing/import-batches/{second["batch_id"]}/undo', {})
        post(client, f'/api/billing/import-batches/{second["batch_id"]}/undo', {})
        rows = client.get('/api/billing/entries', params={'source': 'import'}).json()['entries']
        assert len(rows) == 1 and rows[0]['merchant'] == '咖啡'
        third = post(client, '/api/billing/import', {'filename': 'b.csv', 'content': header+b, 'client_request_id': 'import-c'})
        assert third['imported'] == 1
        rows = client.get('/api/billing/entries', params={'source': 'import'}).json()['entries']
        stationery = next(row for row in rows if row['merchant'] == '文具')
        assert client.delete(f'/api/billing/entries/{stationery["id"]}', params={'version': stationery['version']}).status_code == 200
        post(client, f'/api/billing/import-batches/{third["batch_id"]}/undo', {})
        assert client.get('/api/billing/report', params={'source': 'import'}).json()['net_expense_minor'] == 1000
        assert first['batch_id'] != second['batch_id']


def test_personal_record_reminders_never_become_executable_contracts(tmp_path):
    url = f"sqlite:///{(tmp_path / 'personal.sqlite').as_posix()}"
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        before = client.get('/api/overview').json()
        record = post(client, '/api/subscription-records', {
            'name': '自记健身会员', 'amount_minor': 9900, 'cycle': 'monthly',
            'next_charge_on': '2099-03-05', 'lead_days': 3, 'client_request_id': 'personal-1'})
        cid = post(client, '/api/conversations', {})['id']
        assert client.post(f'/api/subscriptions/{record["id"]}/preview', json={
            'conversation_id': cid, 'action': 'cancel_subscription'}).status_code in (404, 409)
        svc = client.app.state.subscriptions
        svc.renewal_notifications([], as_of='2099-03-03')
        svc.renewal_notifications([], as_of='2099-03-03')
        notices = [n for n in svc.persisted_notifications() if n['subscription_id'] == record['id']]
        assert len(notices) == 1
        assert '不会自动扣款' in notices[0]['text']
        updated = client.patch(f'/api/subscription-records/{record["id"]}', json={
            'version': record['version'], 'reminder_enabled': False})
        assert updated.status_code == 200, updated.text
        assert client.get('/api/overview').json() == before
        version = updated.json()['version']
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        stored = client.get('/api/subscriptions').json()['personal_records'][0]
        assert stored['id'] == record['id'] and stored['reminder_enabled'] is False
        assert client.delete(f'/api/subscription-records/{record["id"]}', params={'version': version}).status_code == 200
        assert client.get('/api/subscriptions').json()['personal_records'] == []


def test_candidate_feedback_and_evidence_are_persistent_and_user_controlled(tmp_path):
    url = f"sqlite:///{(tmp_path / 'feedback.sqlite').as_posix()}"
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        candidate = next(c for c in client.get('/api/subscriptions').json()['candidates'] if c['merchant'] == '随手办公')
        assert candidate['evidence_entries'] and all(e['date'] and e['merchant'] for e in candidate['evidence_entries'])
        feedback = client.put(f'/api/subscription-candidates/{candidate["id"]}/feedback', json={
            'version': 0, 'feedback': 'ignored'})
        assert feedback.status_code == 200, feedback.text
    with TestClient(create_app(url, model=ScriptedModel())) as client:
        current = next(c for c in client.get('/api/subscriptions').json()['candidates'] if c['id'] == candidate['id'])
        assert current['feedback'] == 'ignored'
        assert client.put(f'/api/subscription-candidates/{candidate["id"]}/feedback', json={
            'version': 0, 'feedback': 'confirmed'}).status_code == 409
        payload = {'name': current['merchant'], 'amount_minor': current['amount_minor'], 'cycle': current['cycle'],
                   'next_charge_on': current['next_charge_on'], 'candidate_id': current['id'], 'client_request_id': 'confirm-candidate'}
        personal = post(client, '/api/subscription-records', payload)
        assert post(client, '/api/subscription-records', payload)['id'] == personal['id']
        result = client.get('/api/subscriptions').json()
        selected = next(c for c in result['candidates'] if c['id'] == current['id'])
        assert selected['feedback'] == 'confirmed' and selected['personal_record_id'] == personal['id']
        assert len(result['personal_records']) == 1
