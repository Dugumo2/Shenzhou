"""真实文件发布、隔离、损坏与失败保留测试；仅用公开假内容。"""
import json
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch
import uuid
from bridge.artifacts import ArtifactError, publish, read_artifact


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / ('.artifacts-test-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.owner = uuid.uuid4()
        self.payloads = {k: ('public-test-' + k).encode() for k in ('windows', 'android', 'v2rayng')}

    def cleanup(self):
        target = self.root.resolve()
        assert target.parent == Path(__file__).resolve().parents[1] and target.name.startswith('.artifacts-test-')
        shutil.rmtree(target)

    def test_owner_isolation_and_complete_publish(self):
        other = uuid.uuid4()
        publish(self.root, self.owner, self.payloads)
        publish(self.root, other, {k: b'other' for k in self.payloads})
        for k, raw in self.payloads.items():
            self.assertEqual(read_artifact(self.root, self.owner, k), raw)
            self.assertEqual(read_artifact(self.root, other, k), b'other')

    def test_failed_commit_retains_last_complete_version(self):
        old = publish(self.root, self.owner, self.payloads)
        with patch('bridge.artifacts.os.replace', side_effect=OSError('public test')):
            with self.assertRaises(OSError):
                publish(self.root, self.owner, {k: b'new' for k in self.payloads}, expected_version=old)
        self.assertEqual(read_artifact(self.root, self.owner, 'windows'), self.payloads['windows'])
        self.assertFalse((self.root / str(self.owner) / '.publish-lock').exists())

    def test_stale_publish_refused(self):
        old = publish(self.root, self.owner, self.payloads)
        publish(self.root, self.owner, {k: b'new' for k in self.payloads}, expected_version=old)
        with self.assertRaises(ArtifactError):
            publish(self.root, self.owner, self.payloads, expected_version=old)

    def test_corruption_and_cross_owner_manifest_refused(self):
        version = publish(self.root, self.owner, self.payloads)
        directory = self.root / str(self.owner)
        (directory / 'releases' / version / 'windows.txt').write_bytes(b'corrupt')
        with self.assertRaises(ArtifactError):
            read_artifact(self.root, self.owner, 'windows')
        p = directory / 'current.json'
        manifest = json.loads(p.read_bytes()); manifest['owner'] = str(uuid.uuid4())
        p.write_text(json.dumps(manifest))
        with self.assertRaises(ArtifactError):
            read_artifact(self.root, self.owner, 'android')

    def test_resources_not_inherited_from_old_release(self):
        old = publish(self.root, self.owner, {**self.payloads, 'v2rayng-geoip': b'public-dat'})
        publish(self.root, self.owner, self.payloads, expected_version=old)
        with self.assertRaises(ArtifactError):
            read_artifact(self.root, self.owner, 'v2rayng-geoip')

    def test_invalid_scope_and_incomplete_payload_refused(self):
        for owner, payload in [('../bad', self.payloads), (self.owner, {'windows': b'x'}),
                               (self.owner, {**self.payloads, '../other': b'x'}),
                               (self.owner, {**self.payloads, 'windows': b''})]:
            with self.assertRaises(ArtifactError): publish(self.root, owner, payload)

    def test_symlink_file_refused(self):
        version = publish(self.root, self.owner, self.payloads)
        p = self.root / str(self.owner) / 'releases' / version / 'windows.txt'
        outside = self.root / 'outside'; outside.write_bytes(self.payloads['windows']); p.unlink()
        try: p.symlink_to(outside)
        except OSError: self.skipTest('系统没有创建符号链接权限')
        with self.assertRaises(ArtifactError): read_artifact(self.root, self.owner, 'windows')
