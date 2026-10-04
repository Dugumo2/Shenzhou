"""构建资源与候选入口的边界；不启动真实代理。"""
from pathlib import Path
import shutil
import uuid
import hashlib
import json

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

    def production_settings(self):
        content=(self.dist/'index.html').read_bytes()
        manifest=(json.dumps({'schema_version':1,'files':{'index.html':{
            'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}}})+'\n').encode()
        (self.dist/'frontend-manifest.json').write_bytes(manifest)
        return override_settings(PANEL_LIVE=True, FRONTEND_RELEASE_ROOT=str(self.dist),
            FRONTEND_MANIFEST_SHA256=hashlib.sha256(manifest).hexdigest())

    def test_explicit_production_release_uses_security_middleware(self):
        with self.production_settings():
            response=self.client.get('/app/')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.content,'<title>隔离构建</title>'.encode())
            self.assertEqual(response['Cache-Control'],'no-store, private')
            self.assertIn("script-src 'self'",response['Content-Security-Policy'])

    def test_production_does_not_serve_unlisted_or_source_files(self):
        (self.dist/'secret.js').write_text('not-in-manifest',encoding='utf-8')
        with self.production_settings():
            for path in ('secret.js','source.map','frontend-manifest.json','../private.txt','%2e%2e/private.txt'):
                with self.subTest(path=path):self.assertEqual(self.client.get('/app/'+path).status_code,404)

    def test_production_rejects_changed_asset(self):
        with self.production_settings():
            (self.dist/'index.html').write_text('changed',encoding='utf-8')
            self.assertEqual(self.client.get('/app/').status_code,404)

    def test_production_rejects_changed_manifest(self):
        with self.production_settings():
            with (self.dist/'frontend-manifest.json').open('a') as stream:stream.write(' ')
            self.assertEqual(self.client.get('/app/').status_code,404)

    def test_production_requires_explicit_flag_and_absolute_release(self):
        with self.production_settings():
            with override_settings(FRONTEND_ENABLED=False):
                self.assertEqual(self.client.get('/app/').status_code,404)
            with override_settings(FRONTEND_RELEASE_ROOT='frontend/dist'):
                self.assertEqual(self.client.get('/app/').status_code,404)

    def test_legacy_account_navigation_uses_configured_production_workspace(self):
        from .frontend import workspace_configured
        with self.production_settings():
            self.assertTrue(workspace_configured())
            with override_settings(FRONTEND_RELEASE_ROOT=''):
                self.assertFalse(workspace_configured())
