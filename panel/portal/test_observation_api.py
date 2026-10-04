"""现场快照接口的假资料测试；不读取真实现场文件或连接网络。"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from .observation_api import observed_site


class ObservationApiTests(SimpleTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'observation-fixture.json'
        self.snapshot = {
            'schema_version': 1, 'source_label': '隔离假快照', 'captured_at': None,
            'assembled_at': '2026-10-04T00:00:00+08:00',
            'units': [{'name': 'fixture-panel.service', 'status': 'active'}],
            'accounts': {'total': 1, 'active_admins': 1},
            'membership': {'registered_quota_bytes': '123000000000', 'expires_at': None,
                           'provisioning_state': 'pending_apply', 'usage_state': 'not_connected'},
            'rules': [{'id': index + 1, 'action': 'direct', 'kind': 'suffix',
                       'value': f'fixture{index}.example.invalid', 'scope_domain': '', 'enabled': True,
                       'revision': 1} for index in range(16)],
            'resources': [{'key': 'windows-routing', 'label': '原生路由', 'http_status': None,
                           'format': 'unknown', 'status': 'not_tested', 'message': '尚未核验'}],
            'limitations': ['只显示采集时刻的证据，不代表持续监控。'],
        }
        self.write()
        self.factory = RequestFactory()

    def write(self):
        raw = json.dumps(self.snapshot, ensure_ascii=False).encode()
        self.path.write_bytes(raw)
        self.digest = hashlib.sha256(raw).hexdigest()

    def request(self, *, staff=True, authenticated=True):
        request = self.factory.get('/fixture-observation')
        request.user = SimpleNamespace(is_authenticated=authenticated, is_active=True, is_staff=staff)
        with override_settings(OBSERVATION_PATH=self.path, OBSERVATION_SHA256=self.digest):
            return observed_site(request)

    def test_valid_snapshot_preserves_rules_and_unknown_without_writes(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.content)['data']
        self.assertEqual(len(result['rules']), 16)
        self.assertEqual(result, self.snapshot)
        self.assertIsNone(result['membership']['expires_at'])
        self.assertIsNone(result['resources'][0]['http_status'])
        self.assertIn('no-store', response['Cache-Control'])

    def test_normal_user_and_anonymous_are_rejected_before_file_read(self):
        with patch('portal.observation_api._load') as load:
            self.assertEqual(self.request(staff=False).status_code, 403)
            self.assertEqual(self.request(authenticated=False).status_code, 401)
            load.assert_not_called()

    def test_original_expiry_without_timezone_is_preserved_verbatim(self):
        self.snapshot['membership']['expires_at'] = '2026-12-04 12:34:56'
        self.write()
        response = self.request()
        self.assertEqual(response.status_code, 200)
        result = json.loads(response.content)['data']
        self.assertEqual(result['membership']['expires_at'], '2026-12-04 12:34:56')
        self.assertIsNone(result['captured_at'])

    def test_unconfigured_is_clear_404_and_never_reads(self):
        request = self.factory.get('/fixture-observation')
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, is_staff=True)
        with override_settings(OBSERVATION_PATH=None, OBSERVATION_SHA256=''), patch('portal.observation_api._load') as load:
            response = observed_site(request)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(json.loads(response.content)['error']['code'], 'not_configured')
            self.assertIn('no-store', response['Cache-Control'])
            load.assert_not_called()

    def test_modified_file_or_wrong_digest_is_rejected(self):
        self.path.write_bytes(b'{}')
        self.assertEqual(self.request().status_code, 503)
        self.write()
        self.digest = 'f' * 64
        self.assertEqual(self.request().status_code, 503)

    def test_extra_keys_wrong_types_and_oversize_rows_are_rejected(self):
        original = deepcopy(self.snapshot)
        variants = []
        value = deepcopy(original)
        value['secret'] = 'fixture-only'
        variants.append(value)
        value = deepcopy(original)
        value['membership']['registered_quota_bytes'] = 123
        variants.append(value)
        value = deepcopy(original)
        value['rules'][0]['enabled'] = 1
        variants.append(value)
        value = deepcopy(original)
        value['rules'][0]['uri'] = 'fixture-only'
        variants.append(value)
        value = deepcopy(original)
        value['resources'][0]['http_status'] = True
        variants.append(value)
        value = deepcopy(original)
        value['limitations'] = ['fixture'] * 501
        variants.append(value)
        value = deepcopy(original)
        value['captured_at'] = '2026-10-04T00:00:00'
        variants.append(value)
        for index, value in enumerate(variants):
            with self.subTest(case=index):
                self.snapshot = value
                self.write()
                response = self.request()
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('fixture-only', response.content.decode())

    def test_duplicate_json_key_is_rejected(self):
        raw = b'{"schema_version":1,"schema_version":1}'
        self.path.write_bytes(raw)
        self.digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(self.request().status_code, 503)

    def test_oversize_directory_and_relative_path_are_rejected(self):
        self.path.write_bytes(b'x' * (256 * 1024 + 1))
        self.assertEqual(self.request().status_code, 503)
        self.path = self.path.parent
        self.assertEqual(self.request().status_code, 503)
        self.path = Path('relative-observation.json')
        self.assertEqual(self.request().status_code, 503)

    def test_symlink_is_rejected(self):
        with patch('portal.observation_api.Path.is_symlink', return_value=True):
            self.assertEqual(self.request().status_code, 503)
