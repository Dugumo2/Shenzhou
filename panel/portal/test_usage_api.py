"""R03/R08 本人流量概览的隔离场景；不连接真实采集器或生产。"""
import hashlib
from datetime import timedelta

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path
from django.utils.dateparse import parse_datetime

from .legacy_binding import verify_legacy_binding
from .models import BillingCycle, Entitlement, Membership, NodeIdentity, UsageLedger
from .test_billing_schedule import BillingFixture, GB
from .usage_api import DAY_LIMIT, service_usage


urlpatterns = [path('api/v1/me/services/<str:public_id>/usage', service_usage)]


@override_settings(ROOT_URLCONF='portal.test_usage_api', PANEL_LIVE=False,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class UsageAPITests(BillingFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        self.url = f'/api/v1/me/services/{self.record.public_id}/usage'

    def data(self, period='current', url=None):
        response = self.client.get((url or self.url) + '?period=' + period)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()['error'])
        return response.json()['data']

    def add_entry(self, sequence=2, *, cycle=None, upload=100, download=200, charged=375,
                  quality='metered', observed=None, posted=None):
        entry = UsageLedger.objects.create(cycle=cycle or self.cycle, stream=self.stream,
            sequence=sequence, upload_delta=upload, download_delta=download, weighted_bytes=charged,
            quality=quality, rate_version=self.rate, observed_at=observed or self.now)
        if posted is not None:
            UsageLedger.objects.filter(pk=entry.pk).update(created_at=posted)
        return entry

    def test_current_summary_distinguishes_raw_and_charged_bytes(self):
        data = self.data()
        self.assertEqual(data['service_id'], str(self.record.public_id))
        self.assertEqual(data['time_zone'], 'Asia/Shanghai')
        self.assertEqual(data['summary'], {'quota_bytes': str(100 * GB), 'quota_state': 'applied',
            'charged_bytes': str(30 * GB), 'upload_bytes': str(12 * GB), 'download_bytes': str(12 * GB),
            'remaining_bytes': str(70 * GB), 'next_reset_at': data['summary']['next_reset_at']})
        self.assertEqual(parse_datetime(data['summary']['next_reset_at']), self.cycle.ends_at)
        self.assertEqual(data['quality']['state'], 'measured')
        self.assertEqual(parse_datetime(data['quality']['collected_at']), self.now)
        self.assertEqual(parse_datetime(data['current_cycle']['starts_at']), self.cycle.starts_at)
        self.assertEqual(parse_datetime(data['current_cycle']['ends_at']), self.cycle.ends_at)
        self.assertEqual(data['history']['totals']['charged_bytes'], str(30 * GB))

    def test_historical_charges_are_not_recalculated_with_new_line_multiplier(self):
        self.rate.multiplier = '7.00'
        self.rate.save(update_fields=['multiplier'])
        data = self.data()
        self.assertEqual(data['history']['totals']['charged_bytes'], str(30 * GB))
        self.assertEqual(data['summary']['charged_bytes'], str(30 * GB))

    def test_read_only_get_never_resets_cycles_queues_jobs_or_writes(self):
        before = self.protected_values()
        with CaptureQueriesContext(connection) as queries:
            for period in ('current', '7d', '30d'):
                self.data(period)
        self.assertEqual(before, self.protected_values())
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                             for q in queries))

    def test_missing_cycle_keeps_unknown_and_does_not_create_one(self):
        self.cycle.starts_at = self.now + timedelta(days=1)
        self.cycle.ends_at += timedelta(days=1)
        self.cycle.save(update_fields=['starts_at', 'ends_at'])
        data = self.data()
        self.assertIsNone(data['current_cycle'])
        self.assertIsNone(data['summary']['charged_bytes'])
        self.assertIsNone(data['summary']['upload_bytes'])
        self.assertIsNone(data['summary']['remaining_bytes'])
        self.assertIsNone(data['history']['totals'])
        self.assertEqual(self.record.cycles.count(), 1)

    def test_expired_statistics_preserve_confirmed_charge_but_not_remaining(self):
        observed = self.now - timedelta(minutes=4)
        self.record.usage_updated_at = observed
        self.record.save(update_fields=['usage_updated_at'])
        self.ledger.observed_at = observed
        self.ledger.save(update_fields=['observed_at'])
        data = self.data()
        self.assertEqual(data['quality']['state'], 'stale')
        self.assertEqual(parse_datetime(data['quality']['collected_at']), observed)
        self.assertEqual(data['summary']['charged_bytes'], str(30 * GB))
        self.assertIsNone(data['summary']['remaining_bytes'])

    def test_unobserved_entry_is_gap_and_keeps_only_confirmed_raw_subtotal(self):
        NodeIdentity.objects.create(subscription=self.sub, line=self.line,
            ingress=self.ingress, generation=2, state='simulated')
        data = self.data()
        self.assertEqual(data['quality']['state'], 'gap')
        self.assertIsNone(data['summary']['charged_bytes'])
        self.assertIsNone(data['summary']['remaining_bytes'])
        self.assertEqual(data['summary']['upload_bytes'], str(12 * GB))
        self.assertEqual(data['history']['totals']['charged_bytes'], str(30 * GB))
        self.record.refresh_from_db()
        self.assertFalse(self.record.metering_gap)

    def test_overlapping_current_cycles_do_not_guess_an_active_period(self):
        BillingCycle.objects.create(entitlement=self.record, starts_at=self.now - timedelta(hours=1),
                                    ends_at=self.now + timedelta(days=1), used_bytes=1)
        data = self.data()
        self.assertEqual(data['quality']['state'], 'gap')
        self.assertIsNone(data['current_cycle'])
        self.assertEqual(data['history']['days'], [])

    def test_no_ledger_records_remains_null_rather_than_zero(self):
        self.ledger.delete()
        data = self.data('7d')
        self.assertEqual(data['quality']['state'], 'unknown')
        for field in ('charged_bytes', 'upload_bytes', 'download_bytes', 'remaining_bytes'):
            self.assertIsNone(data['summary'][field])
        self.assertIsNone(data['history']['totals'])
        self.assertEqual(data['history']['days'], [])

    def test_saved_zero_record_is_shown_without_inventing_other_days(self):
        UsageLedger.objects.filter(pk=self.ledger.pk).update(upload_delta=0, download_delta=0, weighted_bytes=0)
        self.cycle.used_bytes, self.cycle.raw_bytes = 0, 0
        self.cycle.save(update_fields=['used_bytes', 'raw_bytes'])
        data = self.data('7d')
        self.assertEqual(data['history']['totals'], {'upload_bytes': '0', 'download_bytes': '0', 'charged_bytes': '0'})
        self.assertEqual([day['date'] for day in data['history']['days']], ['2028-01-10'])

    def test_delayed_sample_is_grouped_only_by_posting_date(self):
        UsageLedger.objects.filter(pk=self.ledger.pk).update(created_at=self.now - timedelta(days=1))
        self.add_entry(observed=self.now - timedelta(days=8), posted=self.now)
        data = self.data('7d')
        self.assertEqual(data['history']['date_basis'], 'created_at')
        self.assertEqual([day['date'] for day in data['history']['days']], ['2028-01-09', '2028-01-10'])
        self.assertEqual(data['history']['days'][1]['charged_bytes'], '375')
        self.assertNotIn('2028-01-02', [day['date'] for day in data['history']['days']])
        self.assertIn('入账日期不代表流量发生日期', data['history']['message'])

    def test_shanghai_natural_day_filter_includes_boundary_and_excludes_previous_second(self):
        boundary = self.now.replace(hour=0, minute=0) - timedelta(days=6)
        self.add_entry(sequence=2, posted=boundary)
        self.add_entry(sequence=3, posted=boundary - timedelta(seconds=1))
        self.add_entry(sequence=4, posted=self.now - timedelta(days=29))
        seven = self.data('7d')
        self.assertEqual(seven['history']['range_start'], boundary.isoformat())
        self.assertEqual(seven['history']['record_count'], 2)
        self.assertEqual([day['date'] for day in seven['history']['days']], ['2028-01-04', '2028-01-10'])
        thirty = self.data('30d')
        self.assertEqual(thirty['history']['record_count'], 4)

    def test_history_can_include_previous_cycle_posted_inside_selected_range(self):
        old = BillingCycle.objects.create(entitlement=self.record,
            starts_at=self.cycle.starts_at - timedelta(days=30), ends_at=self.cycle.starts_at,
            used_bytes=375, raw_bytes=300)
        self.add_entry(cycle=old, observed=old.ends_at, posted=self.now - timedelta(days=2))
        self.assertEqual(self.data()['history']['record_count'], 1)
        history = self.data('7d')['history']
        self.assertEqual(history['record_count'], 2)
        self.assertEqual(history['totals']['charged_bytes'], str(30 * GB + 375))

    def test_history_does_not_include_unknown_unweighted_or_future_observed_samples(self):
        self.add_entry(sequence=2, quality='legacy_unknown')
        self.add_entry(sequence=3, charged=None)
        self.add_entry(sequence=4, observed=self.now + timedelta(minutes=1))
        history = self.data('7d')['history']
        self.assertEqual(history['record_count'], 1)
        self.assertEqual(history['excluded_record_count'], 3)
        self.assertEqual(history['totals']['charged_bytes'], str(30 * GB))

    def test_current_days_are_bounded_but_totals_cover_all_confirmed_records(self):
        self.cycle.starts_at = self.now - timedelta(days=200)
        self.cycle.save(update_fields=['starts_at'])
        for index in range(1, DAY_LIMIT + 1):
            self.add_entry(sequence=index + 1, posted=self.now - timedelta(days=index))
        history = self.data()['history']
        self.assertTrue(history['days_truncated'])
        self.assertEqual(history['day_limit'], DAY_LIMIT)
        self.assertEqual(len(history['days']), DAY_LIMIT)
        self.assertEqual(history['record_count'], DAY_LIMIT + 1)
        self.assertEqual(history['totals']['charged_bytes'], str(30 * GB + DAY_LIMIT * 375))
        self.assertEqual(history['days'][-1]['date'], '2028-01-10')

    def test_large_byte_values_are_strings_with_no_javascript_rounding(self):
        value = 9_007_199_254_740_993
        UsageLedger.objects.filter(pk=self.ledger.pk).update(upload_delta=value, weighted_bytes=value)
        history = self.data()['history']
        self.assertEqual(history['totals']['upload_bytes'], str(value))
        self.assertEqual(history['days'][0]['charged_bytes'], str(value))

    def test_legacy_service_has_clear_unknown_state_and_never_borrows_entitlement_ledger(self):
        legacy = Membership.objects.create(user=self.bob, status='active', quota_bytes=50 * GB,
                                           used_bytes=123, expires_at=self.now + timedelta(days=7))
        self.client.force_login(self.bob)
        data = self.data('30d', f'/api/v1/me/services/{legacy.public_id}/usage')
        self.assertEqual(data['source_type'], 'membership')
        self.assertEqual(data['quality']['state'], 'unknown')
        self.assertIsNone(data['current_cycle'])
        self.assertIsNone(data['summary']['charged_bytes'])
        self.assertIsNone(data['summary']['remaining_bytes'])
        self.assertIsNone(data['history']['totals'])
        self.assertEqual(data['history']['days'], [])

    def test_verified_old_service_alias_uses_same_canonical_usage(self):
        legacy = Membership.objects.create(user=self.user, status='active', quota_bytes=50 * GB)
        verify_legacy_binding(self.admin, legacy.pk, entitlement_id=self.record.pk,
            evidence_sha256=hashlib.sha256(b'usage-alias-synthetic-owner').hexdigest())
        self.assertEqual(self.data(), self.data(url=f'/api/v1/me/services/{legacy.public_id}/usage'))
        legacy.revision += 1
        legacy.save(update_fields=['revision'])
        self.assertEqual(self.client.get(f'/api/v1/me/services/{legacy.public_id}/usage').status_code, 404)

    def test_owner_only_and_staff_cannot_view_another_user_through_me_endpoint(self):
        for user in (self.bob, self.admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_login(self.user)
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_invalid_period_duplicate_period_bad_uuid_and_write_are_rejected(self):
        for query in ('period=day', 'period=', 'period=7d&period=30d'):
            response = self.client.get(self.url + '?' + query)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()['error']['code'], 'invalid_filter')
        self.assertEqual(self.client.get('/api/v1/me/services/not-a-uuid/usage').status_code, 404)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.assertEqual(self.client.get(self.url).json()['data']['period'], 'current')

    def test_mismatched_identity_from_another_service_is_not_counted_in_history(self):
        other = Entitlement.objects.create(user=self.bob, quota_bytes=10 * GB)
        old = BillingCycle.objects.create(entitlement=other, starts_at=self.now - timedelta(days=1),
                                         ends_at=self.now + timedelta(days=1))
        self.add_entry(cycle=old)
        self.assertEqual(self.data('7d')['history']['record_count'], 1)

    @override_settings(ROOT_URLCONF='portal.test_api')
    def test_integrated_api_url_reaches_owner_only_usage_endpoint(self):
        data = self.data('7d')
        self.assertEqual(data['history']['kind'], 'confirmed_ledger_postings')
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(self.url).status_code, 404)
