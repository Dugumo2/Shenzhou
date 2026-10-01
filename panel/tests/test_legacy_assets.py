"""旧资产隔离导入：摘要、冲突回滚、所有者边界及禁止凭据夹带。"""
import hashlib
import json
from pathlib import Path
import shutil
from unittest.mock import patch
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from portal.legacy_assets import LegacyAssetError, canonical, import_rules, load_snapshot, legacy_service_metadata, _target
from portal.models import ClientDirectRule, AuditEvent


class LegacyAssetsTests(TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[2] / 'staging' / 'phase1-test-tmp' / ('legacy-' + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.owner = get_user_model().objects.create_user('legacy-fixture', is_staff=True)
        self.other = get_user_model().objects.create_user('other-admin', is_staff=True)
        self.rules = [{'id': 5, 'outbound': 'direct', 'kind': 'suffix', 'value': 'fixture.example.cn',
                       'scope_domain': '', 'enabled': True, 'revision': 2}]
        self.snapshot = {'schema': 1, 'source': 'production-panel-readonly', 'collected_at': '2026-09-30T00:00:00+00:00',
            'rule_count': 1, 'rules_sha256': hashlib.sha256(canonical(self.rules)).hexdigest(), 'rules': self.rules,
            'published_policy': {'exists': True, 'rule_count': 1}, 'publish_status': {'exists': True, 'state': 'succeeded'},
            'legacy_reference': {'available': True, 'artifact_count': 1, 'kinds': ['windows'], 'sha256': 'a' * 64, 'mode': '0o600'}}

    def write_snapshot(self):
        raw = json.dumps(self.snapshot).encode()
        self.path = self.root / 'snapshot.json'
        self.path.write_bytes(raw)
        self.sha = hashlib.sha256(raw).hexdigest()

    def apply(self):
        with patch('portal.legacy_assets._target', return_value=self.root):
            return import_rules(self.path, self.sha, target_root=self.root, owner=self.owner, confirm_isolated=True)

    def test_explicit_confirmation_and_target_guard(self):
        with override_settings(PANEL_LIVE=True):
            with self.assertRaisesRegex(LegacyAssetError, 'ISOLATED_CONFIRMATION_REQUIRED'):
                _target(self.root, True)
        with override_settings(PANEL_LIVE=False):
            with self.assertRaisesRegex(LegacyAssetError, 'ISOLATED_CONFIRMATION_REQUIRED'):
                _target(self.root, False)
            with self.assertRaisesRegex(LegacyAssetError, 'EXPLICIT_ISOLATED_TARGET_MISMATCH'):
                _target(self.root, True)

    def test_snapshot_hash_and_embedded_rule_hash(self):
        self.write_snapshot()
        with self.assertRaisesRegex(LegacyAssetError, 'SOURCE_HASH_MISMATCH'):
            load_snapshot(self.path, '0' * 64)
        self.snapshot['rules_sha256'] = '0' * 64
        self.write_snapshot()
        with self.assertRaisesRegex(LegacyAssetError, 'SOURCE_RULE_HASH_MISMATCH'):
            load_snapshot(self.path, self.sha)

    def test_secret_field_rejected(self):
        self.snapshot['legacy_reference']['url'] = 'https://example.invalid/fixture'
        self.write_snapshot()
        with self.assertRaisesRegex(LegacyAssetError, 'SOURCE_REFERENCE_SECRET_DENIED'):
            load_snapshot(self.path, self.sha)

    def test_exact_rule_copy_and_idempotency(self):
        self.write_snapshot()
        first, second = self.apply(), self.apply()
        self.assertEqual((first['created'], second['created'], second['unchanged']), (1, 0, 1))
        self.assertEqual(ClientDirectRule.objects.get().revision, 2)
        self.assertEqual(AuditEvent.objects.filter(action='legacy_rules_imported', subject=self.sha).count(), 1)
        self.assertFalse(first['legacy_links_copied'])

    def test_conflict_keeps_existing_and_rolls_back_batch(self):
        ClientDirectRule.objects.create(creator=self.owner, kind='suffix', value='existing.example.cn', outbound='proxy')
        self.rules.append({'id': 6, 'outbound': 'direct', 'kind': 'suffix', 'value': 'existing.example.cn',
                           'scope_domain': '', 'enabled': True, 'revision': 1})
        self.snapshot['rule_count'] = 2
        self.snapshot['rules_sha256'] = hashlib.sha256(canonical(self.rules)).hexdigest()
        self.write_snapshot()
        with self.assertRaisesRegex(LegacyAssetError, 'EXISTING_RULE_CONFLICT'):
            self.apply()
        self.assertEqual(ClientDirectRule.objects.count(), 1)
        self.assertEqual(ClientDirectRule.objects.get().outbound, 'proxy')
        self.assertFalse(AuditEvent.objects.filter(action='legacy_rules_imported').exists())

    def test_metadata_bound_to_original_admin_without_tokens(self):
        self.write_snapshot()
        self.apply()
        with override_settings(DATA_ROOT=self.root):
            result = legacy_service_metadata(self.owner)
            self.assertEqual(result['artifact_count'], 1)
            self.assertEqual(result['rule_count'], 1)
            self.assertNotIn('/s/', json.dumps(result))
            self.assertIsNone(legacy_service_metadata(self.other))
        self.owner.is_staff = False
        with override_settings(DATA_ROOT=self.root):
            self.assertIsNone(legacy_service_metadata(self.owner))

    def test_non_admin_import_rejected(self):
        self.write_snapshot()
        self.owner.is_staff = False
        with self.assertRaisesRegex(LegacyAssetError, 'EXPLICIT_ADMIN_OWNER_REQUIRED'):
            self.apply()
