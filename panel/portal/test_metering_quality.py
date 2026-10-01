"""R08/R13 的账期归属与所有入口计量证据；仅使用隔离测试库。"""
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .api_helpers import project_service
from .billing_schedule import billing_state, parse_reset_at
from .entitlements import (MeteringGap, acknowledge_metering_recovery,
                           record_usage, summary_for)
from .models import NodeIdentity, UsageLedger, UsageStream
from .test_billing_schedule import BillingFixture, GB


class MeteringQualityTests(BillingFixture, TestCase):
    def projections(self, quality, used=None, remaining=None):
        """用户、管理员账期和旧模板入口采用同一计量依据。"""
        self.record.refresh_from_db()
        api = project_service(self.record, 'entitlement')
        billing = billing_state(self.admin, self.record.public_id)
        summary = summary_for(self.user)
        self.assertEqual(api['usage']['quality'], 'measured' if quality == 'fresh' else quality)
        self.assertEqual(billing['usage_quality'], quality)
        for data in (api, billing):
            self.assertEqual(data['used_bytes'], str(used) if used is not None else None)
            self.assertEqual(data['remaining_bytes'], str(remaining) if remaining is not None else None)
        self.assertEqual(summary['used_bytes'], used)
        self.assertEqual(summary['remaining_bytes'], remaining)
        self.assertEqual(summary['usage_fresh'], quality == 'fresh')
        return api, billing

    def other_identity(self, *, retired=False, observed_at=None):
        identity = NodeIdentity.objects.create(subscription=self.sub, line=self.line,
            ingress=self.ingress, generation=2, state='simulated')
        stream = UsageStream.objects.create(identity=identity, epoch='second-entry', sequence=1,
            retired_at=self.now if retired else None)
        if observed_at is not None:
            UsageLedger.objects.create(cycle=self.cycle, stream=stream, sequence=1,
                upload_delta=0, download_delta=0, weighted_bytes=0,
                rate_version=self.rate, quality='metered', observed_at=observed_at)
        return identity, stream

    def test_one_entry_with_no_stream_blocks_complete_remaining_without_read_writes(self):
        NodeIdentity.objects.create(subscription=self.sub, line=self.line,
            ingress=self.ingress, generation=2, state='simulated')
        with CaptureQueriesContext(connection) as queries:
            self.projections('gap')
        self.assertFalse(any(query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                             for query in queries))
        self.record.refresh_from_db()
        self.assertFalse(self.record.metering_gap, '只读投影不能偷偷修改持久缺口标记')

    def test_one_entry_with_empty_current_stream_blocks_complete_remaining(self):
        self.other_identity()
        self.projections('gap')

    def test_one_entry_with_only_previous_period_sample_blocks_complete_remaining(self):
        identity, stream = self.other_identity()
        UsageLedger.objects.create(cycle=self.cycle, stream=stream, sequence=1,
            upload_delta=0, download_delta=0, weighted_bytes=0,
            rate_version=self.rate, quality='metered', observed_at=self.cycle.starts_at - timedelta(seconds=1))
        self.projections('gap')

    def test_every_entry_must_be_fresh_even_when_service_timestamp_is_fresh(self):
        self.other_identity(observed_at=self.now - timedelta(minutes=4))
        self.projections('stale', used=30 * GB)

    def test_every_current_entry_evidence_establishes_observed_remaining(self):
        self.other_identity(observed_at=self.now)
        self.projections('fresh', used=30 * GB, remaining=70 * GB)

    def test_future_entry_sample_cannot_be_masked_by_fresh_service_timestamp(self):
        self.other_identity(observed_at=self.now + timedelta(seconds=31))
        self.projections('unknown')

    def test_revoked_entry_does_not_require_current_samples(self):
        identity, stream = self.other_identity()
        identity.revoked_at, identity.state = self.now, 'revoked'
        identity.save(update_fields=['revoked_at', 'state'])
        self.projections('fresh', used=30 * GB, remaining=70 * GB)
        self.assertTrue(UsageStream.objects.filter(pk=stream.pk).exists())

    def test_all_entries_revoked_cannot_make_historical_samples_current_evidence(self):
        self.identity.revoked_at, self.identity.state = self.now, 'revoked'
        self.identity.save(update_fields=['revoked_at', 'state'])
        _, billing = self.projections('unknown')
        self.assertEqual(billing['current_cycle']['recorded_used_bytes'], str(30 * GB))
        self.assertTrue(UsageLedger.objects.filter(pk=self.ledger.pk).exists())

    def test_retired_epoch_does_not_mask_a_current_epoch_without_sample(self):
        self.stream.retired_at = self.now
        self.stream.save(update_fields=['retired_at'])
        UsageStream.objects.create(identity=self.identity, epoch='new-empty-epoch')
        self.projections('unknown')

    def test_fresh_current_epoch_is_not_made_stale_by_retired_epoch(self):
        self.stream.retired_at = self.now
        self.stream.save(update_fields=['retired_at'])
        self.ledger.observed_at = self.now - timedelta(minutes=4)
        self.ledger.save(update_fields=['observed_at'])
        stream = UsageStream.objects.create(identity=self.identity, epoch='new-observed-epoch', sequence=1)
        UsageLedger.objects.create(cycle=self.cycle, stream=stream, sequence=1,
            upload_delta=0, download_delta=0, weighted_bytes=0,
            rate_version=self.rate, quality='metered', observed_at=self.now)
        self.projections('fresh', used=30 * GB, remaining=70 * GB)

    def test_two_unretired_epochs_are_an_explicit_gap(self):
        UsageStream.objects.create(identity=self.identity, epoch='ambiguous-live-epoch')
        self.projections('gap')

    def test_latest_nonmetered_sample_cannot_be_masked_by_older_metered_sample(self):
        UsageLedger.objects.create(cycle=self.cycle, stream=self.stream, sequence=2,
            upload_delta=0, download_delta=0, weighted_bytes=None,
            quality='legacy_unknown', observed_at=self.now)
        self.projections('unknown')

    def test_candidate_without_current_sample_does_not_prove_complete_remaining(self):
        NodeIdentity.objects.create(subscription=self.sub, line=self.line,
            ingress=self.ingress, generation=2, state='candidate')
        self.projections('gap')

    def test_no_identity_or_no_sample_never_claims_zero_or_remaining(self):
        self.ledger.delete()
        self.stream.delete()
        self.identity.delete()
        _, billing = self.projections('unknown')
        self.assertEqual(billing['current_cycle']['recorded_used_bytes'], str(30 * GB))

    def test_duplicate_samples_preserve_the_ledger_and_current_quality(self):
        before = list(UsageLedger.objects.values())
        self.assertEqual(record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 1,
                                     12 * GB, 12 * GB, self.now), 0)
        self.assertEqual(before, list(UsageLedger.objects.values()))
        self.projections('fresh', used=30 * GB, remaining=70 * GB)

    def test_current_period_read_does_not_accept_old_period_end_as_evidence(self):
        boundary = self.cycle.ends_at
        with patch('django.utils.timezone.now', return_value=boundary):
            record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 2,
                         12 * GB + 100, 12 * GB + 100, boundary)
            self.projections('unknown')
        self.assertEqual(UsageLedger.objects.get(sequence=2).cycle_id, self.cycle.pk)

    def test_skipped_period_nonzero_boundary_preserves_old_account_and_persists_gap(self):
        before_ledger = list(UsageLedger.objects.values())
        before_stream = list(UsageStream.objects.values())
        old_used, old_raw = self.cycle.used_bytes, self.cycle.raw_bytes
        boundary = parse_reset_at('2028-04-01T09:00')
        with patch('django.utils.timezone.now', return_value=boundary):
            with self.assertRaises(MeteringGap):
                record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 2,
                             12 * GB + 100, 12 * GB + 100, boundary)
        self.cycle.refresh_from_db()
        self.record.refresh_from_db()
        self.assertEqual((self.cycle.used_bytes, self.cycle.raw_bytes), (old_used, old_raw))
        self.assertEqual(before_ledger, list(UsageLedger.objects.values()))
        self.assertEqual(before_stream, list(UsageStream.objects.values()))
        self.assertTrue(self.record.metering_gap)
        self.assertEqual(self.record.state, 'enforcement_pending')
        self.assertEqual(self.record.jobs.get(kind='enforce').payload['reason'], 'metering_gap')

    def test_skipped_period_zero_difference_creates_only_current_zero_checkpoint(self):
        boundary = parse_reset_at('2028-04-01T09:00')
        with patch('django.utils.timezone.now', return_value=boundary):
            self.assertEqual(record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 2,
                                         12 * GB, 12 * GB, boundary), 0)
        ledger = UsageLedger.objects.get(sequence=2)
        self.assertEqual(ledger.cycle.starts_at, boundary)
        self.assertEqual(ledger.weighted_bytes, 0)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 30 * GB)
        self.assertEqual(self.cycle.ledger.count(), 1)

    def test_adjacent_boundary_still_charges_only_old_period_then_new_sample(self):
        boundary = self.cycle.ends_at
        with patch('django.utils.timezone.now', return_value=boundary + timedelta(seconds=1)):
            record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 2,
                         12 * GB + 100, 12 * GB + 100, boundary)
            record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 3,
                         12 * GB + 200, 12 * GB + 200, boundary + timedelta(seconds=1))
            self.projections('fresh', used=250, remaining=100 * GB - 250)
        self.assertEqual(UsageLedger.objects.get(sequence=2).cycle_id, self.cycle.pk)
        self.assertNotEqual(UsageLedger.objects.get(sequence=3).cycle_id, self.cycle.pk)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 30 * GB + 250)

    def test_zero_new_epoch_cannot_replace_missing_old_epoch_period_boundary(self):
        boundary = self.cycle.ends_at + timedelta(seconds=1)
        with patch('django.utils.timezone.now', return_value=boundary):
            with self.assertRaises(MeteringGap):
                record_usage(self.identity.pk, self.server.pk, 'new-epoch', 1, 0, 0, boundary)
        self.stream.refresh_from_db()
        self.record.refresh_from_db()
        self.assertIsNone(self.stream.retired_at)
        self.assertEqual(self.identity.streams.count(), 1)
        self.assertTrue(self.record.metering_gap)

    def test_restart_after_real_old_epoch_boundary_can_start_new_epoch(self):
        boundary = self.cycle.ends_at
        with patch('django.utils.timezone.now', return_value=boundary + timedelta(seconds=1)):
            record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 2,
                         12 * GB + 100, 12 * GB + 100, boundary)
            record_usage(self.identity.pk, self.server.pk, 'new-epoch', 1, 0, 0, boundary + timedelta(seconds=1))
            self.projections('fresh', used=0, remaining=100 * GB)
        self.stream.refresh_from_db()
        self.assertIsNotNone(self.stream.retired_at)
        self.assertEqual(self.identity.streams.filter(retired_at__isnull=True).count(), 1)

    def test_recovery_cannot_clear_gap_with_missing_stale_or_ambiguous_current_epoch(self):
        self.record.metering_gap = True
        self.record.save(update_fields=['metering_gap'])
        identity, stream = self.other_identity(observed_at=self.now - timedelta(minutes=4))
        with self.assertRaises(ValidationError):
            acknowledge_metering_recovery(self.admin, self.record.pk)
        UsageLedger.objects.filter(stream=stream).update(observed_at=self.now)
        extra = UsageStream.objects.create(identity=identity, epoch='ambiguous-recovery')
        with self.assertRaises(ValidationError):
            acknowledge_metering_recovery(self.admin, self.record.pk)
        extra.delete()
        acknowledge_metering_recovery(self.admin, self.record.pk)
        self.record.refresh_from_db()
        self.assertFalse(self.record.metering_gap)
        self.projections('fresh', used=30 * GB, remaining=70 * GB)

    def test_overlapping_periods_have_the_same_gap_in_all_projections(self):
        self.record.cycles.create(starts_at=self.now - timedelta(hours=1),
                                  ends_at=self.now + timedelta(hours=1), raw_bytes=0)
        self.projections('gap')
