"""新工作区实际页面、权限和提交链路回归；不调用生产执行器。"""
import tempfile
import uuid
from pathlib import Path
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.test import Client
from django.urls import reverse
from portal.models import Server, Ingress, Egress, Line, Entitlement, DeviceSubscription, DeploymentJob
from portal.jobs import run_job
from portal.v2_forms import SubscriptionActionForm


@override_settings(WORKSPACE_V2=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class WorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user('管理员', is_staff=True)
        cls.user = User.objects.create_user('普通用户')
        cls.other = User.objects.create_user('另一用户')
        cls.server = Server.objects.create(name='测试服务器')
        ingress = Ingress.objects.create(server=cls.server, name='测试入口', protocol='ws')
        egress = Egress.objects.create(server=cls.server, name='测试出口', kind='upstream')
        cls.line = Line.objects.create(name='测试线路', ingress=ingress, egress=egress)

    def test_anonymous_has_no_guide_or_monitor(self):
        with override_settings(MONITOR_URL='https://monitor.example.com/'):
            response = self.client.get('/login/')
            self.assertContains(response, '神舟云')
            self.assertNotContains(response, 'href="/guide/"')
            self.assertNotContains(response, 'monitor.example.com')
            self.assertEqual(self.client.get('/guide/').status_code, 302)

    def test_user_cannot_manage_and_empty_usage_not_zero(self):
        self.client.force_login(self.user)
        for url in ('/manage/allocations/', '/manage/resources/', '/manage/jobs/'):
            self.assertEqual(self.client.get(url).status_code, 403)
        for url in ('/', '/subscriptions/', '/nodes/', '/guide/'):
            self.assertEqual(self.client.get(url).status_code, 200)
        self.assertIsNone(self.client.get('/').context['entitlement_summary'])

    def test_allocation_is_queued_and_without_executor_is_blocked(self):
        self.client.force_login(self.admin)
        data = {'user': self.user.pk, 'lines': [self.line.pk], 'quota_gb': '1000',
                'reset_day': 31, 'reset_time': '12:34', 'reset_mode': 'custom', 'validity_mode': 'months',
                'operation': 'save', 'revision': 0, 'service_months': 2, 'enabled': 'on', 'idempotency_key': str(uuid.uuid4())}
        url = reverse('user_edit', kwargs={'pk': self.user.pk})
        self.assertEqual(self.client.post(url, data).status_code, 302)
        entitlement = Entitlement.objects.get(user=self.user)
        self.assertEqual(entitlement.quota_bytes, 1_000_000_000_000)
        job = DeploymentJob.objects.get(entitlement=entitlement)
        self.assertEqual(job.state, 'queued')
        self.assertEqual(run_job(job.public_id).state, 'blocked')
        self.assertContains(self.client.post(url, data), '服务已被修改')
        self.assertEqual(self.client.post('/manage/allocations/', data).status_code, 405)
        self.assertEqual(DeploymentJob.objects.count(), 1)
        self.client.force_login(self.user)
        summary = self.client.get('/').context['entitlement_summary']
        self.assertIsNone(summary['quota_gb'])
        self.assertIsNone(summary['used_gb'])

    def test_subscriptions_do_not_leak_between_users(self):
        record = Entitlement.objects.create(user=self.other, quota_bytes=100000)
        sub = DeviceSubscription.objects.create(entitlement=record, name='别人的手机', client='android')
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get('/subscriptions/'), '别人的手机')
        response = self.client.post(reverse('subscription_action', kwargs={'public_id': sub.public_id}),
            {'action': 'disable', 'revision': 1, 'idempotency_key': str(uuid.uuid4()), 'confirm': 'yes'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get('/d/not-valid/').status_code, 404)

    def test_renaming_does_not_change_identity(self):
        self.client.force_login(self.admin)
        uid = self.server.public_id
        self.assertEqual(self.client.post('/manage/resources/', {'action': 'rename_server',
            'public_id': str(uid), 'name': '可修改名称'}).status_code, 302)
        self.server.refresh_from_db()
        self.assertEqual(self.server.name, '可修改名称')
        self.assertEqual(self.server.public_id, uid)

    def test_monitor_visible_only_to_admin(self):
        with override_settings(MONITOR_URL='https://monitor.example.com/'):
            self.client.force_login(self.user)
            self.assertNotContains(self.client.get('/'), 'https://monitor.example.com/')
            self.client.force_login(self.admin)
            self.assertContains(self.client.get('/'), 'https://monitor.example.com/')

    def test_management_subscription_is_not_personal_page(self):
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get('/manage/subscriptions/'), '/manage/allocations/')
        self.assertEqual(self.client.get('/manage/operations/').status_code, 200)

    def test_legacy_rotation_cannot_bypass_new_reset(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post('/subscriptions/rotate/', {'confirm': 'yes'}).status_code, 409)

    def test_reset_requires_server_side_confirmation(self):
        fields = {'action': 'reset', 'revision': 1, 'idempotency_key': str(uuid.uuid4()) + '-reset'}
        self.assertFalse(SubscriptionActionForm(fields).is_valid())
        self.assertTrue(SubscriptionActionForm({**fields, 'confirm': 'yes'}).is_valid())

    def test_new_paths_hidden_until_enabled(self):
        self.client.force_login(self.admin)
        with override_settings(WORKSPACE_V2=False):
            self.assertEqual(self.client.get('/manage/allocations/').status_code, 404)

    def test_new_user_is_not_given_three_legacy_subscriptions(self):
        self.client.force_login(self.admin)
        response = self.client.post('/manage/users/new/', {'username': '新用户', 'confirm': 'on'})
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username='新用户')
        self.assertFalse(user.membership.grants.exists())
        self.assertFalse(Entitlement.objects.filter(user=user).exists())

    def test_csrf_protects_administrative_writes(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post('/manage/resources/', {'action': 'rename_server',
            'public_id': str(self.server.public_id), 'name': '错误改名'}).status_code, 403)

    def test_users_lists_accounts_without_legacy_membership(self):
        self.client.force_login(self.admin)
        response = self.client.get('/manage/users/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '普通用户')
        self.assertContains(response, '未分配套餐')
