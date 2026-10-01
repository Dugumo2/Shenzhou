"""第二阶段后台隔离回归：不连接线上核心，不使用真实凭据。"""
import io
import json
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .delivery import (create_subscription, disable_subscription, refresh_subscription,
                       reset_subscription, resolve_subscription, token_for_subscription)
from .entitlements import (SHANGHAI, add_months, assign_entitlement, current_cycle,
                           next_reset, record_usage, summary_for)
from .jobs import (build_plan, claim_job, finish_job, queue_due_enforcements, retry_job, run_job)
from .jobs import queue_resumptions
from .models import (BillingCycle, DeploymentJob, DeviceSubscription, Egress, Entitlement,
                     Ingress, Line, LineRateVersion, Membership, NodeIdentity, Server, UsageLedger)


class IsolatedAdapter:
    evidence_scope = 'isolated'

    def apply(self, plan):
        return {'digest': plan['digest'], 'scope': self.evidence_scope, 'verified': True,
                'revision': plan['revision'], 'active_ids': plan['activate_ids'],
                'revoked_ids': plan['revoke_ids'], 'suspended_ids': plan.get('suspend_ids', []), 'connections_terminated': True,
                'must_not_persist_secret': 'isolated-sentinel'}


class BackendTest(TestCase):
    def setUp(self):
        users = get_user_model()
        self.admin = users.objects.create_user('staff', is_staff=True)
        self.alice = users.objects.create_user('alice')
        self.bob = users.objects.create_user('bob')
        self.server = Server.objects.create(name='隔离测试机')
        ingress = Ingress.objects.create(server=self.server, name='隔离入口', protocol='reality')
        self.line = Line.objects.create(name='住宅出口', ingress=ingress,
            egress=Egress.objects.create(server=self.server, name='测试链路', kind='upstream'))
        self.other = Line.objects.create(name='机房出口', ingress=ingress,
            egress=Egress.objects.create(server=self.server, name='测试机房', kind='direct'))
        # 仅隔离夹具明确1倍，真实线路不会迁移成演示倍率。
        for line in (self.line, self.other):
            LineRateVersion.objects.create(line=line, multiplier=1, actor=self.admin,
                effective_at=timezone.now() - timedelta(days=365), reason='隔离测试明确倍率')

    def assign(self, **kwargs):
        values = {'actor': self.admin, 'user': self.alice, 'line_ids': [self.line.pk],
                  'quota_bytes': 1_000_000_000_000, 'reset_day': 31, 'service_months': 3,
                  'idempotency_key': 'assign-alice'}
        values.update(kwargs)
        return assign_entitlement(**values)

    def activate_fixture(self):
        record, job = self.assign()
        run_job(job.public_id, IsolatedAdapter())
        record.refresh_from_db()
        return record

    def create_fixture(self):
        record = self.activate_fixture()
        sub, job = create_subscription(self.alice, '我的手机', 'android', [self.line.pk], 'create-phone')
        run_job(job.public_id, IsolatedAdapter())
        sub.refresh_from_db()
        return record, sub

    def test_admin_scope_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.assign(actor=self.bob)
        with self.assertRaises(ValidationError):
            self.assign(reset_day=32)
        with self.assertRaises(ValidationError):
            self.assign(quota_bytes=0)
        self.assertEqual(Entitlement.objects.count(), 0)

    def test_atomic_outbox_and_retry_idempotency(self):
        record, job = self.assign()
        again, same = self.assign()
        self.assertEqual(job.pk, same.pk)
        self.assertEqual(record.pk, again.pk)
        with self.assertRaises(ValidationError):
            self.assign(quota_bytes=99)
        self.assertEqual(DeploymentJob.objects.count(), 1)
        with patch('portal.jobs.enqueue', side_effect=RuntimeError('test-only')):
            with self.assertRaises(RuntimeError):
                self.assign(idempotency_key='next-assignment', quota_bytes=99)
        record.refresh_from_db()
        self.assertEqual(record.quota_bytes, 1_000_000_000_000)
        self.assertEqual(record.revision, 1)

    def test_no_production_adapter_is_blocked_not_applied(self):
        record, job = self.assign()
        result = run_job(job.public_id)
        self.assertEqual(result.state, 'blocked')
        record.refresh_from_db()
        self.assertIsNone(record.activated_at)
        self.assertEqual(record.applied_revision, 0)
        self.assertIsNone(summary_for(self.alice)['quota_gb'])
        with self.assertRaises(PermissionDenied):
            retry_job(self.alice, job.public_id)
        retry_job(self.admin, job.public_id)
        self.assertEqual(run_job(job.public_id, IsolatedAdapter()).state, 'simulated')

    def test_simulated_receipt_never_enables_download(self):
        record, sub = self.create_fixture()
        with self.assertRaises(ValidationError):
            token_for_subscription(self.alice, sub.public_id)
        self.assertEqual(summary_for(self.alice)['quota_gb'], 1000)
        self.assertFalse(summary_for(self.alice)['applied'])
        job = DeploymentJob.objects.get(subscription=sub)
        self.assertNotIn('must_not_persist_secret', job.receipt)

    def test_receipt_mismatch_not_applied(self):
        record, job = self.assign()
        adapter = IsolatedAdapter()
        adapter.apply = lambda plan: {'digest': 'wrong'}
        self.assertEqual(run_job(job.public_id, adapter).state, 'failed')
        record.refresh_from_db()
        self.assertIsNone(record.activated_at)

    def test_adapter_cannot_self_approve_production(self):
        record, job = self.assign()
        adapter = IsolatedAdapter()
        adapter.evidence_scope = 'production'
        adapter.verified_protocols = {'reality'}
        result = run_job(job.public_id, adapter)
        self.assertEqual(result.state, 'blocked')
        self.assertEqual(result.result_code, 'PRODUCTION_ADAPTER_NOT_APPROVED')

    def test_month_end_anchor_and_shanghai_boundary(self):
        start = datetime(2027, 1, 31, 12, 30, tzinfo=SHANGHAI)
        february = add_months(start, 1)
        self.assertEqual(february.day, 28)
        self.assertEqual(add_months(february, 1, 31).day, 31)
        self.assertEqual(add_months(start, 3).day, 30)
        boundary = next_reset(start, 31)
        self.assertEqual(boundary, datetime(2027, 2, 28, tzinfo=SHANGHAI))
        self.assertEqual(boundary.astimezone(dt_timezone.utc).hour, 16)

    def test_activation_starts_after_verified_apply(self):
        record, job = self.assign()
        self.assertIsNone(record.expires_at)
        expected = datetime(2027, 1, 31, 12, tzinfo=SHANGHAI)
        with patch('django.utils.timezone.now', return_value=expected):
            run_job(job.public_id, IsolatedAdapter())
        record.refresh_from_db()
        self.assertEqual(record.expires_at.astimezone(SHANGHAI), add_months(expected, 3))

    def test_ownership_and_granted_line_enforced(self):
        record, sub = self.create_fixture()
        with self.assertRaises(PermissionDenied):
            reset_subscription(self.bob, sub.public_id, 'bob-reset-key')
        with self.assertRaises(PermissionDenied):
            create_subscription(self.alice, '越权线路', 'windows', [self.other.pk], 'forbidden-lines')
        with self.assertRaises(ValidationError):
            create_subscription(self.alice, '未知软件', 'router', [self.line.pk], 'unsupported-key')

    def test_one_line_generates_each_protocol_identity(self):
        self.activate_fixture()
        extra = Ingress.objects.create(server=self.server, name='HY2入口', protocol='hy2')
        self.line.additional_ingresses.add(extra)
        sub, job = create_subscription(self.alice, '多协议手机', 'android', [self.line.pk], 'multiprotocol-key')
        identities = list(sub.identities.select_related('ingress'))
        self.assertEqual(len(identities), 2)
        self.assertEqual({x.ingress.protocol for x in identities}, {'reality', 'hy2'})
        self.assertEqual(len({x.credential_ref for x in identities}), 2)
        plan = build_plan(claim_job(job.public_id))
        self.assertEqual(set(plan['protocols']), {'reality', 'hy2'})
        self.assertEqual(len(plan['identity_ingresses']), 2)

    def test_additional_ingress_on_other_server_rejected(self):
        second = Server.objects.create(name='另一机')
        extra = Ingress.objects.create(server=second, name='跨机入口', protocol='ws')
        self.line.additional_ingresses.add(extra)
        with self.assertRaises(ValidationError):
            self.assign()

    def test_inactive_account_queues_server_suspend(self):
        record, sub = self.create_fixture()
        self.alice.is_active = False
        self.alice.save()
        self.assertEqual(queue_due_enforcements(), 1)
        self.assertEqual(record.jobs.get(kind='enforce').payload['reason'], 'account_disabled')

    def test_reset_tombstones_survive_failure_retry(self):
        record, sub = self.create_fixture()
        other, other_job = create_subscription(self.alice, '电脑', 'windows', [self.line.pk], 'create-computer')
        run_job(other_job.public_id, IsolatedAdapter())
        old_id = sub.identities.get()
        sub, job = reset_subscription(self.alice, sub.public_id, 'reset-phone-key')
        self.assertEqual(sub.generation, 2)
        old_id.refresh_from_db()
        self.assertIsNotNone(old_id.revoked_at)
        self.assertEqual(run_job(job.public_id).state, 'blocked')
        retry_job(self.admin, job.public_id)
        result = run_job(job.public_id, IsolatedAdapter())
        self.assertEqual(result.state, 'simulated')
        old_id.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(old_id.state, 'revoked')
        self.assertEqual(other.state, 'simulated')
        again, old_job = reset_subscription(self.alice, sub.public_id, 'reset-phone-key')
        self.assertEqual(again.generation, 2)
        self.assertEqual(old_job.pk, job.pk)

    def test_refresh_preserves_identity_and_token_version(self):
        record, sub = self.create_fixture()
        old_ids = list(sub.identities.values_list('pk', flat=True))
        refreshed, job = refresh_subscription(self.alice, sub.public_id, 'refresh-phone')
        run_job(job.public_id, IsolatedAdapter())
        self.assertEqual(refreshed.token_version, sub.token_version)
        self.assertEqual(refreshed.generation, sub.generation)
        self.assertEqual(list(refreshed.identities.values_list('pk', flat=True)), old_ids)

    def test_lease_expiry_and_stale_callback(self):
        record, job = self.assign()
        first = claim_job(job.public_id)
        self.assertIsNone(claim_job(job.public_id))
        DeploymentJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        Server.objects.filter(pk=self.server.pk).update(deployment_lease_until=timezone.now() - timedelta(seconds=1))
        second = claim_job(job.public_id)
        self.assertNotEqual(first.lease_id, second.lease_id)
        with self.assertRaises(ValidationError):
            finish_job(first.pk, first.lease_id, build_plan(first), IsolatedAdapter().apply(build_plan(first)))

    def test_new_revision_supersedes_old_job(self):
        record, old = self.assign()
        self.assign(idempotency_key='new-assignment', quota_bytes=123)
        self.assertEqual(run_job(old.public_id, IsolatedAdapter()).state, 'superseded')

    def test_same_entitlement_publications_are_serial(self):
        record, sub = self.create_fixture()
        _, first = refresh_subscription(self.alice, sub.public_id, 'refresh-first')
        _, second = refresh_subscription(self.alice, sub.public_id, 'refresh-second')
        self.assertIsNotNone(claim_job(first.public_id))
        self.assertIsNone(claim_job(second.public_id))

    def test_worker_reclaims_expired_running_job(self):
        record, job = self.assign()
        claim_job(job.public_id)
        DeploymentJob.objects.filter(pk=job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        Server.objects.filter(pk=self.server.pk).update(deployment_lease_until=timezone.now() - timedelta(seconds=1))
        call_command('phase1_worker', stdout=io.StringIO())
        job.refresh_from_db()
        self.assertEqual(job.state, 'blocked')
        self.assertEqual(job.attempts, 2)

    def test_build_plan_failure_does_not_leave_running(self):
        record, job = self.assign()
        with patch('portal.jobs.build_plan', side_effect=RuntimeError('do-not-log-sensitive-error')):
            result = run_job(job.public_id, IsolatedAdapter())
        self.assertEqual(result.state, 'failed')
        self.assertEqual(result.result_code, 'ADAPTER_EXECUTION_FAILED')

    def test_usage_dedup_restart_epoch_and_ingress_scope(self):
        record, sub = self.create_fixture()
        identity = sub.identities.get()
        now = timezone.now()
        self.assertEqual(record_usage(identity.pk, self.server.pk, 'boot-a', 1, 100, 200, now), 300)
        self.assertEqual(record_usage(identity.pk, self.server.pk, 'boot-a', 1, 100, 200, now), 0)
        self.assertEqual(record_usage(identity.pk, self.server.pk, 'boot-a', 2, 120, 300, now + timedelta(seconds=1)), 120)
        self.assertEqual(record_usage(identity.pk, self.server.pk, 'boot-b', 1, 10, 20, now + timedelta(seconds=2)), 30)
        self.assertEqual(record_usage(identity.pk, self.server.pk, 'boot-a', 3, 150, 350, now + timedelta(seconds=3)), 0)
        self.assertEqual(record.cycles.get().used_bytes, 450)
        with self.assertRaises(PermissionDenied):
            record_usage(identity.pk, self.server.pk + 1, 'boot-b', 2, 15, 25, now + timedelta(seconds=3))
        with self.assertRaises(ValidationError):
            record_usage(identity.pk, self.server.pk, 'boot-b', 2, 0, 0, now + timedelta(seconds=3))
        self.assertEqual(UsageLedger.objects.count(), 3)

    def test_quota_queues_real_enforcement(self):
        record, sub = self.create_fixture()
        record.applied_snapshot['quota_bytes'] = 100
        record.save()
        identity = sub.identities.get()
        record_usage(identity.pk, self.server.pk, 'boot-a', 1, 100, 100, timezone.now())
        record.refresh_from_db()
        self.assertEqual(record.state, 'enforcement_pending')
        job = record.jobs.get(kind='enforce')
        self.assertEqual(run_job(job.public_id).state, 'blocked')
        identity.refresh_from_db()
        self.assertEqual(identity.state, 'suspension_pending')
        self.assertIsNone(identity.revoked_at)

    def test_expiry_enforcement_is_idempotent(self):
        record, sub = self.create_fixture()
        record.expires_at = timezone.now() - timedelta(seconds=1)
        record.save()
        self.assertEqual(queue_due_enforcements(), 1)
        self.assertEqual(queue_due_enforcements(), 0)

    def test_stale_metering_queues_enforcement(self):
        record = self.activate_fixture()
        record.state = 'active'
        record.activated_at = timezone.now() - timedelta(minutes=4)
        record.save()
        self.assertEqual(queue_due_enforcements(), 1)
        self.assertEqual(record.jobs.get(kind='enforce').payload['reason'], 'metering_stale')

    def test_quota_new_cycle_restores_same_credentials(self):
        from .jobs import enqueue_enforcement
        record, sub = self.create_fixture()
        identity = sub.identities.get()
        credential = identity.credential_ref
        token_version = sub.token_version
        job = enqueue_enforcement(record, 'quota', record.cycles.get().pk)
        self.assertEqual(run_job(job.public_id, IsolatedAdapter()).state, 'simulated')
        identity.refresh_from_db()
        self.assertEqual(identity.state, 'suspended')
        self.assertIsNone(identity.revoked_at)
        record.refresh_from_db()
        record.usage_updated_at = timezone.now()
        record.save()
        self.assertEqual(queue_resumptions(), 1)
        self.assertEqual(queue_resumptions(), 0)
        run_job(record.jobs.get(kind='resume').public_id, IsolatedAdapter())
        identity.refresh_from_db()
        sub.refresh_from_db()
        self.assertEqual(identity.credential_ref, credential)
        self.assertEqual(identity.state, 'simulated')
        self.assertEqual(sub.state, 'simulated')
        self.assertEqual(sub.token_version, token_version)

    def test_resume_does_not_restore_reset_tombstones(self):
        from .jobs import enqueue_enforcement
        record, sub = self.create_fixture()
        old = sub.identities.get()
        _, reset = reset_subscription(self.alice, sub.public_id, 'reset-before-quota')
        run_job(reset.public_id, IsolatedAdapter())
        stop = enqueue_enforcement(record, 'quota', record.cycles.get().pk)
        run_job(stop.public_id, IsolatedAdapter())
        record.refresh_from_db()
        record.usage_updated_at = timezone.now()
        record.save()
        queue_resumptions()
        resume = claim_job(record.jobs.get(kind='resume').public_id)
        plan = build_plan(resume)
        self.assertNotIn(old.pk, plan['activate_ids'])
        self.assertIn(old.pk, plan['revoke_ids'])

    def test_second_stale_incident_is_not_hidden_by_idempotency(self):
        from .jobs import enqueue_enforcement
        record, sub = self.create_fixture()
        first = enqueue_enforcement(record, 'metering_stale')
        run_job(first.public_id, IsolatedAdapter())
        record.refresh_from_db()
        record.usage_updated_at = timezone.now()
        record.save()
        queue_resumptions()
        run_job(record.jobs.get(kind='resume').public_id, IsolatedAdapter())
        record.refresh_from_db()
        second = enqueue_enforcement(record, 'metering_stale')
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(record.state, 'enforcement_pending')

    def test_expiry_renewal_resumes_suspended_identity(self):
        record, sub = self.create_fixture()
        identity = sub.identities.get()
        record.expires_at = timezone.now() - timedelta(seconds=1)
        record.save()
        queue_due_enforcements()
        run_job(record.jobs.get(kind='enforce').public_id, IsolatedAdapter())
        self.assertEqual(queue_resumptions(), 0)
        _, renewal = self.assign(idempotency_key='renew-expired-user', expires_at=timezone.now() + timedelta(days=30))
        run_job(renewal.public_id, IsolatedAdapter())
        identity.refresh_from_db()
        self.assertEqual(identity.state, 'simulated')
        self.assertIsNone(identity.revoked_at)

    def test_cross_cycle_requires_boundary_checkpoint(self):
        record, sub = self.create_fixture()
        identity = sub.identities.get()
        boundary = timezone.now() - timedelta(seconds=10)
        record.cycles.all().delete()
        record.activated_at = boundary - timedelta(days=1)
        record.save()
        BillingCycle.objects.create(entitlement=record, starts_at=record.activated_at, ends_at=boundary)
        BillingCycle.objects.create(entitlement=record, starts_at=boundary, ends_at=boundary + timedelta(days=30))
        record_usage(identity.pk, self.server.pk, 'boot-a', 1, 10, 10, boundary - timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            record_usage(identity.pk, self.server.pk, 'boot-a', 2, 20, 20, boundary + timedelta(seconds=1))
        self.assertEqual(UsageLedger.objects.count(), 1)
        record.refresh_from_db()
        self.assertTrue(record.metering_gap)
        self.assertEqual(record.state, 'enforcement_pending')

    def test_removed_line_revokes_identity(self):
        record, sub = self.create_fixture()
        _, job = self.assign(line_ids=[self.other.pk], idempotency_key='replace-lines')
        self.assertIsNotNone(sub.identities.get().revoked_at)
        self.assertIn(sub.identities.get().pk, job.payload['revoke_ids'])

    def test_migration_is_read_only_by_default(self):
        Membership.objects.create(user=self.alice, quota_bytes=1000, status='active')
        output = io.StringIO()
        call_command('phase1_migration_preview', stdout=output)
        self.assertEqual(Entitlement.objects.count(), 0)
        self.assertEqual(json.loads(output.getvalue())['mode'], 'read_only')
        call_command('phase1_migration_preview', apply_shadow=True, confirm_local=True, stdout=io.StringIO())
        self.assertEqual(Entitlement.objects.get().state, 'pending')
        self.assertEqual(NodeIdentity.objects.count(), 0)
        self.assertEqual(DeploymentJob.objects.count(), 0)

    def test_same_server_different_users_are_serial_and_fenced(self):
        record, alice_job = self.assign()
        _, bob_job = self.assign(user=self.bob, idempotency_key='assign-bob-key')
        claimed = claim_job(alice_job.public_id)
        self.assertIsNone(claim_job(bob_job.public_id))
        old_fence = claimed.payload['_server_fences'][str(self.server.pk)]
        DeploymentJob.objects.filter(pk=alice_job.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        Server.objects.filter(pk=self.server.pk).update(deployment_lease_until=timezone.now() - timedelta(seconds=1))
        new_claim = claim_job(bob_job.public_id)
        self.assertGreater(new_claim.payload['_server_fences'][str(self.server.pk)], old_fence)
        with self.assertRaises(ValidationError):
            finish_job(claimed.pk, claimed.lease_id, build_plan(claimed), IsolatedAdapter().apply(build_plan(claimed)))

    def test_metering_gap_cannot_clear_without_all_identity_samples(self):
        from .entitlements import acknowledge_metering_recovery
        record = self.activate_fixture()
        record.metering_gap = True
        record.save()
        with self.assertRaises(ValidationError):
            acknowledge_metering_recovery(self.admin, record.pk)
        sub, _ = create_subscription(self.alice, '待测手机', 'android', [self.line.pk], 'missing-meter')
        sub.identities.update(state='suspended')
        with self.assertRaises(ValidationError):
            acknowledge_metering_recovery(self.admin, record.pk)

    def test_download_token_rejects_old_version(self):
        # 仅构造数据库验签夹具；不伪称真实核心能力验证。
        record, sub = self.create_fixture()
        record.state = 'active'
        record.usage_updated_at = timezone.now()
        record.save()
        sub.state = 'active'
        sub.save()
        sub.identities.update(state='active')
        token = token_for_subscription(self.alice, sub.public_id)
        with patch('django.core.signing.time.time', return_value=9999999999):
            self.assertEqual(token_for_subscription(self.alice, sub.public_id), token)
        self.assertEqual(resolve_subscription(token).pk, sub.pk)
        reset_subscription(self.alice, sub.public_id, 'reset-token-check')
        with self.assertRaises(PermissionDenied):
            resolve_subscription(token)

    def test_metering_gap_blocks_download_even_with_fresh_counter(self):
        record, sub = self.create_fixture()
        record.state, record.usage_updated_at = 'active', timezone.now()
        record.metering_gap = True
        record.save()
        sub.state = 'active'
        sub.save()
        sub.identities.update(state='active')
        with self.assertRaises(ValidationError):
            token_for_subscription(self.alice, sub.public_id)
        self.assertFalse(summary_for(self.alice)['usage_fresh'])

    def test_summary_does_not_accept_future_metering(self):
        record = self.activate_fixture()
        record.usage_updated_at = timezone.now() + timedelta(minutes=5)
        record.save()
        self.assertFalse(summary_for(self.alice)['usage_fresh'])

    def test_assignment_cannot_bypass_unresolved_gap(self):
        record, sub = self.create_fixture()
        record.metering_gap = True
        record.save()
        _, job = self.assign(idempotency_key='reassign-with-gap')
        adapter = IsolatedAdapter()
        with patch.object(adapter, 'apply') as execute:
            result = run_job(job.public_id, adapter)
        execute.assert_not_called()
        self.assertEqual(result.result_code, 'METERING_GAP_UNRESOLVED')

    def test_assignment_cannot_bypass_disabled_account_or_consumed_quota(self):
        record, sub = self.create_fixture()
        cycle = record.cycles.get()
        cycle.used_bytes = 500
        cycle.save()
        _, job = self.assign(idempotency_key='reduce-below-used', quota_bytes=400)
        self.assertEqual(run_job(job.public_id, IsolatedAdapter()).result_code, 'QUOTA_EXHAUSTED')
        self.alice.is_active = False
        self.alice.save()
        _, job = self.assign(idempotency_key='inactive-reassignment', quota_bytes=1000)
        self.assertEqual(run_job(job.public_id, IsolatedAdapter()).result_code, 'ACCOUNT_DISABLED')
        self.alice.is_active = True
        self.alice.save()
        retry_job(self.admin, job.public_id)
        self.assertEqual(run_job(job.public_id, IsolatedAdapter()).state, 'simulated')
        cycle.refresh_from_db()
        self.assertEqual(cycle.used_bytes, 500)

    def test_restart_epoch_does_not_hide_cross_cycle_gap(self):
        record, sub = self.create_fixture()
        identity = sub.identities.get()
        boundary = timezone.now() - timedelta(seconds=10)
        record.cycles.all().delete()
        record.activated_at = boundary - timedelta(days=1)
        record.save()
        BillingCycle.objects.create(entitlement=record, starts_at=record.activated_at, ends_at=boundary)
        BillingCycle.objects.create(entitlement=record, starts_at=boundary, ends_at=boundary + timedelta(days=30))
        record_usage(identity.pk, self.server.pk, 'before-restart', 1, 10, 10, boundary - timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            record_usage(identity.pk, self.server.pk, 'after-restart', 1, 5, 5, boundary + timedelta(seconds=1))
        self.assertEqual(UsageLedger.objects.count(), 1)
        self.assertIsNone(identity.streams.get().retired_at)
        record.refresh_from_db()
        self.assertTrue(record.metering_gap)

    def test_package_pause_resume_preserves_identity_and_link_generation(self):
        record, sub = self.create_fixture()
        identity = sub.identities.get()
        original_generation = sub.token_version
        _, stop = self.assign(enabled=False, idempotency_key='pause-package')
        self.assertEqual(run_job(stop.public_id, IsolatedAdapter()).state, 'simulated')
        identity.refresh_from_db()
        sub.refresh_from_db()
        self.assertEqual(identity.state, 'suspended')
        self.assertIsNone(identity.revoked_at)
        self.assertEqual(sub.state, 'blocked')
        _, start = self.assign(enabled=True, idempotency_key='resume-package')
        self.assertEqual(run_job(start.public_id, IsolatedAdapter()).state, 'simulated')
        identity.refresh_from_db()
        sub.refresh_from_db()
        self.assertEqual(identity.state, 'simulated')
        self.assertEqual(sub.state, 'simulated')
        self.assertEqual(sub.token_version, original_generation)

    def test_late_refresh_receipt_cannot_activate_newer_protocol_scope(self):
        record, sub = self.create_fixture()
        _, job = refresh_subscription(self.alice, sub.public_id, 'late-scope-refresh')
        claimed = claim_job(job.public_id)
        plan = build_plan(claimed)
        extra = Ingress.objects.create(server=self.server, name='后加入入口', protocol='hy2')
        self.line.additional_ingresses.add(extra)
        candidate = NodeIdentity.objects.create(subscription=sub, line=self.line, ingress=extra, generation=sub.generation)
        result = finish_job(claimed.pk, claimed.lease_id, plan, IsolatedAdapter().apply(plan))
        self.assertEqual(result.state, 'superseded')
        candidate.refresh_from_db()
        self.assertEqual(candidate.state, 'candidate')

    def test_scope_can_readd_line_without_reviving_old_identity(self):
        record, sub = self.create_fixture()
        old = sub.identities.get()
        old.revoked_at, old.state = timezone.now(), 'revoked'
        old.save()
        new = NodeIdentity.objects.create(subscription=sub, line=self.line, ingress=self.line.ingress,
                                         generation=sub.generation)
        self.assertNotEqual(new.credential_ref, old.credential_ref)
        old.refresh_from_db()
        self.assertEqual(old.state, 'revoked')
        self.assertIsNotNone(old.revoked_at)
