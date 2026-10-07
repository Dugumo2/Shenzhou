"""Q4 假资产反例：授权倍率、持久采样与不可猜测的区间边界。"""
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .entitlements import record_usage
from .ingress_metering import record_ingress_sample, set_service_rate
from .models import (BillingCycle, CoreInstance, DeviceSubscription, Egress, Entitlement,
    Ingress, IngressUsageSample, Line, NodeIdentity, Server, ServiceRateVersion, UsageLedger, UsageStream)


class IngressMeteringTests(TestCase):
    def setUp(self):
        self.now = datetime(2028, 1, 15, 12, tzinfo=datetime_timezone.utc)
        self.clock = patch('django.utils.timezone.now', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.admin = get_user_model().objects.create_user('计量管理', is_staff=True)
        self.server = Server.objects.create(name='假入口机器')
        self.ingress = Ingress.objects.create(server=self.server, name='假入口', protocol='reality')
        self.egress = Egress.objects.create(server=self.server, name='假出口', kind='direct')
        self.line = Line.objects.create(name='同一假线路', ingress=self.ingress, egress=self.egress)
        self.core = CoreInstance.objects.create(server=self.server, instance_key='fake', name='假核心',
                                               core_type='xray', configuration_owner='test')
        self.core.ingresses.add(self.ingress)
        self.record, self.identity, self.cycle = self.service('甲')
        self.rate = self.rate_for(self.record, '1.25')

    def service(self, name):
        user = get_user_model().objects.create_user(name)
        record = Entitlement.objects.create(user=user, quota_bytes=10**12,
            activated_at=self.now - timedelta(days=14), expires_at=self.now + timedelta(days=90),
            applied_revision=1, state='simulated')
        record.lines.add(self.line)
        sub = DeviceSubscription.objects.create(entitlement=record, name='假交付', client='windows', state='simulated')
        sub.lines.add(self.line)
        identity = NodeIdentity.objects.create(subscription=sub, line=self.line, ingress=self.ingress,
                                               generation=1, state='simulated')
        cycle = BillingCycle.objects.create(entitlement=record, starts_at=record.activated_at,
            ends_at=self.now + timedelta(days=16), raw_bytes=0)
        return record, identity, cycle

    def rate_for(self, record, multiplier, at=None):
        return set_service_rate(self.admin, record.pk, self.line.pk, self.ingress.pk,
                                multiplier, at or record.activated_at, '测试授权倍率')

    def sample(self, seq=1, up=1000, down=2000, at=None, **changes):
        values = dict(identity_id=self.identity.pk, server_id=self.server.pk, core_instance_id=self.core.pk,
            epoch='epoch-a', sequence=seq, upload_bytes=up, download_bytes=down,
            observed_at=at or self.now - timedelta(seconds=120 - seq * 10),
            coverage_verified=True, counter_kind='cumulative')
        values.update(changes)
        return record_ingress_sample(**values)

    def test_first_sample_is_only_baseline_and_persists_across_reload(self):
        first = self.sample(up=999999999)
        self.assertEqual(first.status, 'baseline')
        self.assertFalse(UsageLedger.objects.exists())
        self.cycle.refresh_from_db()
        self.assertEqual((self.cycle.used_bytes, self.cycle.raw_bytes), (0, 0))
        first = IngressUsageSample.objects.get(pk=first.pk)
        second = self.sample(2, up=first.upload_bytes + 3, down=first.download_bytes + 1)
        self.assertEqual(second.status, 'accepted')
        ledger = UsageLedger.objects.get()
        self.assertEqual((ledger.upload_delta, ledger.download_delta, ledger.weighted_bytes), (3, 1, 5))
        self.assertEqual(ledger.interval_start, first.observed_at)
        self.assertEqual(ledger.interval_end, second.observed_at)
        self.assertEqual(ledger.grant_rate_version_id, self.rate.pk)
        self.assertIsNone(ledger.rate_version_id)

    def test_two_services_on_same_line_have_independent_rates_and_remainders(self):
        other, identity, cycle = self.service('乙')
        self.rate_for(other, '0.333333')
        self.sample()
        self.sample(identity_id=identity.pk)
        for seq in range(2, 6):
            self.sample(seq, up=1000 + seq - 1)
            self.sample(seq, up=1000 + seq - 1, identity_id=identity.pk)
        self.cycle.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual((self.cycle.used_bytes, self.cycle.weighted_remainder), (5, 0))
        self.assertEqual((cycle.used_bytes, cycle.weighted_remainder), (1, 333332))
        self.assertEqual(cycle.raw_bytes, 4)

    def test_identical_replay_does_not_write_or_double_charge(self):
        self.sample()
        accepted = self.sample(2, up=1040)
        before = list(BillingCycle.objects.values())
        with CaptureQueriesContext(connection) as queries:
            replay = self.sample(2, up=1040)
        self.assertEqual(replay.pk, accepted.pk)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(list(BillingCycle.objects.values()), before)

    def test_same_sequence_conflict_rejected(self):
        self.sample()
        with self.assertRaises(ValidationError):
            self.sample(up=1001)
        self.assertEqual(IngressUsageSample.objects.count(), 1)

    def test_transfer_layer_wrong_server_and_unbound_core_cannot_charge(self):
        other = Server.objects.create(name='假转发机')
        for changes in ({'accounting_layer': 'forwarding'}, {'server_id': other.pk}):
            with self.subTest(changes=changes), self.assertRaises(PermissionDenied):
                self.sample(**changes)
        self.core.ingresses.clear()
        with self.assertRaises(PermissionDenied):
            self.sample()
        self.assertFalse(IngressUsageSample.objects.exists())

    def test_unknown_and_delta_counters_are_not_zero(self):
        for changes in ({'counter_kind': 'delta'}, {'up': None}, {'coverage_verified': None}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.sample(**changes)
        self.assertFalse(IngressUsageSample.objects.exists())

    def test_unverified_interval_is_gap_then_new_verified_interval_can_charge(self):
        self.sample()
        gap = self.sample(2, up=1100, coverage_verified=False)
        self.assertEqual((gap.status, gap.reason_code), ('gap', 'coverage_unverified'))
        self.assertFalse(UsageLedger.objects.exists())
        self.sample(3, up=1104)
        self.assertEqual(UsageLedger.objects.get().weighted_bytes, 5)
        self.record.refresh_from_db()
        self.assertTrue(self.record.metering_gap)

    def test_counter_reversal_requires_new_epoch_baseline(self):
        self.sample()
        gap = self.sample(2, up=900)
        self.assertEqual(gap.reason_code, 'counter_reversed')
        self.assertEqual(self.sample(3, up=1200).status, 'late')
        self.assertEqual(self.sample(1, up=4, down=5, epoch='epoch-b', at=self.now - timedelta(seconds=60)).status, 'baseline')
        self.sample(2, up=8, down=5, epoch='epoch-b', at=self.now - timedelta(seconds=50))
        self.assertEqual(UsageLedger.objects.get().weighted_bytes, 5)

    def test_new_epoch_does_not_charge_from_zero_and_old_epoch_stays_retired(self):
        self.sample()
        self.assertEqual(self.sample(1, epoch='epoch-b', at=self.now - timedelta(seconds=60)).status, 'baseline')
        self.assertEqual(self.sample(2, at=self.now - timedelta(seconds=50)).status, 'late')
        self.assertFalse(UsageLedger.objects.exists())

    def test_late_time_and_sequence_never_change_anchor(self):
        first = self.sample(5)
        self.assertEqual(self.sample(4, at=first.observed_at + timedelta(seconds=1)).status, 'late')
        self.assertEqual(self.sample(6, at=first.observed_at - timedelta(seconds=1)).status, 'late')
        self.sample(7, up=1004)
        ledger = UsageLedger.objects.get()
        self.assertEqual(ledger.interval_start, first.observed_at)
        self.assertEqual(ledger.weighted_bytes, 5)

    def test_cross_day_keeps_single_original_interval(self):
        start = self.now.replace(hour=15, minute=59, second=0) - timedelta(days=1)
        end = start + timedelta(minutes=2)
        self.sample(at=start)
        self.sample(2, up=1040, at=end)
        ledger = UsageLedger.objects.get()
        self.assertEqual((ledger.interval_start, ledger.interval_end, ledger.weighted_bytes), (start, end, 50))

    def test_cross_cycle_without_boundary_is_gap_and_does_not_create_cycle(self):
        self.sample(at=self.now - timedelta(days=2))
        self.cycle.ends_at = self.now - timedelta(days=1)
        self.cycle.save(update_fields=['ends_at'])
        new = BillingCycle.objects.create(entitlement=self.record, starts_at=self.cycle.ends_at,
            ends_at=self.now + timedelta(days=30), raw_bytes=0)
        self.assertEqual(self.sample(2, up=1040).reason_code, 'cycle_boundary')
        self.assertEqual(BillingCycle.objects.count(), 2)
        self.assertFalse(UsageLedger.objects.exists())
        self.sample(3, up=1044)
        self.assertEqual(UsageLedger.objects.get().cycle_id, new.pk)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 0)

    def test_no_cycle_never_implicitly_creates_one(self):
        self.cycle.delete()
        self.sample()
        self.assertEqual(self.sample(2, up=1040).reason_code, 'cycle_boundary')
        self.assertFalse(BillingCycle.objects.exists())

    def test_confirmed_closed_cycle_receives_its_complete_delayed_interval(self):
        self.sample()
        self.cycle.ends_at = self.now - timedelta(seconds=30)
        self.cycle.save(update_fields=['ends_at'])
        self.assertEqual(self.sample(2, up=1040).status, 'accepted')
        self.assertEqual(UsageLedger.objects.get().cycle_id, self.cycle.pk)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 50)

    def test_rate_change_at_explicit_boundary_preserves_old_ledger(self):
        self.sample()
        checkpoint = self.sample(2, up=1040)
        old = list(UsageLedger.objects.values())
        new_rate = self.rate_for(self.record, '2', checkpoint.observed_at)
        self.sample(3, up=1050)
        self.assertEqual(list(UsageLedger.objects.filter(pk=old[0]['id']).values()), old)
        ledger = UsageLedger.objects.order_by('-pk').first()
        self.assertEqual((ledger.weighted_bytes, ledger.grant_rate_version_id), (20, new_rate.pk))
        with self.assertRaises(ValidationError):
            self.rate_for(self.record, '3', checkpoint.observed_at - timedelta(seconds=1))
        self.rate.multiplier = '9'
        with self.assertRaises(ValidationError):
            self.rate.save(update_fields=['multiplier'])

    def test_baseline_without_verified_coverage_cannot_claim_previous_bytes(self):
        first = self.sample(coverage_verified=False)
        self.assertEqual(first.status, 'baseline')
        self.assertFalse(first.coverage_verified)
        self.assertFalse(UsageLedger.objects.exists())
        second = self.sample(2, up=1004)
        self.assertEqual(second.status, 'accepted')
        self.assertEqual(UsageLedger.objects.get().upload_delta, 4)

    def test_core_change_in_same_epoch_rejected(self):
        self.sample()
        core = CoreInstance.objects.create(server=self.server, instance_key='fake-two', name='另一假核心',
            core_type='xray', configuration_owner='test')
        core.ingresses.add(self.ingress)
        with self.assertRaises(ValidationError):
            self.sample(2, up=1040, core_instance_id=core.pk)

    def test_two_identities_on_different_ingresses_share_one_service_balance(self):
        ingress = Ingress.objects.create(server=self.server, name='第二假入口', protocol='ws')
        self.line.additional_ingresses.add(ingress)
        self.core.ingresses.add(ingress)
        identity = NodeIdentity.objects.create(subscription=self.identity.subscription, line=self.line,
            ingress=ingress, generation=1, state='simulated')
        set_service_rate(self.admin, self.record.pk, self.line.pk, ingress.pk, '2',
            self.record.activated_at, '第二入口假授权')
        self.sample()
        self.sample(identity_id=identity.pk)
        self.sample(2, up=1004)
        self.sample(2, up=1004, identity_id=identity.pk)
        self.cycle.refresh_from_db()
        self.assertEqual((self.cycle.used_bytes, self.cycle.raw_bytes), (13, 8))

    def test_known_zero_interval_is_recorded_but_has_no_baseline_backfill(self):
        self.sample()
        self.sample(2)
        ledger = UsageLedger.objects.get()
        self.assertEqual((ledger.upload_delta, ledger.download_delta, ledger.weighted_bytes), (0, 0, 0))
        self.assertIsNotNone(ledger.interval_start)

    def test_cross_rate_without_boundary_is_gap(self):
        first = self.sample()
        self.rate_for(self.record, '2', first.observed_at + timedelta(seconds=5))
        self.assertEqual(self.sample(2, up=1040).reason_code, 'rate_boundary')
        self.assertFalse(UsageLedger.objects.exists())
        self.sample(3, up=1044)
        self.assertEqual(UsageLedger.objects.get().weighted_bytes, 8)

    def test_no_service_rate_cannot_fall_back_to_global_rate(self):
        ServiceRateVersion.objects.all().delete()
        from .models import LineRateVersion
        LineRateVersion.objects.create(line=self.line, actor=self.admin, multiplier=1,
                                       effective_at=self.record.activated_at, reason='旧全局测试')
        self.sample()
        self.assertEqual(self.sample(2, up=1040).reason_code, 'rate_missing')
        self.assertFalse(UsageLedger.objects.exists())

    def test_old_record_path_is_blocked_after_baseline(self):
        self.sample()
        with self.assertRaises(ValidationError):
            record_usage(self.identity.pk, self.server.pk, 'other', 1, 100, 100, self.now)
        self.assertEqual(UsageStream.objects.count(), 1)
        self.assertFalse(UsageLedger.objects.exists())

    def test_unknown_historical_raw_total_stays_unknown(self):
        self.cycle.raw_bytes = None
        self.cycle.save(update_fields=['raw_bytes'])
        self.sample()
        self.sample(2, up=1004)
        self.cycle.refresh_from_db()
        self.assertIsNone(self.cycle.raw_bytes)
        self.assertEqual(self.cycle.used_bytes, 5)

    def test_identity_cannot_move_to_other_service_and_reuse_counters(self):
        self.sample()
        other, identity, _ = self.service('乙')
        self.identity.subscription = identity.subscription
        self.identity.generation = 2
        self.identity.save(update_fields=['subscription', 'generation'])
        with self.assertRaises(PermissionDenied):
            self.sample(2, up=1040)

    def test_new_epoch_cannot_hide_identity_generation_drift(self):
        self.sample()
        NodeIdentity.objects.filter(pk=self.identity.pk).update(generation=2)
        with self.assertRaises(PermissionDenied):
            self.sample(1, epoch='washed-epoch', at=self.now)
        self.assertEqual(IngressUsageSample.objects.count(), 1)

    def test_same_service_subscription_move_is_also_scope_drift(self):
        self.sample()
        sub = DeviceSubscription.objects.create(entitlement=self.record, name='另一交付', client='android', state='simulated')
        sub.lines.add(self.line)
        NodeIdentity.objects.filter(pk=self.identity.pk).update(subscription=sub)
        with self.assertRaises(PermissionDenied):
            self.sample(2, up=1040)

    def test_overlap_in_known_cycle_schedule_remains_gap(self):
        self.sample()
        BillingCycle.objects.create(entitlement=self.record, starts_at=self.now - timedelta(seconds=105),
            ends_at=self.now + timedelta(days=2), raw_bytes=0)
        self.assertEqual(self.sample(2, up=1040).reason_code, 'cycle_boundary')
        self.assertFalse(UsageLedger.objects.exists())

    def test_failed_overflow_is_atomic(self):
        self.sample()
        self.cycle.used_bytes = 2**63 - 1
        self.cycle.save(update_fields=['used_bytes'])
        with self.assertRaises(ValidationError):
            self.sample(2, up=1004)
        self.assertEqual(IngressUsageSample.objects.count(), 1)
        self.assertFalse(UsageLedger.objects.exists())
