import json
import os
from pathlib import Path
from unittest import skipUnless
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AdminSubscriptionLinksTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('subscription-admin', password='FixtureOnly-Admin-0001', is_staff=True)
        self.user = User.objects.create_user('subscription-user', password='FixtureOnly-User-0002')
        self.client = Client()

    def _with_links(self, value):
        # 真实读取器会拒绝其他用户可读的POSIX文件，夹具也必须满足同一约束。
        path = Path(settings.DATA_ROOT) / ('test-legacy-links-' + uuid4().hex + '.json')
        self.addCleanup(path.unlink, missing_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        if os.name == 'posix':
            path.chmod(0o600)
        return path

    def test_admin_sees_existing_links_and_user_does_not(self):
        value = {'windows': 'https://sub.relay.example.invalid:2053/s/' + 'a' * 43 + '/windows'}
        path = self._with_links(value)
        with override_settings(LEGACY_LINKS_PATH=path):
            self.client.force_login(self.admin)
            response = self.client.get(reverse('admin_subscriptions'))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, '已有自用订阅')
            self.assertContains(response, 'Windows / v2rayN 节点')
            self.client.force_login(self.user)
            self.assertEqual(self.client.get(reverse('admin_subscriptions')).status_code, 403)

    def test_invalid_link_file_is_hidden(self):
        path = self._with_links({'windows': 'https://evil.example/s/' + 'a' * 43 + '/windows.txt'})
        with override_settings(LEGACY_LINKS_PATH=path):
            self.client.force_login(self.admin)
            response = self.client.get(reverse('admin_subscriptions'))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, '尚无可交付链接')
            self.assertNotContains(response, 'https://evil.example')

    @skipUnless(os.name == 'posix', '仅POSIX系统有其他用户文件权限位')
    def test_other_user_readable_link_file_is_hidden(self):
        path = self._with_links({'windows': 'https://sub.relay.example.invalid:2053/s/' + 'a' * 43 + '/windows'})
        path.chmod(0o604)
        with override_settings(LEGACY_LINKS_PATH=path):
            self.client.force_login(self.admin)
            response = self.client.get(reverse('admin_subscriptions'))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, '尚无可交付链接')
            self.assertNotContains(response, '已有自用订阅')
