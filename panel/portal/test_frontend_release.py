"""跨发行兼容只读测试：显式旧根、有限窗口、摘要和路径边界。"""
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid

from django.conf import settings
from django.http import Http404
from django.test import SimpleTestCase, RequestFactory, override_settings

from .frontend_release import release_asset, release_version


class ReleaseCompatibilityTest(SimpleTestCase):
    def setUp(self):
        self.parent=settings.BASE_DIR.parent/'staging'
        self.root=self.parent/('release-compat-test-'+uuid.uuid4().hex)
        self.old=self.root/'old'; self.new=self.root/'new'
        self.old_name='assets/Page-old12345.js'; self.new_name='assets/Page-new12345.js'
        self.old_digest=self.make_release(self.old,{self.old_name:b'old immutable chunk'})
        self.new_digest=self.make_release(self.new,{self.new_name:b'new immutable chunk'})
        self.compat={'root':str(self.old),'manifest_sha256':self.old_digest,'expires_at':int(time.time())+3600}
        self.override=override_settings(FRONTEND_ENABLED=True,FRONTEND_RELEASE_ROOT=str(self.new),
            FRONTEND_MANIFEST_SHA256=self.new_digest,FRONTEND_COMPAT_RELEASES=[self.compat])
        self.override.enable()
        self.request=RequestFactory().get('/app/')

    def tearDown(self):
        self.override.disable()
        assert self.root.resolve().is_relative_to(self.parent.resolve())
        shutil.rmtree(self.root)

    def make_release(self,root,assets):
        assets={'index.html':b'<html><head></head><body>app</body></html>',**assets}
        for name,raw in assets.items():
            file=root/name; file.parent.mkdir(parents=True,exist_ok=True); file.write_bytes(raw)
        raw=json.dumps({'schema_version':1,'files':{name:{'bytes':len(value),
            'sha256':hashlib.sha256(value).hexdigest()} for name,value in assets.items()}}).encode()
        (root/'frontend-manifest.json').write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    def test_old_and_new_chunks_and_current_html(self):
        self.assertEqual(release_asset(self.request,self.old_name).content,b'old immutable chunk')
        self.assertEqual(release_asset(self.request,self.new_name).content,b'new immutable chunk')
        response=release_asset(self.request)
        self.assertIn(self.new_digest.encode(),response.content)
        self.assertEqual(response['Cache-Control'],'no-store, private')
        self.assertNotIn(self.new_digest.encode(),(self.new/'index.html').read_bytes())

    def test_missing_or_expired_compat_stays_404(self):
        for entries in ([],[{**self.compat,'expires_at':int(time.time())-1}]):
            with self.subTest(entries=len(entries)),override_settings(FRONTEND_COMPAT_RELEASES=entries):
                for _ in range(3):
                    with self.assertRaises(Http404):release_asset(self.request,self.old_name)
                self.assertEqual(release_asset(self.request,self.new_name).status_code,200)

    def test_environment_json_is_strict_and_bounded(self):
        with override_settings(FRONTEND_COMPAT_RELEASES=json.dumps([self.compat])):
            self.assertEqual(release_asset(self.request,self.old_name).status_code,200)
        for raw in ('[', '{}', ' '*8193, '[{"root":"a","root":"b"}]'):
            with self.subTest(raw=raw[:50]),override_settings(FRONTEND_COMPAT_RELEASES=raw):
                with self.assertRaises(Http404):release_asset(self.request,self.old_name)

    def test_invalid_compat_configuration_is_not_enumerated(self):
        for entries in ([self.compat]*3,{},[{**self.compat,'extra':True}],
                        [{**self.compat,'expires_at':True}],[{**self.compat,'expires_at':int(time.time())+8*86400}],
                        [{**self.compat,'root':'relative'}],[{**self.compat,'manifest_sha256':'0'*64}]):
            with self.subTest(entries=entries),override_settings(FRONTEND_COMPAT_RELEASES=entries):
                with self.assertRaises(Http404):release_asset(self.request,self.old_name)

    def test_current_damage_never_falls_back(self):
        digest=self.make_release(self.new,{self.old_name:b'new owns same name'})
        with override_settings(FRONTEND_MANIFEST_SHA256=digest):
            (self.new/self.old_name).write_bytes(b'damaged')
            with self.assertRaises(Http404):release_asset(self.request,self.old_name)
        with override_settings(FRONTEND_MANIFEST_SHA256='0'*64):
            with self.assertRaises(Http404):release_asset(self.request,self.old_name)

    def test_compat_damage_and_unsafe_paths_rejected(self):
        for name in ('../old/'+self.old_name,'assets/%2e%2e/private.js','assets/..\\private.js',
                     'index-old12345.html','frontend-manifest.json','assets/no-hash.js'):
            with self.subTest(name=name),self.assertRaises(Http404):release_asset(self.request,name)
        (self.old/self.old_name).write_bytes(b'changed')
        with self.assertRaises(Http404):release_asset(self.request,self.old_name)

    def test_version_contains_only_current_digest_and_is_read_only(self):
        response=release_version(self.request)
        self.assertEqual(json.loads(response.content),{'schema_version':1,'version':self.new_digest})
        self.assertEqual(response['Cache-Control'],'no-store, private')
        self.assertEqual(release_version(RequestFactory().post('/app/release-version.json')).status_code,405)
        with override_settings(FRONTEND_ENABLED=False):
            with self.assertRaises(Http404):release_version(self.request)
        with override_settings(FRONTEND_MANIFEST_SHA256='0'*64):
            with self.assertRaises(Http404):release_version(self.request)
