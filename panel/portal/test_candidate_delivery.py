"""合成资源稳定交付：本人权限、当前规则、双门禁和原子失败边界。"""
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import uuid

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import path
from django.utils import timezone

from . import api, candidate_delivery
from .client_rules import policy_document
from .models import (BillingCycle, ClientDirectRule, DeploymentJob, DeviceSubscription,
                     Entitlement, NodeIdentity, SubscriptionGrant, UsageLedger)


urlpatterns = [
    path('api/v1/session', api.session),
    path('api/v1/me/services/<str:public_id>/delivery', candidate_delivery.delivery),
    path('api/v1/me/services/<str:public_id>/delivery/<str:client_id>/resources/<str:resource>',
         candidate_delivery.download_resource),
]


@override_settings(ROOT_URLCONF='portal.test_candidate_delivery', PANEL_LIVE=False, CANDIDATE_DEMO_DATA=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   CSRF_FAILURE_VIEW='portal.api.csrf_failure')
class CandidateDeliveryTests(TestCase):
    def setUp(self):
        self.test_root = Path(__file__).resolve().parents[2] / 'staging' / 'candidate-delivery-tests' / uuid.uuid4().hex
        self.test_root.mkdir(parents=True)
        self.addCleanup(self.cleanup_files)
        self.root = self.test_root / 'artifacts'
        self.override = override_settings(ARTIFACT_ROOT=self.root)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user = User.objects.create_user('资源本人', password='Synthetic-only-password-12')
        self.other = User.objects.create_user('另一个用户', password='Synthetic-only-password-12')
        self.admin = User.objects.create_user('资源管理员', password='Synthetic-only-password-12', is_staff=True)
        self.now = timezone.now()
        self.service = Entitlement.objects.create(user=self.user, quota_bytes=100_000_000_000,
            state='simulated', applied_revision=1, expires_at=self.now + timedelta(days=30))
        self.cycle = BillingCycle.objects.create(entitlement=self.service, starts_at=self.now - timedelta(days=1),
            ends_at=self.now + timedelta(days=15), used_bytes=30_000_000_000, raw_bytes=25_000_000_000)
        self.direct = ClientDirectRule.objects.create(creator=self.admin, outbound='direct', kind='suffix',
                                                     value='approved.example.test')
        self.proxy = ClientDirectRule.objects.create(creator=self.admin, outbound='proxy', kind='exact',
                                                    value='openai.com')
        self.disabled = ClientDirectRule.objects.create(creator=self.admin, outbound='direct', kind='exact',
                                                       value='disabled.example.test', enabled=False)
        self.client.force_login(self.user)

    def cleanup_files(self):
        allowed = Path(__file__).resolve().parents[2] / 'staging' / 'candidate-delivery-tests'
        if self.test_root.resolve().is_relative_to(allowed.resolve()):
            shutil.rmtree(self.test_root)

    def endpoint(self, public_id=None):
        return '/api/v1/me/services/' + str(public_id or self.service.public_id) + '/delivery'

    def status(self, client_id='windows', public_id=None):
        return self.client.get(self.endpoint(public_id), {'client_adapter': client_id})

    def obtain(self, client_id='windows', public_id=None, key=None, **changes):
        body = {'client_adapter': client_id, 'expected_revision': self.service.revision,
                'idempotency_key': key or str(uuid.uuid4())}
        body.update(changes)
        return self.client.post(self.endpoint(public_id), json.dumps(body), content_type='application/json')

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()['error'])
        self.assertIn('no-store', response['Cache-Control'])
        return response.json()['data']

    def artifact(self, client_id='windows'):
        directory = self.root / candidate_delivery.NAMESPACE / client_id / 'subscriptions' / str(self.service.public_id) / '1'
        current = directory / 'current.json'
        manifest = json.loads(current.read_bytes())
        return directory, current, manifest

    def invariant(self):
        self.service.refresh_from_db()
        self.cycle.refresh_from_db()
        return (self.service.quota_bytes, self.service.expires_at, self.service.revision,
                self.cycle.pk, self.cycle.used_bytes, self.cycle.raw_bytes,
                Entitlement.objects.count(), BillingCycle.objects.count(), DeviceSubscription.objects.count(),
                NodeIdentity.objects.count(), DeploymentJob.objects.count(), SubscriptionGrant.objects.count(),
                UsageLedger.objects.count())

    def test_status_is_read_only_and_has_no_pretend_link(self):
        before = self.invariant()
        data = self.data(self.status())
        self.assertEqual(data['state'], 'not_prepared')
        self.assertIsNone(data['download_url'])
        self.assertEqual(data['resources'], [])
        self.assertFalse(self.root.exists())
        self.assertEqual(before, self.invariant())
        self.assertEqual(data['mode'], 'synthetic')
        self.assertEqual(data['runtime_acceptance'], 'NOT TESTED')

    def test_first_obtain_downloads_parseable_resources_from_current_rules(self):
        before = self.invariant()
        data = self.data(self.obtain())
        self.assertEqual(data['state'], 'ready')
        self.assertIn('不可连接', data['message'])
        self.assertEqual(data['rule_source'], 'current_candidate_custom_rules')
        self.assertIn('客户端DNS配置', data['unsupported_resources'])
        self.assertEqual(set(item['key'] for item in data['resources']),
                         {'subscription', 'routing', 'policy', 'rules-direct', 'rules-proxy'})
        for item in data['resources']:
            self.assertTrue(item['download_url'].startswith(self.endpoint() + '/windows/resources/'))
            response = self.client.get(item['download_url'])
            self.assertEqual(response.status_code, 200)
            self.assertIn('no-store', response['Cache-Control'])
            self.assertEqual(response['X-Shenzhou-Resource-Mode'], 'synthetic')
            self.assertEqual(sha256(response.content).hexdigest(), item['sha256'])
            self.assertEqual(len(response.content), item['bytes'])
            self.assertNotIn('Subscription-Userinfo', response)
            if item['content_type'] == 'application/json':
                json.loads(response.content)
        links = self.client.get(data['download_url']).content.decode().splitlines()
        self.assertEqual(len(links), 1)
        parsed = urlsplit(links[0])
        self.assertEqual(parsed.scheme, 'vless')
        self.assertEqual(parsed.hostname, 'demo.invalid')
        uuid.UUID(parsed.username)
        self.assertEqual(parse_qs(parsed.query)['allowInsecure'], ['0'])
        policy_url = next(item['download_url'] for item in data['resources'] if item['key'] == 'policy')
        expected, _ = policy_document(ClientDirectRule.objects.all())
        self.assertEqual(self.client.get(policy_url).json(), expected)
        self.assertNotIn('disabled.example.test', json.dumps(expected))
        self.assertEqual(before, self.invariant())

    def test_switching_software_uses_one_fixed_fake_identity_without_database_changes(self):
        before = self.invariant()
        identities = []
        for client_id in ('windows', 'v2rayng', 'android', 'windows'):
            data = self.data(self.obtain(client_id))
            raw = self.client.get(data['download_url']).content
            if client_id == 'android':
                config = json.loads(raw)
                identities.append(config['outbounds'][0]['uuid'])
                self.assertEqual(config['inbounds'], [])
                self.assertEqual(config['route']['final'], 'PROXY')
                self.assertNotIn('experimental', config)
                self.assertNotIn('dns', config)
                self.assertEqual(config['route']['rules'], [
                    {'domain': ['openai.com'], 'outbound': 'PROXY'},
                    {'domain_suffix': ['approved.example.test'], 'outbound': 'DIRECT'}])
            else:
                identities.append(urlsplit(raw.decode().strip()).username)
        self.assertEqual(len(set(identities)), 1)
        self.assertEqual(before, self.invariant())

    def test_repeated_obtain_keeps_same_url_release_and_is_idempotent(self):
        key = 'one-stable-request'
        first = self.data(self.obtain(key=key))
        _, _, manifest = self.artifact()
        for request_key in (key, 'another-request-key'):
            again = self.data(self.obtain(key=request_key))
            self.assertEqual(first['download_url'], again['download_url'])
            self.assertEqual(first['resources'], again['resources'])
            self.assertEqual(manifest['release'], self.artifact()[2]['release'])
        conflict = self.obtain('android', key=key)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['error']['code'], 'idempotency_conflict')
        self.assertFalse((self.root / candidate_delivery.NAMESPACE / 'android').exists())

    def test_rule_updates_publish_new_content_at_same_download_url(self):
        first = self.data(self.obtain())
        previous = self.artifact()[2]
        self.direct.value = 'changed.example.test'
        self.direct.revision += 1
        self.direct.save(update_fields=['value', 'revision'])
        status = self.data(self.status())
        self.assertTrue(status['updates_available'])
        latest = self.data(self.obtain())
        self.assertEqual(first['download_url'], latest['download_url'])
        self.assertNotEqual(first['policy_sha256'], latest['policy_sha256'])
        self.assertNotEqual(previous['release'], self.artifact()[2]['release'])
        self.assertTrue((self.artifact()[0] / 'releases' / previous['release']).is_dir())
        self.assertFalse(self.data(self.status())['updates_available'])

    def test_successful_key_replay_does_not_publish_later_rule_changes(self):
        key = 'same-successful-key'
        first = self.data(self.obtain(key=key))
        release = self.artifact()[2]['release']
        self.direct.value = 'later.example.test'
        self.direct.save(update_fields=['value'])
        replay = self.data(self.obtain(key=key))
        self.assertEqual(replay['download_url'], first['download_url'])
        self.assertEqual(replay['policy_sha256'], first['policy_sha256'])
        self.assertEqual(self.artifact()[2]['release'], release)
        self.assertTrue(self.data(self.status())['updates_available'])

    def test_failed_policy_compile_preserves_last_verified_download(self):
        first = self.data(self.obtain())
        _, current, _ = self.artifact()
        before = current.read_bytes()
        old_content = self.client.get(first['download_url']).content
        ClientDirectRule.objects.filter(pk=self.direct.pk).update(kind='bad-kind')
        failure = self.obtain()
        self.assertEqual(failure.status_code, 422)
        self.assertEqual(current.read_bytes(), before)
        status = self.data(self.status())
        self.assertEqual(status['state'], 'ready')
        self.assertTrue(status['update_error'])
        self.assertEqual(status['download_url'], first['download_url'])
        self.assertEqual(self.client.get(first['download_url']).content, old_content)

    def test_bad_policy_without_previous_asset_gives_no_link(self):
        ClientDirectRule.objects.filter(pk=self.direct.pk).update(outbound='block')
        data = self.data(self.status())
        self.assertEqual(data['state'], 'blocked')
        self.assertIsNone(data['download_url'])
        self.assertEqual(data['resources'], [])
        self.assertEqual(self.obtain().status_code, 422)
        self.assertFalse((self.root / candidate_delivery.NAMESPACE / 'windows').exists())

    def test_atomic_publish_failure_preserves_current_and_old_resources(self):
        first = self.data(self.obtain())
        _, current, _ = self.artifact()
        before = current.read_bytes()
        old_content = self.client.get(first['download_url']).content
        self.direct.value = 'publish-failure.example.test'
        self.direct.save(update_fields=['value'])
        with patch('bridge.client_delivery.os.replace', side_effect=OSError('仅测试原子切换失败')):
            self.assertEqual(self.obtain().status_code, 503)
        self.assertEqual(current.read_bytes(), before)
        self.assertEqual(self.client.get(first['download_url']).content, old_content)

    def test_download_has_no_get_side_effects_and_head_matches(self):
        data = self.data(self.obtain())
        _, current, _ = self.artifact()
        before = current.read_bytes(), self.invariant()
        head = self.client.head(data['download_url'])
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b'')
        self.assertEqual(int(head['Content-Length']), len(self.client.get(data['download_url']).content))
        self.assertEqual(before, (current.read_bytes(), self.invariant()))

    def test_users_and_admin_cannot_download_another_users_resources(self):
        data = self.data(self.obtain())
        for actor in (self.other, self.admin):
            self.client.force_login(actor)
            self.assertEqual(self.status().status_code, 404)
            self.assertEqual(self.obtain().status_code, 404)
            denied = self.client.get(data['download_url'])
            self.assertEqual(denied.status_code, 404)
            self.assertNotIn('demo.invalid', denied.content.decode())
        self.client.logout()
        for response in (self.status(), self.obtain(), self.client.get(data['download_url'])):
            self.assertEqual(response.status_code, 401)
            self.assertIn('no-store', response['Cache-Control'])

    def test_empty_account_bad_service_and_unknown_resource_fail_closed(self):
        self.client.force_login(self.other)
        self.assertEqual(self.status(public_id=uuid.uuid4()).status_code, 404)
        self.assertEqual(self.obtain(public_id='not-a-service').status_code, 404)
        self.assertFalse(self.root.exists())
        self.client.force_login(self.user)
        data = self.data(self.obtain())
        unknown = data['download_url'].replace('/subscription', '/current.json')
        self.assertEqual(self.client.get(unknown).status_code, 404)
        unknown = data['download_url'].replace('/subscription', '/..%2F..%2Fsecret')
        self.assertEqual(self.client.get(unknown).status_code, 404)

    def test_both_gates_are_required_including_download_after_generation(self):
        data = self.data(self.obtain())
        for live, demo in ((True, True), (True, False), (False, False), (False, 1)):
            with self.subTest(live=live, demo=demo), override_settings(PANEL_LIVE=live, CANDIDATE_DEMO_DATA=demo,
                                                                   SECURE_SSL_REDIRECT=False):
                self.assertEqual(self.status().status_code, 404)
                self.assertEqual(self.obtain().status_code, 404)
                self.assertEqual(self.client.get(data['download_url']).status_code, 404)

    def test_not_ready_service_does_not_expose_generated_resource(self):
        data = self.data(self.obtain())
        changes = ({'enabled': False}, {'state': 'pending'}, {'state': 'disabled'}, {'applied_revision': 0},
                   {'metering_gap': True}, {'expires_at': self.now - timedelta(minutes=1)}, {'expires_at': None},
                   {'quota_bytes': self.cycle.used_bytes})
        for values in changes:
            with self.subTest(values=values):
                Entitlement.objects.filter(pk=self.service.pk).update(**values)
                status = self.data(self.status())
                self.assertEqual(status['state'], 'blocked')
                self.assertIsNone(status['download_url'])
                self.assertEqual(status['resources'], [])
                self.assertEqual(self.obtain().status_code, 409)
                self.assertEqual(self.client.get(data['download_url']).status_code, 404)
                Entitlement.objects.filter(pk=self.service.pk).update(enabled=True, state='simulated', applied_revision=1,
                    metering_gap=False, expires_at=self.now + timedelta(days=30), quota_bytes=100_000_000_000)

    def test_missing_current_cycle_blocks_get_and_post_without_creating_a_cycle(self):
        self.cycle.delete()
        self.assertEqual(self.data(self.status())['state'], 'blocked')
        self.assertEqual(self.obtain().status_code, 409)
        self.assertEqual(BillingCycle.objects.count(), 0)
        self.assertFalse(self.root.exists())

    def test_unsupported_clients_bad_revision_and_extra_fields_do_not_write(self):
        for client_id in ('router', 'unknown', '../android'):
            self.assertEqual(self.status(client_id).status_code, 422)
            self.assertEqual(self.obtain(client_id).status_code, 422)
        for revision in (0, True, '1', 2):
            self.assertEqual(self.obtain(expected_revision=revision).status_code, 409)
        self.assertEqual(self.obtain(quota_bytes=999).status_code, 422)
        self.assertEqual(self.obtain(idempotency_key='../escape').status_code, 422)
        self.assertEqual(self.client.get(self.endpoint(), {'client_adapter': ['windows', 'android']}).status_code, 422)
        self.assertFalse(self.root.exists())

    def test_session_csrf_is_enforced_for_obtain(self):
        browser = Client(enforce_csrf_checks=True)
        browser.force_login(self.user)
        body = {'client_adapter': 'windows', 'expected_revision': 1, 'idempotency_key': 'csrf-check-key'}
        denied = browser.post(self.endpoint(), json.dumps(body), content_type='application/json')
        self.assertEqual(denied.status_code, 403)
        self.assertFalse(self.root.exists())
        csrf = browser.get('/api/v1/session').json()['data']['csrf_token']
        accepted = browser.post(self.endpoint(), json.dumps(body), content_type='application/json', HTTP_X_CSRFTOKEN=csrf)
        self.assertEqual(accepted.status_code, 200)

    def test_hash_tamper_cannot_download_or_overwrite_unverified_previous_asset(self):
        data = self.data(self.obtain())
        directory, current, manifest = self.artifact()
        path = directory / 'releases' / manifest['release'] / manifest['resources']['subscription']['filename']
        path.write_bytes(b'not-a-verified-resource')
        before = current.read_bytes()
        self.assertEqual(self.client.get(data['download_url']).status_code, 404)
        self.assertEqual(self.status().status_code, 503)
        self.assertEqual(self.obtain().status_code, 503)
        self.assertEqual(current.read_bytes(), before)

    def test_manifest_cannot_promote_real_uri_even_with_matching_hash(self):
        data = self.data(self.obtain())
        directory, current, manifest = self.artifact()
        item = manifest['resources']['subscription']
        path = directory / 'releases' / manifest['release'] / item['filename']
        changed = path.read_bytes().replace(b'demo.invalid', b'foreign.example.com')
        path.write_bytes(changed)
        item['sha256'], item['bytes'] = sha256(changed).hexdigest(), len(changed)
        current.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertEqual(self.client.get(data['download_url']).status_code, 404)

    def test_manifest_owner_client_generation_and_scope_are_checked(self):
        data = self.data(self.obtain())
        _, current, manifest = self.artifact()
        original = current.read_bytes()
        for field, value in (('subscription', str(uuid.uuid4())), ('client', 'android'), ('generation', 2),
                             ('evidence_scope', 'production'), ('release', '../escape')):
            with self.subTest(field=field):
                modified = dict(manifest, **{field: value})
                current.write_text(json.dumps(modified), encoding='utf-8')
                self.assertEqual(self.client.get(data['download_url']).status_code, 404)
                current.write_bytes(original)

    def test_symlink_root_rejected_when_platform_supports_it(self):
        actual = self.test_root / 'redirect-target'
        actual.mkdir()
        try:
            self.root.symlink_to(actual, target_is_directory=True)
        except OSError:
            self.skipTest('当前Windows权限不支持建立符号链接；实际边界另有摘要/篡改反例覆盖。')
        self.assertEqual(self.obtain().status_code, 503)
        self.assertEqual(list(actual.iterdir()), [])

    def test_busy_publication_returns_failure_and_keeps_existing_resources(self):
        data = self.data(self.obtain())
        before = self.artifact()[1].read_bytes()
        lock = self.root / candidate_delivery.NAMESPACE / 'requests' / str(self.service.public_id) / '.obtain-lock'
        lock.mkdir()
        self.assertEqual(self.obtain().status_code, 503)
        self.assertEqual(self.artifact()[1].read_bytes(), before)
        self.assertEqual(self.client.get(data['download_url']).status_code, 200)
