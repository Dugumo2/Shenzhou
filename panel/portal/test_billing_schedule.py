"""已批准独立重置日期的用户场景；全部是假资产，不连接核心。"""
import json
import threading
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import include, path
from django.utils import timezone

from .billing_schedule import (BillingError, advance_billing_cycles, billing_state,
    lookup_cycle, parse_reset_at, preview_billing, save_billing)
from .entitlements import SHANGHAI, MeteringGap, assign_entitlement, current_cycle, record_usage
from .models import (BillingCycle, BillingPlan, BillingPlanRevision, DeploymentJob, DeviceSubscription,
    Egress, Entitlement, Ingress, Line, LineRateVersion, NodeIdentity, Server, UsageLedger, UsageStream)

urlpatterns = [path('api/v1/', include('portal.billing_urls'))]
GB = 1_000_000_000


class BillingFixture:
    def setUp(self):
        super().setUp()
        self.now = datetime(2028, 1, 10, 12, 0, tzinfo=SHANGHAI)
        self.clock = patch('django.utils.timezone.now', return_value=self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        users = get_user_model()
        self.admin = users.objects.create_user('billing-admin', is_staff=True)
        self.second_admin = users.objects.create_user('billing-second-admin', is_staff=True)
        self.user = users.objects.create_user('billing-alice')
        self.bob = users.objects.create_user('billing-bob')
        self.record = Entitlement.objects.create(user=self.user, quota_bytes=100 * GB,
            activated_at=datetime(2028, 1, 1, 9, 0, tzinfo=SHANGHAI),
            expires_at=datetime(2028, 12, 31, 9, 0, tzinfo=SHANGHAI), revision=4, applied_revision=4,
            applied_snapshot={'quota_bytes': 100 * GB, 'reset_day': 1, 'reset_hour': 9, 'reset_minute': 0},
            usage_updated_at=self.now, state='simulated', reset_day=1, reset_hour=9, reset_minute=0)
        self.cycle = BillingCycle.objects.create(entitlement=self.record, starts_at=self.record.activated_at,
            ends_at=datetime(2028, 2, 1, 9, 0, tzinfo=SHANGHAI), used_bytes=30 * GB,
            raw_bytes=24 * GB, weighted_remainder=333333)
        self.server = Server.objects.create(name='账期假机器')
        self.ingress = Ingress.objects.create(server=self.server, name='假入口', protocol='reality')
        self.line = Line.objects.create(name='假线路', ingress=self.ingress,
            egress=Egress.objects.create(server=self.server, name='假出口', kind='direct'))
        self.record.lines.set([self.line])
        self.sub = DeviceSubscription.objects.create(entitlement=self.record, name='假交付', client='windows', state='simulated')
        self.identity = NodeIdentity.objects.create(subscription=self.sub, line=self.line, ingress=self.ingress,
            generation=1, state='simulated')
        self.stream = UsageStream.objects.create(identity=self.identity, epoch='fake-epoch', sequence=1,
            upload_bytes=12 * GB, download_bytes=12 * GB)
        self.rate = LineRateVersion.objects.create(line=self.line, actor=self.admin,
            multiplier='1.25', effective_at=self.record.activated_at, reason='假倍率')
        self.ledger = UsageLedger.objects.create(cycle=self.cycle, stream=self.stream, sequence=1,
            upload_delta=12 * GB, download_delta=12 * GB, weighted_bytes=30 * GB,
            observed_at=self.now, rate_version=self.rate, quality='metered')
        self.url = f'/api/v1/admin/services/{self.record.public_id}/billing'

    def prepare(self, selected='2028-01-15T09:00', revision=0, actor=None, record=None, key=None):
        actor, record = actor or self.admin, record or self.record
        preview = preview_billing(actor, record.public_id, selected, revision)
        return {'next_reset_at': selected, 'expected_billing_revision': revision,
            'preview_token': preview['preview_token'], 'idempotency_key': key or uuid.uuid4().hex}

    def save(self, **options):
        data = self.prepare(**options)
        return save_billing(options.get('actor') or self.admin, (options.get('record') or self.record).public_id, **data)

    def protected_values(self):
        self.record.refresh_from_db()
        self.cycle.refresh_from_db()
        self.sub.refresh_from_db()
        self.identity.refresh_from_db()
        return (self.record.quota_bytes, self.record.expires_at, self.record.revision, self.record.applied_revision,
            self.record.enabled, self.record.state, self.record.applied_snapshot, self.record.metering_gap,
            self.record.enforcement_epoch, self.cycle.pk, self.cycle.starts_at, self.cycle.used_bytes,
            self.cycle.raw_bytes, self.cycle.weighted_remainder, self.sub.generation, self.sub.token_version,
            self.sub.state, self.identity.credential_ref, self.identity.revoked_at,
            list(self.record.lines.values_list('pk', flat=True)), list(UsageLedger.objects.values()), list(UsageStream.objects.values()))


@override_settings(ROOT_URLCONF='portal.test_billing_schedule')
class BillingScheduleTests(BillingFixture, TestCase):
    def test_preview_and_get_are_pure_reads(self):
        self.client.force_login(self.admin)
        before = self.protected_values()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
            preview = self.client.post(self.url + '/preview', json.dumps({'next_reset_at': '2028-01-15T09:00',
                'expected_billing_revision': 0}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(before, self.protected_values())
        self.assertEqual(BillingPlan.objects.count(), 0)
        self.assertEqual(BillingPlanRevision.objects.count(), 0)
        data = preview.json()['data']
        self.assertEqual(data['shift_seconds'], -17 * 86400)
        self.assertEqual(data['remaining_bytes'], str(70 * GB))
        self.assertEqual(data['next_resets'], ['2028-02-15T09:00:00+08:00', '2028-03-15T09:00:00+08:00', '2028-04-15T09:00:00+08:00'])

    def test_save_only_moves_current_endpoint_and_plan(self):
        before = self.protected_values()
        result = self.save()
        self.assertEqual(before, self.protected_values())
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.ends_at, parse_reset_at('2028-01-15T09:00'))
        self.assertEqual(result['message'], '重置计划已保存')
        self.assertEqual(result['billing']['billing_revision'], 1)
        self.assertEqual(BillingPlanRevision.objects.get().before['next_reset_at'], '2028-02-01T09:00:00+08:00')
        self.assertEqual(DeploymentJob.objects.count(), 0)

    def test_delay_across_old_months_preserves_same_usage_then_resets_once(self):
        self.save(selected='2028-04-15T14:37')
        for now in (datetime(2028, 2, 1, 10, tzinfo=SHANGHAI), datetime(2028, 3, 1, 10, tzinfo=SHANGHAI)):
            with patch('django.utils.timezone.now', return_value=now):
                cycle = current_cycle(self.record)
            self.assertEqual(cycle.pk, self.cycle.pk)
            self.assertEqual(cycle.used_bytes, 30 * GB)
            self.assertEqual(self.record.cycles.count(), 1)
        boundary = parse_reset_at('2028-04-15T14:37')
        with patch('django.utils.timezone.now', return_value=boundary):
            cycle = current_cycle(self.record)
            again = current_cycle(self.record)
        self.assertEqual(cycle.pk, again.pk)
        self.assertEqual(cycle.starts_at, boundary)
        self.assertEqual(cycle.ends_at, parse_reset_at('2028-05-15T14:37'))
        self.assertEqual(cycle.used_bytes, 0)
        self.assertEqual(self.record.cycles.count(), 2)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 30 * GB)
        self.assertEqual(BillingPlan.objects.get().revision, 1)

    def test_31_anchor_retains_leap_february_then_march_31(self):
        result = self.save(selected='2028-01-31T14:37')
        self.assertEqual(result['billing']['plan']['anchor_day'], 31)
        with patch('django.utils.timezone.now', return_value=parse_reset_at('2028-01-31T14:37')):
            february = current_cycle(self.record)
        self.assertEqual(february.ends_at, parse_reset_at('2028-02-29T14:37'))
        with patch('django.utils.timezone.now', return_value=parse_reset_at('2028-02-29T14:37')):
            march = current_cycle(self.record)
        self.assertEqual(march.ends_at, parse_reset_at('2028-03-31T14:37'))

    def test_nonleap_month_preview(self):
        preview = preview_billing(self.admin, self.record.public_id, '2029-01-31T14:37', 0)
        self.assertEqual(preview['next_resets'][0], '2029-02-28T14:37:00+08:00')
        self.assertEqual(preview['next_resets'][1], '2029-03-31T14:37:00+08:00')

    def test_unchanged_short_month_preserves_31_anchor(self):
        self.record.reset_day = 31
        self.record.applied_snapshot['reset_day'] = 31
        self.record.save()
        self.cycle.ends_at = parse_reset_at('2028-02-29T09:00')
        self.cycle.save()
        result = self.save(selected='2028-02-29T09:00')
        self.assertEqual(result['billing']['plan']['anchor_day'], 31)
        preview = preview_billing(self.admin, self.record.public_id, '2028-02-29T09:00', 1)
        self.assertEqual(preview['next_resets'][0], '2028-03-31T09:00:00+08:00')

    def test_two_edits_keep_cycle_identity_and_prior_revision(self):
        first = self.save()
        second = self.save(selected='2028-03-20T11:15', revision=1)
        self.assertEqual(first['billing']['current_cycle']['id'], second['billing']['current_cycle']['id'])
        self.assertEqual(second['billing']['billing_revision'], 2)
        self.assertEqual(BillingPlanRevision.objects.count(), 2)
        self.assertEqual(second['billing']['used_bytes'], str(30 * GB))

    def test_get_unknown_usage_stays_unknown(self):
        self.record.usage_updated_at = None
        self.record.save()
        self.cycle.raw_bytes = None
        self.cycle.save()
        state = billing_state(self.admin, self.record.public_id)
        self.assertEqual(state['usage_quality'], 'unknown')
        self.assertIsNone(state['used_bytes'])
        self.assertIsNone(state['remaining_bytes'])
        self.assertIsNone(state['current_cycle']['raw_bytes'])
        self.assertIsNone(state['current_cycle']['used_bytes'])
        self.assertIsNone(state['current_cycle']['weighted_remainder'])

    def test_expiry_before_reset_is_visible_and_does_not_extend(self):
        self.record.expires_at = parse_reset_at('2028-01-12T12:00')
        self.record.save()
        preview = preview_billing(self.admin, self.record.public_id, '2028-01-15T09:00', 0)
        self.assertTrue(preview['expires_before_reset'])
        expiry = self.record.expires_at
        self.save()
        with patch('django.utils.timezone.now', return_value=parse_reset_at('2028-01-16T12:00')):
            self.assertIsNone(current_cycle(self.record))
        self.record.refresh_from_db()
        self.assertEqual(self.record.expires_at, expiry)
        self.assertEqual(self.record.state, 'simulated')
        self.assertEqual(self.record.cycles.count(), 1)

    def test_suspended_or_gap_service_is_never_resumed_by_date_save(self):
        self.record.state, self.record.suspension_reason, self.record.metering_gap = 'enforcement_pending', 'quota', True
        self.record.save()
        job = DeploymentJob.objects.create(actor=self.admin, entitlement=self.record, kind='enforce', revision=4,
            idempotency_key='billing-pending-stop', request_fingerprint='fake', payload={'reason': 'quota'})
        before = self.protected_values()
        self.save()
        self.assertEqual(before, self.protected_values())
        job.refresh_from_db()
        self.assertEqual(job.state, 'queued')
        self.assertEqual(job.revision, self.record.revision)

    def test_invalid_seconds_naive_formats_and_past_are_rejected(self):
        for value in ('garbage', '2028-02-30T12:00', '2028-01-20T12:00:01+08:00',
                      '2028-01-20T12:00:00.000001+08:00', '2028-01-20T12:00:00.000000001+08:00',
                      '2028-01-20 12:00', '2028-01-20T12:00:00', '2028-01-10T12:00', '2028-01-09T12:00', None):
            with self.subTest(value=value), self.assertRaises(BillingError) as caught:
                preview_billing(self.admin, self.record.public_id, value, 0)
            self.assertEqual(caught.exception.status, 422)
        self.assertEqual(BillingPlan.objects.count(), 0)

    def test_utc_input_is_normalized_to_shanghai(self):
        preview = preview_billing(self.admin, self.record.public_id, '2028-01-15T01:00:00Z', 0)
        self.assertEqual(preview['next_reset_at'], '2028-01-15T09:00:00+08:00')

    def test_permissions_and_unknown_service(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        for actor in (self.user, self.bob):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.patch(self.url, '{}', content_type='application/json').status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(f'/api/v1/admin/services/{uuid.uuid4()}/billing').status_code, 404)

    def test_preview_is_bound_to_actor_object_input_and_period(self):
        data = self.prepare()
        with self.assertRaises(BillingError):
            save_billing(self.second_admin, self.record.public_id, **data)
        changed = dict(data, next_reset_at='2028-01-16T09:00', idempotency_key=uuid.uuid4().hex)
        with self.assertRaises(BillingError):
            save_billing(self.admin, self.record.public_id, **changed)
        other = Entitlement.objects.create(user=self.bob, quota_bytes=100 * GB, activated_at=self.record.activated_at,
            expires_at=self.record.expires_at, usage_updated_at=self.now)
        BillingCycle.objects.create(entitlement=other, starts_at=other.activated_at, ends_at=self.cycle.ends_at)
        with self.assertRaises(BillingError):
            save_billing(self.admin, other.public_id, **data)
        self.cycle.ends_at += timedelta(minutes=1)
        self.cycle.save()
        with self.assertRaises(BillingError):
            save_billing(self.admin, self.record.public_id, **data)
        self.assertEqual(BillingPlan.objects.count(), 0)

    def test_preview_expiry_and_time_passed_revalidate(self):
        data = self.prepare()
        with patch('portal.billing_schedule.signing.loads', side_effect=signing.SignatureExpired()):
            with self.assertRaises(BillingError):
                save_billing(self.admin, self.record.public_id, **data)
        with patch('django.utils.timezone.now', return_value=parse_reset_at('2028-01-16T12:00')):
            with self.assertRaises(BillingError):
                save_billing(self.admin, self.record.public_id, **data)
        self.assertEqual(BillingPlan.objects.count(), 0)

    def test_success_replay_ignores_expired_preview_but_checks_current_permission(self):
        data = self.prepare()
        first = save_billing(self.admin, self.record.public_id, **data)
        with patch('portal.billing_schedule.signing.loads', side_effect=signing.SignatureExpired()):
            with patch('django.utils.timezone.now', return_value=parse_reset_at('2028-03-01T12:00')):
                again = save_billing(self.admin, self.record.public_id, **data)
        self.assertEqual(first, again)
        self.assertEqual(BillingPlanRevision.objects.count(), 1)
        self.admin.is_staff = False
        self.admin.save()
        with self.assertRaises(PermissionDenied):
            save_billing(self.admin, self.record.public_id, **data)

    def test_same_key_different_payload_and_stale_revision_conflict(self):
        first = self.prepare()
        stale = self.prepare(actor=self.second_admin)
        save_billing(self.admin, self.record.public_id, **first)
        with self.assertRaises(BillingError):
            save_billing(self.admin, self.record.public_id, **dict(first, next_reset_at='2028-01-16T09:00'))
        with self.assertRaises(BillingError):
            save_billing(self.second_admin, self.record.public_id, **stale)
        self.assertEqual(BillingPlanRevision.objects.count(), 1)

    def test_unmaterialized_gap_and_confirmed_later_period_refuse_without_writes(self):
        missing = Entitlement.objects.create(user=self.bob, quota_bytes=100 * GB,
            activated_at=self.record.activated_at, expires_at=self.record.expires_at)
        before = missing.cycles.count()
        self.assertFalse(billing_state(self.admin, missing.public_id)['can_modify'])
        with self.assertRaises(BillingError):
            self.prepare(record=missing)
        self.assertEqual(missing.cycles.count(), before)
        cycle = BillingCycle.objects.create(entitlement=missing, starts_at=missing.activated_at,
            ends_at=parse_reset_at('2028-02-01T09:00'))
        BillingCycle.objects.create(entitlement=missing, starts_at=cycle.ends_at,
            ends_at=parse_reset_at('2028-03-01T09:00'))
        with self.assertRaises(BillingError):
            self.prepare(selected='2028-04-01T09:00', record=missing)

    def test_not_activated_refuses_date_operation(self):
        self.record.activated_at = None
        self.record.save()
        self.assertFalse(billing_state(self.admin, self.record.public_id)['can_modify'])
        with self.assertRaises(BillingError):
            self.prepare()

    def test_new_end_must_be_strictly_after_observed_sample(self):
        self.ledger.observed_at = parse_reset_at('2028-01-20T12:00')
        self.ledger.save()
        for value in ('2028-01-20T12:00', '2028-01-20T11:59'):
            with self.assertRaises(BillingError):
                self.prepare(selected=value)
        self.assertTrue(self.prepare(selected='2028-01-20T12:01')['preview_token'])

    def test_confirmed_end_sample_blocks_reopening_even_when_delayed(self):
        self.ledger.observed_at = self.cycle.ends_at
        self.ledger.save()
        with self.assertRaises(BillingError):
            self.prepare(selected='2028-03-01T09:00')

    def test_late_sample_keeps_old_period_after_new_period_exists(self):
        self.save()
        with patch('django.utils.timezone.now', return_value=parse_reset_at('2028-01-16T12:00')):
            current_cycle(self.record)
            old_count = self.record.cycles.count()
            raw = record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 2, 12 * GB + 2, 12 * GB + 2,
                parse_reset_at('2028-01-12T12:00'))
        self.assertEqual(raw, 4)
        self.assertEqual(UsageLedger.objects.get(sequence=2).cycle_id, self.cycle.pk)
        self.assertEqual(self.record.cycles.count(), old_count)

    def test_historical_lookup_and_future_sample_cannot_create_periods(self):
        self.save()
        before = self.record.cycles.count()
        self.assertIsNone(current_cycle(self.record, parse_reset_at('2028-04-01T09:00')))
        self.assertIsNone(current_cycle(self.record, self.record.activated_at - timedelta(days=1)))
        self.assertEqual(self.record.cycles.count(), before)
        before_end = parse_reset_at('2028-01-15T09:00') - timedelta(seconds=10)
        with patch('django.utils.timezone.now', return_value=before_end):
            with self.assertRaises(MeteringGap):
                record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 2, 12 * GB + 2, 12 * GB + 2,
                    parse_reset_at('2028-01-15T09:00') + timedelta(seconds=1))
        self.assertEqual(self.record.cycles.count(), before)
        self.assertEqual(UsageLedger.objects.count(), 1)

    def test_clock_at_new_boundary_requires_real_checkpoint(self):
        self.save()
        boundary = parse_reset_at('2028-01-15T09:00')
        with patch('django.utils.timezone.now', return_value=boundary):
            record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 2, 12 * GB + 2, 12 * GB + 2, boundary)
        self.assertEqual(UsageLedger.objects.get(sequence=2).cycle_id, self.cycle.pk)
        with patch('django.utils.timezone.now', return_value=boundary + timedelta(seconds=1)):
            record_usage(self.identity.pk, self.server.pk, 'fake-epoch', 3, 12 * GB + 4, 12 * GB + 4,
                         boundary + timedelta(seconds=1))
        self.assertNotEqual(UsageLedger.objects.get(sequence=3).cycle_id, self.cycle.pk)
        self.assertEqual(self.record.cycles.count(), 2)

    def test_sample_arriving_after_preview_blocks_conflicting_save(self):
        data = self.prepare(selected='2028-01-20T12:00')
        self.ledger.observed_at = parse_reset_at('2028-01-20T12:00')
        self.ledger.save()
        with self.assertRaises(BillingError):
            save_billing(self.admin, self.record.public_id, **data)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.ends_at, parse_reset_at('2028-02-01T09:00'))
        self.assertEqual(BillingPlan.objects.count(), 0)

    def test_previous_cycle_timestamp_does_not_claim_new_cycle_zero_usage(self):
        self.save()
        with patch('django.utils.timezone.now', return_value=parse_reset_at('2028-01-15T09:00')):
            current_cycle(self.record)
            state = billing_state(self.admin, self.record.public_id)
        self.assertEqual(state['usage_quality'], 'unknown')
        self.assertIsNone(state['used_bytes'])
        self.assertIsNone(state['remaining_bytes'])
        self.assertEqual(state['current_cycle']['recorded_used_bytes'], '0')
        self.assertIsNone(state['current_cycle']['used_bytes'])

    def test_stale_or_unapplied_quota_never_claims_remaining(self):
        self.record.usage_updated_at -= timedelta(minutes=4)
        self.record.save()
        self.ledger.observed_at = self.record.usage_updated_at
        self.ledger.save()
        state = billing_state(self.admin, self.record.public_id)
        self.assertEqual(state['used_bytes'], str(30 * GB))
        self.assertEqual(state['usage_quality'], 'stale')
        self.assertIsNone(state['remaining_bytes'])
        self.record.usage_updated_at = self.now
        self.record.applied_revision = 0
        self.record.save()
        state = billing_state(self.admin, self.record.public_id)
        self.assertIsNone(state['quota_bytes'])
        self.assertIsNone(state['remaining_bytes'])

    def test_restart_catches_up_contiguously_once_and_preserves_history(self):
        self.save(selected='2028-01-31T14:37')
        with patch('django.utils.timezone.now', return_value=parse_reset_at('2028-05-01T12:00')):
            cycle = current_cycle(self.record)
            count = self.record.cycles.count()
            self.assertEqual(current_cycle(self.record).pk, cycle.pk)
        self.assertEqual(count, 5)
        periods = list(self.record.cycles.order_by('starts_at'))
        self.assertTrue(all(a.ends_at == b.starts_at for a, b in zip(periods, periods[1:])))
        self.assertEqual(periods[0].used_bytes, 30 * GB)
        self.assertEqual(BillingPlanRevision.objects.count(), 1)

    def test_old_form_disables_managed_reset_fields(self):
        from .v2_forms import ServiceAllocationForm
        form = ServiceAllocationForm(billing_managed=True)
        for name in ('reset_mode', 'reset_day', 'reset_time'):
            self.assertTrue(form.fields[name].disabled)
        self.assertFalse(form.fields['quota_gb'].disabled)

    def test_old_comprehensive_save_cannot_override_independent_plan(self):
        self.save()
        plan = BillingPlan.objects.get()
        assign_entitlement(self.admin, self.user, [self.line.pk], 150 * GB,
            reset_day=22, reset_hour=1, reset_minute=2, idempotency_key='old-total-save-plan')
        plan.refresh_from_db()
        self.record.refresh_from_db()
        self.assertEqual(plan.next_reset_at, parse_reset_at('2028-01-15T09:00'))
        self.assertEqual(plan.revision, 1)
        self.assertEqual((self.record.reset_day, self.record.reset_hour, self.record.reset_minute), (1, 9, 0))
        self.assertEqual(self.record.quota_bytes, 150 * GB)

    def test_partial_save_failure_rolls_back_endpoint_and_plan(self):
        data = self.prepare()
        with patch('portal.billing_schedule.BillingPlanRevision.objects.create', side_effect=RuntimeError('假事务故障')):
            with self.assertRaises(RuntimeError):
                save_billing(self.admin, self.record.public_id, **data)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.ends_at, parse_reset_at('2028-02-01T09:00'))
        self.assertEqual(BillingPlan.objects.count(), 0)

    def test_api_patch_envelope_rejects_extra_fields_and_boolean_revision(self):
        self.client.force_login(self.admin)
        data = self.prepare()
        response = self.client.patch(self.url, json.dumps(dict(data, quota_bytes=0)), content_type='application/json')
        self.assertEqual(response.status_code, 422)
        self.assertTrue(response.json()['error']['fields'])
        response = self.client.patch(self.url, json.dumps(dict(data, expected_billing_revision=True)), content_type='application/json')
        self.assertEqual(response.status_code, 422)
        response = self.client.patch(self.url, json.dumps(data), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['message'], '重置计划已保存')
        self.assertEqual(self.client.patch(self.url, json.dumps(data), content_type='application/json').json(), response.json())

    def test_api_invalid_json_wrong_method_and_csrf(self):
        self.client.force_login(self.admin)
        for body in ('{', '{\"next_reset_at\":\"2028-01-15T09:00\",\"expected_billing_revision\":0,\"expected_billing_revision\":0}',
                     '{\"next_reset_at\":NaN,\"expected_billing_revision\":0}'):
            response = self.client.post(self.url + '/preview', body, content_type='application/json')
            self.assertEqual(response.status_code, 422)
            self.assertIsNone(response.json()['data'])
        self.assertEqual(self.client.post(self.url, '{}', content_type='application/json').status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.patch(self.url, json.dumps(self.prepare()), content_type='application/json').status_code, 403)


class BillingConcurrencyTests(BillingFixture, TransactionTestCase):
    def test_two_simultaneous_admins_only_one_endpoint_wins(self):
        first = self.prepare(selected='2028-01-15T09:00', actor=self.admin)
        second = self.prepare(selected='2028-01-16T09:00', actor=self.second_admin)
        gate = threading.Barrier(2)
        results, errors = [], []

        def worker(actor_id, data):
            close_old_connections()
            try:
                actor = get_user_model().objects.get(pk=actor_id)
                gate.wait(timeout=10)
                results.append(save_billing(actor, self.record.public_id, **data))
            except BillingError as exc:
                errors.append(exc.code)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker, args=(self.admin.pk, first)),
                   threading.Thread(target=worker, args=(self.second_admin.pk, second))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=25)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(BillingPlanRevision.objects.count(), 1)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.used_bytes, 30 * GB)
        self.assertEqual(self.record.cycles.count(), 1)


class BillingPlanMigrationTests(TransactionTestCase):
    def test_plan_migration_preserves_old_periods_raw_unknown_and_ledger(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        old_target = [('portal', '0011_identity_scope_reuse')]
        try:
            executor.migrate(old_target)
            old = executor.loader.project_state(old_target).apps
            User = old.get_model('auth', 'User')
            Ent = old.get_model('portal', 'Entitlement')
            Cycle = old.get_model('portal', 'BillingCycle')
            Ledger = old.get_model('portal', 'UsageLedger')
            actor = User.objects.create(username='旧计划假管理员', is_staff=True)
            user = User.objects.create(username='旧计划假用户')
            now = timezone.now()
            record = Ent.objects.create(user=user, quota_bytes=100 * GB, activated_at=now - timedelta(days=1),
                expires_at=now + timedelta(days=90), reset_day=31, reset_hour=14, reset_minute=37, revision=7)
            cycle = Cycle.objects.create(entitlement=record, starts_at=record.activated_at,
                ends_at=now + timedelta(days=20), used_bytes=123456789, raw_bytes=None, weighted_remainder=777777)
            server = old.get_model('portal', 'Server').objects.create(name='旧计划假机')
            ingress = old.get_model('portal', 'Ingress').objects.create(server=server, name='假入口', protocol='reality')
            egress = old.get_model('portal', 'Egress').objects.create(server=server, name='假出口', kind='direct')
            line = old.get_model('portal', 'Line').objects.create(name='假线路', ingress=ingress, egress=egress)
            sub = old.get_model('portal', 'DeviceSubscription').objects.create(entitlement=record, name='假交付', client='windows')
            identity = old.get_model('portal', 'NodeIdentity').objects.create(subscription=sub, line=line, ingress=ingress, generation=1)
            stream = old.get_model('portal', 'UsageStream').objects.create(identity=identity, epoch='old-fake', sequence=1)
            Ledger.objects.create(cycle=cycle, stream=stream, sequence=1, upload_delta=3, download_delta=4,
                weighted_bytes=None, quality='legacy_unknown', observed_at=now)
            before_record = list(Ent.objects.values())
            before_cycles = list(Cycle.objects.values())
            before_ledger = list(Ledger.objects.values())
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            upgraded = executor.loader.project_state(latest).apps
            self.assertEqual(list(upgraded.get_model('portal', 'Entitlement').objects.values()), before_record)
            self.assertEqual(list(upgraded.get_model('portal', 'BillingCycle').objects.values()), before_cycles)
            self.assertEqual(list(upgraded.get_model('portal', 'UsageLedger').objects.values()), before_ledger)
            self.assertEqual(upgraded.get_model('portal', 'BillingPlan').objects.count(), 0)
            self.assertEqual(upgraded.get_model('portal', 'BillingPlanRevision').objects.count(), 0)
        finally:
            MigrationExecutor(connection).migrate(latest)
