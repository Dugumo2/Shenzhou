"""公共接线的定向反例，不替代实际核心执行及生产验收。"""
import json
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from . import core_executor
from .resource_plan_source import read_resource_plan_source


class CoreBridgeTests(SimpleTestCase):
    def setUp(self):
        self.job = SimpleNamespace(public_id='job', core=SimpleNamespace(public_id='core'),
            action='check', fence=4, candidate=SimpleNamespace(artifact_ref='a' * 32,
            base_sha256='b' * 64, candidate_sha256='c' * 64, binding_fingerprint='d' * 64))
        self.result = {'status': 'succeeded', 'core_id': 'core', 'check_fence': 4,
                       'candidate_sha256': 'c' * 64, 'binding_fingerprint': 'd' * 64}
        self.checker = Mock()

    def test_check_maps_verified_configuration_hash(self):
        self.checker.check_candidate.return_value = self.result
        with patch.object(core_executor, '_binding', return_value=SimpleNamespace(checker=self.checker)):
            value = core_executor.execute_job(self.job)
        self.assertEqual(value['config_sha256'], 'c' * 64)
        self.assertEqual(value['fence'], 4)

    def test_check_does_not_relabel_wrong_receipt(self):
        self.checker.check_candidate.return_value = {**self.result, 'check_fence': 2}
        with patch.object(core_executor, '_binding', return_value=SimpleNamespace(checker=self.checker)):
            value = core_executor.execute_job(self.job)
        self.assertEqual(value['status'], 'unknown')
        self.assertNotIn('config_sha256', value)

    def test_reconcile_only_loads_persisted_check(self):
        self.checker.load_check.return_value = self.result
        with patch.object(core_executor, '_binding', return_value=SimpleNamespace(checker=self.checker)):
            value = core_executor.reconcile_job(self.job)
        self.assertEqual(value['status'], 'succeeded')
        self.checker.check_candidate.assert_not_called()

    @override_settings(CORE_CONTROL_BINDINGS={})
    def test_production_binding_is_not_inferred(self):
        self.assertFalse(core_executor.binding_status(self.job.core)['available'])


class AuthoritySourceTests(SimpleTestCase):
    @override_settings(PANEL_LIVE=False)
    def test_fresh_finite_source_and_expiry(self):
        plan = {'id': 'home', 'label': 'HOME', 'source': 'residential', 'scope': 'external_node',
                'quota_bytes': '400000000000', 'cycle': {'kind': 'fixed_days', 'days': 30,
                'anchor': '2026-09-20T00:00:00+08:00'}, 'expires_on': '2027-01-19',
                'accepted_initial_gap': True}
        directory = Path(tempfile.gettempdir()) / ('m1-authority-' + uuid.uuid4().hex)
        directory.mkdir(mode=0o755)
        path = directory / 'authority.json'
        try:
            doc = {'schema_version': 1, 'observed_at': timezone.now().isoformat(), 'plans': [plan]}
            path.write_text(json.dumps(doc), encoding='utf-8')
            with override_settings(RESOURCE_PLAN_AUTHORITY_TEST_PATH=path):
                self.assertEqual(read_resource_plan_source('home'), plan)
                doc['observed_at'] = '2000-01-01T00:00:00Z'
                path.write_text(json.dumps(doc), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'authority_expired'):
                    read_resource_plan_source('home')
        finally:
            path.unlink(missing_ok=True)
            directory.rmdir()
