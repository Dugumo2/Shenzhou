"""服务中心的真实请求链路：注册无服务、交付去重与容量分账。"""
from datetime import timedelta
from decimal import Decimal
import uuid

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from portal.delivery import obtain_delivery, reset_service_deliveries
from portal.models import (Server, Ingress, Egress, Line, Entitlement, DeviceSubscription,
    DeploymentJob, Invitation, CapacityPool, CapacitySample, BillingCycle)
from portal.services import invite_hash


@override_settings(WORKSPACE_V2=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ServiceCenterTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('服务管理员', is_staff=True)
        self.user = User.objects.create_user('服务用户')
        self.other = User.objects.create_user('未开通用户')
        server = Server.objects.create(name='隔离服务器')
        ingress = Ingress.objects.create(server=server, name='测试入口', protocol='ws')
        egress = Egress.objects.create(server=server, name='测试出口', kind='upstream')
        self.line = Line.objects.create(name='测试线路甲', ingress=ingress, egress=egress)
        self.second = Line.objects.create(name='测试线路乙', ingress=ingress, egress=egress)
        self.now = timezone.now()

    def service(self):
        record = Entitlement.objects.create(user=self.user, quota_bytes=100_000_000_000,
            state='simulated', activated_at=self.now, expires_at=self.now + timedelta(days=30),
            applied_revision=1, applied_snapshot={'quota_bytes': 100_000_000_000, 'line_ids': [self.line.pk, self.second.pk]})
        record.lines.set([self.line, self.second])
        return record

    def test_invitation_registration_cannot_grant_service_even_with_legacy_quota(self):
        Invitation.objects.create(code_hash=invite_hash('test-only-invitation'), creator=self.admin,
            quota_bytes=900_000_000_000, expires_at=self.now + timedelta(days=1))
        response = self.client.post('/register/', {'username': '新注册用户', 'invitation': 'test-only-invitation',
            'password1': 'Test-only-9u@Pw8j19', 'password2': 'Test-only-9u@Pw8j19'})
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username='新注册用户')
        self.assertFalse(user.is_staff)
        self.assertFalse(Entitlement.objects.filter(user=user).exists())
        self.assertEqual(user.membership.quota_bytes, 0)
        self.assertFalse(user.membership.grants.exists())
        self.assertContains(self.client.get('/'), '暂时没有服务')
        self.assertEqual(self.client.post('/subscriptions/create/', {}).status_code, 409)

    def test_admin_allocation_only_at_user_detail_and_post_cannot_switch_target(self):
        self.client.force_login(self.admin)
        url = reverse('user_edit', kwargs={'pk': self.user.pk})
        response = self.client.post(url, {'user': self.other.pk, 'lines': [self.line.pk, self.second.pk],
            'quota_gb': 100, 'validity_mode': 'months', 'service_months': 3, 'reset_mode': 'activation',
            'enabled': 'on', 'operation': 'save', 'revision': 0, 'idempotency_key': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Entitlement.objects.filter(user=self.user).exists())
        self.assertFalse(Entitlement.objects.filter(user=self.other).exists())
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url, {}).status_code, 403)

    def test_same_format_and_scope_is_idempotent_across_clicks(self):
        record = self.service()
        first, job, created = obtain_delivery(self.user, record.public_id, 'windows', 'all', str(uuid.uuid4()))
        second, same, repeated = obtain_delivery(self.user, record.public_id, 'windows', 'all', str(uuid.uuid4()))
        self.assertTrue(created)
        self.assertFalse(repeated)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(job.pk, same.pk)
        self.assertEqual(DeviceSubscription.objects.count(), 1)
        self.assertEqual(DeploymentJob.objects.count(), 1)

    def test_different_formats_and_scopes_share_one_service(self):
        record = self.service()
        for client, scope in [('windows', 'all'), ('v2rayng', 'all'), ('android', f'line:{self.line.pk}')]:
            obtain_delivery(self.user, record.public_id, client, scope, str(uuid.uuid4()))
        self.assertEqual(Entitlement.objects.count(), 1)
        self.assertEqual(record.subscriptions.count(), 3)
        record.refresh_from_db()
        self.assertEqual(record.quota_bytes, 100_000_000_000)

    def test_all_scope_keeps_identity_when_admin_adds_line(self):
        record = self.service()
        record.lines.set([self.line])
        first, _, _ = obtain_delivery(self.user, record.public_id, 'windows', 'all', str(uuid.uuid4()))
        single, _, _ = obtain_delivery(self.user, record.public_id, 'windows', f'line:{self.line.pk}', str(uuid.uuid4()))
        self.assertNotEqual(first.pk, single.pk)
        record.lines.add(self.second)
        updated, job, changed = obtain_delivery(self.user, record.public_id, 'windows', 'all', str(uuid.uuid4()))
        self.assertTrue(changed)
        self.assertEqual(updated.pk, first.pk)
        self.assertEqual(updated.token_version, first.token_version)
        self.assertEqual(updated.generation, first.generation)
        self.assertEqual(job.kind, 'refresh')
        self.assertEqual(set(updated.lines.values_list('pk', flat=True)), {self.line.pk, self.second.pk})
        self.assertEqual(set(single.lines.values_list('pk', flat=True)), {self.line.pk})

    def test_reset_replay_does_not_reset_new_format(self):
        record = self.service()
        obtain_delivery(self.user, record.public_id, 'windows', 'all', str(uuid.uuid4()))
        key = str(uuid.uuid4())
        first = reset_service_deliveries(self.user, record.public_id, key)
        new_sub, _, _ = obtain_delivery(self.user, record.public_id, 'android', 'all', str(uuid.uuid4()))
        repeated = reset_service_deliveries(self.user, record.public_id, key)
        self.assertEqual([j.pk for j in first], [j.pk for j in repeated])
        new_sub.refresh_from_db()
        self.assertEqual(new_sub.generation, 1)

    def test_single_scope_normalizes_id_and_added_protocol_is_synchronized(self):
        record = self.service()
        first, _, _ = obtain_delivery(self.user, record.public_id, 'android', f'line:00{self.line.pk}', str(uuid.uuid4()))
        same, _, created = obtain_delivery(self.user, record.public_id, 'android', f'line:{self.line.pk}', str(uuid.uuid4()))
        self.assertFalse(created)
        self.assertEqual(same.pk, first.pk)
        extra = Ingress.objects.create(server=self.line.ingress.server, name='新增测试入口', protocol='reality')
        self.line.additional_ingresses.add(extra)
        updated, job, changed = obtain_delivery(self.user, record.public_id, 'android', f'line:{self.line.pk}', str(uuid.uuid4()))
        self.assertTrue(changed)
        self.assertEqual(updated.pk, first.pk)
        self.assertEqual(job.kind, 'refresh')
        self.assertTrue(updated.identities.filter(ingress=extra, revoked_at__isnull=True).exists())

    def test_same_request_key_cannot_change_all_scope_to_single(self):
        record = self.service()
        record.lines.set([self.line])
        key = str(uuid.uuid4())
        obtain_delivery(self.user, record.public_id, 'windows', 'all', key)
        with self.assertRaises(ValidationError):
            obtain_delivery(self.user, record.public_id, 'windows', f'line:{self.line.pk}', key)
        self.assertEqual(record.subscriptions.get().delivery_scope, 'all')

    def test_admin_can_inspect_inactive_user_without_granting_them_access(self):
        record = self.service()
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('user_edit', kwargs={'pk': self.user.pk})).status_code, 200)
        with self.assertRaises(PermissionDenied):
            obtain_delivery(self.user, record.public_id, 'windows')

    def test_unapplied_or_other_users_service_cannot_be_obtained(self):
        record = self.service()
        with self.assertRaises(PermissionDenied):
            obtain_delivery(self.other, record.public_id, 'windows')
        record.state = 'pending'
        record.save()
        with self.assertRaises(ValidationError):
            obtain_delivery(self.user, record.public_id, 'windows')
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('service_detail', kwargs={'public_id': record.public_id})).status_code, 404)

    def test_reset_all_formats_once_preserves_quota_expiry_and_usage(self):
        record = self.service()
        expiry = record.expires_at
        cycle = BillingCycle.objects.create(entitlement=record, starts_at=self.now, ends_at=expiry,
            used_bytes=12345, raw_bytes=12345)
        for client in ('windows', 'android'):
            obtain_delivery(self.user, record.public_id, client, 'all', str(uuid.uuid4()))
        key = str(uuid.uuid4())
        jobs = reset_service_deliveries(self.user, record.public_id, key)
        repeat = reset_service_deliveries(self.user, record.public_id, key)
        self.assertEqual([j.pk for j in jobs], [j.pk for j in repeat])
        self.assertEqual(set(record.subscriptions.values_list('generation', flat=True)), {2})
        record.refresh_from_db()
        cycle.refresh_from_db()
        self.assertEqual(record.quota_bytes, 100_000_000_000)
        self.assertEqual(record.expires_at, expiry)
        self.assertEqual(cycle.used_bytes, 12345)

    def test_reset_http_confirmation_and_legacy_bypass(self):
        record = self.service()
        sub, _, _ = obtain_delivery(self.user, record.public_id, 'windows', 'all', str(uuid.uuid4()))
        self.client.force_login(self.user)
        url = reverse('service_action', kwargs={'public_id': record.public_id})
        fields = {'action': 'reset', 'revision': record.revision, 'idempotency_key': str(uuid.uuid4())}
        self.assertEqual(self.client.post(url, fields).status_code, 400)
        self.assertEqual(self.client.post(url, {**fields, 'confirm': 'yes'}).status_code, 302)
        sub.refresh_from_db()
        self.assertEqual(sub.generation, 2)
        self.assertEqual(self.client.post(reverse('subscription_action', kwargs={'public_id': sub.public_id}), fields).status_code, 409)

    def test_service_pages_and_admin_filters(self):
        record = self.service()
        self.client.force_login(self.user)
        self.assertContains(self.client.get('/'), '我的服务')
        self.assertContains(self.client.get(reverse('service_detail', kwargs={'public_id': record.public_id})), '测试线路甲')
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get('/manage/allocations/', {'line': self.line.pk}).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.client.get('/manage/allocations/', {'state': 'expired'}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get('/manage/users/', {'state': 'none'}).context['page_obj'].paginator.count, 2)

    def test_capacity_manual_input_does_not_change_service_or_impersonate_api(self):
        record = self.service()
        pool = CapacityPool.objects.create(name='资源包', capacity_mode='limited', planned_bytes=1000_000_000_000,
            period_start=self.now - timedelta(days=1), period_end=self.now + timedelta(days=29),
            accounting_basis='供应商双向', source_priority=['api', 'manual', 'agent'])
        self.client.force_login(self.admin)
        data = {'action': 'record_sample', 'pool': pool.pk, 'used_gb': '70', 'observed_at': self.now.isoformat(),
            'source': 'api', 'accounting_basis': '供应商双向', 'reason': '控制台人工校准', 'idempotency_key': str(uuid.uuid4())}
        self.assertEqual(self.client.post('/manage/capacity/', data).status_code, 302)
        self.assertEqual(CapacitySample.objects.get().source, 'manual')
        record.refresh_from_db()
        self.assertEqual(record.quota_bytes, 100_000_000_000)
        self.assertContains(self.client.get('/manage/capacity/'), '人工校准')
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/manage/capacity/').status_code, 403)

    def test_capacity_create_and_edit_and_line_rate_form(self):
        self.client.force_login(self.admin)
        response = self.client.post('/manage/capacity/', {'action': 'save_pool', 'name': '新资源包',
            'capacity_mode': 'limited', 'planned_gb': '1000', 'period_start': self.now.isoformat(),
            'period_end': (self.now + timedelta(days=30)).isoformat(), 'accounting_basis': '双向',
            'source_priority': 'api,manual,agent', 'original_unit': 'GB', 'stale_after_seconds': 21600, 'safety_gb': '50', 'warning_percent': 70,
            'critical_percent': 85, 'urgent_percent': 95, 'enabled': 'on'})
        self.assertEqual(response.status_code, 302)
        pool = CapacityPool.objects.get()
        self.assertEqual(pool.safety_bytes, 50_000_000_000)
        self.assertEqual(self.client.get('/manage/capacity/', {'edit': pool.pk}).context['form'].initial['name'], '新资源包')
        prefix = f'rate-{self.line.pk}-'
        response = self.client.post('/manage/resources/', {'action': 'set_rate', 'line_id': self.line.pk,
            prefix + 'multiplier': '2.5', prefix + 'effective_at': self.now.isoformat(), prefix + 'reason': '隔离测试倍率'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.line.rate_versions.get().multiplier, Decimal('2.5'))
