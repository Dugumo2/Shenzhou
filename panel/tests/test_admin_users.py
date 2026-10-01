"""管理员创建用户的离线回归；初始密码均替换为公开测试常量。"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from portal.models import AuditEvent, Membership, SubscriptionGrant


FIXTURE_PASSWORD = 'FixtureOnly-RandomInitial-000001x'
CHANGED_PASSWORD = 'FixtureOnly-ChangedPassword-002x'
ADMIN_PASSWORD = 'FixtureOnly-AdminLogin-000003x'


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AdminUserCreateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username='admin-fixture', password=ADMIN_PASSWORD, is_staff=True)
        cls.normal = User.objects.create_user(username='ordinary-fixture', password=ADMIN_PASSWORD)

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('user_create')
        self.values = {'username': 'new-fixture-user', 'quota_gb': '123.4', 'service_days': '60', 'confirm': 'on'}
        generator = patch('portal.views.secrets.token_urlsafe', return_value=FIXTURE_PASSWORD)
        self.password_generator = generator.start()
        self.addCleanup(generator.stop)

    def create_user(self, values=None, client=None):
        return (client or self.client).post(self.url, values or self.values)

    def test_admin_get_has_form_csrf_and_no_password(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'name="username"')
        self.assertNotContains(response, 'initial-user-password')
        self.assertNotContains(response, FIXTURE_PASSWORD)
        self.assertNotIn('initial_password', response.context)
        self.password_generator.assert_not_called()
        self.assertEqual(User.objects.count(), 2)

    def test_create_is_pending_atomic_and_has_three_independent_grants(self):
        with self.assertNoLogs('portal', level='DEBUG'):
            response = self.create_user()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, FIXTURE_PASSWORD)
        self.assertContains(response, '初始密码仅本次显示')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])
        self.password_generator.assert_called_once_with(24)
        user = User.objects.get(username=self.values['username'])
        self.assertTrue(user.check_password(FIXTURE_PASSWORD))
        self.assertNotEqual(user.password, FIXTURE_PASSWORD)
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        member = Membership.objects.get(user=user)
        self.assertEqual(member.quota_bytes, 123_400_000_000)
        self.assertEqual(member.service_days, 60)
        self.assertEqual(member.status, 'pending')
        self.assertEqual(member.provisioning_state, 'not_provisioned')
        self.assertEqual(member.usage_state, 'not_connected')
        self.assertIsNone(member.expires_at)
        self.assertIsNone(member.last_applied_at)
        self.assertFalse(member.eligible)
        self.assertEqual(set(member.grants.values_list('device', flat=True)), {'windows', 'android', 'v2rayng'})
        self.assertEqual(member.grants.count(), 3)
        event = AuditEvent.objects.get(action='admin_user_created')
        self.assertEqual(event.actor_id, self.admin.id)
        self.assertEqual(event.subject, str(member.public_id))
        self.assertEqual(event.result, 'PENDING')
        self.assertNotIn(FIXTURE_PASSWORD, str(list(AuditEvent.objects.values())))
        self.assertNotIn(FIXTURE_PASSWORD, str(dict(self.client.session)))
        self.assertEqual(int(self.client.session['_auth_user_id']), self.admin.id)

    def test_initial_password_is_not_redisplayed_on_get_or_repeated_post(self):
        self.create_user()
        self.assertNotContains(self.client.get(self.url), FIXTURE_PASSWORD)
        repeated = self.create_user()
        self.assertEqual(repeated.status_code, 200)
        self.assertNotContains(repeated, FIXTURE_PASSWORD)
        self.assertNotIn('initial_password', repeated.context)
        self.assertEqual(User.objects.filter(username=self.values['username']).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action='admin_user_created').count(), 1)
        self.password_generator.assert_called_once_with(24)

    def test_anonymous_and_normal_users_cannot_create(self):
        anonymous = Client()
        for client in (anonymous, Client()):
            if client is not anonymous:
                client.force_login(self.normal)
            for method in ('get', 'post'):
                response = getattr(client, method)(self.url, self.values if method == 'post' else {})
                self.assertEqual(response.status_code, 302 if client is anonymous else 403)
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(Membership.objects.count(), 0)
        self.password_generator.assert_not_called()

    def test_csrf_is_required_and_valid_token_works(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post(self.url, self.values).status_code, 403)
        self.assertEqual(User.objects.count(), 2)
        page = client.get(self.url)
        self.assertEqual(page.status_code, 200)
        token = client.cookies['csrftoken'].value
        response = client.post(self.url, self.values | {'csrfmiddlewaretoken': token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(User.objects.count(), 3)

    def test_unsupported_methods_cannot_write(self):
        for method in ('put', 'patch', 'delete'):
            self.assertEqual(getattr(self.client, method)(self.url).status_code, 405)
        self.assertEqual(User.objects.count(), 2)
        self.password_generator.assert_not_called()

    def test_extra_privileged_fields_and_supplied_password_are_ignored(self):
        values = self.values | {'is_staff': 'on', 'is_superuser': 'on', 'status': 'active',
                                'provisioning_state': 'applied', 'password': 'attacker-fixture-password'}
        response = self.create_user(values)
        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username=self.values['username'])
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password(FIXTURE_PASSWORD))
        self.assertEqual(user.membership.status, 'pending')
        self.assertEqual(user.membership.provisioning_state, 'not_provisioned')
        self.assertNotContains(response, 'attacker-fixture-password')

    def test_username_case_and_unicode_duplicates_are_rejected(self):
        for name in ('ordinary-fixture', 'ORDINARY-FIXTURE', 'ｏｒｄｉｎａｒｙ-fixture', ' ordinary-fixture '):
            response = self.create_user(self.values | {'username': name})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
            self.assertNotIn('initial_password', response.context)
        self.assertEqual(User.objects.count(), 2)
        self.password_generator.assert_not_called()

    def test_invalid_fields_do_not_create_anything(self):
        cases = ({'username': 'bad name'}, {'username': ''}, {'quota_gb': '0'},
                 {'quota_gb': '-1'}, {'quota_gb': 'NaN'}, {'quota_gb': '100001'},
                 {'service_days': '0'}, {'service_days': '3661'}, {'confirm': ''})
        for changes in cases:
            response = self.create_user(self.values | changes)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(Membership.objects.count(), 0)
        self.password_generator.assert_not_called()

    def test_missing_post_is_bound_and_not_accepted(self):
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].is_bound)
        self.assertTrue(response.context['form'].errors)
        self.assertEqual(User.objects.count(), 2)

    def test_rate_limit_precedes_creation(self):
        with patch('portal.views.throttle', return_value=False):
            response = self.create_user()
        self.assertEqual(response.status_code, 429)
        self.assertNotContains(response, FIXTURE_PASSWORD, status_code=429)
        self.assertEqual(User.objects.count(), 2)
        self.password_generator.assert_not_called()

    def test_write_transaction_rechecks_username(self):
        with patch('portal.views.clean_username', side_effect=ValidationError('fixture-race')):
            response = self.create_user()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(User.objects.count(), 2)
        self.assertNotContains(response, 'fixture-race')
        self.password_generator.assert_not_called()

    def test_partial_grant_failure_rolls_back_user_membership_and_audit(self):
        create = SubscriptionGrant.objects.create
        count = 0

        def fail_second_grant(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise IntegrityError('fixture-grant-error')
            return create(**kwargs)

        with patch('portal.views.SubscriptionGrant.objects.create', side_effect=fail_second_grant):
            response = self.create_user()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].non_field_errors())
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(Membership.objects.count(), 0)
        self.assertEqual(SubscriptionGrant.objects.count(), 0)
        self.assertFalse(AuditEvent.objects.filter(action='admin_user_created').exists())
        self.assertNotContains(response, FIXTURE_PASSWORD)
        self.assertNotContains(response, 'fixture-grant-error')

    def test_new_user_can_log_in_and_change_initial_password(self):
        self.create_user()
        client = Client()
        response = client.post(reverse('login'), {'username': self.values['username'], 'password': FIXTURE_PASSWORD})
        self.assertRedirects(response, reverse('dashboard'))
        response = client.post(reverse('account'), {'action': 'password', 'old_password': FIXTURE_PASSWORD,
                               'new_password1': CHANGED_PASSWORD, 'new_password2': CHANGED_PASSWORD})
        self.assertRedirects(response, reverse('account'))
        user = User.objects.get(username=self.values['username'])
        self.assertTrue(user.check_password(CHANGED_PASSWORD))
        self.assertFalse(user.check_password(FIXTURE_PASSWORD))
        self.assertFalse(user.membership.eligible)

    def test_users_page_links_to_new_admin_only_entry(self):
        response = self.client.get(reverse('users'))
        self.assertContains(response, self.url)
        self.assertContains(response, '创建用户')
