"""构建资源与候选入口的边界；不启动真实代理。"""
from pathlib import Path
import shutil
import uuid

from django.conf import settings
from django.test import SimpleTestCase, override_settings


class FrontendBoundaryTest(SimpleTestCase):
    def setUp(self):
        self.root = settings.BASE_DIR.parent / 'staging' / ('frontend-test-' + uuid.uuid4().hex)
        self.dist = self.root / 'frontend' / 'dist'
        self.dist.mkdir(parents=True)
        (self.dist / 'index.html').write_text('<title>隔离构建</title>', encoding='utf-8')
        (self.root / 'private.txt').write_text('must-not-serve', encoding='utf-8')
        (self.dist / 'source.map').write_text('{}', encoding='utf-8')
        self.override = override_settings(BASE_DIR=self.root, FRONTEND_ENABLED=True, PANEL_LIVE=False)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.root)

    def test_shell_and_style_policy(self):
        response = self.client.get('/app/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), '<title>隔离构建</title>'.encode())
        self.assertIn("script-src 'self'", response['Content-Security-Policy'])
        self.assertIn("style-src-attr 'unsafe-inline'", response['Content-Security-Policy'])

    def test_no_source_or_traversal(self):
        for url in ('/app/source.map', '/app/../../private.txt', '/app/%2e%2e/%2e%2e/private.txt'):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_local_file_server_disabled_in_production(self):
        with override_settings(PANEL_LIVE=True):
            self.assertEqual(self.client.get('/app/').status_code, 404)

    def test_opt_in(self):
        with override_settings(FRONTEND_ENABLED=False):
            self.assertEqual(self.client.get('/app/').status_code, 404)
