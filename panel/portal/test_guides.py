"""实际指南内容、全文搜索和登录隔离。"""
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from .guide_content import ARTICLES, guide_catalog

@override_settings(ROOT_URLCONF='megabox.urls', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class GuideContentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('guide-reader')
        self.client.force_login(self.user)

    def test_catalog_requires_login_and_has_useful_default_content(self):
        response=self.client.get('/api/v1/catalog/guides')
        self.assertEqual(response.status_code,200)
        data=response.json()['data']
        self.assertGreaterEqual(data['total'],10)
        self.assertEqual(data['articles'][0]['id'],'first-import')
        self.client.logout()
        self.assertEqual(self.client.get('/api/v1/catalog/guides').status_code,401)

    def test_search_includes_body_not_only_software_name(self):
        values=guide_catalog(query='可选地址')['articles']
        self.assertEqual([v['id'] for v in values],['windows-v2rayn'])
        self.assertTrue(guide_catalog(query='ｖ２ｒａｙｎ')['articles'])
        self.assertTrue(guide_catalog(query='更新规则')['articles'])
        self.assertEqual(guide_catalog(query='missing-guide-xyz')['articles'],[])

    def test_filter_keeps_common_tasks_but_excludes_other_software(self):
        data=self.client.get('/api/v1/catalog/guides?client=windows').json()['data']
        self.assertTrue(all('all' in a['clients'] or 'windows' in a['clients'] for a in data['articles']))
        self.assertIn('first-import',[a['id'] for a in data['articles']])
        self.assertEqual(self.client.get('/api/v1/catalog/guides?client=invalid').status_code,422)
        self.assertEqual(self.client.get('/api/v1/catalog/guides?q='+'x'*201).status_code,422)

    def test_legacy_client_endpoint_has_real_steps_without_false_verification(self):
        data=self.client.get('/api/v1/catalog/clients/windows/guide').json()['data']
        self.assertIn('可选地址',str(data['steps']))
        self.assertIsNone(data['software_version'])
        self.assertEqual(data['verification'],'not_tested')
        router=self.client.get('/api/v1/catalog/clients/router/guide').json()['data']
        self.assertEqual(router['steps'],[])
        self.assertEqual(router['verification'],'unsupported')

    def test_content_is_generic_and_get_never_creates_data(self):
        text=str(ARTICLES)
        for forbidden in ['HOME-Reality','Megabox','BWH','P8-绕过大陆','默认包含全部已授权线路。也可选择单条线路']:
            self.assertNotIn(forbidden,text)
        with CaptureQueriesContext(connection) as queries:
            self.client.get('/api/v1/catalog/guides?q=流量')
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in queries))
        self.assertEqual(self.client.post('/api/v1/catalog/guides').status_code,405)
