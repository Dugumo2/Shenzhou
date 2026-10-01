import json
from pathlib import Path

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
        path = Path(__file__).resolve().parents[1] / 'var' / 'test-legacy-links.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')
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
