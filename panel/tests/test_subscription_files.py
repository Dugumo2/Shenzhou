"""独立订阅文件边界测试，不生成真实订阅或访问网络。"""
import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings
from portal.subscription_files import _read


class SubscriptionFilesTests(SimpleTestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[2] / 'staging' / 'phase1-test-tmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        # Windows 受限令牌下 tempfile 的 0700 ACL 不继承工作区权限，使用普通测试目录。
        self.root = temp_root / ('case-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.override = override_settings(ARTIFACT_ROOT=self.root)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.sub = SimpleNamespace(public_id=uuid.uuid4(), generation=1, client='windows')
        self.directory = self.root / 'subscriptions' / str(self.sub.public_id) / '1'
        self.directory.mkdir(parents=True)
        self.payload = b'fixture-only-not-a-node'
        self.manifest = {'subscription': str(self.sub.public_id), 'generation': 1, 'client': 'windows',
                         'evidence_scope': 'production', 'sha256': hashlib.sha256(self.payload).hexdigest()}

    def write(self):
        (self.directory / 'subscription.txt').write_bytes(self.payload)
        (self.directory / 'manifest.json').write_text(json.dumps(self.manifest), encoding='utf-8')

    def test_missing_payload_denied(self):
        with self.assertRaises(ValidationError):
            _read(self.sub)

    def test_matching_manifest_can_be_read(self):
        self.write()
        self.assertEqual(_read(self.sub)[0], self.payload)

    def test_other_owner_and_old_generation_rejected(self):
        for key, value in [('subscription', str(uuid.uuid4())), ('generation', 2), ('client', 'android'),
                           ('evidence_scope', 'isolated'), ('sha256', '0' * 64)]:
            original = self.manifest[key]
            self.manifest[key] = value
            self.write()
            with self.assertRaises(ValidationError):
                _read(self.sub)
            self.manifest[key] = original

    def test_changed_file_rejected(self):
        self.write()
        (self.directory / 'subscription.txt').write_bytes(b'changed')
        with self.assertRaises(ValidationError):
            _read(self.sub)

    def test_atomic_release_resources_are_bound_and_checked(self):
        release = uuid.uuid4().hex
        destination = self.directory / 'releases' / release
        destination.mkdir(parents=True)
        raw = b'{"rules":[]}'
        (destination / 'routing.json').write_bytes(raw)
        manifest = {**self.manifest, 'schema': 2, 'release': release,
            'resources': {'routing': {'filename': 'routing.json', 'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(), 'content_type': 'application/json'}}}
        current = self.directory / 'current.json'
        current.write_text(json.dumps(manifest), encoding='utf-8')
        self.assertEqual(_read(self.sub, 'routing')[0], raw)
        manifest['release'] = '../../outside'
        current.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(ValidationError):
            _read(self.sub, 'routing')

    def test_non_object_manifest_is_denied(self):
        (self.directory / 'current.json').write_text('[]', encoding='utf-8')
        with self.assertRaises(ValidationError):
            _read(self.sub)
