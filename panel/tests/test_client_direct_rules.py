"""客户端本地直连编辑器的权限、匹配和导出回归。"""
import json
from pathlib import Path

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from portal.models import ClientDirectRule


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ClientDirectRulesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user('rule-admin', password='FixtureOnly-Admin-0001', is_staff=True)
        cls.normal = User.objects.create_user('rule-user', password='FixtureOnly-User-0002')

    def setUp(self):
        self.client.force_login(self.admin)
        self.url = reverse('client_direct_rules')

    def post_rule(self, **overrides):
        payload = {'action': 'save', 'outbound': 'direct', 'kind': 'suffix', 'value': 'school.example.cn',
                   'scope_domain': '', 'enabled': 'on'} | overrides
        return self.client.post(self.url, payload)

    def test_save_preview_edit_and_export(self):
        self.assertEqual(self.post_rule().status_code, 302)
        rule = ClientDirectRule.objects.get()
        self.assertEqual(rule.value, 'school.example.cn')
        preview = self.client.post(self.url, {'action': 'preview', 'domain': 'jxgl.school.example.cn'})
        self.assertContains(preview, '命中直连候选')
        export = self.client.get(reverse('client_direct_export'))
        self.assertEqual(export.status_code, 200)
        self.assertEqual(json.loads(export.content), {'version': 3, 'rules': [{'domain_suffix': ['school.example.cn']}]})
        policy = self.client.get(reverse('client_direct_policy_export'))
        self.assertEqual(policy.status_code, 200)
        self.assertEqual(json.loads(policy.content), {'schema_version': 2, 'rules': [
            {'action': 'direct', 'kind': 'suffix', 'value': 'school.example.cn', 'scope': ''}]})
        self.assertEqual(len(export['X-Content-SHA256']), 64)
        edit = self.client.get(self.url + f'?edit={rule.pk}')
        self.assertContains(edit, f'name="revision" value="{rule.revision}"')
        updated = self.post_rule(rule_id=str(rule.pk), revision=str(rule.revision), kind='exact', value='jxgl.example.cn')
        self.assertEqual(updated.status_code, 302)
        rule.refresh_from_db()
        self.assertEqual((rule.kind, rule.value, rule.revision), ('exact', 'jxgl.example.cn', 2))
        stale = self.post_rule(rule_id=str(rule.pk), revision='1', value='wrong.example.cn')
        self.assertEqual(stale.status_code, 200)
        rule.refresh_from_db()
        self.assertEqual(rule.value, 'jxgl.example.cn')

    def test_scoped_regex_and_protected_domains(self):
        self.assertEqual(self.post_rule(kind='regex', value=r'^api[0-9]+\.example\.com$',
                                        scope_domain='example.com').status_code, 302)
        preview = self.client.post(self.url, {'action': 'preview', 'domain': 'api42.example.com'})
        self.assertContains(preview, '命中直连候选')
        doc = json.loads(self.client.get(reverse('client_direct_export')).content)
        self.assertEqual(doc['rules'], [{'domain_regex': [r'^api[0-9]+\.example\.com$']}])
        policy = json.loads(self.client.get(reverse('client_direct_policy_export')).content)
        self.assertEqual(policy['rules'][0]['scope'], 'example.com')
        for bad in ({'kind': 'suffix', 'value': 'googleapis.cn'},
                    {'kind': 'suffix', 'value': 'com'},
                    {'kind': 'regex', 'value': r'^(a+)+\.example\.com$', 'scope_domain': 'example.com'},
                    {'kind': 'regex', 'value': r'^.*\.chatgpt\.com$', 'scope_domain': 'chatgpt.com'}):
            response = self.post_rule(**bad)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context['form'].errors)
        self.assertEqual(ClientDirectRule.objects.count(), 1)
        self.assertContains(self.client.post(self.url, {'action': 'preview', 'domain': 'chatgpt.com'}), '受保护')

    def test_non_admin_cannot_read_or_modify(self):
        for client, expected in ((Client(), 302), (Client(), 403)):
            if expected == 403:
                client.force_login(self.normal)
            self.assertEqual(client.get(self.url).status_code, expected)
            self.assertEqual(client.post(self.url, {'action': 'save', 'kind': 'exact', 'value': 'example.com'}).status_code, expected)
            self.assertEqual(client.get(reverse('client_direct_export')).status_code, expected)
            self.assertEqual(client.get(reverse('client_direct_policy_export')).status_code, expected)
        self.assertEqual(ClientDirectRule.objects.count(), 0)

    def test_csrf_and_disabled_export(self):
        self.assertEqual(self.client.get(reverse('client_direct_export')).status_code, 409)
        self.assertEqual(self.client.get(reverse('client_direct_policy_export')).status_code, 409)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post(self.url, {'action': 'save', 'kind': 'exact', 'value': 'example.com'}).status_code, 403)
        self.assertEqual(ClientDirectRule.objects.count(), 0)
        self.post_rule(enabled='')
        self.assertEqual(self.client.get(reverse('client_direct_export')).status_code, 409)

    def test_proxy_search_duplicate_and_delete(self):
        self.assertEqual(self.post_rule(outbound='proxy', value='school.example.com').status_code, 302)
        rule = ClientDirectRule.objects.get()
        self.assertEqual(rule.outbound, 'proxy')
        match = self.client.post(self.url, {'action': 'preview', 'domain': 'api.school.example.com'})
        self.assertContains(match, '命中代理候选')
        self.assertContains(match, 'school.example.com')
        related = self.client.post(self.url, {'action': 'preview', 'domain': 'example.com'})
        self.assertContains(related, '已配置子域名')
        duplicate = self.post_rule(outbound='direct', value='school.example.com')
        self.assertContains(duplicate, '请编辑现有规则')
        self.assertEqual(ClientDirectRule.objects.count(), 1)
        proxy_source = self.client.get(reverse('client_proxy_export'))
        self.assertEqual(json.loads(proxy_source.content)['rules'], [{'domain_suffix': ['school.example.com']}])
        self.assertEqual(self.client.get(reverse('client_direct_export')).status_code, 409)
        deleted = self.client.post(self.url, {'action': 'delete', 'rule_id': rule.pk,
                                              'revision': rule.revision, 'confirm': 'yes'})
        self.assertEqual(deleted.status_code, 302)
        self.assertFalse(ClientDirectRule.objects.exists())

    def test_same_direction_parent_child_overlap_is_rejected(self):
        self.assertEqual(self.post_rule(value='example.com').status_code, 302)
        child = self.post_rule(value='api.example.com')
        self.assertEqual(child.status_code, 200)
        self.assertContains(child, '已有父域或子域规则覆盖')
        self.assertEqual(ClientDirectRule.objects.count(), 1)

    def test_save_writes_versioned_policy_queue(self):
        path = Path(__file__).resolve().parents[1] / 'var' / 'test-routing-policy.json'
        with override_settings(ROUTING_POLICY_PATH=path):
            self.assertEqual(self.post_rule(outbound='proxy', value='api.example.com').status_code, 302)
            document = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(document['schema_version'], 2)
        self.assertEqual(document['rules'][0]['action'], 'proxy')
        path.unlink(missing_ok=True)
