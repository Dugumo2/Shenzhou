"""W1 查询接口的权限、真实对象投影和无副作用反例。"""
from datetime import timedelta
import json
from unittest.mock import patch
import uuid

from django.contrib.auth.models import User
from django.db import connection
from django.http import HttpResponse
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import include, path
from django.utils import timezone

from .models import (BillingCycle, ClientDirectRule, DeploymentJob, DeviceSubscription,
                     Egress, Entitlement, Ingress, Line, Membership, NodeIdentity, Server,
                     SubscriptionGrant, UsageLedger, UsageStream)


def old_page(request):
    return HttpResponse('旧页面')


urlpatterns = [path('api/v1/', include('portal.api_urls')), path('old-page', old_page)]


@override_settings(ROOT_URLCONF='portal.test_api', CSRF_FAILURE_VIEW='portal.api.csrf_failure',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReadOnlyAPITests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('管理员', password='Local-test-Password-12', is_staff=True)
        self.user = User.objects.create_user('甲用户', password='Local-test-Password-12')
        self.other = User.objects.create_user('乙用户', password='Local-test-Password-12')
        self.now = timezone.now()
        self.client.force_login(self.user)

    def service(self, user=None, **changes):
        fields = {'user': user or self.user, 'quota_bytes': 100_000_000_000,
                  'state': 'active', 'activated_at': self.now - timedelta(days=1),
                  'expires_at': self.now + timedelta(days=30), 'applied_revision': 1,
                  'usage_updated_at': self.now, 'applied_snapshot': {'quota_bytes': 100_000_000_000}}
        fields.update(changes)
        return Entitlement.objects.create(**fields)

    def cycle(self, service, *, evidence=True, observed_at=None, **changes):
        fields = {'entitlement': service, 'starts_at': self.now - timedelta(days=1),
                  'ends_at': self.now + timedelta(days=15), 'used_bytes': 30_000_000_000,
                  'raw_bytes': 25_000_000_000}
        fields.update(changes)
        cycle = BillingCycle.objects.create(**fields)
        if evidence:
            server = Server.objects.create(name='接口隔离假服务器')
            ingress = Ingress.objects.create(server=server, name='假入口', protocol='ws')
            egress = Egress.objects.create(server=server, name='假出口', kind='upstream')
            line = Line.objects.create(name='假线路', ingress=ingress, egress=egress)
            subscription = DeviceSubscription.objects.create(entitlement=service, client='windows', name='假计量来源')
            identity = NodeIdentity.objects.create(subscription=subscription, line=line, ingress=ingress, generation=1)
            stream = UsageStream.objects.create(identity=identity, epoch='test-only-epoch')
            UsageLedger.objects.create(cycle=cycle, stream=stream, sequence=1, upload_delta=cycle.raw_bytes or 0,
                download_delta=0, weighted_bytes=cycle.used_bytes, quality='metered', observed_at=observed_at or self.now)
        return cycle

    def data(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertIsNone(payload['error'])
        return payload['data']

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertIsNone(response.json()['data'])
        self.assertEqual(response.json()['error']['code'], code)

    def test_anonymous_session_is_safe_and_supplies_csrf(self):
        self.client.logout()
        data = self.data('/api/v1/session')
        self.assertFalse(data['authenticated'])
        self.assertIsNone(data['user'])
        self.assertTrue(data['csrf_token'])
        self.assertEqual(data['brand'], '神舟云')
        self.assertEqual(data['password_policy']['min_length'], 12)
        self.assertEqual(data['environment'], {'kind': 'local_candidate', 'is_demo': False})
        self.assertIn('csrftoken', self.client.cookies)

    @override_settings(CANDIDATE_DEMO_DATA=True)
    def test_explicit_demo_fixture_marker_is_visible_without_private_paths(self):
        data = self.data('/api/v1/session')
        self.assertEqual(data['environment'], {'kind': 'local_candidate', 'is_demo': True})

    def test_anonymous_queries_return_json_401_without_redirect(self):
        self.client.logout()
        for path in ['/api/v1/me/services', '/api/v1/me/services/' + str(uuid.uuid4()),
                     '/api/v1/catalog/clients', '/api/v1/catalog/clients/windows/guide',
                     '/api/v1/admin/services', '/api/v1/admin/rules']:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assert_error(response, 401, 'authentication_required')
                self.assertNotIn('Location', response)

    def test_user_cannot_read_admin_data_or_inactive_session(self):
        for path in ['/api/v1/admin/services', '/api/v1/admin/rules']:
            self.assert_error(self.client.get(path), 403, 'permission_denied')
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assert_error(self.client.get('/api/v1/me/services'), 401, 'authentication_required')

    def test_empty_registered_account_does_not_become_a_service(self):
        Membership.objects.create(user=self.user, quota_bytes=0, status='pending')
        before = (Membership.objects.count(), Entitlement.objects.count(), BillingCycle.objects.count(),
                  DeviceSubscription.objects.count(), SubscriptionGrant.objects.count(), NodeIdentity.objects.count())
        data = self.data('/api/v1/me/services')
        self.assertEqual(data['items'], [])
        self.assertEqual(data['service_count'], 0)
        self.assertEqual(before, (Membership.objects.count(), Entitlement.objects.count(), BillingCycle.objects.count(),
                                 DeviceSubscription.objects.count(), SubscriptionGrant.objects.count(), NodeIdentity.objects.count()))

    def test_administrator_also_has_no_automatic_service(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.data('/api/v1/me/services')['items'], [])

    def test_owner_projection_uses_stable_id_and_decimal_byte_strings(self):
        record = self.service()
        cycle = self.cycle(record)
        data = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(data['id'], str(record.public_id))
        self.assertEqual(data['source_type'], 'entitlement')
        self.assertEqual(data['name'], '神舟云')
        self.assertEqual(data['quota_bytes'], '100000000000')
        self.assertEqual(data['used_bytes'], '30000000000')
        self.assertEqual(data['raw_bytes'], '25000000000')
        self.assertEqual(data['remaining_bytes'], '70000000000')
        self.assertEqual(data['usage']['quality'], 'measured')
        self.assertEqual(data['next_reset_at'], cycle.ends_at.isoformat())
        self.assertIsNone(data['delivery']['download_url'])
        self.assertFalse(data['actions']['billing'])

    def test_large_quota_round_trips_without_javascript_precision_loss(self):
        large = 2**63 - 1
        record = self.service(quota_bytes=large, applied_snapshot={'quota_bytes': large})
        self.cycle(record, used_bytes=2**53 + 1, raw_bytes=2**53 + 1)
        data = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(data['quota_bytes'], str(large))
        self.assertEqual(data['used_bytes'], str(2**53 + 1))
        self.assertEqual(data['remaining_bytes'], str(large - (2**53 + 1)))

    def test_other_user_service_and_invalid_uuid_have_same_safe_error(self):
        record = self.service(user=self.other)
        other = self.client.get('/api/v1/me/services/' + str(record.public_id))
        invalid = self.client.get('/api/v1/me/services/not-a-uuid')
        missing = self.client.get('/api/v1/me/services/' + str(uuid.uuid4()))
        for response in (other, invalid, missing):
            self.assert_error(response, 404, 'not_found')
        self.assertEqual(other.content, missing.content)
        self.assertEqual(other.content, invalid.content)
        self.assertEqual(self.data('/api/v1/me/services')['items'], [])

    def test_get_does_not_create_a_missing_billing_cycle_or_delivery(self):
        record = self.service()
        with CaptureQueriesContext(connection) as queries:
            for path in ['/api/v1/me/services', '/api/v1/me/services/' + str(record.public_id),
                         '/api/v1/catalog/clients', '/api/v1/catalog/clients/android/guide']:
                self.data(path)
        self.assertFalse(BillingCycle.objects.exists())
        self.assertFalse(DeviceSubscription.objects.exists())
        self.assertFalse(NodeIdentity.objects.exists())
        self.assertFalse(DeploymentJob.objects.exists())
        writes = [q['sql'] for q in queries.captured_queries if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        self.assertEqual(writes, [])

    def test_gap_unknown_stale_and_cross_cycle_statistics_are_not_false_remaining(self):
        record = self.service(metering_gap=True)
        cycle = self.cycle(record)
        data = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(data['usage']['quality'], 'gap')
        self.assertIsNone(data['used_bytes'])
        self.assertIsNone(data['remaining_bytes'])
        record.metering_gap = False
        record.usage_updated_at = self.now - timedelta(minutes=4)
        record.save()
        stale = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(stale['usage']['quality'], 'stale')
        self.assertEqual(stale['used_bytes'], '30000000000')
        self.assertIsNone(stale['remaining_bytes'])
        record.usage_updated_at = cycle.starts_at - timedelta(seconds=1)
        record.save()
        unknown = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(unknown['usage']['quality'], 'unknown')
        self.assertIsNone(unknown['used_bytes'])
        self.assertIsNone(unknown['raw_bytes'])

    def test_overlapping_cycles_are_an_explicit_gap_not_arbitrary_balance(self):
        record = self.service()
        self.cycle(record)
        self.cycle(record, starts_at=self.now - timedelta(hours=1))
        data = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(data['usage']['quality'], 'gap')
        self.assertIsNone(data['used_bytes'])
        self.assertIsNone(data['next_reset_at'])

    def test_future_statistics_do_not_become_current_used_or_remaining(self):
        record = self.service(usage_updated_at=self.now + timedelta(hours=1))
        self.cycle(record)
        data = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(data['usage']['quality'], 'unknown')
        self.assertIsNone(data['used_bytes'])
        self.assertIsNone(data['remaining_bytes'])

    def test_previous_cycle_boundary_sample_never_proves_a_new_cycle_zero(self):
        record = self.service()
        old_cycle = self.cycle(record, ends_at=self.now, observed_at=self.now)
        new_cycle = self.cycle(record, starts_at=self.now, used_bytes=0, raw_bytes=0, evidence=False)
        self.assertEqual(old_cycle.ledger.get().observed_at, new_cycle.starts_at)
        self.assertEqual(record.usage_updated_at, new_cycle.starts_at)
        data = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(data['usage']['quality'], 'unknown')
        self.assertIsNone(data['used_bytes'])
        self.assertIsNone(data['remaining_bytes'])

    def test_current_cycle_without_metered_ledger_evidence_has_unknown_usage(self):
        record = self.service()
        self.cycle(record, evidence=False, used_bytes=0, raw_bytes=0)
        data = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(data['usage']['quality'], 'unknown')
        self.assertIsNone(data['used_bytes'])
        self.assertIsNone(data['remaining_bytes'])

    def test_desired_quota_not_yet_applied_does_not_create_a_remaining_balance(self):
        record = self.service(applied_revision=0, applied_snapshot={})
        self.cycle(record)
        data = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(data['quota_state'], 'configured')
        self.assertIsNone(data['remaining_bytes'])
        self.assertEqual(data['application']['state'], 'waiting')

    def test_membership_only_is_explicit_read_only_legacy_projection(self):
        membership = Membership.objects.create(user=self.user, status='active', quota_bytes=100_000_000_000,
            used_bytes=10, provisioning_state='applied', usage_state='measured', usage_updated_at=self.now,
            expires_at=self.now + timedelta(days=30))
        SubscriptionGrant.objects.create(membership=membership, device='windows')
        data = self.data('/api/v1/me/services')['items'][0]
        self.assertEqual(data['id'], str(membership.public_id))
        self.assertEqual(data['source_type'], 'membership')
        self.assertIsNone(data['used_bytes'])
        self.assertIsNone(data['remaining_bytes'])
        self.assertFalse(data['actions']['billing'])
        detail = self.data('/api/v1/me/services/' + str(membership.public_id))
        self.assertIn('尚未迁入', detail['delivery']['message'])
        self.assertFalse(Entitlement.objects.exists())

    def test_dual_chains_never_invent_a_second_quota_or_merge_by_user(self):
        record = self.service()
        membership = Membership.objects.create(user=self.user, status='active', quota_bytes=50_000_000_000)
        data = self.data('/api/v1/me/services')
        self.assertEqual(data['service_count'], 1)
        self.assertEqual(data['items'][0]['id'], str(record.public_id))
        self.assertEqual(data['compatibility']['state'], 'mapping_required')
        self.assert_error(self.client.get('/api/v1/me/services/' + str(membership.public_id)), 404, 'not_found')
        self.client.force_login(self.admin)
        self.assertEqual(self.data('/api/v1/admin/services')['pagination']['total'], 1)

    def test_registered_membership_placeholder_does_not_require_mapping(self):
        self.service()
        Membership.objects.create(user=self.user, quota_bytes=0)
        self.assertEqual(self.data('/api/v1/me/services')['compatibility']['state'], 'clear')

    def test_subscriptions_do_not_add_services_and_queries_do_not_expand_identity(self):
        record = self.service()
        self.cycle(record)
        for client in ('windows', 'v2rayng', 'android'):
            DeviceSubscription.objects.create(entitlement=record, name='测试资源', client=client, state='active')
        counts = (DeviceSubscription.objects.count(), NodeIdentity.objects.count())
        data = self.data('/api/v1/me/services/' + str(record.public_id))
        self.assertEqual(len(data['clients']), 4)
        self.assertEqual(self.data('/api/v1/me/services')['service_count'], 1)
        self.assertEqual((DeviceSubscription.objects.count(), NodeIdentity.objects.count()), counts)
        self.assertEqual(data['delivery']['state'], 'verification_required')
        self.assertTrue(all(client['delivery']['download_url'] is None for client in data['clients']))

    def test_private_snapshot_fields_never_appear_in_the_response(self):
        record = self.service(applied_snapshot={'quota_bytes': 100, 'credential_ref': 'PRIVATE-CREDENTIAL',
            'token': 'PRIVATE-TOKEN', 'private_path': 'PRIVATE-PATH', 'identities': ['PRIVATE-IDENTITY']})
        response = self.client.get('/api/v1/me/services/' + str(record.public_id))
        for text in ('PRIVATE-CREDENTIAL', 'PRIVATE-TOKEN', 'PRIVATE-PATH', 'PRIVATE-IDENTITY',
                     'applied_snapshot', 'credential_ref', 'identities'):
            self.assertNotIn(text, response.content.decode())

    def test_catalog_is_real_capability_unknown_and_guides_do_not_claim_unverified_menus(self):
        data = self.data('/api/v1/catalog/clients')
        self.assertEqual({c['id'] for c in data['items']}, {'windows', 'v2rayng', 'android', 'router'})
        for client in data['items']:
            self.assertIsNone(client['software_version'])
            self.assertIsNone(client['core_version'])
            guide = self.data(client['guide_url'])
            self.assertEqual(guide['update_status']['applied'], 'unknown')
        router = self.data('/api/v1/catalog/clients/router/guide')
        self.assertEqual(router['verification'], 'unsupported')
        self.assertEqual(router['steps'], [])
        self.assert_error(self.client.get('/api/v1/catalog/clients/unknown/guide'), 404, 'not_found')

    def test_admin_search_pagination_and_state_are_based_on_existing_objects(self):
        first = self.service(state='pending')
        second = self.service(user=self.other)
        membership = Membership.objects.create(user=self.admin, status='active', quota_bytes=5)
        self.client.force_login(self.admin)
        data = self.data('/api/v1/admin/services?page_size=1')
        self.assertEqual(data['pagination'], {'page': 1, 'page_size': 1, 'total': 3, 'pages': 3,
                                           'has_next': True, 'has_previous': False})
        other_page = self.data('/api/v1/admin/services?page=2&page_size=1')
        self.assertTrue(other_page['pagination']['has_previous'])
        self.assertNotEqual(data['items'][0]['id'], other_page['items'][0]['id'])
        search = self.data('/api/v1/admin/services?q=乙用户')
        self.assertEqual([row['id'] for row in search['items']], [str(second.public_id)])
        self.assertEqual(self.data('/api/v1/admin/services?state=pending')['items'][0]['id'], str(first.public_id))
        self.assertEqual(self.data('/api/v1/admin/services?q=' + str(second.public_id))['pagination']['total'], 1)
        row = next(row for row in self.data('/api/v1/admin/services')['items'] if row['id'] == str(membership.public_id))
        self.assertFalse(row['actions']['billing'])
        self.assertNotIn('id', row['user'])
        self.assertNotIn('email', row['user'])

    def test_admin_empty_page_filters_and_invalid_values_are_safe(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.data('/api/v1/admin/services')['pagination']['total'], 0)
        for query in ('page=0', 'page_size=101', 'page=x'):
            self.assert_error(self.client.get('/api/v1/admin/services?' + query), 422, 'invalid_pagination')
        self.assert_error(self.client.get('/api/v1/admin/services?state=unknown'), 422, 'invalid_filter')
        self.assert_error(self.client.get('/api/v1/admin/services?page=2'), 404, 'page_not_found')

    def test_admin_expired_and_metering_gap_filters(self):
        record = self.service(expires_at=self.now - timedelta(seconds=1), metering_gap=True)
        self.client.force_login(self.admin)
        for state in ('expired', 'metering_gap'):
            self.assertEqual(self.data('/api/v1/admin/services?state=' + state)['items'][0]['id'], str(record.public_id))

    def test_rules_source_index_never_claims_real_restore_or_client_application(self):
        ClientDirectRule.objects.create(creator=self.admin, kind='exact', value='example.com')
        ClientDirectRule.objects.create(creator=self.admin, kind='suffix', value='example.org', outbound='proxy')
        self.client.force_login(self.admin)
        data = self.data('/api/v1/admin/rules')
        self.assertFalse(data['read_only'])
        self.assertEqual(data['scope'], 'local_candidate')
        self.assertEqual(len(data['items']), 2)
        self.assertEqual({r['action'] for r in data['items']}, {'client_direct', 'proxy'})
        self.assertEqual(data['sources'][0]['state'], 'candidate')
        self.assertEqual(len(data['sources'][0]['sha256']), 64)
        self.assertEqual(data['sources'][2]['state'], 'not_connected')
        self.assertEqual(data['publish_state'], 'unknown')
        self.assertEqual(data['client_apply_state'], 'unknown')
        self.assertFalse(BillingCycle.objects.exists())

    def test_invalid_candidate_does_not_leak_validation_exception(self):
        ClientDirectRule.objects.create(creator=self.admin, kind='exact', value='openai.com')
        self.client.force_login(self.admin)
        data = self.data('/api/v1/admin/rules')
        self.assertEqual(data['sources'][0]['state'], 'invalid')
        self.assertIsNone(data['sources'][0]['sha256'])

    def test_invalid_rule_value_does_not_expose_a_private_path(self):
        ClientDirectRule.objects.create(creator=self.admin, kind='exact', value='PRIVATE-PATH/SECRET-TOKEN')
        self.client.force_login(self.admin)
        response = self.client.get('/api/v1/admin/rules')
        self.assertNotIn('PRIVATE-PATH', response.content.decode())
        self.assertNotIn('SECRET-TOKEN', response.content.decode())
        self.assertIsNone(response.json()['data']['items'][0]['value'])

    def test_read_endpoints_reject_writes_without_side_effects(self):
        response = self.client.post('/api/v1/me/services', {}, content_type='application/json')
        self.assert_error(response, 405, 'method_not_allowed')
        self.assertEqual(response['Allow'], 'GET')
        self.assertFalse(Entitlement.objects.exists())

    def test_csrf_login_rotation_logout_and_cross_origin_are_enforced(self):
        browser = Client(enforce_csrf_checks=True)
        body = {'username': self.user.username, 'password': 'Local-test-Password-12'}
        self.assert_error(browser.post('/api/v1/login', body, content_type='application/json'), 403, 'csrf_failed')
        initial = browser.get('/api/v1/session').json()['data']['csrf_token']
        cross = browser.post('/api/v1/login', body, content_type='application/json',
                             HTTP_X_CSRFTOKEN=initial, HTTP_ORIGIN='https://foreign.example.invalid')
        self.assert_error(cross, 403, 'csrf_failed')
        response = browser.post('/api/v1/login', body, content_type='application/json', HTTP_X_CSRFTOKEN=initial)
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['user']['username'], self.user.username)
        self.assertNotEqual(initial, data['csrf_token'])
        self.assert_error(browser.post('/api/v1/logout', {}, content_type='application/json'), 403, 'csrf_failed')
        self.assertTrue(browser.get('/api/v1/session').json()['data']['authenticated'])
        response = browser.post('/api/v1/logout', {}, content_type='application/json', HTTP_X_CSRFTOKEN=data['csrf_token'])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['data']['authenticated'])
        self.assert_error(browser.get('/api/v1/me/services'), 401, 'authentication_required')

    def test_csrf_failure_preserves_existing_html_page_behavior(self):
        response = Client(enforce_csrf_checks=True).post('/old-page')
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response['Content-Type'].startswith('text/html'))

    def test_login_invalid_credentials_extra_fields_and_rate_limit(self):
        self.client.logout()
        bad = self.client.post('/api/v1/login', {'username': self.user.username, 'password': 'wrong'}, content_type='application/json')
        self.assert_error(bad, 401, 'invalid_credentials')
        response = self.client.post('/api/v1/login', {'username': self.user.username, 'password': 'wrong', 'is_staff': True},
                                    content_type='application/json')
        self.assert_error(response, 422, 'invalid_fields')
        self.assert_error(self.client.post('/api/v1/login', '{"username":"x","username":"y","password":"x"}',
                         content_type='application/json'), 422, 'invalid_json')
        self.assert_error(self.client.post('/api/v1/login', {'username': [], 'password': 'x'},
                         content_type='application/json'), 422, 'invalid_fields')
        self.assert_error(self.client.get('/api/v1/login'), 405, 'method_not_allowed')
        with patch('portal.api.throttle', return_value=False):
            response = self.client.post('/api/v1/login', {'username': self.user.username, 'password': 'wrong'},
                                        content_type='application/json')
        self.assert_error(response, 429, 'rate_limited')
        self.assertEqual(response['Retry-After'], '900')

    def test_invalid_and_inactive_credentials_do_not_reveal_account_state(self):
        self.client.logout()
        self.other.is_active = False
        self.other.save(update_fields=['is_active'])
        inactive = self.client.post('/api/v1/login', {'username': self.other.username, 'password': 'Local-test-Password-12'},
                                    content_type='application/json')
        missing = self.client.post('/api/v1/login', {'username': '不存在的用户', 'password': 'Local-test-Password-12'},
                                   content_type='application/json')
        self.assertEqual(inactive.content, missing.content)
        self.assert_error(inactive, 401, 'invalid_credentials')
