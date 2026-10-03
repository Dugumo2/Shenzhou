"""文件来源预览、不可变版本和签名确认的隔离 API 场景。"""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from threading import Barrier
from unittest.mock import patch
import uuid

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import path

from . import api, rule_source_api
from .models import AuditEvent, ClientDirectRule, DeploymentJob, OperationJob, RuleSource, RuleSourceVersion
from .rule_sources import MAX_BYTES, PREVIEW_SECONDS, RECEIPT_ACTION


ROOT = '/api/v1/admin/rule-sources'
urlpatterns = [
    path('api/v1/session', api.session),
    path('api/v1/admin/rule-sources', rule_source_api.sources),
    path('api/v1/admin/rule-sources/preview', rule_source_api.preview),
    path('api/v1/admin/rule-sources/commit', rule_source_api.commit),
    path('api/v1/admin/rule-sources/<uuid:source_id>', rule_source_api.source_detail),
]
SETTINGS = {'ROOT_URLCONF': 'portal.test_rule_sources', 'PANEL_LIVE': False,
            'CSRF_FAILURE_VIEW': 'portal.api.csrf_failure',
            'PASSWORD_HASHERS': ['django.contrib.auth.hashers.MD5PasswordHasher']}


def rule(value='school.example.com', **changes):
    return {'action': 'client_direct', 'kind': 'suffix', 'value': value, **changes}


def document(*rows):
    return {'schema_version': 1, 'rules': list(rows or (rule(),))}


def payload(**changes):
    return {'name': '校园来源', 'source_id': None, 'expected_revision': 0,
            'document': json.dumps(document(), ensure_ascii=False), **changes}


@override_settings(**SETTINGS)
class RuleSourceAPITests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('来源管理员', is_staff=True)
        self.other = User.objects.create_user('另一来源管理员', is_staff=True)
        self.user = User.objects.create_user('来源普通用户')
        self.client.force_login(self.admin)

    def send(self, body, suffix='/preview', client=None):
        return (client or self.client).post(ROOT + suffix, json.dumps(body, ensure_ascii=True),
                                           content_type='application/json')

    def preview(self, body=None):
        response = self.send(body or payload())
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def commit_body(self, body=None):
        body = body or payload()
        value = self.preview(body)
        return {**body, 'preview_token': value['preview_token'], 'idempotency_key': str(uuid.uuid4())}

    def saved(self, body=None):
        response = self.send(self.commit_body(body), '/commit')
        self.assertIn(response.status_code, (200, 201), response.content)
        return response.json()['data']['item']

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertIsNone(response.json()['data'])
        self.assertEqual(response.json()['error']['code'], code)
        return response.json()['error']

    def update_payload(self, item, **changes):
        return payload(source_id=item['source_id'], expected_revision=item['revision'], name=item['name'], **changes)

    def test_preview_and_empty_list_have_no_database_side_effects(self):
        data = self.preview()
        self.assertEqual(data['diff']['counts'], {'added': 1, 'removed': 0, 'modified': 0})
        self.assertEqual(data['count'], 1)
        self.assertEqual(len(data['sha256']), 64)
        self.assertTrue(data['can_commit'])
        self.assertEqual(data['expires_in'], 600)
        self.assertEqual(data['state'], 'candidate_unbound')
        self.assertEqual(self.client.get(ROOT).json()['data']['items'], [])
        for model in (RuleSource, RuleSourceVersion, ClientDirectRule, OperationJob, AuditEvent, DeploymentJob):
            self.assertEqual(model.objects.count(), 0)

    def test_file_text_normalizes_without_modifying_custom_rules_or_publishing(self):
        custom = ClientDirectRule.objects.create(creator=self.admin, kind='exact', value='keep.example.com')
        item = self.saved(payload(document=document(rule('  SCHOOL.Example.COM.  '),
                                                  rule('例子.中国', kind='exact', action='proxy'))))
        self.assertEqual(item['revision'], 1)
        self.assertEqual(item['state'], 'candidate_unbound')
        version = RuleSourceVersion.objects.get()
        self.assertEqual(version.document['rules'][0]['value'], 'school.example.com')
        self.assertEqual(version.document['rules'][1]['value'], 'xn--fsqu00a.xn--fiqs8s')
        self.assertEqual(version.document['rules'][0]['scope_domain'], '')
        self.assertEqual(ClientDirectRule.objects.get().pk, custom.pk)
        self.assertEqual(ClientDirectRule.objects.get().revision, 1)
        receipt = OperationJob.objects.get()
        self.assertEqual(receipt.action, RECEIPT_ACTION)
        self.assertEqual(receipt.state, 'done')
        self.assertIsNotNone(receipt.finished_at)
        self.assertFalse(DeploymentJob.objects.exists())
        self.assertEqual(AuditEvent.objects.get().result, 'CANDIDATE_ONLY')

    def test_schema_and_extra_fields_are_rejected_without_silent_conversion(self):
        invalid = [[], {}, {'schema_version': True, 'rules': []}, {'schema_version': 2, 'rules': []},
                   {'schema_version': 1, 'rules': {}, 'dns': {}},
                   {'schema_version': 1, 'rules': [], 'url': 'https://example.com'},
                   {'version': 3, 'rules': [{'domain': ['example.com']}]},
                   document({'action': 'proxy', 'kind': 'exact'}), document({**rule(), 'port': 443})]
        for value in invalid:
            self.assert_error(self.send(payload(document=value)), 422, 'invalid_document')
        self.assertEqual(RuleSource.objects.count(), 0)

    def test_duplicate_json_keys_nan_invalid_utf8_and_depth_are_rejected(self):
        for value in ('{', '{"schema_version":1,"schema_version":1,"rules":[]}',
                      '{"schema_version":1,"rules":[{"action":"proxy","action":"client_direct"}]}',
                      '{"schema_version":1,"rules":NaN}'):
            self.assert_error(self.send(payload(document=value)), 422, 'invalid_json')
        deep = self.send(payload(document='[' * 1100 + ']' * 1100))
        self.assertEqual(deep.status_code, 422)
        self.assertIn(deep.json()['error']['code'], ('invalid_json', 'invalid_document'))
        raw = json.dumps(payload()).encode().replace(b'"name"', b'"name"\xff')
        self.assert_error(self.client.post(ROOT + '/preview', raw, content_type='application/json'), 422, 'invalid_json')
        self.assert_error(self.client.post(ROOT + '/preview', json.dumps(payload()).encode('utf-16'),
                                          content_type='application/json'), 422, 'invalid_json')
        self.assert_error(self.client.post(ROOT + '/preview', '{"name":"a","name":"b"}',
                                          content_type='application/json'), 422, 'invalid_json')
        self.assert_error(self.send(payload(document='\ud800')), 422, 'invalid_json')

    def test_bad_action_types_regex_scope_ip_and_protected_direct_are_rejected(self):
        rows = [rule(action='server_direct'), rule(action='block'), rule(action=['proxy']),
                rule(kind=[]), rule(enabled=1), rule(value=None), rule(scope_domain='example.com'),
                rule('192.168.1.1'), rule('169.254.169.254'), rule('2001:db8::1'),
                rule('https://example.com'), rule('localhost'), rule('openai.com'),
                rule('chatgpt.com', enabled=False), rule('com'),
                rule(r'^.*\.google\.com$', kind='regex', scope_domain='google.com'),
                rule(r'^(a+)+\.school\.example\.com$', kind='regex', scope_domain='school.example.com')]
        for row in rows:
            self.assert_error(self.send(payload(document=document(row))), 422, 'invalid_rule')
        valid = self.preview(payload(document=document(rule('openai.com', action='proxy'),
            rule(r'^[a-z]+\.school\.example\.com$', kind='regex', scope_domain='school.example.com'))))
        self.assertEqual(valid['count'], 2)

    def test_file_size_and_item_limits_and_empty_replacement(self):
        self.assert_error(self.send(payload(document=' ' * (MAX_BYTES + 1))), 422, 'document_too_large')
        self.assert_error(self.send(payload(document=document(*[rule(f'a{i}.example.com') for i in range(501)]))),
                          422, 'rule_limit')
        self.assertEqual(self.preview(payload(document=document(*[rule(f'a{i}.example.com')
                             for i in range(500)])))['count'], 500)
        item = self.saved()
        update = self.update_payload(item, document={'schema_version': 1, 'rules': []})
        self.assertEqual(self.preview(update)['diff']['counts']['removed'], 1)
        self.assertEqual(self.saved(update)['count'], 0)
        self.assertEqual(RuleSourceVersion.objects.count(), 2)

    def test_normalized_duplicate_rejected_even_opposite_action_or_disabled(self):
        for second in (rule('SCHOOL.EXAMPLE.COM.'), rule(action='proxy'), rule(enabled=False)):
            error = self.assert_error(self.send(payload(document=document(rule(), second))), 422, 'duplicate_rule')
            self.assertEqual(error['conflicts'][0]['rule_index'], 0)
            self.assertEqual(error['conflicts'][0]['candidate_rule_index'], 1)
            self.assertEqual(error['conflicts'][0]['severity'], 'blocking')

    def test_parent_same_opposite_and_regex_overlap_are_explained_without_final_action(self):
        value = self.preview(payload(document=document(rule('example.com'),
            rule('a.example.com', action='proxy', kind='exact'),
            rule('b.example.com', kind='exact'),
            rule(r'^[a-z]+\.example\.com$', kind='regex', scope_domain='example.com'))))
        self.assertIn('child_covered_by_parent', {row['relation'] for row in value['conflicts']})
        self.assertIn('regex_scope_overlap', {row['relation'] for row in value['conflicts']})
        self.assertTrue(any(row['same_action'] for row in value['conflicts']))
        self.assertTrue(any(not row['same_action'] for row in value['conflicts']))
        self.assertTrue(all(row['severity'] == 'warning' for row in value['conflicts']))
        self.assertNotIn('final_action', value)
        self.assertTrue(value['can_commit'])

    def test_exact_parent_does_not_claim_to_cover_exact_child(self):
        value = self.preview(payload(document=document(rule('example.com', kind='exact'),
                                                       rule('a.example.com', kind='exact'))))
        self.assertEqual(value['conflicts'], [])

    def test_cross_custom_and_other_source_overlap_does_not_change_either(self):
        custom = ClientDirectRule.objects.create(creator=self.admin, kind='suffix', value='example.com', outbound='proxy')
        source = self.saved(payload(name='旧来源', document=document(rule('shared.example.com', action='proxy'))))
        value = self.preview(payload(document=document(rule('shared.example.com'))))
        self.assertEqual({row['origin'] for row in value['conflicts']}, {'custom', 'source'})
        other = next(row for row in value['conflicts'] if row['origin'] == 'source')
        self.assertEqual(other['source_id'], source['source_id'])
        self.assertEqual(other['source_name'], '旧来源')
        self.assertEqual(other['rule_index'], 0)
        self.assertEqual(other['relation'], 'duplicate')
        self.saved(payload(document=document(rule('shared.example.com'))))
        self.assertEqual(ClientDirectRule.objects.get().pk, custom.pk)
        self.assertEqual(RuleSource.objects.get(public_id=source['source_id']).revision, 1)

    def test_conflict_limit_is_explicit(self):
        rows = [rule('example.com')] + [rule(f'a{i}.example.com', kind='exact') for i in range(150)]
        value = self.preview(payload(document=document(*rows)))
        self.assertEqual(len(value['conflicts']), 100)
        self.assertTrue(value['conflicts_truncated'])

    def test_invalid_existing_custom_is_redacted_and_explained(self):
        bad = ClientDirectRule.objects.create(creator=self.admin, kind='regex', value='private-invalid-value')
        value = self.preview()
        self.assertEqual(value['conflicts'][0]['rule_id'], bad.pk)
        self.assertEqual(value['conflicts'][0]['relation'], 'invalid_existing_rule')
        self.assertNotIn('private-invalid-value', json.dumps(value))

    def test_diff_added_removed_modified_and_previous_version_retained(self):
        first = self.saved(payload(document=document(rule('keep.example.com'), rule('remove.example.com'))))
        old = copy.deepcopy(RuleSourceVersion.objects.get().document)
        body = self.update_payload(first, document=document(rule('keep.example.com', action='proxy', enabled=False),
                                                          rule('new.example.com')))
        value = self.preview(body)
        self.assertEqual(value['diff']['counts'], {'added': 1, 'removed': 1, 'modified': 1})
        self.assertEqual(value['diff']['modified'][0]['before']['action'], 'client_direct')
        self.assertEqual(value['diff']['modified'][0]['after']['action'], 'proxy')
        second = self.saved(body)
        self.assertEqual(second['source_id'], first['source_id'])
        self.assertEqual(second['revision'], 2)
        self.assertNotEqual(first['sha256'], second['sha256'])
        self.assertEqual(RuleSourceVersion.objects.get(revision=1).document, old)
        self.assertEqual(RuleSourceVersion.objects.count(), 2)

    def test_reorder_is_visible_and_version_hash_changes(self):
        first = self.saved(payload(document=document(rule('a.example.com'), rule('b.example.com'))))
        body = self.update_payload(first, document=document(rule('b.example.com'), rule('a.example.com')))
        value = self.preview(body)
        self.assertTrue(value['diff']['order_changed'])
        self.assertEqual(value['diff']['counts'], {'added': 0, 'removed': 0, 'modified': 0})
        self.assertNotEqual(first['sha256'], value['sha256'])

    def test_bad_update_keeps_previous_source_version(self):
        item = self.saved()
        self.assert_error(self.send(self.update_payload(item, document='bad json')), 422, 'invalid_json')
        self.assertEqual(RuleSource.objects.get().revision, 1)
        self.assertEqual(RuleSourceVersion.objects.count(), 1)

    def test_source_list_search_pagination_and_historical_entry_lookup(self):
        first = self.saved()
        self.saved(payload(name='其它来源', document=document(rule('other.example.net'))))
        result = self.client.get(ROOT, {'q': '校园', 'page_size': 1}).json()['data']
        self.assertEqual(result['pagination']['total'], 1)
        self.assertEqual(result['items'][0]['source_id'], first['source_id'])
        self.assertEqual(self.client.get(ROOT, {'q': first['source_id']}).json()['data']['pagination']['total'], 1)
        second = self.saved(self.update_payload(first, document=document(rule('new.example.com'))))
        current = self.client.get(ROOT + '/' + first['source_id']).json()['data']
        self.assertEqual(current['item']['revision'], 2)
        self.assertEqual(current['version']['sha256'], second['sha256'])
        self.assertEqual(len(current['history']), 2)
        old = self.client.get(ROOT + '/' + first['source_id'], {'version': 1, 'q': 'school'}).json()['data']
        self.assertEqual(old['item']['revision'], 2)
        self.assertEqual(old['version']['sha256'], first['sha256'])
        self.assertEqual(old['items'][0]['value'], 'school.example.com')
        self.assertFalse(old['history_truncated'])
        self.assert_error(self.client.get(ROOT + '/' + first['source_id'], {'version': 99}), 404, 'not_found')
        self.assert_error(self.client.get(ROOT + '/' + first['source_id'], {'version': 'bad'}), 422, 'invalid_fields')
        self.assert_error(self.client.get(ROOT, {'page': 0}), 422, 'invalid_pagination')
        self.assert_error(self.client.get(ROOT, {'page': 99}), 404, 'page_not_found')
        self.assert_error(self.client.get(ROOT, {'q': 'a' * 254}), 422, 'invalid_filter')

    def test_history_limit_is_explicit_and_older_revision_still_queryable(self):
        item = self.saved()
        source = RuleSource.objects.get()
        version = RuleSourceVersion.objects.get()
        RuleSourceVersion.objects.bulk_create([RuleSourceVersion(source=source, revision=i,
            document=version.document, sha256=version.sha256, count=version.count, creator=self.admin)
            for i in range(2, 103)])
        RuleSource.objects.filter(pk=source.pk).update(revision=102)
        data = self.client.get(ROOT + '/' + item['source_id']).json()['data']
        self.assertEqual(len(data['history']), 100)
        self.assertTrue(data['history_truncated'])
        old = self.client.get(ROOT + '/' + item['source_id'], {'version': 1}).json()['data']
        self.assertEqual(old['version']['revision'], 1)

    def test_model_prevents_rewriting_or_deleting_saved_version(self):
        self.saved()
        version = RuleSourceVersion.objects.get()
        version.count = 9
        with self.assertRaises(ValidationError):
            version.save()
        with self.assertRaises(ValidationError):
            version.delete()
        replacement = RuleSourceVersion(pk=version.pk, source=version.source, revision=1,
            document=version.document, sha256=version.sha256, count=9, creator=self.admin)
        with self.assertRaises(ValidationError):
            replacement.save()
        self.assertEqual(RuleSourceVersion.objects.get().count, 1)

    def test_corrupt_hash_count_or_noncanonical_document_is_not_reported_as_valid(self):
        item = self.saved()
        version = RuleSourceVersion.objects.get()
        baseline = {'sha256': version.sha256, 'count': version.count, 'document': version.document}
        changed_document = copy.deepcopy(version.document)
        changed_document['rules'][0]['value'] = 'SCHOOL.EXAMPLE.COM.'
        for changes in ({'sha256': 'f' * 64}, {'count': 0}, {'document': changed_document}):
            RuleSourceVersion.objects.filter(pk=version.pk).update(**changes)
            self.assert_error(self.client.get(ROOT), 409, 'invalid_existing_source')
            self.assert_error(self.client.get(ROOT + '/' + item['source_id']), 409, 'invalid_existing_source')
            self.assert_error(self.send(payload()), 409, 'invalid_existing_source')
            RuleSourceVersion.objects.filter(pk=version.pk).update(**baseline)
        self.assertEqual(RuleSource.objects.get().revision, 1)
        self.assertEqual(RuleSourceVersion.objects.count(), 1)

    def test_corrupt_selected_history_is_rejected_and_unchanged_previous_is_retained(self):
        first = self.saved()
        self.saved(self.update_payload(first, document=document(rule('new.example.com'))))
        RuleSourceVersion.objects.filter(revision=1).update(count=0)
        self.assert_error(self.client.get(ROOT + '/' + first['source_id'], {'version': 1}),
                          409, 'invalid_existing_source')
        self.assert_error(self.client.get(ROOT + '/' + first['source_id']), 409, 'invalid_existing_source')
        self.assertEqual(RuleSource.objects.get().revision, 2)

    def test_corrupted_version_after_preview_is_rejected_before_save(self):
        first = self.saved()
        body = self.commit_body(self.update_payload(first))
        RuleSourceVersion.objects.filter(revision=1).update(sha256='0' * 64)
        self.assert_error(self.send(body, '/commit'), 409, 'invalid_existing_source')
        self.assertEqual(RuleSourceVersion.objects.count(), 1)

    def test_missing_current_version_is_rejected_for_query_and_preview(self):
        first = self.saved()
        RuleSource.objects.filter(public_id=first['source_id']).update(revision=2)
        self.assert_error(self.client.get(ROOT), 409, 'invalid_existing_source')
        self.assert_error(self.send(payload()), 409, 'invalid_existing_source')

    def test_surrogate_name_token_and_file_fields_return_json_rejection(self):
        for changes in ({'name': '\ud800'}, {'document': document(rule('\ud800.example.com'))}):
            self.assert_error(self.send(payload(**changes)), 422, 'invalid_json')
        body = self.commit_body()
        self.assert_error(self.send({**body, 'preview_token': '\ud800'}, '/commit'), 422, 'invalid_json')
        self.assertEqual(RuleSource.objects.count(), 0)

    def test_missing_source_and_bad_metadata_never_create_rows(self):
        for body in (payload(name=' '), payload(name='a' * 101), payload(name='a\x00b'),
                     payload(source_id='bad'), payload(source_id=[], expected_revision=1),
                     payload(expected_revision=True), payload(expected_revision=1),
                     payload(source_id=str(uuid.uuid4()), expected_revision=0)):
            self.assert_error(self.send(body), 422, 'invalid_fields')
        self.assert_error(self.send(payload(source_id=str(uuid.uuid4()), expected_revision=1)), 404, 'not_found')
        self.assert_error(self.send({**payload(), 'url': 'https://example.com'}), 422, 'invalid_fields')
        self.assertEqual(RuleSource.objects.count(), 0)

    def test_modified_input_name_and_token_actor_or_signature_are_rejected(self):
        body = self.commit_body()
        for change in ({'name': '替换名称'}, {'document': document(rule('changed.example.com'))},
                       {'preview_token': body['preview_token'][:-1] + '!'}):
            self.assert_error(self.send({**body, **change}, '/commit'), 409, 'invalid_preview')
        other_client = Client()
        other_client.force_login(self.other)
        self.assert_error(self.send(body, '/commit', other_client), 409, 'invalid_preview')
        self.assertEqual(RuleSource.objects.count(), 0)

    def test_token_cannot_be_used_for_another_source_with_same_contents(self):
        first = self.saved()
        second = self.saved(payload(name='第二来源'))
        body = self.commit_body(self.update_payload(first))
        self.assert_error(self.send({**body, 'source_id': second['source_id']}, '/commit'), 409, 'invalid_preview')
        self.assertTrue(all(value == 1 for value in RuleSource.objects.values_list('revision', flat=True)))

    def test_changes_to_custom_or_other_sources_invalidate_preview(self):
        body = self.commit_body()
        custom = ClientDirectRule.objects.create(creator=self.admin, kind='exact', value='keep.example.com')
        self.assert_error(self.send(body, '/commit'), 409, 'stale_preview')
        body = self.commit_body()
        ClientDirectRule.objects.filter(pk=custom.pk).update(enabled=False)
        self.assert_error(self.send(body, '/commit'), 409, 'stale_preview')
        body = self.commit_body()
        self.saved(payload(name='其它来源'))
        self.assert_error(self.send(body, '/commit'), 409, 'stale_preview')
        self.assertEqual(RuleSource.objects.count(), 1)

    def test_same_source_revision_race_rejects_old_confirmation(self):
        item = self.saved()
        body = self.commit_body(self.update_payload(item))
        self.saved(self.update_payload(item, document=document(rule('new.example.com'))))
        self.assert_error(self.send(body, '/commit'), 409, 'revision_conflict')
        self.assertEqual(RuleSourceVersion.objects.count(), 2)

    def test_idempotent_success_replay_survives_expiry_and_rejects_changed_input(self):
        import time
        body = self.commit_body()
        first = self.send(body, '/commit')
        self.assertEqual(first.status_code, 201)
        with patch('django.core.signing.time.time', return_value=time.time() + PREVIEW_SECONDS + 1):
            replay = self.send(body, '/commit')
        self.assertEqual(replay.status_code, 201)
        self.assertTrue(replay.json()['data']['replayed'])
        self.assertEqual(first.json()['data']['item'], replay.json()['data']['item'])
        self.assert_error(self.send({**body, 'name': '其它输入'}, '/commit'), 409, 'idempotency_conflict')
        self.assertEqual(RuleSource.objects.count(), 1)
        self.assertEqual(RuleSourceVersion.objects.count(), 1)
        self.assertEqual(OperationJob.objects.count(), 1)

    def test_success_token_with_new_idempotency_key_is_stale(self):
        body = self.commit_body()
        self.send(body, '/commit')
        self.assert_error(self.send({**body, 'idempotency_key': str(uuid.uuid4())}, '/commit'), 409, 'stale_preview')

    def test_expired_preview_is_rejected_before_writes(self):
        import time
        issued_at = time.time()
        with patch('django.core.signing.time.time', return_value=issued_at):
            body = self.commit_body()
        with patch('django.core.signing.time.time', return_value=issued_at + PREVIEW_SECONDS + 1):
            self.assert_error(self.send(body, '/commit'), 409, 'preview_expired')
        self.assertEqual(RuleSource.objects.count(), 0)

    def test_failed_receipt_write_rolls_back_source_and_version(self):
        from django.db import OperationalError
        body = self.commit_body()
        with patch('portal.rule_sources.OperationJob.objects.create', side_effect=OperationalError('忙碌')):
            self.assert_error(self.send(body, '/commit'), 409, 'database_busy')
        self.assertEqual(RuleSource.objects.count(), 0)
        self.assertEqual(RuleSourceVersion.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_authorization_covers_all_operations_and_replay(self):
        item = self.saved()
        body = self.commit_body()
        self.send(body, '/commit')
        for user, status, code in ((None, 401, 'authentication_required'),
                                   (self.user, 403, 'permission_denied')):
            client = Client()
            if user:
                client.force_login(user)
            for response in (client.get(ROOT), client.get(ROOT + '/' + item['source_id']),
                             self.send(payload(), client=client), self.send(body, '/commit', client)):
                self.assert_error(response, status, code)
        self.admin.is_staff = False
        self.admin.save(update_fields=['is_staff'])
        self.assert_error(self.send(body, '/commit'), 403, 'permission_denied')
        self.admin.is_active = False
        self.admin.save(update_fields=['is_active'])
        self.assert_error(self.send(body, '/commit'), 401, 'authentication_required')

    def test_service_rechecks_actor_before_replay(self):
        from .rule_sources import SourceError, commit_source
        body = self.commit_body()
        self.send(body, '/commit')
        User.objects.filter(pk=self.admin.pk).update(is_staff=False)
        with self.assertRaises(SourceError) as caught:
            commit_source(self.admin, body)
        self.assertEqual(caught.exception.code, 'permission_denied')

    def test_csrf_required_for_preview_commit_and_authenticated_save(self):
        body = self.commit_body()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assert_error(self.send(payload(), client=client), 403, 'csrf_failed')
        self.assert_error(self.send(body, '/commit', client), 403, 'csrf_failed')
        csrf = client.get('/api/v1/session').json()['data']['csrf_token']
        value = client.post(ROOT + '/preview', json.dumps(payload()), content_type='application/json',
                            HTTP_X_CSRFTOKEN=csrf).json()['data']
        response = client.post(ROOT + '/commit', json.dumps({**payload(), 'preview_token': value['preview_token'],
            'idempotency_key': str(uuid.uuid4())}), content_type='application/json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(response.status_code, 201, response.content)

    def test_live_and_unsupported_storage_have_no_usable_preview_or_save(self):
        body = self.commit_body()
        self.send(body, '/commit')
        with override_settings(PANEL_LIVE=True):
            value = self.preview()
            self.assertFalse(value['can_commit'])
            self.assertIsNone(value['preview_token'])
            self.assertTrue(self.client.get(ROOT).json()['data']['read_only'])
            self.assert_error(self.send(body, '/commit'), 403, 'candidate_only')
        with patch.dict(connection.settings_dict['OPTIONS'], {'transaction_mode': 'DEFERRED'}):
            self.assertFalse(self.preview()['can_commit'])
            self.assert_error(self.send(body, '/commit'), 409, 'unsupported_storage')
        self.assertEqual(RuleSource.objects.count(), 1)

    def test_method_and_media_and_confirm_fields_are_explicit(self):
        self.assert_error(self.client.post(ROOT, '{}', content_type='application/json'), 405, 'method_not_allowed')
        self.assert_error(self.client.post(ROOT + '/preview', {}), 415, 'unsupported_media_type')
        self.assert_error(self.send(payload(), '/commit'), 422, 'invalid_fields')
        body = self.commit_body()
        self.assert_error(self.send({**body, 'idempotency_key': 'bad'}, '/commit'), 422, 'invalid_fields')
        self.assert_error(self.send({**body, 'preview_token': None}, '/commit'), 409, 'invalid_preview')

    @override_settings(ROOT_URLCONF='megabox.urls')
    def test_actual_application_routes_integrate_all_four_endpoints(self):
        self.assertEqual(self.client.get(ROOT).status_code, 200)
        body = self.commit_body()
        response = self.send(body, '/commit')
        self.assertEqual(response.status_code, 201, response.content)
        item = response.json()['data']['item']
        detail = self.client.get(ROOT + '/' + item['source_id'])
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()['data']['version']['revision'], 1)


@override_settings(**SETTINGS)
class RuleSourceConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.admin = User.objects.create_user('来源并发管理员', is_staff=True)
        self.clients = [Client(), Client()]
        for client in self.clients:
            client.force_login(self.admin)

    def preview(self, body):
        response = self.clients[0].post(ROOT + '/preview', json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        return {**body, 'preview_token': response.json()['data']['preview_token'],
                'idempotency_key': str(uuid.uuid4())}

    def concurrent(self, bodies):
        barrier = Barrier(2)

        def call(index):
            close_old_connections()
            barrier.wait(timeout=10)
            try:
                response = self.clients[index].post(ROOT + '/commit', json.dumps(bodies[index]),
                                                    content_type='application/json')
                return response.status_code, response.json()
            finally:
                connections['default'].close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(call, (0, 1)))

    def test_concurrent_same_key_creates_one_source_or_returns_retryable_busy(self):
        body = self.preview(payload())
        results = self.concurrent([body, body])
        self.assertTrue(any(status == 201 for status, _ in results), results)
        self.assertTrue(all(status in (201, 409) for status, _ in results), results)
        self.assertEqual(RuleSource.objects.count(), 1)
        self.assertEqual(RuleSourceVersion.objects.count(), 1)
        self.assertEqual(OperationJob.objects.count(), 1)
        replay = self.clients[0].post(ROOT + '/commit', json.dumps(body), content_type='application/json')
        self.assertTrue(replay.json()['data']['replayed'])

    def test_concurrent_revision_updates_preserve_one_previous_and_one_new_version(self):
        initial = self.clients[0].post(ROOT + '/commit', json.dumps(self.preview(payload())),
                                      content_type='application/json').json()['data']['item']
        bodies = [self.preview(payload(source_id=initial['source_id'], expected_revision=1,
            document=document(rule(f'new{i}.example.com')))) for i in range(2)]
        results = self.concurrent(bodies)
        self.assertEqual(sum(status == 200 for status, _ in results), 1, results)
        self.assertEqual(sum(status == 409 for status, _ in results), 1, results)
        self.assertEqual(RuleSource.objects.get().revision, 2)
        self.assertEqual(RuleSourceVersion.objects.count(), 2)
        self.assertEqual(RuleSourceVersion.objects.get(revision=1).document['rules'][0]['value'], 'school.example.com')
