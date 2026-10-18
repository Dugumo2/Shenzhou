"""服务登记、有限管理员诊断与旧计量安全门禁的隔离反例。"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import include, path

from .api_helpers import project_service
from .metering_diagnostics import service_metering_diagnostics
from .models import BillingCycle, ServiceRateVersion, UsageLedger, UsageStream
from .subscription_usage_headers import userinfo_for_service
from .test_billing_schedule import BillingFixture, GB
from .usage_api import admin_service_metering
from . import test_partial_observed as partial_fixtures
from . import test_subscription_usage_headers as p8_fixtures


urlpatterns = [path('api/v1/admin/services/<uuid:service_id>/metering', admin_service_metering),
               path('api/v1/', include('portal.api_urls'))]


@override_settings(ROOT_URLCONF='portal.test_metering_diagnostics', PANEL_LIVE=False,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class MeteringDiagnosticsTests(BillingFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.record.state = 'active'
        self.record.save(update_fields=['state'])
        self.url = f'/api/v1/admin/services/{self.record.public_id}/metering'
        self.client.force_login(self.admin)

    def data(self, query=''):
        response = self.client.get(self.url + query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def test_applied_service_status_does_not_promote_gap_or_legacy_delivery(self):
        self.record.metering_gap = True
        self.record.save(update_fields=['metering_gap'])
        row = project_service(self.record, 'entitlement', now=self.now)
        self.assertEqual(row['service_status'], {'state': 'active', 'label': '已应用', 'message': None})
        self.assertEqual(row['business_state'], 'metering_gap')
        self.assertEqual(row['usage']['quality'], 'gap')
        self.assertEqual(row['delivery']['state'], 'blocked')
        self.assertIsNone(row['remaining_bytes'])
        self.assertIsNone(userinfo_for_service(self.user, self.record.public_id, now=self.now))
        old_filter = self.client.get('/api/v1/admin/services?state=metering_gap').json()['data']
        self.assertEqual(old_filter['items'][0]['id'], str(self.record.public_id))
        self.assertEqual(self.client.get('/api/v1/admin/services?state=active').json()['data']['items'], [])

    def test_all_user_and_admin_projections_share_registration_status(self):
        self.record.metering_gap = True
        self.record.save(update_fields=['metering_gap'])
        paths = ((self.user, 'me/services'), (self.user, f'me/services/{self.record.public_id}'),
                 (self.admin, 'admin/services'), (self.admin, f'admin/users/{self.user.pk}'))
        for actor, path in paths:
            with self.subTest(path=path):
                self.client.force_login(actor)
                data = self.client.get('/api/v1/' + path).json()['data']
                row = data['items'][0] if 'items' in data else data['services'][0] if 'services' in data else data
                self.assertEqual(row['service_status']['state'], 'active')
                self.assertEqual(row['usage']['quality'], 'gap')
                self.assertNotIn('metering', row)

    def test_unapplied_disabled_expired_and_isolated_never_turn_active(self):
        cases = [({'enabled': False}, 'disabled'), ({'expires_at': self.now}, 'expired'),
                 ({'state': 'simulated'}, 'simulated'), ({'state': 'failed'}, 'failed'),
                 ({'revision': 5}, 'pending'), ({'expires_at': None}, 'verification_required'),
                 ({'applied_snapshot': {}}, 'verification_required')]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.record.refresh_from_db()
                self.record.metering_gap = True
                for key, value in changes.items():
                    setattr(self.record, key, value)
                row = project_service(self.record, 'entitlement', now=self.now)
                self.assertEqual(row['service_status']['state'], expected)
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.record.refresh_from_db()
        self.assertEqual(project_service(self.record, 'entitlement')['service_status']['state'], 'account_disabled')

    def test_unknown_and_stale_quality_do_not_replace_applied_registration(self):
        self.ledger.observed_at = self.now - timedelta(minutes=4)
        self.ledger.save(update_fields=['observed_at'])
        row = project_service(self.record, 'entitlement')
        self.assertEqual(row['service_status']['state'], 'active')
        self.assertEqual(row['usage']['quality'], 'stale')
        self.ledger.delete()
        row = project_service(self.record, 'entitlement')
        self.assertEqual(row['service_status']['state'], 'active')
        self.assertEqual(row['usage']['quality'], 'unknown')
        self.assertIsNone(row['used_bytes'])

    def test_admin_permissions_include_current_database_revocation_and_read_only_method(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)
        for actor in (self.user, self.bob):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        request = RequestFactory().get(self.url)
        request.user = self.admin
        get_user_model().objects.filter(pk=self.admin.pk).update(is_staff=False)
        self.assertEqual(admin_service_metering(request, self.record.public_id).status_code, 403)

    def test_filters_are_bounded_and_private_payload_is_absent(self):
        for query in ('?period=current', '?period=7d&period=year', '?cursor=anything', '?owner_id=1'):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.url + query).status_code, 422)
        data = self.data('?period=year')
        self.assertEqual(set(data), {'service_id', 'period', 'generated_at', 'service_status', 'metering',
                                     'historical_baseline', 'coverage', 'limitations'})
        self.assertEqual(set(data['metering']), {'quality', 'evidence_quality', 'updated_at', 'persisted_gap',
            'active_identity_count', 'evidence_identity_count', 'unverified_identity_count',
            'ambiguous_identity_count', 'partial_observed_identity_count', 'reason_codes'})
        self.assertEqual(data['historical_baseline'], {'state': 'none', 'charged_bytes': None, 'source_observed_at': None})
        self.assertNotIn('epoch', json.dumps(data))
        self.assertNotIn('fingerprint', json.dumps(data))
        self.assertTrue(all(value is False for value in data['limitations'].values()))

    def test_read_does_not_write_or_expose_diagnostics_on_owner_usage(self):
        before = self.protected_values()
        with CaptureQueriesContext(connection) as queries:
            self.data()
        self.assertEqual(before, self.protected_values())
        self.assertFalse(any(row['sql'].lstrip().upper().startswith(('INSERT ', 'UPDATE ', 'DELETE ')) for row in queries))
        self.client.force_login(self.user)
        payload = self.client.get(f'/api/v1/me/services/{self.record.public_id}/usage?period=7d').json()['data']
        self.assertNotIn('metering', payload)
        self.assertNotIn('historical_baseline', payload)

    def test_partial_label_is_not_complete_evidence_or_asserted_collection_failure(self):
        self.ledger.quality = 'partial_observed'
        self.ledger.save(update_fields=['quality'])
        data = self.data()
        self.assertEqual(data['service_status']['state'], 'active')
        self.assertEqual(data['metering']['quality'], 'unknown')
        self.assertEqual(data['metering']['partial_observed_identity_count'], 1)
        self.assertEqual(data['metering']['evidence_identity_count'], 0)
        self.assertIn('partial_observed_evidence', data['metering']['reason_codes'])
        self.assertIn('missing_complete_evidence', data['metering']['reason_codes'])
        self.assertFalse(data['metering']['persisted_gap'])

    def test_overlap_and_ambiguous_stream_are_separate_actual_causes(self):
        UsageStream.objects.create(identity=self.identity, epoch='another-current')
        BillingCycle.objects.create(entitlement=self.record, starts_at=self.now-timedelta(hours=1),
                                    ends_at=self.now+timedelta(hours=1))
        reasons = self.data()['metering']['reason_codes']
        self.assertIn('overlapping_cycles', reasons)
        self.assertIn('ambiguous_streams', reasons)
        self.record.refresh_from_db()
        self.assertFalse(self.record.metering_gap)

    def test_accepted_baseline_stays_in_its_current_cycle_and_not_timeseries(self):
        self.record.metering_gap = True
        accepted = {'version': 1, 'owner_id': self.user.pk, 'service_id': str(self.record.public_id),
            'cycle_starts_at': self.cycle.starts_at.isoformat(), 'cycle_ends_at': self.cycle.ends_at.isoformat(),
            'accepted_at': self.now.isoformat(), 'source_observed_at': (self.now-timedelta(minutes=1)).isoformat(),
            'source_sha256': 'a'*64, 'approval_ref': 'test-approved', 'charged_bytes': str(10*GB),
            'upload_bytes': str(4*GB), 'download_bytes': str(6*GB),
            'ledger_baseline': {'charged_bytes': str(30*GB), 'upload_bytes': str(12*GB), 'download_bytes': str(12*GB)}}
        self.record.applied_snapshot['accepted_display_estimate'] = accepted
        self.record.save(update_fields=['metering_gap', 'applied_snapshot'])
        data = self.data()
        self.assertEqual(data['historical_baseline']['charged_bytes'], str(10*GB))
        self.assertEqual(data['historical_baseline']['state'], 'applied')
        self.assertIsNone(data['coverage']['unallocated_charged_bytes'])
        later = service_metering_diagnostics(self.record, self.record.public_id, '7d', self.cycle.ends_at)
        self.assertEqual(later['historical_baseline']['state'], 'none')
        accepted['owner_id'] = self.bob.pk
        self.record.save(update_fields=['applied_snapshot'])
        self.assertEqual(self.data()['historical_baseline']['state'], 'none')

    def test_cross_day_interval_is_unallocated_not_lost_or_distributed(self):
        rate = ServiceRateVersion.objects.create(entitlement=self.record, line=self.line, ingress=self.ingress,
            multiplier='1', effective_at=self.cycle.starts_at, actor=self.admin, reason='隔离测试')
        UsageLedger.objects.create(cycle=self.cycle, stream=self.stream, sequence=2, upload_delta=3,
            download_delta=4, weighted_bytes=7, quality='metered', observed_at=self.now,
            interval_start=self.now-timedelta(days=1), interval_end=self.now, grant_rate_version=rate)
        coverage = self.data()['coverage']
        self.assertEqual(coverage['unallocated_charged_bytes'], '7')
        self.assertEqual(coverage['message_code'], 'unallocated_intervals')

    def test_fresh_sample_tail_is_reported_without_asserting_actual_gap(self):
        rate = ServiceRateVersion.objects.create(entitlement=self.record, line=self.line, ingress=self.ingress,
            multiplier='1', effective_at=self.cycle.starts_at, actor=self.admin, reason='隔离测试')
        start = self.now.replace(hour=0, minute=0, second=0, microsecond=0)-timedelta(days=6)
        for index in range(7):
            left = start + timedelta(days=index)
            right = min(left + timedelta(days=1), self.now-timedelta(seconds=30))
            UsageLedger.objects.create(cycle=self.cycle, stream=self.stream, sequence=index+2, upload_delta=0,
                download_delta=0, weighted_bytes=0, quality='metered', observed_at=right,
                interval_start=left, interval_end=right, grant_rate_version=rate)
        coverage = self.data()['coverage']
        self.assertEqual(coverage['message_code'], 'sampling_pending')
        self.assertEqual(coverage['unallocated_charged_bytes'], '0')


class ConfirmedPartialDiagnosticsTests(TestCase):
    setUp = partial_fixtures.PartialObservedTests.setUp
    service = partial_fixtures.PartialObservedTests.service
    rate_for = partial_fixtures.PartialObservedTests.rate_for
    sample = partial_fixtures.PartialObservedTests.sample

    def test_actual_partial_amount_is_preserved_without_full_coverage_or_header(self):
        self.sample()
        self.sample(2, 1040, 2040)
        self.record.refresh_from_db()
        self.record.state = 'active'
        self.record.save(update_fields=['state'])
        before = list(UsageLedger.objects.values())
        data = service_metering_diagnostics(self.record, self.record.public_id, '7d', self.now)
        self.assertEqual(data['service_status']['state'], 'active')
        self.assertEqual(data['metering']['partial_observed_identity_count'], 1)
        self.assertEqual(data['metering']['quality'], 'gap')
        self.assertEqual(data['coverage']['state'], 'partial')
        self.assertEqual(data['coverage']['message_code'], 'partial_coverage')
        projection = project_service(self.record, 'entitlement')
        self.assertEqual(projection['recorded_used_bytes'], '100')
        self.assertIsNone(userinfo_for_service(self.record.user, self.record.public_id))
        self.assertEqual(before, list(UsageLedger.objects.values()))


@override_settings(PANEL_LIVE=False, P8_COMPAT_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class P8MeteringDiagnosticsTests(p8_fixtures.p8_fixture.P8FixtureMixin, TestCase):
    setUp = p8_fixtures.P8SubscriptionHeaderTests.setUp
    target = p8_fixtures.P8SubscriptionHeaderTests.target
    verify = p8_fixtures.P8SubscriptionHeaderTests.verify
    seed_usage = p8_fixtures.P8SubscriptionHeaderTests.seed_usage
    applied_target = p8_fixtures.P8SubscriptionHeaderTests.applied_target

    def response(self, public_id, actor=None):
        request = RequestFactory().get('/api/v1/admin/services/x/metering')
        request.user = actor or self.admin
        return admin_service_metering(request, public_id)

    def test_canonical_only_and_invalid_owner_mapping_fail_closed_without_private_reads(self):
        target = self.applied_target()
        target.metering_gap = True
        target.save(update_fields=['metering_gap'])
        with patch('portal.p8_compat._read_private_file') as private:
            response = self.response(self.source.public_id)
            self.assertEqual(response.status_code, 200, response.content)
            data = json.loads(response.content)['data']
            self.assertEqual(data['service_id'], str(self.source.public_id))
            self.assertEqual(data['service_status']['state'], 'active')
            self.assertEqual(data['metering']['quality'], 'gap')
            self.assertEqual(self.response(self.member.public_id).status_code, 404)
            self.assertEqual(self.response(self.source.public_id, self.owner).status_code, 403)
            target.user = get_user_model().objects.create_user('诊断错归属用户')
            target.save(update_fields=['user'])
            self.assertEqual(self.response(self.source.public_id).status_code, 404)
            private.assert_not_called()
