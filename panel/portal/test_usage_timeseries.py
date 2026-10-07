"""Q5 自然时间、账期重置、区间覆盖和封闭响应的独立假资产验收。"""
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .models import (BillingCycle, DeviceSubscription, Egress, Entitlement, Ingress, Line,
                     NodeIdentity, Server, ServiceRateVersion, UsageLedger, UsageStream)
from .usage_timeseries import (BYTE_FIELDS, CURSOR_SALT, SHANGHAI, TimeseriesUnavailable,
                              build_line_usage, build_timeseries, build_usage_views, empty_usage_views)


GB = 1_000_000_000


def at(month=10, day=1, hour=0, minute=0, *, year=2026):
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI)


class NaturalUsageTests(TestCase):
    def setUp(self):
        self.now = at(day=7, hour=12)
        self.clock = patch('django.utils.timezone.now', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.user = get_user_model().objects.create_user('natural-usage')
        self.service = Entitlement.objects.create(user=self.user, quota_bytes=100 * GB)
        self.cycle = BillingCycle.objects.create(entitlement=self.service, starts_at=at(month=1), ends_at=at(year=2027, month=1))
        self.server = Server.objects.create(name='隔离假机器')
        self.ingress = Ingress.objects.create(server=self.server, name='入口显示名称', protocol='reality')
        self.line = Line.objects.create(name='线路显示名称', ingress=self.ingress,
            egress=Egress.objects.create(server=self.server, name='出口', kind='direct'))
        self.sub = DeviceSubscription.objects.create(entitlement=self.service, name='假交付', client='windows')
        self.identity = NodeIdentity.objects.create(subscription=self.sub, line=self.line, ingress=self.ingress,
            generation=1, state='simulated')
        self.stream = UsageStream.objects.create(identity=self.identity, epoch='test-epoch')
        self.rate = ServiceRateVersion.objects.create(entitlement=self.service, line=self.line, ingress=self.ingress,
            multiplier='1', effective_at=at(month=1), actor=self.user, reason='隔离测试')
        self.sequence = 0

    def row(self, start, end, upload=0, download=0, charged=None, *, stream=None, rate=None, cycle=None, quality='metered'):
        self.sequence += 1
        return UsageLedger.objects.create(cycle=cycle or self.cycle, stream=stream or self.stream,
            sequence=self.sequence, upload_delta=upload, download_delta=download,
            weighted_bytes=upload + download if charged is None else charged,
            grant_rate_version=rate or self.rate, quality=quality, observed_at=end,
            interval_start=start, interval_end=end)

    def test_contract_100gb_package_40gb_charge_and_raw_30gb(self):
        second_ingress = Ingress.objects.create(server=self.server, name='二倍入口', protocol='hy2')
        second_line = Line.objects.create(name='二倍线路', ingress=second_ingress, egress=self.line.egress)
        second_identity = NodeIdentity.objects.create(subscription=self.sub, line=second_line,
            ingress=second_ingress, generation=1)
        second_stream = UsageStream.objects.create(identity=second_identity, epoch='second')
        second_rate = ServiceRateVersion.objects.create(entitlement=self.service, line=second_line, ingress=second_ingress,
            multiplier='2', effective_at=at(month=1), actor=self.user, reason='授权二倍')
        raw_upload = [0, 1, 1, 0, 3, 2, 5]
        raw_download = [0, 1, 2, 0, 4, 4, 7]
        double_upload = [0, 1, 1, 0, 1, 1, 0]
        double_download = [0, 1, 2, 0, 2, 1, 0]
        for index in range(7):
            left, right = at(day=index + 1), min(at(day=index + 2), self.now)
            up, down = double_upload[index] * GB, double_download[index] * GB
            self.row(left, right, up, down, (up + down) * 2, stream=second_stream, rate=second_rate)
            self.row(left, right, (raw_upload[index] - double_upload[index]) * GB,
                     (raw_download[index] - double_download[index]) * GB)
        views = build_usage_views(self.service, '7d', self.now)
        value = views['timeseries']
        self.assertEqual([bucket['charged_bytes'] for bucket in value['buckets']],
                         [str(value * GB) for value in [0, 4, 6, 0, 10, 8, 12]])
        self.assertEqual(value['totals'], {'state': 'complete', 'charged_bytes': str(40 * GB),
            'upload_bytes': str(12 * GB), 'download_bytes': str(18 * GB), 'unallocated_charged_bytes': '0'})
        self.assertTrue(all(bucket['state'] == 'complete' for bucket in value['buckets']))
        self.assertTrue(value['buckets'][-1]['is_open'])
        self.assertEqual(value['buckets'][-1]['covered_through'], self.now.isoformat())
        self.assertEqual(views['line_usage']['totals'], value['totals'])
        self.assertEqual(views['line_usage']['summary_revision'], value['summary_revision'])
        self.assertEqual(sorted(line['charged_bytes'] for line in views['line_usage']['lines']), [str(20 * GB)] * 2)

    def test_whole_month_axis_and_year_unknown_future_are_not_zero(self):
        self.row(at(month=9), at(), 20 * GB, 40 * GB)
        self.row(at(), self.now, 12 * GB, 18 * GB, 40 * GB)
        year = build_timeseries(self.service, 'year', self.now)
        self.assertEqual(len(year['buckets']), 12)
        self.assertEqual([bucket['state'] for bucket in year['buckets']], ['missing'] * 8 + ['complete'] * 2 + ['future'] * 2)
        self.assertEqual(year['totals']['state'], 'partial')
        self.assertEqual(year['totals']['charged_bytes'], str(100 * GB))
        self.assertTrue(all(bucket['charged_bytes'] is None for bucket in year['buckets'][:8] + year['buckets'][10:]))
        month = build_timeseries(self.service, 'month', self.now)
        self.assertEqual(len(month['buckets']), 31)
        self.assertEqual(month['buckets'][7]['state'], 'future')
        self.assertEqual(month['totals']['state'], 'complete')
        self.assertEqual(month['totals']['charged_bytes'], str(40 * GB))
        self.assertEqual(month['unallocated'][0]['charged_bytes'], str(40 * GB))

    def test_cross_midnight_unallocated_is_never_split_or_counted_twice(self):
        self.row(at(day=6, hour=23, minute=59), at(day=7, minute=1), GB, GB)
        value = build_timeseries(self.service, '7d', self.now)
        self.assertEqual(value['totals']['charged_bytes'], str(2 * GB))
        self.assertEqual(value['totals']['unallocated_charged_bytes'], str(2 * GB))
        self.assertEqual(len(value['unallocated']), 1)
        self.assertEqual([bucket['state'] for bucket in value['buckets'][-2:]], ['partial', 'partial'])
        self.assertEqual([bucket['charged_bytes'] for bucket in value['buckets'][-2:]], [None, None])
        self.assertEqual(value['message_code'], 'unallocated_intervals')

    def test_query_boundary_interval_is_pending_without_guessed_amount(self):
        self.row(at(month=9, day=30, hour=23, minute=59), at(minute=1), GB, GB)
        value = build_timeseries(self.service, 'month', self.now)
        self.assertEqual(value['boundary_pending_count'], 1)
        self.assertEqual(value['totals']['state'], 'partial')
        self.assertIsNone(value['totals']['charged_bytes'])
        self.assertEqual(value['unallocated'], [])
        self.assertEqual(value['message_code'], 'boundary_pending')

    def test_month_reset_preserves_earlier_natural_month_usage(self):
        now = at(day=20)
        self.cycle.ends_at = at(day=15)
        self.cycle.save(update_fields=['ends_at'])
        current = BillingCycle.objects.create(entitlement=self.service, starts_at=at(day=15),
            ends_at=at(month=11, day=15), used_bytes=10 * GB)
        self.row(at(), at(day=15), 30 * GB)
        self.row(at(day=15), now, 10 * GB, cycle=current)
        value = build_timeseries(self.service, 'month', now)
        self.assertEqual(value['totals']['charged_bytes'], str(40 * GB))
        self.assertEqual(current.used_bytes, 10 * GB)

    def test_late_posting_uses_interval_and_single_timestamp_legacy_is_excluded(self):
        self.row(at(day=2), at(day=3), 17)
        legacy = self.row(at(day=4), at(day=5), 999)
        UsageLedger.objects.filter(pk=legacy.pk).update(interval_start=None, interval_end=None)
        value = build_timeseries(self.service, '7d', self.now)
        self.assertEqual(value['buckets'][1]['charged_bytes'], '17')
        self.assertEqual(value['buckets'][-1]['charged_bytes'], None)
        self.assertEqual(value['totals']['charged_bytes'], '17')

    def test_zero_requires_every_identity_and_full_contiguous_interval(self):
        self.row(at(), at(day=2))
        first = build_timeseries(self.service, 'month', self.now)
        self.assertEqual(first['buckets'][0]['state'], 'complete')
        self.assertEqual(first['buckets'][0]['charged_bytes'], '0')
        other = NodeIdentity.objects.create(subscription=self.sub, line=self.line, ingress=self.ingress, generation=2)
        second = build_timeseries(self.service, 'month', self.now)
        self.assertEqual(second['buckets'][0]['state'], 'partial')
        self.assertIsNone(second['buckets'][0]['covered_through'])
        other_stream = UsageStream.objects.create(identity=other, epoch='second')
        self.row(at(), at(hour=12), stream=other_stream)
        third = build_timeseries(self.service, 'month', self.now)
        self.assertEqual(third['buckets'][0]['state'], 'partial')
        self.assertEqual(third['buckets'][0]['covered_through'], at(hour=12).isoformat())
        self.row(at(hour=12), at(day=2), stream=other_stream)
        self.assertEqual(build_timeseries(self.service, 'month', self.now)['buckets'][0]['state'], 'complete')

    def test_current_day_open_complete_is_distinct_from_sampling_gap(self):
        self.row(at(day=7), self.now, 1)
        value = build_timeseries(self.service, '7d', self.now)['buckets'][-1]
        self.assertEqual(value['state'], 'complete')
        self.assertTrue(value['is_open'])
        UsageLedger.objects.all().update(interval_start=at(day=7, hour=1))
        value = build_timeseries(self.service, '7d', self.now)['buckets'][-1]
        self.assertEqual(value['state'], 'partial')
        self.assertIsNone(value['covered_through'])

    def test_gap_in_middle_never_becomes_full_coverage(self):
        self.row(at(), at(hour=6), 1)
        self.row(at(hour=7), at(day=2), 2)
        bucket = build_timeseries(self.service, 'month', self.now)['buckets'][0]
        self.assertEqual(bucket['state'], 'partial')
        self.assertEqual(bucket['charged_bytes'], '3')
        self.assertEqual(bucket['covered_through'], at(hour=6).isoformat())

    def test_fresh_continuous_sampling_tail_keeps_partial_amounts_without_gap_warning(self):
        measured = self.now - timedelta(seconds=2)
        for day in range(1, 8):
            self.row(at(day=day), min(at(day=day + 1), measured), day)
        views = build_usage_views(self.service, '7d', self.now)
        value = views['timeseries']
        self.assertEqual(value['message_code'], 'sampling_pending')
        self.assertEqual(views['line_usage']['message_code'], 'sampling_pending')
        self.assertEqual(value['totals']['state'], 'partial')
        self.assertEqual(value['totals']['charged_bytes'], '28')
        self.assertEqual(value['as_of'], self.now.isoformat())
        self.assertEqual(value['buckets'][-1]['covered_through'], measured.isoformat())
        self.assertEqual(value['buckets'][-1]['state'], 'partial')
        self.assertTrue(value['buckets'][-1]['is_open'])

    def test_fresh_sample_after_middle_gap_does_not_hide_missing_history(self):
        for day in range(1, 7):
            self.row(at(day=day), at(day=day + 1), day)
        self.row(at(day=7), self.now - timedelta(seconds=2), 7)
        self.row(self.now - timedelta(seconds=1), self.now, 1)
        value = build_timeseries(self.service, '7d', self.now)
        self.assertEqual(value['message_code'], 'partial_coverage')
        self.assertEqual(value['totals']['state'], 'partial')
        self.assertEqual(value['buckets'][-1]['covered_through'], (self.now - timedelta(seconds=2)).isoformat())

    def test_stale_tail_missing_identity_and_absent_history_never_claim_sampling_pending(self):
        self.assertEqual(build_timeseries(self.service, '7d', self.now)['message_code'], 'missing_history')
        for day in range(1, 8):
            self.row(at(day=day), min(at(day=day + 1), self.now - timedelta(minutes=4)), day)
        self.assertEqual(build_timeseries(self.service, '7d', self.now)['message_code'], 'partial_coverage')
        self.row(self.now - timedelta(minutes=4), self.now - timedelta(seconds=2), 1)
        self.assertEqual(build_timeseries(self.service, '7d', self.now)['message_code'], 'sampling_pending')
        NodeIdentity.objects.create(subscription=self.sub, line=self.line, ingress=self.ingress, generation=2)
        self.assertEqual(build_timeseries(self.service, '7d', self.now)['message_code'], 'partial_coverage')

    def test_month_and_year_boundaries_keep_full_intervals_once(self):
        self.row(at(month=9, day=30, hour=23, minute=59), at(minute=1), 2 * GB)
        year = build_timeseries(self.service, 'year', self.now)
        self.assertEqual(year['totals']['charged_bytes'], str(2 * GB))
        self.assertEqual(len(year['unallocated']), 1)
        self.assertEqual(year['boundary_pending_count'], 0)
        self.assertIsNone(year['buckets'][8]['charged_bytes'])
        self.assertIsNone(year['buckets'][9]['charged_bytes'])
        UsageLedger.objects.all().delete()
        self.cycle.ends_at = at(year=2027, month=2)
        self.cycle.save(update_fields=['ends_at'])
        now = at(year=2027, month=1, day=2)
        self.row(at(month=12, day=31, hour=23, minute=59), at(year=2027, month=1, minute=1), 3 * GB)
        new_year = build_timeseries(self.service, 'year', now)
        self.assertEqual(new_year['boundary_pending_count'], 1)
        self.assertIsNone(new_year['totals']['charged_bytes'])
        seven_days = build_timeseries(self.service, '7d', now)
        self.assertEqual(seven_days['totals']['charged_bytes'], str(3 * GB))
        self.assertEqual(len(seven_days['unallocated']), 1)

    def test_revoked_identity_needs_only_its_actual_required_interval(self):
        self.row(at(), at(day=2), 10)
        other = NodeIdentity.objects.create(subscription=self.sub, line=self.line, ingress=self.ingress,
            generation=2, revoked_at=at(hour=12))
        stream = UsageStream.objects.create(identity=other, epoch='revoked')
        self.row(at(), at(hour=12), 5, stream=stream)
        value = build_timeseries(self.service, 'month', self.now)
        self.assertEqual(value['buckets'][0]['state'], 'complete')
        self.assertEqual(value['buckets'][0]['covered_through'], at(day=2).isoformat())
        # 撤销之后的可疑区间不作为本人已确认量。
        self.row(at(hour=12), at(day=2), 999, stream=stream)
        self.assertEqual(build_timeseries(self.service, 'month', self.now)['totals']['charged_bytes'], '15')

    def test_rejected_quality_wrong_grant_and_cross_rate_boundary_are_excluded(self):
        self.row(at(), at(hour=1), 999, quality='legacy_unknown')
        other = Entitlement.objects.create(user=get_user_model().objects.create_user('other'), quota_bytes=100)
        wrong_rate = ServiceRateVersion.objects.create(entitlement=other, line=self.line, ingress=self.ingress,
            multiplier='1', effective_at=at(month=1), actor=self.user, reason='他人授权')
        self.row(at(hour=1), at(hour=2), 999, rate=wrong_rate)
        ServiceRateVersion.objects.create(entitlement=self.service, line=self.line, ingress=self.ingress,
            multiplier='2', effective_at=at(hour=3), actor=self.user, reason='倍率变更')
        self.row(at(hour=2), at(hour=4), 999)
        self.assertEqual(build_timeseries(self.service, 'month', self.now)['totals']['state'], 'missing')

    def test_foreign_identity_cannot_enter_same_service_cycle(self):
        other = Entitlement.objects.create(user=get_user_model().objects.create_user('other'), quota_bytes=100)
        other_sub = DeviceSubscription.objects.create(entitlement=other, name='他人', client='windows')
        identity = NodeIdentity.objects.create(subscription=other_sub, line=self.line, ingress=self.ingress, generation=1)
        stream = UsageStream.objects.create(identity=identity, epoch='other')
        self.row(at(), at(day=2), 999, stream=stream)
        self.assertEqual(build_timeseries(self.service, 'month', self.now)['totals']['state'], 'missing')

    def test_line_versions_keep_historic_amounts_and_public_fields_only(self):
        second = ServiceRateVersion.objects.create(entitlement=self.service, line=self.line, ingress=self.ingress,
            multiplier='2', effective_at=at(day=2), actor=self.user, reason='二倍')
        self.row(at(), at(day=2), 10, 20, 30)
        self.row(at(day=2), at(day=3), 10, 20, 60, rate=second)
        value = build_line_usage(self.service, 'month', self.now)
        self.assertEqual([line['charged_bytes'] for line in value['lines']], ['30', '60'])
        self.assertEqual([line['multiplier'] for line in value['lines']], ['1.000000', '2.000000'])
        self.assertEqual(value['lines'][0]['effective_to'], at(day=2).isoformat())
        self.assertEqual(set(value['lines'][0]), {'line_id', 'line_name', 'node_name', 'multiplier',
                         'effective_from', 'effective_to', *BYTE_FIELDS})
        self.assertEqual(value['lines'][0]['line_id'], str(self.line.public_id))

    def test_leap_day_year_rollover_and_business_timezone(self):
        self.assertEqual(len(empty_usage_views('test', 'month', at(year=2028, month=2, day=15))['timeseries']['buckets']), 29)
        self.assertEqual(len(empty_usage_views('test', 'month', at(year=2027, month=2, day=15))['timeseries']['buckets']), 28)
        value = empty_usage_views('test', '7d', at(year=2027, month=1, day=2))['timeseries']
        self.assertEqual(value['range_start'], at(month=12, day=27).isoformat())
        self.assertEqual(value['buckets'][-1]['state'], 'missing')
        self.assertTrue(value['buckets'][-1]['is_open'])
        utc = datetime.fromisoformat('2026-10-06T17:00:00+00:00')
        self.assertEqual(empty_usage_views('test', '7d', utc)['timeseries']['buckets'][-1]['start'], at(day=7).isoformat())

    def test_read_only_closed_schema_and_exact_large_integer(self):
        self.row(at(), at(day=2), 2 ** 60)
        with CaptureQueriesContext(connection) as queries:
            value = build_timeseries(self.service, 'month', self.now)
        self.assertFalse(any(query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'BEGIN IMMEDIATE')) for query in queries))
        self.assertEqual(value['totals']['charged_bytes'], str(2 ** 60))
        self.assertEqual(set(value), {'service_id', 'time_zone', 'period', 'granularity', 'range_start', 'range_end',
            'as_of', 'summary_revision', 'totals', 'buckets', 'unallocated', 'unallocated_next_cursor',
            'boundary_pending_count', 'message_code'})
        self.assertEqual(set(value['buckets'][0]), {'start', 'end', 'state', 'is_open', 'covered_through', *BYTE_FIELDS})
        with self.assertNumQueries(0):
            empty_usage_views('unmapped', 'month', self.now)

    def test_unallocated_pagination_keeps_totals_and_watermark(self):
        for index in range(101):
            identity = NodeIdentity.objects.create(subscription=self.sub, line=self.line, ingress=self.ingress, generation=index + 2)
            stream = UsageStream.objects.create(identity=identity, epoch='page')
            self.row(at(hour=23, minute=59), at(day=2, minute=1), index, 1, stream=stream)
        first = build_usage_views(self.service, 'month', self.now)
        first_chart = first['timeseries']
        self.assertEqual(len(first_chart['unallocated']), 100)
        self.assertEqual(first_chart['totals']['charged_bytes'], str(sum(range(101)) + 101))
        cursor = first_chart['unallocated_next_cursor']
        self.assertTrue(cursor)
        self.row(at(day=3), at(day=4), 10000)
        second = build_usage_views(self.service, 'month', self.now, cursor=cursor)
        self.assertEqual(len(second['timeseries']['unallocated']), 1)
        self.assertIsNone(second['timeseries']['unallocated_next_cursor'])
        self.assertEqual(second['timeseries']['totals'], first_chart['totals'])
        self.assertEqual(second['timeseries']['summary_revision'], first_chart['summary_revision'])
        self.assertEqual(second['line_usage'], first['line_usage'])
        self.assertNotEqual(build_timeseries(self.service, 'month', self.now)['summary_revision'], first_chart['summary_revision'])
        with self.assertRaises(TimeseriesUnavailable):
            build_timeseries(self.service, 'year', self.now, cursor=cursor)
        with self.assertRaises(TimeseriesUnavailable):
            build_timeseries(self.service, 'month', self.now, cursor=cursor + 'x')
        NodeIdentity.objects.filter(pk=self.identity.pk).update(revoked_at=self.now)
        with self.assertRaises(TimeseriesUnavailable):
            build_timeseries(self.service, 'month', self.now, cursor=cursor)

    def test_cursor_is_service_bound_and_expires(self):
        data = {'service': str(self.service.public_id), 'period': 'month', 'as_of': self.now.isoformat(),
                'watermark': 0, 'offset': 100, 'shape': 'test'}
        with patch('django.core.signing.time.time', return_value=1):
            cursor = signing.dumps(data, salt=CURSOR_SALT)
        with self.assertRaises(TimeseriesUnavailable):
            build_timeseries(self.service, 'month', self.now, cursor=cursor)
        data['service'] = 'another-service'
        with self.assertRaises(TimeseriesUnavailable):
            build_timeseries(self.service, 'month', self.now, cursor=signing.dumps(data, salt=CURSOR_SALT))

    def test_overlapping_identity_intervals_fail_explicitly(self):
        self.row(at(), at(day=2), 10)
        self.row(at(hour=12), at(day=2, hour=12), 10)
        with self.assertRaises(TimeseriesUnavailable) as error:
            build_timeseries(self.service, 'month', self.now)
        self.assertEqual(error.exception.code, 'overlapping_intervals')

    def test_explicit_utc_aware_time_and_known_period_required(self):
        for period, now in [('30d', self.now), ('month', datetime(2026, 10, 7))]:
            with self.assertRaises(TimeseriesUnavailable):
                build_timeseries(self.service, period, now)

    def test_streaming_uses_bounded_query_count_for_many_samples(self):
        entries = []
        for index in range(2500):
            left = at() + timedelta(minutes=index)
            right = left + timedelta(minutes=1)
            entries.append(UsageLedger(cycle=self.cycle, stream=self.stream, sequence=index + 1,
                upload_delta=1, download_delta=2, weighted_bytes=3, grant_rate_version=self.rate,
                quality='metered', observed_at=right, interval_start=left, interval_end=right))
        UsageLedger.objects.bulk_create(entries, batch_size=100)
        with self.assertNumQueries(4):
            views = build_usage_views(self.service, 'month', self.now)
        self.assertEqual(views['timeseries']['totals']['charged_bytes'], '7500')
        self.assertEqual(views['line_usage']['totals'], views['timeseries']['totals'])
        self.assertEqual(views['timeseries']['buckets'][0]['state'], 'complete')
