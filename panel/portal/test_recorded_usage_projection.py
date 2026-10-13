"""部分确认金额的同源/归属/零与未知反例；仅隔离数据库，不改计费。"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from .api_helpers import project_service
from .models import BillingCycle, DeviceSubscription, Entitlement, NodeIdentity, UsageLedger, UsageStream
from .subscription_usage_headers import userinfo_for_service
from .test_billing_schedule import BillingFixture, GB
from . import test_subscription_usage_headers as p8_fixtures

FIELDS = ('recorded_used_bytes', 'recorded_upload_bytes', 'recorded_download_bytes')


@override_settings(ROOT_URLCONF='megabox.urls', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RecordedUsageProjectionTests(BillingFixture, TestCase):
    def partial(self):
        NodeIdentity.objects.create(subscription=self.sub, line=self.line, ingress=self.ingress,
                                    generation=2, state='candidate')

    def read(self, path, actor=None):
        self.client.force_login(actor or self.user)
        response = self.client.get('/api/v1/' + path)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def test_partial_amount_matches_list_detail_usage_and_two_admin_views_without_writes(self):
        self.partial()
        with patch('portal.resource_usage_view.read_resource_usage_view') as supplier:
            rows = [self.read('me/services')['items'][0], self.read('me/services/' + str(self.record.public_id)),
                self.read('admin/services', self.admin)['items'][0],
                self.read('admin/users/' + str(self.user.pk), self.admin)['services'][0]]
            usage = self.read('me/services/' + str(self.record.public_id) + '/usage')['summary']
        with CaptureQueriesContext(connection) as queries:
            project_service(self.record, 'entitlement')
            self.assertIsNone(userinfo_for_service(self.user, self.record.public_id))
        supplier.assert_not_called()
        for row in rows + [usage]:
            self.assertEqual([row[key] for key in FIELDS], [str(30 * GB), str(12 * GB), str(12 * GB)])
            self.assertIsNone(row['remaining_bytes'])
            self.assertIsNone(row.get('used_bytes', row.get('charged_bytes')))
        self.assertFalse(any(row['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for row in queries))
        self.assertIsNone(userinfo_for_service(self.user, self.record.public_id))

    def test_confirmed_zero_is_zero_without_claiming_full_usage_or_remaining(self):
        self.partial()
        self.ledger.upload_delta = self.ledger.download_delta = self.ledger.weighted_bytes = 0
        self.ledger.save(update_fields=['upload_delta', 'download_delta', 'weighted_bytes'])
        row = project_service(self.record, 'entitlement')
        self.assertEqual([row[key] for key in FIELDS], ['0', '0', '0'])
        self.assertIsNone(row['used_bytes'])
        self.assertIsNone(row['remaining_bytes'])

    def test_no_records_or_no_unique_current_cycle_is_unknown_not_zero(self):
        self.ledger.delete()
        row = project_service(self.record, 'entitlement')
        self.assertEqual([row[key] for key in FIELDS], [None] * 3)
        BillingCycle.objects.create(entitlement=self.record, starts_at=self.now - timedelta(hours=1),
                                    ends_at=self.now + timedelta(hours=1))
        row = project_service(self.record, 'entitlement')
        self.assertEqual([row[key] for key in FIELDS], [None] * 3)

    def test_foreign_identity_cycle_unconfirmed_and_future_rows_are_excluded(self):
        other = get_user_model().objects.create_user('recorded-foreign')
        record = Entitlement.objects.create(user=other, quota_bytes=GB)
        subscription = DeviceSubscription.objects.create(entitlement=record, name='假外部服务', client='windows')
        identity = NodeIdentity.objects.create(subscription=subscription, line=self.line, ingress=self.ingress, generation=1)
        stream = UsageStream.objects.create(identity=identity, epoch='foreign')
        UsageLedger.objects.create(cycle=self.cycle, stream=stream, sequence=1, upload_delta=10 * GB,
            download_delta=10 * GB, weighted_bytes=20 * GB, quality='metered', observed_at=self.now)
        for sequence, quality, observed in ((2, 'legacy_unknown', self.now),
                                           (3, 'metered', self.now + timedelta(seconds=31))):
            UsageLedger.objects.create(cycle=self.cycle, stream=self.stream, sequence=sequence,
                upload_delta=GB, download_delta=GB, weighted_bytes=2 * GB, quality=quality, observed_at=observed)
        old = BillingCycle.objects.create(entitlement=self.record, starts_at=self.now - timedelta(days=60),
            ends_at=self.now - timedelta(days=31))
        UsageLedger.objects.create(cycle=old, stream=self.stream, sequence=4, upload_delta=GB,
            download_delta=GB, weighted_bytes=2 * GB, quality='metered', observed_at=old.starts_at)
        row = project_service(self.record, 'entitlement')
        self.assertEqual([row[key] for key in FIELDS], [str(30 * GB), str(12 * GB), str(12 * GB)])
        self.client.force_login(other)
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(self.record.public_id)).status_code, 404)


@override_settings(ROOT_URLCONF='megabox.urls', PANEL_LIVE=False, P8_COMPAT_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RecordedP8ProjectionTests(p8_fixtures.p8_fixture.P8FixtureMixin, TestCase):
    setUp = p8_fixtures.P8SubscriptionHeaderTests.setUp
    target = p8_fixtures.P8SubscriptionHeaderTests.target
    verify = p8_fixtures.P8SubscriptionHeaderTests.verify
    seed_usage = p8_fixtures.P8SubscriptionHeaderTests.seed_usage
    applied_target = p8_fixtures.P8SubscriptionHeaderTests.applied_target

    def test_p8_alias_keeps_recorded_amount_and_broken_mapping_clears_every_field(self):
        target = self.applied_target()
        target.metering_gap = True
        target.save(update_fields=['metering_gap'])
        self.client.force_login(self.owner)
        for public_id in (self.source.public_id, self.member.public_id):
            detail = self.client.get('/api/v1/me/services/' + str(public_id)).json()['data']
            self.assertEqual([detail[key] for key in FIELDS], ['375', '100', '200'])
            self.assertIsNone(detail['used_bytes'])
            self.assertIsNone(userinfo_for_service(self.owner, public_id))
        self.source.enabled = False
        self.source.save(update_fields=['enabled'])
        for path in ('me/services/' + str(self.member.public_id),
                     'me/services/' + str(self.source.public_id) + '/usage'):
            row = self.client.get('/api/v1/' + path).json()['data']
            value = row.get('summary', row)
            self.assertEqual([value[key] for key in FIELDS], [None] * 3)
