"""候选规则的真实 JSON 接口、并发与生产隔离场景。"""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
from unittest.mock import patch
import uuid

from django.conf import settings
from django.contrib.auth.models import User
from django.db import close_old_connections, connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import path

from . import api, rule_api
from .models import AuditEvent, ClientDirectRule, DeploymentJob, OperationJob


urlpatterns = [
    path('api/v1/session', api.session),
    path('api/v1/admin/rules', rule_api.rules),
    path('api/v1/admin/rules/preview', rule_api.preview),
    path('api/v1/admin/rules/<int:rule_id>', rule_api.rule_detail),
]

SETTINGS = {'ROOT_URLCONF': 'portal.test_rule_api', 'PANEL_LIVE': False,
            'CSRF_FAILURE_VIEW': 'portal.api.csrf_failure',
            'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher']}
ROOT = '/api/v1/admin/rules'


def payload(**changes):
    return {'action': 'client_direct', 'kind': 'suffix', 'value': 'school.example.com',
            'scope_domain': '', 'enabled': True, 'idempotency_key': str(uuid.uuid4()), **changes}


@override_settings(**SETTINGS)
class CandidateRuleAPITests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('候选管理员', is_staff=True)
        self.user = User.objects.create_user('普通候选用户')
        self.client.force_login(self.admin)

    def send(self, method, body, path=ROOT, client=None):
        return getattr(client or self.client, method)(path, json.dumps(body), content_type='application/json')

    def saved(self, body=None):
        response = self.send('post', body or payload())
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['data']['item']

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertIsNone(response.json()['data'])
        self.assertEqual(response.json()['error']['code'], code)
        return response.json()['error']

    def test_empty_query_and_preview_do_not_create_rules_or_jobs(self):
        response = self.client.get(ROOT)
        data = response.json()['data']
        self.assertFalse(data['read_only'])
        self.assertEqual(data['items'], [])
        self.assertEqual(data['sources'][2]['state'], 'not_connected')
        preview = self.send('post', {'domain': 'unknown.example.com'}, ROOT + '/preview').json()['data']
        self.assertEqual(preview['result'], 'no_custom_match')
        self.assertIsNone(preview['final_action'])
        self.assertEqual(preview['publish_state'], 'unknown')
        self.assertEqual(preview['client_apply_state'], 'unknown')
        self.assertEqual(ClientDirectRule.objects.count(), 0)
        self.assertEqual(OperationJob.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_staff_gate_and_inactive_session_cover_read_write_preview(self):
        for client, status, code in ((Client(), 401, 'authentication_required'),
                                     (self.client, 403, 'permission_denied')):
            if client is self.client:
                client.force_login(self.user)
            for method, body, path in [('get', None, ROOT), ('post', payload(), ROOT),
                                      ('patch', {'revision': 1}, ROOT + '/1'),
                                      ('delete', {'revision': 1}, ROOT + '/1'),
                                      ('post', {'domain': 'example.com'}, ROOT + '/preview')]:
                response = client.get(path) if method == 'get' else self.send(method, body, path, client)
                self.assert_error(response, status, code)
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assert_error(self.client.get(ROOT), 401, 'authentication_required')
        self.assertEqual(ClientDirectRule.objects.count(), 0)

    def test_csrf_required_for_create_patch_delete_and_preview(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        for method, body, path in [('post', payload(), ROOT), ('patch', {}, ROOT + '/1'),
                                  ('delete', {}, ROOT + '/1'), ('post', {'domain': 'example.com'}, ROOT + '/preview')]:
            self.assert_error(self.send(method, body, path, client), 403, 'csrf_failed')
        token = client.get('/api/v1/session').json()['data']['csrf_token']
        response = client.post(ROOT, json.dumps(payload()), content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201, response.content)
        item = response.json()['data']['item']
        edited = client.patch(ROOT + '/' + str(item['id']), json.dumps({'revision': 1,
            'idempotency_key': str(uuid.uuid4()), 'enabled': False}),
            content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(edited.status_code, 200, edited.content)
        preview = client.post(ROOT + '/preview', json.dumps({'domain': 'school.example.com'}),
                              content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(preview.status_code, 200, preview.content)
        deleted = client.delete(ROOT + '/' + str(item['id']), json.dumps({'revision': 2,
            'idempotency_key': str(uuid.uuid4()), 'confirm': True}),
            content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(deleted.status_code, 200, deleted.content)

    def test_method_gate_returns_allow_and_json(self):
        response = self.client.put(ROOT, '{}', content_type='application/json')
        self.assert_error(response, 405, 'method_not_allowed')
        self.assertEqual(response['Allow'], 'GET, POST')

    def test_create_normalizes_domain_and_records_only_finished_local_receipt(self):
        item = self.saved(payload(value='  SCHOOL.Example.COM.  '))
        self.assertEqual(item['value'], 'school.example.com')
        self.assertEqual(item['scope_domain'], '')
        self.assertEqual(item['revision'], 1)
        row = ClientDirectRule.objects.get()
        self.assertEqual(row.creator, self.admin)
        receipt = OperationJob.objects.get()
        self.assertEqual(receipt.action, rule_api.RECEIPT_ACTION)
        self.assertEqual(receipt.state, 'done')
        self.assertIsNotNone(receipt.finished_at)
        self.assertFalse(DeploymentJob.objects.exists())
        self.assertEqual(AuditEvent.objects.get().result, 'CANDIDATE_ONLY')

    def test_create_normalizes_unicode_domain_without_url_conversion(self):
        item = self.saved(payload(value='例子.中国'))
        self.assertEqual(item['value'], 'xn--fsqu00a.xn--fiqs8s')
        for value in ('https://example.com', '192.168.1.1', '169.254.169.254', 'localhost', 'a..example.com'):
            self.assert_error(self.send('post', payload(value=value)), 422, 'invalid_rule')

    def test_duplicate_normalized_match_and_opposite_action_are_rejected(self):
        item = self.saved()
        for action in ('client_direct', 'proxy'):
            error = self.assert_error(self.send('post', payload(value='SCHOOL.EXAMPLE.COM.', action=action)),
                                      409, 'duplicate_rule')
            self.assertTrue(error['field_errors']['value'])
            self.assertEqual(error['conflicts'][0]['rule_id'], item['id'])
            self.assertEqual(error['conflicts'][0]['relation'], 'duplicate')
        self.assertEqual(ClientDirectRule.objects.count(), 1)

    def test_legacy_noncanonical_values_are_compared_and_previewed_after_normalization(self):
        row = ClientDirectRule.objects.create(creator=self.admin, kind='suffix', value='SCHOOL.EXAMPLE.COM.')
        self.assertEqual(self.client.get(ROOT).json()['data']['items'][0]['value'], 'school.example.com')
        duplicate = self.assert_error(self.send('post', payload()), 409, 'duplicate_rule')
        self.assertEqual(duplicate['conflicts'][0]['rule_id'], row.pk)
        self.assert_error(self.send('post', payload(kind='exact', value='api.school.example.com')), 422, 'invalid_rule')
        preview = self.send('post', {'domain': 'api.school.example.com'}, ROOT + '/preview').json()['data']
        self.assertEqual(preview['matched_id'], row.pk)
        self.assertTrue(preview['related'][0]['matched'])
        row.refresh_from_db()
        self.assertEqual(row.value, 'SCHOOL.EXAMPLE.COM.')

    def test_same_action_parent_child_overlap_rejected_in_both_directions(self):
        self.saved(payload(value='example.com'))
        error = self.assert_error(self.send('post', payload(kind='exact', value='api.example.com')),
                                  422, 'invalid_rule')
        self.assertEqual(error['conflicts'][0]['relation'], 'child_covered_by_parent')
        ClientDirectRule.objects.all().delete()
        self.saved(payload(kind='exact', value='api.example.com'))
        error = self.assert_error(self.send('post', payload(value='example.com')), 422, 'invalid_rule')
        self.assertEqual(error['conflicts'][0]['relation'], 'parent_covers_child')

    def test_exact_parent_and_suffix_child_do_not_overlap_in_either_creation_order(self):
        parent = self.saved(payload(kind='exact', value='example.com'))
        child = self.saved(payload(kind='suffix', value='api.example.com'))
        self.assertEqual(ClientDirectRule.objects.count(), 2)
        value = self.send('post', {'domain': 'www.api.example.com'}, ROOT + '/preview').json()['data']
        self.assertEqual(value['matched_id'], child['id'])
        self.assertEqual(value['conflicts'], [])
        ClientDirectRule.objects.all().delete()
        self.saved(payload(kind='suffix', value='api.example.com'))
        self.saved(payload(kind='exact', value='example.com'))
        self.assertNotEqual(parent['id'], child['id'])

    def test_disabled_rule_remains_duplicate_guard_but_does_not_match(self):
        self.saved(payload(enabled=False))
        self.assert_error(self.send('post', payload(kind='exact', value='api.school.example.com')), 422, 'invalid_rule')
        value = self.send('post', {'domain': 'api.school.example.com'}, ROOT + '/preview').json()['data']
        self.assertEqual(value['result'], 'no_custom_match')
        self.assertFalse(value['related'][0]['enabled'])
        self.assertTrue(value['related'][0]['matched'])

    def test_opposite_action_overlap_warns_and_proxy_wins_preview(self):
        direct = self.saved(payload(value='example.com'))
        response = self.send('post', payload(kind='exact', value='login.example.com', action='proxy'))
        self.assertEqual(response.status_code, 201, response.content)
        warning = response.json()['data']['conflicts'][0]
        self.assertEqual((warning['rule_id'], warning['severity']), (direct['id'], 'warning'))
        proxy = response.json()['data']['item']
        value = self.send('post', {'domain': 'LOGIN.EXAMPLE.COM.'}, ROOT + '/preview').json()['data']
        self.assertEqual(value['domain'], 'login.example.com')
        self.assertEqual(value['result'], 'proxy_candidate')
        self.assertEqual(value['final_action'], 'proxy')
        self.assertEqual(value['matched_id'], proxy['id'])
        self.assertEqual(value['source_id'], 'custom')
        self.assertEqual(len(value['related']), 2)
        self.assertEqual(value['conflicts'][0]['severity'], 'warning')
        self.assertIn('代理候选优先', value['order_semantics'])

    def test_protected_domains_fail_closed_for_direct_and_preview_explains_validation_only(self):
        for change in ({'value': 'chatgpt.com'}, {'value': 'api.googleapis.com'},
                       {'kind': 'regex', 'value': r'^api[0-9]+\.google\.com$', 'scope_domain': 'google.com'}):
            self.assert_error(self.send('post', payload(**change)), 422, 'invalid_rule')
        value = self.send('post', {'domain': 'chatgpt.com'}, ROOT + '/preview').json()['data']
        self.assertEqual(value['result'], 'protected_proxy')
        self.assertEqual(value['final_action'], 'proxy')
        self.assertEqual(value['source_id'], 'protected')
        self.assertIsNone(value['matched_id'])
        self.assertEqual(ClientDirectRule.objects.count(), 0)

    def test_safe_regex_scope_and_syntax_rejection(self):
        item = self.saved(payload(kind='regex', value=r'^api[0-9]+\.example\.com$', scope_domain='EXAMPLE.COM.'))
        self.assertEqual(item['scope_domain'], 'example.com')
        for domain, expected in [('api42.example.com', 'local_direct_candidate'),
                                 ('evil-api42.example.com', 'no_custom_match'),
                                 ('api42.other.com', 'no_custom_match')]:
            value = self.send('post', {'domain': domain}, ROOT + '/preview').json()['data']
            self.assertEqual(value['result'], expected)
        for value in (r'^(a+)+\.example\.com$', r'^a.*b.*\.example\.com$',
                      r'^api[0-9]+\.other\.com$', r'^api([0-9]+)\.example\.com$', r'^api[\.example\.com$'):
            self.assert_error(self.send('post', payload(kind='regex', value=value, scope_domain='example.com')),
                              422, 'invalid_rule')
        self.assertEqual(ClientDirectRule.objects.count(), 1)

    def test_regex_overlap_is_warning_and_domain_preview_checks_actual_match(self):
        self.saved(payload(kind='regex', value=r'^api[0-9]+\.example\.com$', scope_domain='example.com'))
        response = self.send('post', payload(kind='exact', value='www.example.com', action='proxy'))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['data']['conflicts'][0]['relation'], 'regex_scope_overlap')
        value = self.send('post', {'domain': 'www.example.com'}, ROOT + '/preview').json()['data']
        self.assertEqual(value['result'], 'proxy_candidate')
        self.assertFalse(next(r for r in value['related'] if r['kind'] == 'regex')['matched'])

    def test_patch_toggle_and_edit_advance_revision_without_recreating(self):
        item = self.saved()
        first = self.send('patch', {'enabled': False, 'revision': item['revision'], 'idempotency_key': str(uuid.uuid4())},
                          ROOT + '/' + str(item['id']))
        self.assertEqual(first.status_code, 200, first.content)
        updated = first.json()['data']['item']
        self.assertEqual((updated['enabled'], updated['revision']), (False, 2))
        edited = self.send('patch', {'value': 'another.example.com', 'revision': 2, 'idempotency_key': str(uuid.uuid4())},
                           ROOT + '/' + str(item['id']))
        self.assertEqual(edited.status_code, 200, edited.content)
        self.assertEqual(edited.json()['data']['item']['revision'], 3)
        self.assertEqual(ClientDirectRule.objects.count(), 1)

    def test_stale_patch_and_delete_never_overwrite_newer_rule(self):
        item = self.saved()
        self.send('patch', {'value': 'new.example.com', 'revision': 1, 'idempotency_key': str(uuid.uuid4())},
                  ROOT + '/' + str(item['id']))
        for method, fields in [('patch', {'value': 'lost.example.com'}), ('delete', {'confirm': True})]:
            response = self.send(method, {'revision': 1, 'idempotency_key': str(uuid.uuid4()), **fields},
                                 ROOT + '/' + str(item['id']))
            self.assert_error(response, 409, 'revision_conflict')
        row = ClientDirectRule.objects.get()
        self.assertEqual((row.value, row.revision), ('new.example.com', 2))

    def test_delete_confirmation_and_absent_object(self):
        item = self.saved()
        bad = self.send('delete', {'revision': 1, 'idempotency_key': str(uuid.uuid4()), 'confirm': False},
                        ROOT + '/' + str(item['id']))
        self.assert_error(bad, 422, 'invalid_fields')
        good = self.send('delete', {'revision': 1, 'idempotency_key': str(uuid.uuid4()), 'confirm': True},
                         ROOT + '/' + str(item['id']))
        self.assertEqual(good.status_code, 200, good.content)
        self.assertIsNone(good.json()['data']['item'])
        self.assertEqual(good.json()['data']['deleted_id'], item['id'])
        self.assertFalse(ClientDirectRule.objects.exists())
        missing = self.send('patch', {'revision': 1, 'enabled': False, 'idempotency_key': str(uuid.uuid4())}, ROOT + '/99999')
        self.assert_error(missing, 404, 'not_found')

    def test_idempotent_create_patch_and_delete_survive_changed_state(self):
        create = payload()
        item = self.saved(create)
        replay = self.send('post', create)
        self.assertEqual(replay.status_code, 201)
        self.assertTrue(replay.json()['data']['replayed'])
        edit = {'revision': 1, 'enabled': False, 'idempotency_key': str(uuid.uuid4())}
        self.assertEqual(self.send('patch', edit, ROOT + '/' + str(item['id'])).status_code, 200)
        self.assertTrue(self.send('patch', edit, ROOT + '/' + str(item['id'])).json()['data']['replayed'])
        delete = {'revision': 2, 'confirm': True, 'idempotency_key': str(uuid.uuid4())}
        self.assertEqual(self.send('delete', delete, ROOT + '/' + str(item['id'])).status_code, 200)
        self.assertTrue(self.send('delete', delete, ROOT + '/' + str(item['id'])).json()['data']['replayed'])
        self.assertEqual(OperationJob.objects.count(), 3)
        self.assertEqual(AuditEvent.objects.count(), 3)

    def test_idempotency_key_reuse_with_different_payload_is_rejected(self):
        body = payload()
        self.saved(body)
        self.assert_error(self.send('post', body | {'enabled': False}), 409, 'idempotency_conflict')
        self.assertEqual(ClientDirectRule.objects.get().revision, 1)

    def test_replay_rechecks_staff_and_live_gate(self):
        body = payload()
        self.saved(body)
        self.admin.is_staff = False
        self.admin.save(update_fields=['is_staff'])
        self.assert_error(self.send('post', body), 403, 'permission_denied')
        self.admin.is_staff = True
        self.admin.save(update_fields=['is_staff'])
        with override_settings(PANEL_LIVE=True):
            self.assert_error(self.send('post', body), 403, 'candidate_only')

    def test_live_environment_is_read_only_including_edit_and_delete(self):
        item = self.saved()
        with override_settings(PANEL_LIVE=True):
            self.assertTrue(self.client.get(ROOT).json()['data']['read_only'])
            for method, body, path in [('post', payload(), ROOT),
                                      ('patch', {'enabled': False, 'revision': 1, 'idempotency_key': str(uuid.uuid4())}, ROOT + '/' + str(item['id'])),
                                      ('delete', {'confirm': True, 'revision': 1, 'idempotency_key': str(uuid.uuid4())}, ROOT + '/' + str(item['id']))]:
                self.assert_error(self.send(method, body, path), 403, 'candidate_only')
        self.assertEqual(ClientDirectRule.objects.get().revision, 1)
        self.assertEqual(OperationJob.objects.count(), 1)

    def test_unverified_database_mode_refuses_write(self):
        options = connection.settings_dict['OPTIONS']
        with patch.dict(options, {'transaction_mode': 'DEFERRED'}):
            self.assert_error(self.send('post', payload()), 409, 'unsupported_storage')
            self.assertTrue(self.client.get(ROOT).json()['data']['read_only'])
        self.assertFalse(ClientDirectRule.objects.exists())

    def test_unknown_fields_types_and_unsupported_actions_are_rejected(self):
        for change in ({'server_id': 1}, {'enabled': 'false'}, {'enabled': 0}, {'kind': []},
                       {'action': {}}, {'action': 'direct'}, {'action': 'server_direct'}, {'action': 'block'},
                       {'value': 12}, {'scope_domain': None}, {'idempotency_key': 'bad'},
                       {'scope_domain': 'example.com'}):
            error = self.assert_error(self.send('post', payload(**change)), 422, 'invalid_fields')
            self.assertTrue(error['field_errors'])
        self.assert_error(self.send('post', {'action': 'proxy'}), 422, 'invalid_fields')
        for revision in (True, '1', 0, -1, 2_147_483_648):
            self.assert_error(self.send('patch', {'revision': revision, 'enabled': False, 'idempotency_key': str(uuid.uuid4())}, ROOT + '/1'),
                              422, 'invalid_fields')
        self.assertFalse(ClientDirectRule.objects.exists())

    def test_invalid_json_duplicate_keys_nan_size_and_non_json_rejected(self):
        for raw in ('{', '[]', '{"action":"proxy","action":"client_direct"}', '{"enabled":NaN}'):
            response = self.client.post(ROOT, raw, content_type='application/json')
            self.assertIn(response.status_code, (422,))
            self.assertIsNotNone(response.json()['error'])
        self.assert_error(self.client.post(ROOT, {'action': 'proxy'}), 415, 'unsupported_media_type')
        response = self.send('post', payload(value='a' * 9000))
        self.assert_error(response, 422, 'invalid_fields')

    def test_invalid_existing_rule_disables_final_preview_action_without_exposing_bad_value(self):
        self.saved()
        bad = ClientDirectRule.objects.create(creator=self.admin, kind='regex', value='private-invalid-token',
                                              scope_domain='example.com', outbound='proxy')
        listed = self.client.get(ROOT).json()['data']
        row = next(r for r in listed['items'] if r['id'] == bad.pk)
        self.assertIsNone(row['value'])
        value = self.send('post', {'domain': 'school.example.com'}, ROOT + '/preview').json()['data']
        self.assertEqual(value['result'], 'invalid_candidate')
        self.assertIsNone(value['final_action'])
        self.assertIsNone(value['matched_id'])
        self.assertEqual(value['invalid_rule_ids'], [bad.pk])
        self.assertNotIn('private-invalid-token', json.dumps(value))

    def test_invalid_preview_domain_returns_field_error(self):
        for domain in ('https://example.com', '192.168.1.1', 'localhost', {}, 3):
            error = self.assert_error(self.send('post', {'domain': domain}, ROOT + '/preview'), 422, 'invalid_domain')
            self.assertTrue(error['field_errors']['domain'])

    def test_legacy_ip_shaped_domain_is_redacted_and_cannot_authorize_preview(self):
        row = ClientDirectRule.objects.create(creator=self.admin, kind='exact', value='169.254.169.254')
        data = self.client.get(ROOT).json()['data']
        self.assertEqual(data['items'][0]['validation'], 'invalid')
        self.assertIsNone(data['items'][0]['value'])
        self.assertEqual(data['sources'][0]['state'], 'invalid')
        preview = self.send('post', {'domain': 'example.com'}, ROOT + '/preview').json()['data']
        self.assertEqual(preview['invalid_rule_ids'], [row.pk])
        self.assertIsNone(preview['final_action'])

    def test_candidate_snapshot_changes_on_toggle_and_no_policy_file_is_written(self):
        # Windows 的 mkdtemp 受限 ACL 不适用于本沙箱；普通夹具目录继承测试根权限。
        directory = settings.DATA_ROOT / ('rule-file-fixture-' + uuid.uuid4().hex)
        directory.mkdir()
        policy = directory / 'policy.json'
        status = directory / 'status.json'
        try:
            policy.write_text('保持活动策略', encoding='utf-8')
            status.write_text('保持发布状态', encoding='utf-8')
            with override_settings(ROUTING_POLICY_PATH=policy, ROUTING_STATUS_PATH=status), \
                    patch('portal.views.write_routing_policy', side_effect=AssertionError('不得调用发布器')):
                before = self.client.get(ROOT).json()['data']['candidate_revision']
                item = self.saved()
                created = self.client.get(ROOT).json()['data']['candidate_revision']
                response = self.send('patch', {'enabled': False, 'revision': 1, 'idempotency_key': str(uuid.uuid4())},
                                     ROOT + '/' + str(item['id']))
                disabled = response.json()['data']['candidate_revision']
                self.assertNotEqual(before, created)
                self.assertNotEqual(created, disabled)
                self.send('delete', {'revision': 2, 'confirm': True, 'idempotency_key': str(uuid.uuid4())},
                          ROOT + '/' + str(item['id']))
            self.assertEqual(policy.read_text(encoding='utf-8'), '保持活动策略')
            self.assertEqual(status.read_text(encoding='utf-8'), '保持发布状态')
        finally:
            policy.unlink(missing_ok=True)
            status.unlink(missing_ok=True)
            directory.rmdir()


@override_settings(**SETTINGS)
class CandidateRuleConcurrencyTests(TransactionTestCase):
    """SQLite 的实际并发反例；忙碌可重试，但不能重复建行或丢更新。"""
    def setUp(self):
        self.admin = User.objects.create_user('并发甲管理员', is_staff=True)
        self.other = User.objects.create_user('并发乙管理员', is_staff=True)
        self.clients = [Client(), Client()]
        for client, user in zip(self.clients, (self.admin, self.other)):
            client.force_login(user)

    def concurrent(self, requests):
        barrier = Barrier(2)

        def call(index):
            close_old_connections()
            method, url, data = requests[index]
            barrier.wait(timeout=10)
            try:
                response = getattr(self.clients[index], method)(url, json.dumps(data), content_type='application/json')
                return response.status_code, response.json()
            finally:
                connections['default'].close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(call, (0, 1)))

    def test_concurrent_normalized_duplicate_creates_exactly_one_rule(self):
        bodies = [payload(value='school.example.com'), payload(value='SCHOOL.EXAMPLE.COM.')]
        results = self.concurrent([('post', ROOT, body) for body in bodies])
        self.assertEqual(sum(status == 201 for status, _ in results), 1, results)
        self.assertEqual(ClientDirectRule.objects.count(), 1)
        self.assertEqual(OperationJob.objects.count(), 1)
        for index, (status, data) in enumerate(results):
            if status == 409 and data['error']['code'] == 'database_busy':
                response = self.clients[index].post(ROOT, json.dumps(bodies[index]), content_type='application/json')
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['error']['code'], 'duplicate_rule')

    def test_concurrent_parent_child_creates_cannot_bypass_coverage_check(self):
        bodies = [payload(value='example.com'), payload(value='api.example.com')]
        results = self.concurrent([('post', ROOT, body) for body in bodies])
        self.assertEqual(sum(status == 201 for status, _ in results), 1, results)
        self.assertEqual(ClientDirectRule.objects.count(), 1)
        for index, (status, data) in enumerate(results):
            if status == 409 and data['error']['code'] == 'database_busy':
                response = self.clients[index].post(ROOT, json.dumps(bodies[index]), content_type='application/json')
                self.assertEqual(response.status_code, 422)

    def test_concurrent_same_revision_edit_preserves_one_winner(self):
        rule = ClientDirectRule.objects.create(creator=self.admin, kind='exact', value='initial.example.com')
        url = ROOT + '/' + str(rule.pk)
        bodies = [{'revision': 1, 'value': value, 'idempotency_key': str(uuid.uuid4())}
                  for value in ('first.example.com', 'second.example.com')]
        results = self.concurrent([('patch', url, body) for body in bodies])
        self.assertEqual(sum(status == 200 for status, _ in results), 1, results)
        rule.refresh_from_db()
        winner = next(i for i, (status, _) in enumerate(results) if status == 200)
        self.assertEqual((rule.value, rule.revision), (bodies[winner]['value'], 2))
        retry = self.clients[1 - winner].patch(url, json.dumps(bodies[1 - winner]), content_type='application/json')
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(retry.json()['error']['code'], 'revision_conflict')

    def test_same_actor_concurrent_idempotent_create_has_one_receipt(self):
        self.clients[1].force_login(self.admin)
        body = payload()
        results = self.concurrent([('post', ROOT, body), ('post', ROOT, body)])
        self.assertTrue(any(status == 201 for status, _ in results), results)
        self.assertTrue(all(status in (201, 409) for status, _ in results), results)
        for client in self.clients:
            response = client.post(ROOT, json.dumps(body), content_type='application/json')
            self.assertEqual(response.status_code, 201)
            self.assertTrue(response.json()['data']['replayed'])
        self.assertEqual(ClientDirectRule.objects.count(), 1)
        self.assertEqual(OperationJob.objects.count(), 1)
