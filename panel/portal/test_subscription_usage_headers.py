"""Q7 隔离反例：同套餐标头、原链接权限与未知量，不访问真实资源。"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.core import signing
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .api_helpers import project_service
from .delivery import TOKEN_SALT
from .models import BillingCycle, DeviceSubscription, NodeIdentity, SubscriptionGrant, UsageLedger
from .services import token_for
from .subscription_usage_headers import p8_usage_snapshot, userinfo_for_service, userinfo_from_projection
from . import test_ingress_metering as metering_fixture
from tests import test_p8_entitlement_binding as p8_fixture


class UserinfoProjectionTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.datetime(2026, 10, 13, 3, tzinfo=timezone.get_default_timezone())
        self.item = {'state': 'active', 'enabled': True, 'quota_state': 'applied',
            'application': {'state': 'applied'}, 'usage': {'quality': 'measured', 'updated_at': self.now.isoformat()},
            'quota_bytes': '100000000000', 'used_bytes': '40000000000', 'raw_bytes': '30000000000',
            'next_reset_at': (self.now + timedelta(days=15)).isoformat(),
            'expires_at': (self.now + timedelta(days=90)).isoformat()}

    def header(self, item=None):
        return userinfo_from_projection(self.item if item is None else item, now=self.now)

    def test_charged_total_is_not_raw_download_or_supplier_total(self):
        self.item['provider_usage'] = {'total': '400000000000', 'used': '90000000000'}
        header = self.header()
        self.assertIn('upload=0; download=40000000000; total=100000000000;', header)
        self.assertNotIn('30000000000', header)
        self.assertNotIn('90000000000', header)
        self.assertEqual(header.count(';'), 3)

    def test_known_zero_and_over_quota_are_not_clamped(self):
        for used in ('0', '100000000001', str(2**63 - 1)):
            self.item['used_bytes'] = used
            self.assertIn('download=' + used + ';', self.header())
        self.item['quota_bytes'] = '0'
        self.assertIsNone(self.header())

    def test_unknown_bad_and_injected_bytes_are_not_zero(self):
        for key in ('quota_bytes', 'used_bytes'):
            for value in (None, True, 0, 1.5, '-1', '1.0', '01', ' 1', str(2**63), '1\r\nBad: yes'):
                item = deepcopy(self.item)
                item[key] = value
                with self.subTest(key=key, value=value):
                    self.assertIsNone(self.header(item))

    def test_partial_stale_unknown_and_unapplied_are_omitted(self):
        for quality in ('gap', 'partial', 'stale', 'unknown', 'unavailable'):
            item = deepcopy(self.item)
            item['usage']['quality'] = quality
            self.assertIsNone(self.header(item))
        for key, value in (('quota_state', 'configured'), ('state', 'simulated'),
                           ('enabled', False), ('usage', None), ('application', {})):
            item = deepcopy(self.item)
            item[key] = value
            self.assertIsNone(self.header(item))

    def test_bad_time_missing_cycle_and_future_sample_are_omitted(self):
        for key in ('expires_at', 'next_reset_at'):
            for value in (None, 'broken', '2026-10-13T04:00:00', self.now.isoformat()):
                item = deepcopy(self.item)
                item[key] = value
                self.assertIsNone(self.header(item))
        for observed in (self.now - timedelta(minutes=3, seconds=1), self.now + timedelta(seconds=31)):
            item = deepcopy(self.item)
            item['usage']['updated_at'] = observed.isoformat()
            self.assertIsNone(self.header(item))
        self.item['next_reset_at'] = (self.now + timedelta(seconds=10)).isoformat()
        self.item['usage']['updated_at'] = (self.now + timedelta(seconds=10)).isoformat()
        self.assertIsNone(self.header())


@override_settings(ROOT_URLCONF='megabox.urls', WORKSPACE_V2=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SubscriptionUsageHeaderTests(TestCase):
    service = metering_fixture.IngressMeteringTests.service
    rate_for = metering_fixture.IngressMeteringTests.rate_for
    sample = metering_fixture.IngressMeteringTests.sample

    def setUp(self):
        metering_fixture.IngressMeteringTests.setUp(self)
        self.root = Path(settings.DATA_ROOT) / ('q7-artifacts-' + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        override = override_settings(ARTIFACT_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.record.state = 'active'
        self.record.applied_snapshot = {'quota_bytes': self.record.quota_bytes}
        self.record.save()
        self.subs = []
        for client in ('windows', 'v2rayng', 'android'):
            if client == 'windows':
                sub, identity = self.identity.subscription, self.identity
                sub.state, sub.applied_generation = 'active', sub.generation
                sub.save()
                identity.state = 'active'
                identity.save()
            else:
                sub = DeviceSubscription.objects.create(entitlement=self.record, client=client,
                    name='Q7假格式', state='active', applied_generation=1)
                sub.lines.add(self.line)
                identity = NodeIdentity.objects.create(subscription=sub, line=self.line,
                    ingress=self.ingress, generation=1, state='active')
            # 完整假周期从起点建基线；迁入中途基线必须保持gap，不能人工清空。
            self.sample(up=100, down=200, identity_id=identity.pk, at=self.cycle.starts_at)
            self.sample(2, up=110, down=210, identity_id=identity.pk)
            self.write_artifact(sub)
            self.subs.append(sub)
        self.record.refresh_from_db()
        self.cycle.refresh_from_db()

    def write_artifact(self, sub):
        directory = self.root / 'subscriptions' / str(sub.public_id) / str(sub.generation)
        directory.mkdir(parents=True)
        raw = b'{"fixture_only":true}' if sub.client == 'android' else b'fixture-only-not-a-node'
        filename = 'subscription.json' if sub.client == 'android' else 'subscription.txt'
        (directory / filename).write_bytes(raw)
        # 仅假内容匹配读取边界，不标记为真实客户端导入证据。
        (directory / 'manifest.json').write_text(json.dumps({'subscription': str(sub.public_id),
            'generation': sub.generation, 'client': sub.client, 'evidence_scope': 'production',
            'sha256': hashlib.sha256(raw).hexdigest()}), encoding='utf-8')

    def url(self, sub=None):
        sub = sub or self.subs[0]
        token = signing.Signer(salt=TOKEN_SALT).sign_object({'id': str(sub.public_id), 'v': sub.token_version}, compress=False)
        return '/d/' + token + '/'

    def test_three_formats_get_head_match_same_applied_package_without_writes(self):
        expected = userinfo_for_service(self.record.user, self.record.public_id)
        self.assertIsNotNone(expected, project_service(self.record, 'entitlement'))
        self.assertIn('download=75; total=1000000000000;', expected)
        self.assertEqual(self.cycle.raw_bytes, 60)
        with CaptureQueriesContext(connection) as queries, \
                patch('portal.resource_usage_view.read_resource_usage_view') as provider:
            for sub in self.subs:
                for method in (self.client.get, self.client.head):
                    response = method(self.url(sub))
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response['Subscription-Userinfo'], expected)
        provider.assert_not_called()
        self.assertFalse(any(row['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE '))
                             for row in queries))
        self.assertEqual(DeviceSubscription.objects.count(), 3)
        self.assertEqual(NodeIdentity.objects.count(), 3)
        self.assertEqual(UsageLedger.objects.count(), 3)

    def test_pending_quota_edit_does_not_change_applied_header(self):
        self.record.quota_bytes = 12345
        self.record.save(update_fields=['quota_bytes'])
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        projection = project_service(self.record, 'entitlement')
        self.assertIn('total=' + projection['quota_bytes'] + ';', response['Subscription-Userinfo'])

    def test_p8_snapshot_does_not_accept_an_unrelated_entitlement(self):
        value = p8_usage_snapshot(self.record.user, self.record.public_id, 'a' * 64)
        self.assertEqual(value['state'], 'unavailable')
        self.assertIsNone(value['userinfo'])

    def test_permission_generation_token_and_revocation_stay_closed(self):
        sub = self.subs[0]
        original = self.url(sub)
        for changes in ({'state': 'disabled'}, {'generation': 2}, {'token_version': 2}):
            old = {key: getattr(sub, key) for key in changes}
            DeviceSubscription.objects.filter(pk=sub.pk).update(**changes)
            response = self.client.get(original)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('Subscription-Userinfo', response)
            DeviceSubscription.objects.filter(pk=sub.pk).update(**old)
        NodeIdentity.objects.filter(pk=self.identity.pk).update(revoked_at=self.now)
        self.assertEqual(self.client.get(original).status_code, 404)

    def test_stale_future_missing_or_overlapping_cycle_never_gets_header(self):
        for changes in ({'usage_updated_at': self.now - timedelta(minutes=4)},
                        {'usage_updated_at': self.now + timedelta(seconds=31)}, {'metering_gap': True}):
            old = {key: getattr(self.record, key) for key in changes}
            type(self.record).objects.filter(pk=self.record.pk).update(**changes)
            response = self.client.get(self.url())
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('Subscription-Userinfo', response)
            type(self.record).objects.filter(pk=self.record.pk).update(**old)
        BillingCycle.objects.create(entitlement=self.record, starts_at=self.now - timedelta(hours=1),
                                    ends_at=self.now + timedelta(hours=1))
        response = self.client.get(self.url())
        self.assertNotIn('Subscription-Userinfo', response)
        self.assertIsNone(userinfo_for_service(self.record.user, self.record.public_id))

    def test_cycle_switch_does_not_reuse_previous_period_usage(self):
        boundary = self.now + timedelta(minutes=1)
        self.cycle.ends_at = boundary
        self.cycle.save(update_fields=['ends_at'])
        new = BillingCycle.objects.create(entitlement=self.record, starts_at=boundary,
                                         ends_at=boundary + timedelta(days=31))
        later = boundary + timedelta(seconds=20)
        with patch('django.utils.timezone.now', return_value=later):
            self.assertIsNone(userinfo_for_service(self.record.user, self.record.public_id))
            for identity in NodeIdentity.objects.all():
                self.sample(3, up=110, down=210, identity_id=identity.pk, at=boundary)
                self.sample(4, up=114, down=212, identity_id=identity.pk,
                            at=boundary + timedelta(seconds=10))
            new.refresh_from_db()
            self.assertEqual(new.used_bytes, 22)
            header = userinfo_for_service(self.record.user, self.record.public_id)
            self.assertIn('download=22;', header)
            self.assertNotIn('download=75;', header)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 75)


@override_settings(ROOT_URLCONF='megabox.urls', PANEL_LIVE=False, P8_COMPAT_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class P8SubscriptionHeaderTests(p8_fixture.P8FixtureMixin, TestCase):
    setUp = p8_fixture.P8EntitlementBindingTests.setUp
    target = p8_fixture.P8EntitlementBindingTests.target
    verify = p8_fixture.P8EntitlementBindingTests.verify
    seed_usage = p8_fixture.P8EntitlementBindingTests.seed_usage
    read = p8_fixture.P8EntitlementBindingTests.read
    protected = p8_fixture.P8EntitlementBindingTests.protected
    assert_original_delivery = p8_fixture.P8EntitlementBindingTests.assert_original_delivery

    def applied_target(self):
        target = self.target()
        self.verify(target)
        self.seed_usage(target)
        target.state = 'active'
        target.save(update_fields=['state'])
        self.member.provisioning_state, self.member.usage_state = 'applied', 'measured'
        self.member.used_bytes, self.member.usage_updated_at = 17, timezone.now()
        self.member.expires_at = timezone.now() + timedelta(days=30)
        self.member.save()
        return target

    def test_old_alias_and_eight_resource_urls_keep_same_source_without_supplier_read(self):
        target = self.applied_target()
        before = self.protected()
        with patch('portal.resource_usage_view.read_resource_usage_view') as supplier:
            for public_id in (self.source.public_id, self.member.public_id):
                header = userinfo_for_service(self.owner, public_id)
                self.assertIn('download=375;', header)
                self.assertNotIn('download=17;', header)
            self.assert_original_delivery()
        supplier.assert_not_called()
        self.assertEqual(before, self.protected())
        self.assertIsNone(userinfo_for_service(self.other, target.public_id))

    def test_p8_snapshot_keeps_measured_time_and_binds_original_manifest(self):
        self.applied_target()
        value = p8_usage_snapshot(self.owner, self.member.public_id, 'a' * 64)
        self.assertEqual(value['service_id'], str(self.source.public_id))
        self.assertEqual(value['manifest_sha256'], 'a' * 64)
        self.assertEqual(value['state'], 'available')
        self.assertIn('download=375;', value['userinfo'])
        self.assertLessEqual(value['measured_at'], value['generated_at'] + 30)
        self.assertNotIn('provider_usage', value)
        self.source.enabled = False
        self.source.save(update_fields=['enabled'])
        value = p8_usage_snapshot(self.owner, self.member.public_id, 'a' * 64)
        self.assertEqual(value['state'], 'unavailable')
        self.assertIsNone(value['userinfo'])
        self.assertIsNone(value['measured_at'])

    def test_missing_current_identity_sample_is_not_a_complete_header(self):
        target = self.applied_target()
        UsageLedger.objects.all().delete()
        self.assertIsNone(userinfo_for_service(self.owner, target.public_id))

    def test_old_django_signed_links_match_target_and_still_check_grants(self):
        self.applied_target()
        for device in ('windows', 'v2rayng', 'android'):
            grant = SubscriptionGrant.objects.create(membership=self.member, device=device)
            url = '/s/' + token_for(grant) + '/' + device
            with patch('portal.views.read_artifact', return_value=b'fake-original-content'):
                for method in (self.client.get, self.client.head):
                    response = method(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('download=375;', response['Subscription-Userinfo'])
                    # 消费测试客户端包装的流；重复close会错误关闭文件SQLite的TestCase事务。
                    b''.join(response.streaming_content)
                grant.enabled = False
                grant.save(update_fields=['enabled'])
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn('Subscription-Userinfo', response)

    def test_invalid_mapping_does_not_fall_back_to_member_or_another_owner(self):
        target = self.applied_target()
        self.assertIsNotNone(userinfo_for_service(self.owner, self.member.public_id))
        self.source.enabled = False
        self.source.save(update_fields=['enabled'])
        self.assertIsNone(userinfo_for_service(self.owner, self.member.public_id))
        self.assertIsNone(userinfo_for_service(self.owner, target.public_id))

    def test_unmapped_original_resources_still_work_without_inventing_usage(self):
        self.assertIsNone(userinfo_for_service(self.owner, self.source.public_id))
        self.assert_original_delivery()
        for client in ('android', 'windows', 'v2rayng'):
            self.client.force_login(self.owner)
            response = self.client.get(f'/api/v1/me/services/{self.source.public_id}/p8-delivery?client_adapter={client}')
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('Subscription-Userinfo', response)
