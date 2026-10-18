"""采购接口权限与输入边界；HTTP夹具不冒充真实采集器消费。"""
import json
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import path

from . import resource_plan_api
from .api import session
from .resource_plan_service import ResourcePlanError
from .resource_plan_service import register_resource_plan
from .resource_plan_models import ResourcePlanBinding, ResourcePlanConsumption, ResourcePlanVersion
from .models import Egress, Server
from .test_resource_usage import PLANS


urlpatterns = [
    path('api/v1/session', session),
    path('api/v1/admin/resource-plans/<str:binding_key>', resource_plan_api.resource_plan),
    path('api/v1/admin/resource-plans/<str:binding_key>/preview', resource_plan_api.preview),
    path('api/v1/admin/resource-plans/<str:binding_key>/confirm', resource_plan_api.confirm),
]


@override_settings(ROOT_URLCONF=__name__, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ResourcePlanHttpBoundaryTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user('plan-admin', is_staff=True)
        self.member = get_user_model().objects.create_user('plan-member')
        self.client.force_login(self.admin)
        self.base = '/api/v1/admin/resource-plans/' + 'b' * 64
        self.draft = {'expected_revision': 1, 'source_fingerprint': 'a' * 64,
                      'operation': 'quota', 'changes': {'add_bytes': '1000000000'}}

    def post(self, body, suffix='/preview', **kwargs):
        return self.client.post(self.base + suffix, body if isinstance(body, str) else json.dumps(body),
                                content_type='application/json', **kwargs)

    def test_anonymous_and_member_cannot_access_any_plan_operation(self):
        with patch.object(resource_plan_api, 'resource_plan_state') as read, \
                patch.object(resource_plan_api, 'preview_resource_plan') as preview, \
                patch.object(resource_plan_api, 'confirm_resource_plan') as confirm:
            for actor, expected in ((None, 401), (self.member, 403)):
                self.client.logout()
                if actor is not None:
                    self.client.force_login(actor)
                self.assertEqual(self.client.get(self.base).status_code, expected)
                self.assertEqual(self.post(self.draft).status_code, expected)
                self.assertEqual(self.post({}, '/confirm').status_code, expected)
            read.assert_not_called()
            preview.assert_not_called()
            confirm.assert_not_called()

    def test_cached_staff_identity_is_rechecked_for_reads_and_writes(self):
        factory = RequestFactory()
        with patch.object(resource_plan_api, 'resource_plan_state') as read, \
                patch.object(resource_plan_api, 'preview_resource_plan') as preview:
            for change in ({'is_staff': False}, {'is_staff': True, 'is_active': False}):
                get_user_model().objects.filter(pk=self.admin.pk).update(**change)
                for method, view in (('get', resource_plan_api.resource_plan), ('post', resource_plan_api.preview)):
                    request = getattr(factory, method)(self.base, json.dumps(self.draft),
                                                       content_type='application/json') if method == 'post' else factory.get(self.base)
                    request.user = self.admin
                    self.assertEqual(view(request, 'home').status_code, 403)
            read.assert_not_called()
            preview.assert_not_called()

    def test_method_restrictions_do_not_dispatch_to_service(self):
        with patch.object(resource_plan_api, 'preview_resource_plan') as preview:
            response = self.client.get(self.base + '/preview')
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response['Allow'], 'POST')
            self.assertEqual(self.post(self.draft, '').status_code, 405)
            self.assertEqual(self.client.get(self.base + '/confirm').status_code, 405)
            preview.assert_not_called()

    def test_csrf_required_and_valid_same_origin_token_reaches_service(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        with patch.object(resource_plan_api, 'preview_resource_plan', return_value={'preview_token': 'signed'}) as preview:
            self.assertEqual(client.post(self.base + '/preview', json.dumps(self.draft),
                                        content_type='application/json').status_code, 403)
            preview.assert_not_called()
            token = client.get('/api/v1/session').json()['data']['csrf_token']
            response = client.post(self.base + '/preview', json.dumps(self.draft),
                                   content_type='application/json', HTTP_X_CSRFTOKEN=token)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Cache-Control'], 'no-store, private')

    def test_duplicate_keys_unknown_fields_and_nonfinite_numbers_are_rejected(self):
        valid = json.dumps(self.draft)
        invalid = [valid[:-1] + ',"operation":"expiry"}',
                   valid.replace('"add_bytes": "1000000000"', '"add_bytes":"1","add_bytes":"2"'),
                   valid.replace('"expected_revision": 1', '"expected_revision": NaN'),
                   {**self.draft, 'provider_reset_at': '2030-01-01'},
                   {'operation': 'quota'}, [], None]
        with patch.object(resource_plan_api, 'preview_resource_plan') as preview:
            for body in invalid:
                with self.subTest(body=body):
                    self.assertEqual(self.post(body).status_code, 422)
            preview.assert_not_called()

    def test_large_deep_invalid_unicode_and_wrong_content_type_fail_closed(self):
        with patch.object(resource_plan_api, 'preview_resource_plan') as preview:
            for body in (' ' * 8193, '[' * 1200 + '0' + ']' * 1200, '{'):
                self.assertEqual(self.post(body).status_code, 422)
            self.assertEqual(self.client.post(self.base + '/preview', b'\xff',
                                             content_type='application/json').status_code, 422)
            self.assertEqual(self.client.post(self.base + '/preview', self.draft).status_code, 415)
            preview.assert_not_called()

    def test_confirm_requires_the_exact_signed_request_fields(self):
        with patch.object(resource_plan_api, 'confirm_resource_plan') as confirm:
            self.assertEqual(self.post(self.draft, '/confirm').status_code, 422)
            self.assertEqual(self.post({**self.draft, 'preview_token': 't', 'idempotency_key': 'k',
                                        'force': True}, '/confirm').status_code, 422)
            confirm.assert_not_called()

    def test_controlled_conflict_retains_code_without_internal_exception_details(self):
        with patch.object(resource_plan_api, 'preview_resource_plan',
                          side_effect=ResourcePlanError('source_changed', '采购来源已变化，请重新读取。', 409)):
            response = self.post(self.draft)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'data': None, 'error': {
            'code': 'source_changed', 'message': '采购来源已变化，请重新读取。'}})


@override_settings(ROOT_URLCONF=__name__, PANEL_LIVE=False, RESOURCE_PLAN_WRITE_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ResourcePlanServiceHttpTests(TransactionTestCase):
    """调用真实计划事务；只用假采购采样授权和假权威来源，未调用供应商。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # root尚未接入迁移时只在本进程独立测试库建表，不修改共享迁移文件。
        cls.created_models = []
        existing = set(connection.introspection.table_names())
        with connection.schema_editor() as editor:
            for model in (ResourcePlanBinding, ResourcePlanVersion, ResourcePlanConsumption):
                if model._meta.db_table not in existing:
                    editor.create_model(model)
                    cls.created_models.append(model)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            for model in reversed(cls.created_models):
                editor.delete_model(model)
        super().tearDownClass()

    def setUp(self):
        self.admin = get_user_model().objects.create_user('plan-owner', is_staff=True)
        self.other = get_user_model().objects.create_user('other-admin', is_staff=True)
        self.client.force_login(self.admin)
        self.plans = {plan['id']: deepcopy(plan) for plan in PLANS}
        self.keys = {'home': 'b' * 64, 'bwg': 'c' * 64}
        self.live_keys = dict(self.keys)
        self.source = override_settings(RESOURCE_PLAN_SOURCE_READER=lambda meter: deepcopy(self.plans[meter]))
        self.source.enable()
        self.addCleanup(self.source.disable)
        self.purchase = patch('portal.resource_plan_service.InventoryPurchases.for_resource', side_effect=lambda meter, scope: {
            'purchase_meter_state': 'ready', 'purchase_binding_key': self.live_keys[meter], 'purchase_meter': {}})
        self.purchase_mock = self.purchase.start()
        self.addCleanup(self.purchase.stop)
        self.server = Server.objects.create(name='采购绑定机器', purchase_meter_id='bwg')
        self.home = Egress.objects.create(server=self.server, name='采购外部节点', kind='upstream',
                                         ownership='external', external_kind='node', purchase_meter_id='home')
        for meter, kind, target in (('home', 'egress', self.home), ('bwg', 'server', self.server)):
            register_resource_plan(self.plans[meter], target_kind=kind, target_id=target.pk, actor=self.admin)

    def request(self, suffix='', body=None, meter='home'):
        url = '/api/v1/admin/resource-plans/' + self.keys[meter] + suffix
        return self.client.get(url) if body is None else self.client.post(
            url, json.dumps(body), content_type='application/json')

    def draft(self, operation='quota', changes=None, meter='home'):
        response = self.request(meter=meter)
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()['data']
        return {'expected_revision': data['revision'], 'source_fingerprint': data['source_fingerprint'],
                'operation': operation, 'changes': changes or {'add_bytes': '1000000000'}}

    def signed(self, draft, key='request-plan-001', meter='home'):
        response = self.request('/preview', draft, meter=meter)
        self.assertEqual(response.status_code, 200, response.content)
        return {**draft, 'preview_token': response.json()['data']['preview_token'], 'idempotency_key': key}

    def test_addition_saves_once_and_keeps_expiry_cycle_and_consumption_separate(self):
        request = self.signed(self.draft())
        self.assertEqual(ResourcePlanVersion.objects.count(), 0)
        first = self.request('/confirm', request)
        self.assertEqual(first.status_code, 200, first.content)
        data = first.json()['data']
        self.assertEqual(data['plan']['quota_bytes'], '401000000000')
        self.assertEqual(data['plan']['expires_on'], PLANS[0]['expires_on'])
        self.assertEqual(data['plan']['cycle'], PLANS[0]['cycle'])
        self.assertEqual(data['consumption']['status'], 'pending')
        self.assertIsNone(data['consumption']['consumed_at'])
        self.assertFalse(data['receipt']['replayed'])
        replay = self.request('/confirm', request)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertTrue(replay.json()['data']['receipt']['replayed'])
        self.assertEqual(replay.json()['data']['receipt']['id'], data['receipt']['id'])
        self.assertEqual(ResourcePlanVersion.objects.count(), 1)
        self.assertEqual(ResourcePlanConsumption.objects.count(), 0)

    def test_same_key_with_changed_payload_and_stale_preview_are_conflicts(self):
        old = self.signed(self.draft('expiry', {'expires_on': '2028-01-19'}), 'old-expiry-preview')
        quota = self.signed(self.draft())
        self.assertEqual(self.request('/confirm', quota).status_code, 200)
        conflict = self.request('/confirm', {**quota, 'changes': {'add_bytes': '2000000000'}})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['error']['code'], 'idempotency_conflict')
        stale = self.request('/confirm', old)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()['error']['code'], 'revision_conflict')
        self.assertEqual(ResourcePlanVersion.objects.count(), 1)

    def test_other_staff_cannot_read_preview_or_replay_owner_plan(self):
        request = self.signed(self.draft())
        self.assertEqual(self.request('/confirm', request).status_code, 200)
        self.client.force_login(self.other)
        for suffix, body in (('', None), ('/preview', self.draft_from(request)), ('/confirm', request)):
            with self.subTest(suffix=suffix):
                response = self.request(suffix, body)
                self.assertEqual(response.status_code, 403)
                self.assertIsNone(response.json()['data'])
        self.assertEqual(ResourcePlanVersion.objects.count(), 1)

    @staticmethod
    def draft_from(request):
        return {key: value for key, value in request.items() if key in resource_plan_api.PREVIEW_FIELDS}

    def test_source_plan_drift_rejects_confirmation_and_keeps_versions_empty(self):
        request = self.signed(self.draft())
        self.plans['home']['quota_bytes'] = '500000000000'
        response = self.request('/confirm', request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'purchase_source_changed')
        self.assertEqual(ResourcePlanVersion.objects.count(), 0)

    def test_source_generation_and_target_rebinding_reject_saved_replay(self):
        request = self.signed(self.draft())
        self.assertEqual(self.request('/confirm', request).status_code, 200)
        self.live_keys['home'] = 'd' * 64
        self.assertEqual(self.request('/confirm', request).status_code, 409)
        self.live_keys['home'] = self.keys['home']
        self.home.purchase_meter_id = 'changed'
        self.home.save(update_fields=['purchase_meter_id'])
        self.assertEqual(self.request('/confirm', request).status_code, 409)
        self.assertEqual(ResourcePlanVersion.objects.count(), 1)

    def test_official_quota_and_reset_are_read_only_but_expiry_is_independent(self):
        state = self.request(meter='bwg').json()['data']
        self.assertEqual(state['allowed_operations'], ['expiry'])
        for operation, changes in (('quota', {'quota_bytes': '1000'}),
                                   ('cycle', {'days': 30, 'next_reset_at': '2030-01-01T00:00:00+08:00'})):
            response = self.request('/preview', self.draft(operation, changes, 'bwg'), meter='bwg')
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()['error']['code'], 'official_plan_read_only')
        signed = self.signed(self.draft('expiry', {'expires_on': '2030-01-19'}, 'bwg'), meter='bwg')
        response = self.request('/confirm', signed, meter='bwg')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()['data']['plan']['quota_bytes'])
        self.assertEqual(response.json()['data']['plan']['cycle'], {'kind': 'provider'})

    def test_tampered_token_and_changed_operation_never_save(self):
        request = self.signed(self.draft())
        for modified in ({**request, 'preview_token': request['preview_token'] + 'x'},
                         {**request, 'operation': 'expiry', 'changes': {'expires_on': '2028-01-19'}}):
            self.assertEqual(self.request('/confirm', modified).status_code, 409)
        self.assertEqual(ResourcePlanVersion.objects.count(), 0)

    def test_write_gate_blocks_confirm_without_disabling_read(self):
        request = self.signed(self.draft())
        with override_settings(RESOURCE_PLAN_WRITE_ENABLED=False):
            self.assertEqual(self.request().status_code, 200)
            self.assertEqual(self.request('/confirm', request).status_code, 403)
        self.assertEqual(ResourcePlanVersion.objects.count(), 0)

    def test_bare_meter_names_and_unknown_binding_keys_do_not_resolve(self):
        for identifier in ('home', 'bwg', 'e' * 64, 'B' * 64):
            response = self.client.get('/api/v1/admin/resource-plans/' + identifier)
            self.assertEqual(response.status_code, 404)
            self.assertIsNone(response.json()['data'])
