"""跨服务入口的资源摘要与权限回归；只使用假快照和隔离测试数据库。"""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Entitlement, P8SourceBinding
from .p8_compat import binding_verification_sha256
from .resource_usage import project_resource_usage
from .service_projection import project, project_index, service_index
from .service_usage_source import ServiceUsageSource
from .test_resource_usage import PLANS, snapshot


@override_settings(ROOT_URLCONF='megabox.urls', P8_COMPAT_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ServiceUsageSourceTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.owner = users.objects.create_user('summary-owner', is_staff=True)
        self.other = users.objects.create_user('summary-other', is_staff=True)
        self.empty = users.objects.create_user('summary-empty')
        self.binding = P8SourceBinding.objects.create(owner=self.owner, source_instance='fixture',
            source_id='summary', verified_by=self.other, verified_at=timezone.now(), enabled=True,
            state='verified', evidence_sha256='e' * 64, links_sha256='a' * 64)
        self.binding.verification_sha256 = binding_verification_sha256(self.binding)
        self.binding.save(update_fields=['verification_sha256'])
        self.source = {'source_instance': 'fixture', 'source_id': 'summary', 'path': '/fixture/usage.json'}
        self.config = {'source_instance': 'fixture', 'source_id': 'summary', 'evidence_sha256': 'e' * 64}
        self.override = override_settings(P8_COMPAT_SOURCES={('fixture', 'summary'): self.config},
                                          PROVIDER_USAGE_SOURCE=self.source)
        self.override.enable()
        self.addCleanup(self.override.disable)
        # 固定在已确认HOME首周期内，避免测试日期改变资源周期语义。
        self.now = timezone.datetime(2026, 10, 7, 5, tzinfo=timezone.get_default_timezone())
        _, self.dto = project_resource_usage(snapshot(self.now.timestamp()), PLANS, None, self.now.timestamp())
        self.client.force_login(self.owner)

    def get(self, path):
        response = self.client.get('/api/v1/' + path)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def projections(self):
        identifier = str(self.binding.public_id)
        return [self.get('me/services')['items'][0], self.get('me/services/' + identifier),
                self.get('admin/services')['items'][0],
                self.get('admin/users/' + str(self.owner.pk))['services'][0],
                self.get('me/services/' + identifier + '/usage')]

    def test_all_entrypoints_preserve_same_sources_scope_quality_and_sample_times(self):
        with patch('portal.resource_usage_view.read_resource_usage_view', return_value=self.dto) as read, \
                patch('portal.p8_compat._read_private_file') as secret:
            entries = self.projections()
        self.assertEqual(read.call_count, 5)
        secret.assert_not_called()
        source_key = entries[0]['provider_usage']['source_key']
        self.assertRegex(source_key, r'^[a-f0-9]{64}$')
        self.assertNotIn('source_key', self.dto)
        for entry in entries:
            self.assertEqual(entry['provider_usage'], {**self.dto, 'source_key': source_key})
            quota = entry.get('summary', entry)
            for key in ('quota_bytes', 'remaining_bytes', 'next_reset_at'):
                self.assertIsNone(quota[key])
            if 'summary' not in entry:
                self.assertIsNone(entry['used_bytes'])
                self.assertIsNone(entry['expires_at'])
        home, bwg = entries[0]['provider_usage']['meters']
        self.assertEqual((home['scope'], bwg['scope']), ('external_node', 'server'))
        self.assertEqual((home['used_bytes'], bwg['used_bytes']), ('300', '1000'))
        self.assertEqual((home['expires_on'], bwg['expires_on']), ('2027-01-19', None))
        self.assertNotEqual(home['cycle']['next_reset_at'], bwg['cycle']['next_reset_at'])

    def test_missing_corrupt_and_stale_snapshots_have_same_safe_state_in_all_entrypoints(self):
        for raw in (FileNotFoundError('private-path'), b'{broken-json'):
            kwargs = {'side_effect': raw} if isinstance(raw, Exception) else {'return_value': raw}
            with self.subTest(raw=type(raw).__name__), \
                    patch('portal.resource_usage_view._private_read', **kwargs) as read:
                entries = self.projections()
            self.assertEqual(read.call_count, 5)
            for entry in entries:
                source = entry['provider_usage']
                self.assertEqual(source['meters'], [])
                self.assertIsNone(source['generated_at'])
                self.assertEqual(source['error']['code'], 'usage_unavailable')
                self.assertNotIn('private-path', json.dumps(source))
        stale = deepcopy(self.dto)
        for meter in stale['meters']:
            meter['expires_at'] = (self.now - timedelta(days=1)).isoformat()
        with patch('portal.resource_usage_view._private_read', return_value=json.dumps(stale).encode()):
            entries = self.projections()
        for entry in entries:
            for actual, original in zip(entry['provider_usage']['meters'], stale['meters']):
                self.assertEqual(actual['quality'], 'stale')
                self.assertEqual(actual['used_bytes'], original['used_bytes'])
                self.assertEqual(actual['observed_at'], original['observed_at'])
                self.assertIn('source_stale', [item['code'] for item in actual['alerts']])

    def test_other_staff_metadata_access_does_not_grant_global_statistics(self):
        self.client.force_login(self.other)
        with patch('portal.resource_usage_view.read_resource_usage_view') as read:
            admin = self.get('admin/services')['items'][0]
            user = self.get('admin/users/' + str(self.owner.pk))['services'][0]
            self.assertNotIn('provider_usage', admin)
            self.assertNotIn('provider_usage', user)
            for suffix in ('', '/usage'):
                response = self.client.get('/api/v1/me/services/' + str(self.binding.public_id) + suffix)
                self.assertEqual(response.status_code, 404)
            read.assert_not_called()

    def test_nonstaff_owner_and_empty_account_never_read_global_statistics(self):
        self.owner.is_staff = False
        self.owner.save(update_fields=['is_staff'])
        with patch('portal.resource_usage_view.read_resource_usage_view') as read:
            self.assertNotIn('provider_usage', self.get('me/services')['items'][0])
            self.assertNotIn('provider_usage', self.get('me/services/' + str(self.binding.public_id)))
            self.assertNotIn('provider_usage', self.get('me/services/' + str(self.binding.public_id) + '/usage'))
            self.client.force_login(self.empty)
            self.assertEqual(self.get('me/services')['items'], [])
            read.assert_not_called()
        self.assertEqual(P8SourceBinding.objects.count(), 1)

    def test_other_services_do_not_inherit_source_and_list_reads_only_once(self):
        entitlement = Entitlement.objects.create(user=self.owner, quota_bytes=123)
        second = P8SourceBinding.objects.create(owner=self.owner, source_instance='fixture',
            source_id='other-service', verified_by=self.other, verified_at=timezone.now(), enabled=True,
            state='verified', evidence_sha256='e' * 64, links_sha256='a' * 64)
        second.verification_sha256 = binding_verification_sha256(second)
        second.save(update_fields=['verification_sha256'])
        configs = {('fixture', 'summary'): self.config,
                   ('fixture', 'other-service'): {**self.config, 'source_id': 'other-service'}}
        with override_settings(P8_COMPAT_SOURCES=configs), \
                patch('portal.resource_usage_view.read_resource_usage_view', return_value=self.dto) as read:
            items = self.get('me/services')['items']
            read.assert_called_once_with(self.source['path'])
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertEqual('provider_usage' in item, item['id'] == str(self.binding.public_id))
        self.assertEqual(next(item for item in items if item['id'] == str(entitlement.public_id))['quota_bytes'], '123')

    def test_source_cache_is_request_local_and_permission_checked_before_cache(self):
        with patch('portal.resource_usage_view.read_resource_usage_view', return_value=self.dto) as read:
            reader = ServiceUsageSource(self.owner)
            first = project(self.binding, 'p8', usage_source=reader)
            second = project(self.binding, 'p8', usage_source=reader)
            self.assertEqual(first['provider_usage'], second['provider_usage'])
            read.assert_called_once()
            self.assertIsNone(reader.read(self.binding, 'p8', {'state': 'mapping_required'}))
            self.assertIsNone(reader.read(self.binding, 'entitlement', first))
            reader.viewer = self.other
            self.assertIsNone(reader.read(self.binding, 'p8', first))
            project_index(service_index(self.owner), viewer=self.owner)
            self.assertEqual(read.call_count, 2)

    def test_source_key_is_stable_for_success_failure_and_path_changes(self):
        path = 'me/services/' + str(self.binding.public_id)
        with patch('portal.resource_usage_view.read_resource_usage_view', return_value=self.dto):
            success = self.get(path)['provider_usage']
            repeated = self.get(path)['provider_usage']
        self.assertEqual(success['source_key'], repeated['source_key'])
        with patch('portal.resource_usage_view._private_read', side_effect=FileNotFoundError()):
            missing = self.get(path)['provider_usage']
        self.assertEqual(missing['source_key'], success['source_key'])
        self.assertEqual(missing['error']['code'], 'usage_unavailable')
        with patch('portal.resource_usage_view._private_read', return_value=b'{broken'):
            corrupt = self.get(path)['provider_usage']
        self.assertEqual(corrupt['source_key'], success['source_key'])
        with override_settings(PROVIDER_USAGE_SOURCE={**self.source, 'path': '/another/usage.json'}), \
                patch('portal.resource_usage_view.read_resource_usage_view', return_value=self.dto):
            moved = self.get(path)['provider_usage']
        self.assertEqual(moved['source_key'], success['source_key'])

    def test_reverification_and_source_change_replace_key_without_reusing_old_identity(self):
        path = 'me/services/' + str(self.binding.public_id)
        with patch('portal.resource_usage_view.read_resource_usage_view', return_value=self.dto):
            before = self.get(path)['provider_usage']['source_key']
            self.binding.revision += 1
            self.binding.verification_sha256 = binding_verification_sha256(self.binding)
            self.binding.save(update_fields=['revision', 'verification_sha256'])
            verified = self.get(path)['provider_usage']['source_key']
            self.assertNotEqual(before, verified)
            self.binding.source_id = 'replacement'
            self.binding.verification_sha256 = binding_verification_sha256(self.binding)
            self.binding.save(update_fields=['source_id', 'verification_sha256'])
            with override_settings(PROVIDER_USAGE_SOURCE={**self.source, 'source_id': 'replacement'},
                    P8_COMPAT_SOURCES={('fixture', 'replacement'): {**self.config, 'source_id': 'replacement'}}):
                replaced = self.get(path)['provider_usage']['source_key']
            self.assertNotEqual(verified, replaced)
        # 未核对的改变不提供source_key；不能靠旧缓存推测新来源。
        with patch('portal.resource_usage_view.read_resource_usage_view') as read:
            self.assertNotIn('provider_usage', self.get(path))
            read.assert_not_called()

    def test_source_key_includes_public_service_identity_and_is_not_exposed_cross_account(self):
        from uuid import uuid4
        with patch('portal.resource_usage_view.read_resource_usage_view', return_value=self.dto):
            before = self.get('me/services/' + str(self.binding.public_id))['provider_usage']['source_key']
            self.binding.public_id = uuid4()
            self.binding.verification_sha256 = binding_verification_sha256(self.binding)
            self.binding.save(update_fields=['public_id', 'verification_sha256'])
            after = self.get('me/services/' + str(self.binding.public_id))['provider_usage']['source_key']
        self.assertNotEqual(before, after)
        self.client.force_login(self.other)
        with patch('portal.resource_usage_view.read_resource_usage_view') as read:
            response = self.get('admin/services')
            self.assertNotIn('source_key', json.dumps(response))
            self.assertNotIn(before, json.dumps(response))
            self.assertNotIn(after, json.dumps(response))
            read.assert_not_called()

    def test_revoked_or_unmapped_binding_and_mismatched_source_never_read(self):
        for config in ({}, {**self.source, 'source_id': 'other'}, {**self.source, 'source_instance': 'other'}):
            with override_settings(PROVIDER_USAGE_SOURCE=config), \
                    patch('portal.resource_usage_view.read_resource_usage_view') as read:
                for entry in self.projections():
                    self.assertNotIn('provider_usage', entry)
                read.assert_not_called()
        for change in ({'revision': 2}, {'enabled': False}, {'state': 'revoked'}):
            with self.subTest(change=change):
                P8SourceBinding.objects.filter(pk=self.binding.pk).update(**change)
                with patch('portal.resource_usage_view.read_resource_usage_view') as read:
                    for entry in self.projections():
                        self.assertNotIn('provider_usage', entry)
                    read.assert_not_called()
