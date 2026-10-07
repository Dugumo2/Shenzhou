"""Q4 独立账务反例；只用假资产、真实 ORM 和隔离测试库。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from . import test_ingress_metering as fixtures
from .entitlements import record_usage
from .ingress_metering import record_ingress_sample
from .models import (BillingCycle, CoreInstance, DeviceSubscription, Ingress,
                     IngressUsageSample, NodeIdentity, Server, ServiceRateVersion, UsageLedger, UsageStream)


class ReviewFixture:
    # 只复用假资产构造方法，不继承原测试类或重复运行其测试。
    setUp = fixtures.IngressMeteringTests.setUp
    service = fixtures.IngressMeteringTests.service
    rate_for = fixtures.IngressMeteringTests.rate_for
    sample = fixtures.IngressMeteringTests.sample

    def balances(self):
        return (list(BillingCycle.objects.order_by('pk').values()),
                list(UsageLedger.objects.order_by('pk').values()),
                list(UsageStream.objects.order_by('pk').values()),
                list(IngressUsageSample.objects.order_by('pk').values()))


class IngressMeteringReviewTests(ReviewFixture, TestCase):
    def test_adjacent_cycles_at_exact_now_accept_old_period_endpoint_once(self):
        self.cycle.ends_at = self.now
        self.cycle.save(update_fields=['ends_at'])
        following = BillingCycle.objects.create(entitlement=self.record, starts_at=self.now,
            ends_at=self.now + timedelta(days=30), raw_bytes=0)
        self.sample(at=self.now - timedelta(seconds=10))
        endpoint = self.sample(2, up=1040, at=self.now)
        self.assertEqual(endpoint.status, 'accepted')
        ledger = UsageLedger.objects.get()
        self.assertEqual((ledger.cycle_id, ledger.weighted_bytes), (self.cycle.pk, 50))
        self.cycle.refresh_from_db()
        following.refresh_from_db()
        self.assertEqual((self.cycle.used_bytes, following.used_bytes), (50, 0))
        with patch('django.utils.timezone.now', return_value=self.now + timedelta(seconds=10)):
            next_sample = self.sample(3, up=1044, at=self.now + timedelta(seconds=10))
        self.assertEqual(next_sample.status, 'accepted')
        following.refresh_from_db()
        self.assertEqual(following.used_bytes, 5)
        self.assertEqual(UsageLedger.objects.count(), 2)

    def test_verified_coverage_does_not_override_core_and_ingress_server_drift(self):
        self.sample()
        moved_server = Server.objects.create(name='另一台隔离假机器')
        CoreInstance.objects.filter(pk=self.core.pk).update(server=moved_server)
        Ingress.objects.filter(pk=self.ingress.pk).update(server=moved_server)
        before = self.balances()
        with self.assertRaises((PermissionDenied, ValidationError)):
            self.sample(2, up=1040, server_id=moved_server.pk, coverage_verified=True)
        self.assertEqual(self.balances(), before)

    def test_verified_coverage_cannot_join_counters_after_identity_ingress_changes(self):
        self.sample()
        second = Ingress.objects.create(server=self.server, name='其他假入口', protocol='ws')
        self.line.additional_ingresses.add(second)
        self.core.ingresses.add(second)
        from .ingress_metering import set_service_rate
        set_service_rate(self.admin, self.record.pk, self.line.pk, second.pk, '2',
                         self.record.activated_at, '另一入口独立授权')
        NodeIdentity.objects.filter(pk=self.identity.pk).update(ingress=second)
        before = self.balances()
        with self.assertRaises((PermissionDenied, ValidationError)):
            self.sample(2, up=1040, coverage_verified=True)
        self.assertEqual(self.balances(), before)

    def test_other_service_identity_drift_rejected_even_with_verified_coverage(self):
        self.sample()
        other, identity, _ = self.service('独立复核乙')
        self.rate_for(other, '2')
        NodeIdentity.objects.filter(pk=self.identity.pk).update(subscription=identity.subscription, generation=2)
        before = self.balances()
        with self.assertRaises(PermissionDenied):
            self.sample(2, up=1040, coverage_verified=True)
        self.assertEqual(self.balances(), before)

    def test_identity_generation_drift_cannot_reuse_previous_epoch_counter(self):
        self.sample()
        NodeIdentity.objects.filter(pk=self.identity.pk).update(generation=2)
        before = self.balances()
        with self.assertRaises((PermissionDenied, ValidationError)):
            self.sample(2, up=1040, coverage_verified=True)
        self.assertEqual(self.balances(), before)

    def test_revoked_admin_cannot_append_service_rate_using_stale_actor(self):
        get_user_model().objects.filter(pk=self.admin.pk).update(is_staff=False)
        self.assertTrue(self.admin.is_staff)
        before = list(ServiceRateVersion.objects.values())
        with self.assertRaises(PermissionDenied):
            self.rate_for(self.record, '2', self.now)
        self.assertEqual(list(ServiceRateVersion.objects.values()), before)

    def test_two_identities_same_authorization_share_balance_and_fractional_remainder(self):
        second_sub = DeviceSubscription.objects.create(entitlement=self.record, name='另一设备',
            client='android', state='simulated')
        second_sub.lines.add(self.line)
        identity = NodeIdentity.objects.create(subscription=second_sub, line=self.line,
            ingress=self.ingress, generation=1, state='simulated')
        self.sample()
        self.sample(identity_id=identity.pk)
        self.sample(2, up=1001)
        self.sample(2, up=1001, identity_id=identity.pk)
        self.sample(3, up=1002)
        self.sample(3, up=1002, identity_id=identity.pk)
        self.cycle.refresh_from_db()
        self.assertEqual((self.cycle.used_bytes, self.cycle.raw_bytes, self.cycle.weighted_remainder), (5, 4, 0))
        self.assertEqual(UsageLedger.objects.count(), 4)
        self.assertEqual(set(UsageLedger.objects.values_list('grant_rate_version_id', flat=True)), {self.rate.pk})

    def test_rate_effective_exactly_at_endpoint_charges_old_then_new_rate(self):
        start = self.now - timedelta(seconds=30)
        checkpoint = self.now - timedelta(seconds=20)
        self.sample(at=start)
        changed = self.rate_for(self.record, '2', checkpoint)
        self.sample(2, up=1040, at=checkpoint)
        self.sample(3, up=1050, at=checkpoint + timedelta(seconds=10))
        self.assertEqual(list(UsageLedger.objects.order_by('pk').values_list('weighted_bytes', 'grant_rate_version_id')),
                         [(50, self.rate.pk), (20, changed.pk)])

    def test_replay_after_acceptance_is_read_only_and_old_path_cannot_double_charge(self):
        self.sample()
        accepted = self.sample(2, up=1040)
        before = self.balances()
        with CaptureQueriesContext(connection) as queries:
            repeated = self.sample(2, up=1040)
        self.assertEqual(repeated.pk, accepted.pk)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        for epoch in ('epoch-a', 'attempted-bypass'):
            with self.subTest(epoch=epoch), self.assertRaises(ValidationError):
                record_usage(self.identity.pk, self.server.pk, epoch, 999, 1040, 2000, self.now)
        self.assertEqual(self.balances(), before)

    def test_counter_reversal_and_new_epoch_cannot_charge_unseen_restart_total(self):
        self.sample()
        reversed_sample = self.sample(2, up=900)
        self.assertEqual(reversed_sample.status, 'gap')
        self.assertEqual(self.sample(3, up=1200).status, 'late')
        baseline = self.sample(1, up=1_000_000, down=2_000_000, epoch='replacement',
                               at=self.now - timedelta(seconds=60))
        self.assertEqual(baseline.status, 'baseline')
        self.assertFalse(UsageLedger.objects.exists())
        self.sample(2, up=1_000_004, down=2_000_000, epoch='replacement',
                    at=self.now - timedelta(seconds=50))
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 5)

    def test_unknown_counter_is_rejected_and_does_not_reset_existing_totals(self):
        self.sample()
        self.sample(2, up=1040)
        before = self.balances()
        for unknown in (None, '', False, -1):
            with self.subTest(value=unknown), self.assertRaises(ValidationError):
                self.sample(3, up=unknown)
        self.assertEqual(self.balances(), before)


class IngressMeteringConcurrencyReviewTests(ReviewFixture, TransactionTestCase):
    def test_concurrent_identical_samples_and_retry_never_double_charge(self):
        self.sample()
        gate = Barrier(2)
        values = dict(identity_id=self.identity.pk, server_id=self.server.pk, core_instance_id=self.core.pk,
            epoch='epoch-a', sequence=2, upload_bytes=1040, download_bytes=2000,
            observed_at=self.now - timedelta(seconds=100), coverage_verified=True, counter_kind='cumulative')

        def deliver():
            close_old_connections()
            try:
                gate.wait(timeout=10)
                try:
                    return record_ingress_sample(**values).pk
                except OperationalError as exc:
                    # SQLite 共享内存测试库可能立即报告锁竞争；失败请求不算成功，随后原样重试。
                    if 'locked' not in str(exc).lower():
                        raise
                    return None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(deliver) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        retry = record_ingress_sample(**values)
        self.assertTrue(all(value is None or value == retry.pk for value in results))
        self.assertEqual(IngressUsageSample.objects.count(), 2)
        self.assertEqual(UsageLedger.objects.count(), 1)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 50)
