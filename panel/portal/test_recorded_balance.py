"""跨期账本余额可显示，但部分观测不能获得完整计量或订阅标头。"""
from copy import deepcopy
from datetime import datetime, timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from .api_helpers import project_service
from .billing_schedule import billing_state
from .models import BillingCycle, IngressUsageSample, UsageLedger
from .subscription_usage_headers import userinfo_for_service
from . import test_partial_observed as partial_fixture
from . import test_subscription_usage_headers as p8_fixture
from .usage_timeseries import SHANGHAI


@override_settings(ROOT_URLCONF='megabox.urls', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RecordedBalanceTests(TestCase):
    service = partial_fixture.PartialObservedTests.service
    rate_for = partial_fixture.PartialObservedTests.rate_for
    sample = partial_fixture.PartialObservedTests.sample

    def setUp(self):
        partial_fixture.PartialObservedTests.setUp(self)
        self.now = datetime(2026, 10, 19, 12, tzinfo=SHANGHAI)
        self.clock.stop()
        self.clock = patch('django.utils.timezone.now', return_value=self.now)
        self.clock_mock = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.record.activated_at = datetime(2026, 9, 20, tzinfo=SHANGHAI)
        self.record.expires_at = datetime(2027, 12, 30, tzinfo=SHANGHAI)
        self.record.save(update_fields=['activated_at', 'expires_at'])
        self.cycle.starts_at = self.record.activated_at
        self.cycle.ends_at = datetime(2026, 10, 20, tzinfo=SHANGHAI)
        self.cycle.save(update_fields=['starts_at', 'ends_at'])
        # 倍率版本只能追加；为本次旧期夹具新增合法授权，保留既有版本。
        self.rate = self.rate_for(self.record, '1.25', at=self.record.activated_at)

    def confirmed(self, upload=1040, download=2040, **changes):
        self.sample(**changes)
        return self.sample(2, upload, download, **changes)

    def read(self, path, actor=None):
        self.client.force_login(actor or self.record.user)
        response = self.client.get('/api/v1/' + path)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def view(self):
        self.record.refresh_from_db()
        return project_service(self.record, 'entitlement', now=self.now)

    def assert_amount(self, row, used, remaining):
        self.assertEqual(row['recorded_used_bytes'], used)
        self.assertEqual(row['recorded_remaining_bytes'], remaining)
        self.assertIsNone(row['remaining_bytes'])
        self.assertIsNone(row.get('used_bytes', row.get('charged_bytes')))

    def test_october_twentieth_rollover_uses_new_partial_postings_and_never_old_opening(self):
        self.confirmed()
        self.record.refresh_from_db()
        self.record.applied_snapshot['accepted_display_estimate'] = {
            'version': 1, 'owner_id': self.record.user_id, 'service_id': str(self.record.public_id),
            'cycle_starts_at': self.cycle.starts_at.isoformat(), 'cycle_ends_at': self.cycle.ends_at.isoformat(),
            'accepted_at': self.now.isoformat(), 'source_observed_at': (self.now - timedelta(minutes=1)).isoformat(),
            'source_sha256': 'a' * 64, 'approval_ref': 'recorded-balance-old-cycle-test',
            'charged_bytes': '98000000000', 'upload_bytes': '40000000000', 'download_bytes': '58000000000',
            'ledger_baseline': {'charged_bytes': '100', 'upload_bytes': '40', 'download_bytes': '40'},
        }
        self.record.save(update_fields=['applied_snapshot'])
        old = self.view()
        self.assertEqual(old['usage_estimate']['used_bytes'], '98000000000')
        self.assertEqual(old['usage_estimate']['remaining_bytes'], '902000000000')
        self.assert_amount(old, '100', '999999999900')
        old_cycle = self.cycle
        self.cycle = BillingCycle.objects.create(entitlement=self.record, starts_at=old_cycle.ends_at,
            ends_at=datetime(2026, 11, 20, tzinfo=SHANGHAI))
        self.now = datetime(2026, 10, 21, 12, tzinfo=SHANGHAI)
        self.clock_mock.return_value = self.now
        self.confirmed(epoch='新账期真实采样')
        service_id = str(self.record.public_id)
        rows = [self.read('me/services')['items'][0], self.read('me/services/' + service_id),
            self.read('admin/services', self.admin)['items'][0],
            self.read('admin/users/' + str(self.record.user_id), self.admin)['services'][0],
            self.read('me/services/' + service_id + '/usage')['summary'],
            self.read('admin/services/' + service_id + '/billing', self.admin)]
        for row in rows:
            self.assert_amount(row, '100', '999999999900')
            self.assertIsNone(row.get('usage_estimate'))
        diagnostic = self.read('admin/services/' + service_id + '/metering', self.admin)
        self.assertEqual(diagnostic['metering']['partial_observed_identity_count'], 1)
        self.assertEqual(diagnostic['metering']['evidence_identity_count'], 0)
        self.assertIsNone(userinfo_for_service(self.record.user, self.record.public_id, now=self.now))
        old_cycle.refresh_from_db()
        self.assertEqual(old_cycle.used_bytes, 100)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 100)

    def test_true_zero_and_over_quota_keep_amount_without_reliable_remaining(self):
        self.confirmed(upload=1000, download=2000)
        self.assert_amount(self.view(), '0', '1000000000000')
        self.sample(3, 2_000_000_001_000, 2000)
        self.assert_amount(self.view(), '2500000000000', '0')
        self.assertIsNone(userinfo_for_service(self.record.user, self.record.public_id, now=self.now))

    def test_baseline_only_is_unknown_not_full_balance(self):
        self.sample()
        self.assert_amount(self.view(), None, None)
        self.assert_amount(billing_state(self.admin, self.record.public_id), None, None)

    def test_unapplied_quota_does_not_turn_recorded_amount_into_balance(self):
        self.confirmed()
        self.record.applied_revision = 0
        self.record.save(update_fields=['applied_revision'])
        self.assert_amount(self.view(), '100', None)
        self.assert_amount(billing_state(self.admin, self.record.public_id), '100', None)

    def test_invalid_applied_quota_fails_closed_in_both_service_and_billing(self):
        self.confirmed()
        snapshot = deepcopy(self.record.applied_snapshot)
        for bad in (True, '1000000000000', -1, 2**63, None):
            with self.subTest(quota=bad):
                self.record.applied_snapshot = {**snapshot, 'quota_bytes': bad}
                self.record.save(update_fields=['applied_snapshot'])
                self.assert_amount(self.view(), '100', None)
                self.assert_amount(billing_state(self.admin, self.record.public_id), '100', None)

    def test_overlapping_or_no_current_cycle_never_uses_arbitrary_cycle(self):
        self.confirmed()
        extra = BillingCycle.objects.create(entitlement=self.record, starts_at=self.now - timedelta(hours=1),
            ends_at=self.now + timedelta(hours=1))
        self.assert_amount(self.view(), None, None)
        self.assert_amount(billing_state(self.admin, self.record.public_id), None, None)
        extra.delete()
        self.now = self.cycle.ends_at
        self.clock_mock.return_value = self.now
        self.assert_amount(self.view(), None, None)

    def test_rejected_or_foreign_sample_cannot_back_a_partial_balance(self):
        sample = self.confirmed()
        other, identity, _ = self.service('余额隔离乙')
        for changes in ({'status': 'gap'}, {'source_config_fingerprint': 'invalid'}, {'identity_id': identity.pk}):
            with self.subTest(changes=changes):
                IngressUsageSample.objects.filter(pk=sample.pk).update(status='accepted',
                    source_config_fingerprint='a'*64, identity_id=self.identity.pk)
                IngressUsageSample.objects.filter(pk=sample.pk).update(**changes)
                self.assert_amount(self.view(), None, None)
                self.assert_amount(billing_state(self.admin, self.record.public_id), None, None)
        self.client.force_login(other.user)
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(self.record.public_id)).status_code, 404)

    def test_foreign_cycle_total_cannot_replace_confirmed_personal_amount(self):
        self.confirmed()
        self.cycle.used_bytes = 987654321
        self.cycle.save(update_fields=['used_bytes'])
        self.assert_amount(self.view(), '100', '999999999900')
        self.assert_amount(billing_state(self.admin, self.record.public_id), '100', '999999999900')

    def test_projection_is_read_only_and_cannot_clear_gap_or_upgrade_header(self):
        self.confirmed()
        before = (list(UsageLedger.objects.values()), list(BillingCycle.objects.values()))
        with CaptureQueriesContext(connection) as queries:
            service = self.view()
            billing = billing_state(self.admin, self.record.public_id)
            header = userinfo_for_service(self.record.user, self.record.public_id, now=self.now)
        self.assertEqual(service['usage']['quality'], 'gap')
        self.assertNotEqual(billing['usage_quality'], 'fresh')
        self.assertIsNone(header)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for q in queries))
        self.assertEqual(before, (list(UsageLedger.objects.values()), list(BillingCycle.objects.values())))


@override_settings(ROOT_URLCONF='megabox.urls', PANEL_LIVE=False, P8_COMPAT_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RecordedBalanceAliasTests(p8_fixture.p8_fixture.P8FixtureMixin, TestCase):
    setUp = p8_fixture.P8SubscriptionHeaderTests.setUp
    target = p8_fixture.P8SubscriptionHeaderTests.target
    verify = p8_fixture.P8SubscriptionHeaderTests.verify
    seed_usage = p8_fixture.P8SubscriptionHeaderTests.seed_usage
    applied_target = p8_fixture.P8SubscriptionHeaderTests.applied_target

    def test_original_p8_alias_balance_matches_target_and_broken_mapping_fails_closed(self):
        target = self.applied_target()
        target.metering_gap = True
        target.save(update_fields=['metering_gap'])
        expected = str(target.applied_snapshot['quota_bytes'] - 375)
        self.client.force_login(self.owner)
        for public_id in (self.source.public_id, self.member.public_id):
            response = self.client.get('/api/v1/me/services/' + str(public_id))
            self.assertEqual(response.status_code, 200)
            row = response.json()['data']
            self.assertEqual(row['recorded_used_bytes'], '375')
            self.assertEqual(row['recorded_remaining_bytes'], expected)
            self.assertIsNone(row['used_bytes'])
            self.assertIsNone(row['remaining_bytes'])
            self.assertIsNone(userinfo_for_service(self.owner, public_id))
        self.source.enabled = False
        self.source.save(update_fields=['enabled'])
        for path in ('me/services/' + str(self.member.public_id),
                     'me/services/' + str(self.source.public_id) + '/usage'):
            response = self.client.get('/api/v1/' + path)
            self.assertEqual(response.status_code, 200)
            row = response.json()['data']
            row = row.get('summary', row)
            self.assertIsNone(row['recorded_used_bytes'])
            self.assertIsNone(row['recorded_remaining_bytes'])
