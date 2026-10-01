"""服务统一扣费、资源分账与分钟账期的隔离业务验收。"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from .billing import current_line_rate, set_line_rate
from .capacity import (add_capacity, capacity_overview, link_line, pool_summary,
                       record_sample, refresh_alerts, save_pool)
from .delivery import create_subscription
from .entitlements import SHANGHAI, add_months, current_cycle, next_reset, record_usage, summary_for
from .jobs import run_job
from .models import CapacityAlert, DeviceSubscription, LineRateVersion, ServiceDeliveryReset, UsageLedger
from .test_phase1_backend import BackendTest, IsolatedAdapter


class ServiceCapacityTest(TestCase):
    setUp = BackendTest.setUp
    assign = BackendTest.assign
    activate_fixture = BackendTest.activate_fixture
    create_fixture = BackendTest.create_fixture

    def pool(self, **changes):
        now = timezone.now()
        values = dict(name='隔离共享资源', capacity_mode='limited', planned_bytes=1000,
            period_start=now - timedelta(days=2), period_end=now + timedelta(hours=1),
            accounting_basis='供应商双向账单')
        values.update(changes)
        return save_pool(self.admin, **values)

    def sample(self, pool, **changes):
        values = dict(source='manual', source_key='manual-first', used_bytes=200,
            observed_at=timezone.now(), reason='隔离人工校准')
        values.update(changes)
        return record_sample(self.admin, pool.pk, **values)

    def test_fractional_bytes_carry_across_samples_and_lines(self):
        record, job = self.assign(line_ids=[self.line.pk, self.other.pk])
        run_job(job.public_id, IsolatedAdapter())
        for line in (self.line, self.other):
            line.rate_versions.all().delete()
            LineRateVersion.objects.create(line=line, multiplier=Decimal('0.5'), actor=self.admin,
                reason='隔离半倍', effective_at=timezone.now() - timedelta(days=1))
        sub, job = create_subscription(self.alice, '统一服务', 'android', [self.line.pk, self.other.pk], 'two-lines-rate')
        run_job(job.public_id, IsolatedAdapter())
        now = timezone.now()
        identities = list(sub.identities.order_by('pk'))
        record_usage(identities[0].pk, self.server.pk, 'a', 1, 1, 0, now)
        record_usage(identities[1].pk, self.server.pk, 'b', 1, 1, 0, now)
        record_usage(identities[0].pk, self.server.pk, 'a', 2, 2, 0, now + timedelta(seconds=1))
        cycle = record.cycles.get()
        self.assertEqual((cycle.used_bytes, cycle.raw_bytes, cycle.weighted_remainder), (1, 3, 500000))
        self.assertEqual(list(UsageLedger.objects.order_by('pk').values_list('weighted_bytes', flat=True)), [0, 1, 0])

    def test_rate_boundary_checkpoint_and_history_are_preserved(self):
        record, sub = self.create_fixture()
        identity = sub.identities.get()
        now = timezone.now()
        record_usage(identity.pk, self.server.pk, 'a', 1, 10, 0, now)
        version = set_line_rate(self.admin, self.line.pk, '2', now + timedelta(seconds=2), '隔离未来二倍')
        record_usage(identity.pk, self.server.pk, 'a', 2, 20, 0, now + timedelta(seconds=2))
        record_usage(identity.pk, self.server.pk, 'a', 3, 25, 0, now + timedelta(seconds=3))
        self.assertEqual(record.cycles.get().used_bytes, 30)
        rows = list(UsageLedger.objects.order_by('pk'))
        self.assertEqual(rows[1].rate_version.multiplier, 1)
        self.assertEqual(rows[2].rate_version_id, version.pk)
        with self.assertRaises(ValidationError):
            set_line_rate(self.admin, self.line.pk, '3', now, '不能重算历史')

    def test_rate_crossing_without_checkpoint_fails_closed(self):
        record, sub = self.create_fixture()
        identity = sub.identities.get()
        now = timezone.now()
        record_usage(identity.pk, self.server.pk, 'a', 1, 10, 0, now)
        set_line_rate(self.admin, self.line.pk, '2', now + timedelta(seconds=2), '需要边界')
        with self.assertRaises(ValidationError):
            record_usage(identity.pk, self.server.pk, 'a', 2, 20, 0, now + timedelta(seconds=3))
        self.assertEqual(record.cycles.get().used_bytes, 10)
        record.refresh_from_db()
        self.assertTrue(record.metering_gap)

    def test_unknown_rate_is_not_assumed_one(self):
        self.line.rate_versions.all().delete()
        record, sub = self.create_fixture()
        self.assertIsNone(current_line_rate(self.line))
        with self.assertRaises(ValidationError):
            record_usage(sub.identities.get().pk, self.server.pk, 'a', 1, 100, 0, timezone.now())
        self.assertEqual(UsageLedger.objects.count(), 0)
        self.assertEqual(record.cycles.get().used_bytes, 0)

    def test_multiplier_precision_and_permission(self):
        with self.assertRaises(PermissionDenied):
            set_line_rate(self.alice, self.line.pk, '2', timezone.now(), '越权')
        for invalid in ('NaN', 'Infinity', '-1', '0', '0.0000001'):
            with self.assertRaises(ValidationError):
                set_line_rate(self.admin, self.line.pk, invalid, timezone.now(), '错误倍率')

    def test_minute_anchor_short_month_and_default_opening_day(self):
        start = datetime(2027, 1, 31, 14, 37, tzinfo=SHANGHAI)
        self.assertEqual(next_reset(start, 31, 14, 37), datetime(2027, 2, 28, 14, 37, tzinfo=SHANGHAI))
        record, job = self.assign(reset_day=None, reset_hour=None, reset_minute=None)
        with patch('django.utils.timezone.now', return_value=start):
            run_job(job.public_id, IsolatedAdapter())
        record.refresh_from_db()
        self.assertEqual(record.cycles.get().ends_at.astimezone(SHANGHAI), datetime(2027, 2, 28, 14, 37, tzinfo=SHANGHAI))
        march = current_cycle(record, datetime(2027, 3, 1, tzinfo=SHANGHAI))
        self.assertEqual(march.ends_at.astimezone(SHANGHAI), datetime(2027, 3, 31, 14, 37, tzinfo=SHANGHAI))

    def test_edit_does_not_renew_and_explicit_renewal_extends_expiry(self):
        record = self.activate_fixture()
        expiry = record.expires_at
        _, edit = self.assign(service_months=12, idempotency_key='edit-only-months')
        run_job(edit.public_id, IsolatedAdapter())
        record.refresh_from_db()
        self.assertEqual(record.expires_at, expiry)
        _, renew = self.assign(renew=True, service_months=1, idempotency_key='explicit-renewal')
        run_job(renew.public_id, IsolatedAdapter())
        record.refresh_from_db()
        self.assertEqual(record.expires_at, add_months(expiry, 1))

    def test_capacity_manual_calibration_and_supplier_values_are_separate(self):
        pool = self.pool()
        self.sample(pool)
        add_capacity(self.admin, pool.pk, 500, '管理员记录追加包', 'add-first')
        self.sample(pool, source='api', source_key='provider-first', used_bytes=300,
            reported_capacity_bytes=1400, reason='')
        summary = pool_summary(pool)
        self.assertEqual(summary['planned_bytes'], 1500)
        self.assertEqual(summary['supplier_capacity_bytes'], 1400)
        self.assertEqual(summary['capacity_bytes'], 1400)
        self.assertEqual(summary['source'], 'api')
        self.assertEqual(summary['remaining_bytes'], 1100)
        pool = save_pool(self.admin, pool.pk, source_priority=['manual', 'api', 'agent'])
        self.assertEqual(pool_summary(pool)['used_bytes'], 200)
        self.assertEqual(pool.samples.count(), 2)

    def test_capacity_cannot_modify_entitlement_or_repeat_addon(self):
        record = self.activate_fixture()
        before = (record.quota_bytes, record.expires_at, record.revision)
        pool = self.pool()
        first = add_capacity(self.admin, pool.pk, 500, '追加记录', 'same-request')
        again = add_capacity(self.admin, pool.pk, 500, '追加记录', 'same-request')
        self.assertEqual(first.pk, again.pk)
        with self.assertRaises(ValidationError):
            add_capacity(self.admin, pool.pk, 501, '追加记录', 'same-request')
        self.sample(pool)
        record.refresh_from_db()
        self.assertEqual((record.quota_bytes, record.expires_at, record.revision), before)
        self.assertEqual(pool_summary(pool)['planned_bytes'], 1500)

    def test_unknown_unlimited_and_stale_are_distinct(self):
        pool = self.pool(capacity_mode='unknown', planned_bytes=None)
        summary = pool_summary(pool)
        self.assertIsNone(summary['used_bytes'])
        self.assertIsNone(summary['remaining_bytes'])
        self.assertTrue(summary['stale'])
        refresh_alerts(self.admin)
        self.assertTrue(pool.alerts.filter(code='capacity_unknown', active=True).exists())
        save_pool(self.admin, pool.pk, capacity_mode='unlimited')
        pool.refresh_from_db()
        self.assertEqual(pool_summary(pool)['capacity_mode'], 'unlimited')
        self.assertIsNone(pool_summary(pool)['capacity_bytes'])

    def test_sample_idempotency_and_manual_reason_required(self):
        pool = self.pool()
        at = timezone.now()
        first = self.sample(pool, observed_at=at)
        self.assertEqual(self.sample(pool, observed_at=at).pk, first.pk)
        with self.assertRaises(ValidationError):
            self.sample(pool, observed_at=at, used_bytes=999)
        with self.assertRaises(ValidationError):
            self.sample(pool, source_key='missing-reason', reason='')
        with self.assertRaises(PermissionDenied):
            capacity_overview(self.alice)

    def test_trend_and_threshold_alerts_are_persistent_and_close(self):
        pool = self.pool()
        now = timezone.now()
        self.sample(pool, source='api', source_key='earlier', observed_at=now - timedelta(hours=2), used_bytes=100, reason='')
        self.sample(pool, source='api', source_key='latest', observed_at=now, used_bytes=960, reason='')
        refresh_alerts(self.admin)
        self.assertEqual(pool.alerts.get(code='capacity_threshold').severity, 'urgent')
        self.assertTrue(pool.alerts.get(code='forecast_exhaustion').active)
        self.assertGreater(pool_summary(pool)['forecast_bytes'], 1000)
        save_pool(self.admin, pool.pk, planned_bytes=10000)
        refresh_alerts(self.admin)
        self.assertFalse(pool.alerts.get(code='capacity_threshold').active)
        self.assertEqual(CapacityAlert.objects.filter(pool=pool, code='capacity_threshold').count(), 1)

    def test_shared_capacity_counts_each_service_once(self):
        record, job = self.assign(line_ids=[self.line.pk, self.other.pk], quota_bytes=1000)
        run_job(job.public_id, IsolatedAdapter())
        record.refresh_from_db()
        record.usage_updated_at = timezone.now()
        record.save()
        pool = self.pool()
        for line in (self.line, self.other):
            link_line(self.admin, line.pk, pool.pk, topology_verified=True)
        self.assertEqual(pool_summary(pool)['commitment_bytes'], 1000)
        link_line(self.admin, self.other.pk, pool.pk, topology_verified=False)
        self.assertIsNone(pool_summary(pool)['commitment_bytes'])

    def test_rollover_keeps_old_resource_samples_and_addons(self):
        pool = self.pool()
        self.sample(pool)
        add_capacity(self.admin, pool.pk, 500, '本期追加', 'previous-addon')
        next_start = pool.period_end
        pool = save_pool(self.admin, pool.pk, period_start=next_start, period_end=add_months(next_start, 1))
        summary = pool_summary(pool)
        self.assertIsNone(summary['used_bytes'])
        self.assertEqual(summary['added_bytes'], 0)
        self.assertEqual(pool.samples.count(), 1)
        self.assertEqual(pool.adjustments.count(), 1)

    def test_different_accounting_basis_does_not_claim_comparable_remaining(self):
        pool = self.pool()
        self.sample(pool, source='agent', source_key='interface', used_bytes=950,
            accounting_basis='整机接口收发，包含隧道转发')
        summary = pool_summary(pool)
        self.assertFalse(summary['basis_matches'])
        self.assertIsNone(summary['remaining_bytes'])
        self.assertIsNone(summary['usage_percent'])
        self.assertIsNone(summary['planning_available_bytes'])
        self.assertIsNone(summary['forecast_bytes'])
        refresh_alerts(self.admin)
        self.assertTrue(pool.alerts.filter(code='basis_mismatch', active=True).exists())
        self.assertFalse(pool.alerts.filter(code='capacity_threshold', active=True).exists())

    def test_stale_api_remains_selected_but_cannot_support_capacity_commitment(self):
        pool = self.pool(stale_after_seconds=3600)
        self.sample(pool, source='api', source_key='old-api', used_bytes=600,
            observed_at=timezone.now() - timedelta(hours=2), reason='')
        self.sample(pool, source_key='fresh-manual', used_bytes=700)
        summary = pool_summary(pool)
        self.assertEqual(summary['source'], 'api')
        self.assertTrue(summary['stale'])
        self.assertIsNone(summary['planning_available_bytes'])
        self.assertIsNone(summary['forecast_bytes'])

    def test_scheduled_lower_rate_is_included_in_shared_commitment(self):
        record, job = self.assign(quota_bytes=1000)
        run_job(job.public_id, IsolatedAdapter())
        record.refresh_from_db()
        record.usage_updated_at = timezone.now()
        record.save()
        pool = self.pool()
        link_line(self.admin, self.line.pk, pool.pk, topology_verified=True)
        set_line_rate(self.admin, self.line.pk, '0.5', timezone.now() + timedelta(minutes=10), '已排程半倍')
        self.assertEqual(pool_summary(pool)['commitment_bytes'], 2000)

    def test_delivery_scope_has_database_uniqueness_while_legacy_remains_compatible(self):
        record = self.activate_fixture()
        first = DeviceSubscription.objects.create(entitlement=record, name='全部', client='android', delivery_scope='all')
        with self.assertRaises(IntegrityError), transaction.atomic():
            DeviceSubscription.objects.create(entitlement=record, name='重复', client='android', delivery_scope='all')
        DeviceSubscription.objects.create(entitlement=record, name='单线路', client='android', delivery_scope=f'line:{self.line.pk}')
        first.state = 'disabled'
        first.save()
        DeviceSubscription.objects.create(entitlement=record, name='重新获取', client='android', delivery_scope='all')
        for index in range(2):
            DeviceSubscription.objects.create(entitlement=record, name=f'旧设备{index}', client='android')

    def test_service_reset_manifest_key_cannot_duplicate(self):
        record = self.activate_fixture()
        ServiceDeliveryReset.objects.create(entitlement=record, actor=self.alice, key='reset-once', subscription_ids=[1], job_ids=[2])
        with self.assertRaises(IntegrityError), transaction.atomic():
            ServiceDeliveryReset.objects.create(entitlement=record, actor=self.alice, key='reset-once', subscription_ids=[3], job_ids=[4])

    def test_admin_can_inspect_disabled_service_without_weakening_user_access(self):
        record = self.activate_fixture()
        self.alice.is_active = False
        self.alice.save()
        with self.assertRaises(PermissionDenied):
            summary_for(self.alice)
        with self.assertRaises(PermissionDenied):
            summary_for(self.alice, actor=self.bob)
        self.assertEqual(summary_for(self.alice, actor=self.admin)['entitlement'].pk, record.pk)

    def test_fresh_manual_usage_does_not_refresh_stale_supplier_capacity(self):
        pool = self.pool(stale_after_seconds=3600, source_priority=['manual', 'api', 'agent'])
        self.sample(pool, source='api', source_key='old-capacity', used_bytes=100, reported_capacity_bytes=1500,
            observed_at=timezone.now() - timedelta(hours=2), reason='')
        self.sample(pool, source_key='fresh-usage', used_bytes=200)
        summary = pool_summary(pool)
        self.assertTrue(summary['supplier_capacity_stale'])
        self.assertTrue(summary['stale'])
        self.assertIsNone(summary['planning_available_bytes'])

    def test_expired_resource_period_does_not_claim_usable_capacity(self):
        now = timezone.now()
        pool = self.pool(period_start=now - timedelta(days=1), period_end=now - timedelta(minutes=1))
        self.sample(pool, observed_at=now - timedelta(minutes=2))
        summary = pool_summary(pool)
        self.assertFalse(summary['period_active'])
        self.assertTrue(summary['stale'])
        self.assertIsNone(summary['planning_available_bytes'])
        refresh_alerts(self.admin)
        self.assertTrue(pool.alerts.get(code='period_inactive').active)
