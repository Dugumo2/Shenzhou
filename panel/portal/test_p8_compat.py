"""真实 P8 文件结构的假资源验证；不读取现场文件、令牌或网络。"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from .p8_compat import (P8SourceError, RESOURCE_FORMATS, binding_verification_sha256, bound_membership_ids, claimed_membership_ids,
                        list_services, load_p8_source, service_detail, service_usage)
from .p8_compat_api import delivery
from .models import Entitlement, LegacyServiceBinding, Membership, P8SourceBinding


class P8FixtureMixin:
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'fixture-links.json'
        self.origin = 'https://fixture.example.invalid:2053'
        self.links = {kind: self.origin + '/s/' + ('w' if kind.startswith('windows') else 'f') * 43 + '/' + kind
                      for kind in RESOURCE_FORMATS if kind != 'windows-routing'}
        self.config = {
            'source_instance': 'fixture-p8', 'source_id': 'fixture-service',
            'links_path': str(self.path), 'origin': self.origin, 'evidence_sha256': 'e' * 64,
            'resources': {kind: {'format': RESOURCE_FORMATS[kind], 'verified': kind != 'windows'}
                          for kind in self.links},
        }
        self.write(self.links)

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding='utf-8')
        if os.name == 'posix':
            self.path.chmod(0o600)
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def load(self):
        return load_p8_source(self.config, self.digest, 'e' * 64)


class P8FileAdapterTests(P8FixtureMixin, unittest.TestCase):
    def test_actual_seven_resource_shape_preserves_original_urls(self):
        source = self.load()
        self.assertEqual(len(source.resources), 7)
        self.assertEqual(source.resource('v2rayng').url, self.links['v2rayng'])
        self.assertEqual(source.resource('windows-rules').format, 'windows_rules_bundle')
        self.assertIsNone(source.resource('windows'))
        self.assertIsNone(source.resource('windows-routing'))
        self.assertNotIn('https:', repr(source))
        self.assertNotIn('/s/', repr(source.resources))

    def test_old_url_is_stable_across_repeated_reads(self):
        self.assertEqual(self.load(), self.load())

    def test_changed_file_fails_until_binding_is_reverified(self):
        self.path.write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(P8SourceError, '^P8_SOURCE_CHANGED$'):
            self.load()

    def test_current_evidence_is_rechecked(self):
        self.config['evidence_sha256'] = 'd' * 64
        with self.assertRaisesRegex(P8SourceError, '^P8_EVIDENCE_CHANGED$'):
            self.load()

    def test_resource_set_change_cannot_partially_deliver(self):
        del self.links['android']
        self.write(self.links)
        with self.assertRaisesRegex(P8SourceError, '^P8_RESOURCE_SET_CHANGED$'):
            self.load()

    def test_native_routing_requires_its_own_source_entry_and_evidence(self):
        self.links['windows-routing'] = self.origin + '/s/' + 'w' * 43 + '/windows-routing'
        self.config['resources']['windows-routing'] = {'format': 'v2rayn_routing_json', 'verified': False}
        self.write(self.links)
        self.assertIsNone(self.load().resource('windows-routing'))
        self.config['resources']['windows-routing']['verified'] = True
        self.assertEqual(self.load().resource('windows-routing').url, self.links['windows-routing'])

    def test_wrong_format_is_not_accepted_for_bundle(self):
        self.config['resources']['windows-rules']['format'] = 'v2rayn_routing_json'
        with self.assertRaisesRegex(P8SourceError, '^P8_RESOURCE_CONFIG_INVALID$'):
            self.load()

    def test_independent_windows_and_android_token_families(self):
        self.assertEqual(len(self.load().resources), 7)
        self.links['v2rayng'] = self.links['v2rayng'].replace('f' * 43, 'z' * 43)
        self.write(self.links)
        with self.assertRaisesRegex(P8SourceError, '^P8_RESOURCE_FAMILY_CHANGED$'):
            self.load()

    def test_link_validation_and_errors_never_echo_secret_input(self):
        original = self.links['android']
        invalid = [original.replace('https:', 'http:'), original + '?token=secret',
                   original + '#secret', original.replace(':2053', ':443'),
                   original.replace('fixture.example.invalid', 'other.example.invalid'),
                   original.replace('/android', '/windows'), original.replace('/s/', '/s/%2e/'),
                   original.replace('https://', 'https://person:secret@'), '\n' + original]
        for url in invalid:
            with self.subTest(case=invalid.index(url)):
                self.links['android'] = url
                self.write(self.links)
                with self.assertRaisesRegex(P8SourceError, '^P8_LINK_INVALID$'):
                    self.load()

    def test_duplicate_keys_rejected(self):
        self.path.write_text('{"android":"fixture","android":"fixture"}', encoding='utf-8')
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(P8SourceError, '^P8_DUPLICATE_RESOURCE$'):
            self.load()

    def test_non_object_and_invalid_json_rejected(self):
        for value in ([], None, 'invalid'):
            with self.subTest(value=value):
                self.write(value)
                with self.assertRaises(P8SourceError):
                    self.load()

    def test_relative_file_and_oversize_file_rejected(self):
        self.config['links_path'] = 'fixture-links.json'
        with self.assertRaisesRegex(P8SourceError, '^P8_FILE_INVALID$'):
            self.load()
        self.config['links_path'] = str(self.path)
        self.path.write_bytes(b'x' * 8193)
        with self.assertRaisesRegex(P8SourceError, '^P8_FILE_INVALID$'):
            self.load()

    def test_missing_file_error_does_not_include_path(self):
        self.path.unlink()
        with self.assertRaisesRegex(P8SourceError, '^P8_FILE_UNAVAILABLE$'):
            self.load()

    def test_python310_path_without_junction_method_is_supported(self):
        class OlderPath:
            def __init__(self, value):
                self.value = Path(value)

            def __fspath__(self):
                return str(self.value)

            @property
            def parents(self):
                return tuple(OlderPath(value) for value in self.value.parents)

            def __getattr__(self, key):
                if key == 'is_junction':
                    raise AttributeError(key)
                return getattr(self.value, key)

        with patch('portal.p8_compat.Path', OlderPath):
            self.assertEqual(len(self.load().resources), 7)

    def test_open_handle_rechecks_link_count_and_changed_metadata(self):
        current = self.path.stat()
        fields = {key: getattr(current, key) for key in ('st_dev', 'st_ino', 'st_size',
            'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink')}
        for patch_values, code in (({'st_nlink': 2}, 'P8_FILE_INVALID'),
                                   ({'st_mtime_ns': current.st_mtime_ns + 1}, 'P8_FILE_CHANGED')):
            changed = SimpleNamespace(**{**fields, **patch_values})
            with self.subTest(code=code), patch('portal.p8_compat.os.fstat', return_value=changed):
                with self.assertRaisesRegex(P8SourceError, '^' + code + '$'):
                    self.load()

    def test_final_handle_metadata_change_blocks_delivery(self):
        current = self.path.stat()
        fields = {key: getattr(current, key) for key in ('st_dev', 'st_ino', 'st_size',
            'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink')}
        for patch_values, code in (({'st_nlink': 2}, 'P8_FILE_INVALID'),
                                   ({'st_ctime_ns': current.st_ctime_ns + 1}, 'P8_FILE_CHANGED')):
            changed = SimpleNamespace(**{**fields, **patch_values})
            with self.subTest(code=code), patch('portal.p8_compat.os.fstat', side_effect=[current, changed]):
                with self.assertRaisesRegex(P8SourceError, '^' + code + '$'):
                    self.load()

    def test_malformed_source_configuration_rejected(self):
        original = deepcopy(self.config)
        changes = [('source_id', '../fixture'), ('origin', self.origin + '/'),
                   ('origin', 'https://fixture.example.invalid:bad'), ('resources', {}),
                   ('source_instance', None)]
        for key, value in changes:
            self.config = deepcopy(original)
            self.config[key] = value
            with self.subTest(key=key), self.assertRaises(P8SourceError):
                self.load()

    @unittest.skipUnless(os.name == 'posix', 'POSIX 文件权限仅在对应系统验证')
    def test_world_readable_file_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(P8SourceError, '^P8_FILE_PERMISSIONS$'):
            self.load()


@override_settings(P8_COMPAT_ENABLED=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class P8BindingTests(P8FixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.owner = User.objects.create_user('fixture-owner')
        self.verifier = User.objects.create_user('fixture-verifier', is_staff=True)
        self.other = User.objects.create_user('fixture-other', is_staff=True)
        self.binding = P8SourceBinding.objects.create(source_instance='fixture-p8', source_id='fixture-service',
            owner=self.owner, verified_by=self.verifier, enabled=True, state='verified',
            evidence_sha256='e' * 64, links_sha256=self.digest, verified_at=timezone.now())
        self.reverify_fixture()
        self.source_settings = override_settings(P8_COMPAT_SOURCES={('fixture-p8', 'fixture-service'): self.config})
        self.source_settings.enable()
        self.addCleanup(self.source_settings.disable)
        self.requests = RequestFactory()

    def reverify_fixture(self):
        self.binding.refresh_from_db()
        P8SourceBinding.objects.filter(pk=self.binding.pk).update(
            verification_sha256=binding_verification_sha256(self.binding))

    def response(self, user=None, client='android', public_id=None, method='get'):
        request = getattr(self.requests, method)('/fixture', {'client_adapter': client})
        request.user = user or self.owner
        return delivery(request, str(public_id or self.binding.public_id))

    def data(self, **kwargs):
        return json.loads(self.response(**kwargs).content)['data']

    def test_enabled_owner_can_get_original_resource_but_not_fake_acceptance(self):
        response = self.response()
        result = json.loads(response.content)['data']
        self.assertEqual(result['state'], 'available')
        self.assertEqual(result['resources'][0]['download_url'], self.links['android'])
        self.assertEqual(result['resources'][0]['verification'], 'http_format_verified')
        self.assertEqual(result['runtime_acceptance'], 'not_tested')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')

    def test_disabled_feature_never_reads_file_or_exposes_service(self):
        with override_settings(P8_COMPAT_ENABLED=False), patch('portal.p8_compat._read_private_file') as read:
            self.assertEqual(list_services(self.owner), [])
            self.assertIsNone(service_detail(self.owner, self.binding.public_id))
            self.assertEqual(self.response().status_code, 404)
            read.assert_not_called()

    def test_other_staff_and_verifier_cannot_read_owner_resources(self):
        with patch('portal.p8_compat._read_private_file') as read:
            for user in (self.other, self.verifier):
                self.assertEqual(list_services(user), [])
                self.assertEqual(self.response(user=user).status_code, 404)
            read.assert_not_called()

    def test_owner_current_database_status_is_rechecked(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assertTrue(self.owner.is_active)
        with patch('portal.p8_compat._read_private_file') as read:
            self.assertEqual(self.response().status_code, 404)
            read.assert_not_called()

    def test_verifier_permission_is_rechecked(self):
        get_user_model().objects.filter(pk=self.verifier.pk).update(is_staff=False)
        with patch('portal.p8_compat._read_private_file') as read:
            self.assertEqual(self.response().status_code, 404)
            read.assert_not_called()

    def test_revocation_and_evidence_change_close_all_views(self):
        for change in ({'state': 'revoked'}, {'enabled': False}, {'evidence_sha256': 'a' * 64}):
            with self.subTest(change=change):
                P8SourceBinding.objects.filter(pk=self.binding.pk).update(**change)
                self.assertEqual(list_services(self.owner), [])
                self.assertEqual(self.response().status_code, 404)
                P8SourceBinding.objects.filter(pk=self.binding.pk).update(
                    state='verified', enabled=True, evidence_sha256='e' * 64)

    def test_file_changed_after_metadata_projection_blocks_delivery(self):
        self.assertEqual(len(list_services(self.owner)), 1)
        self.path.write_text('{}', encoding='utf-8')
        self.assertEqual(self.data()['state'], 'blocked')
        self.assertEqual(self.data()['resources'], [])

    def test_service_metadata_does_not_consume_private_file_or_invent_usage(self):
        with patch('portal.p8_compat._read_private_file') as read:
            value = service_detail(self.owner, self.binding.public_id)
            usage = service_usage(self.owner, self.binding.public_id, '7d')
            self.assertEqual(value['source_type'], 'p8')
            for field in ('quota_bytes', 'used_bytes', 'remaining_bytes', 'expires_at', 'next_reset_at'):
                self.assertIsNone(value[field])
            self.assertNotIn('/s/', json.dumps(value))
            self.assertEqual(usage['quality']['state'], 'unknown')
            self.assertIsNone(usage['summary']['charged_bytes'])
            self.assertEqual(usage['history']['days'], [])
            read.assert_not_called()

    def test_windows_unverified_and_router_unsupported_before_file_read(self):
        with patch('portal.p8_compat._read_private_file') as read:
            self.assertEqual(self.data(client='windows')['state'], 'blocked')
            self.assertEqual(self.data(client='router')['state'], 'unsupported')
            read.assert_not_called()

    def test_ng_resources_are_ordered_and_keep_original_urls(self):
        resources = self.data(client='v2rayng')['resources']
        self.assertEqual([item['key'] for item in resources],
                         ['v2rayng', 'v2rayng-routes', 'v2rayng-geosite', 'v2rayng-geoip'])
        self.assertTrue(all(item['download_url'] == self.links[item['key']] for item in resources))

    def test_get_only_and_invalid_client(self):
        self.assertEqual(self.response(method='post').status_code, 405)
        self.assertEqual(self.response(client='unknown').status_code, 422)

    def test_source_identity_mismatch_never_reads_file(self):
        self.config['source_id'] = 'fixture-other-service'
        with patch('portal.p8_compat._read_private_file') as read:
            self.assertEqual(self.response().status_code, 404)
            read.assert_not_called()

    def attach_member(self, user=None):
        member = Membership.objects.create(user=user or self.owner, quota_bytes=123,
                                            status='active', provisioning_state='pending_apply')
        P8SourceBinding.objects.filter(pk=self.binding.pk).update(legacy_membership=member)
        self.reverify_fixture()
        return member

    def test_explicit_member_alias_deduplicates_without_changing_old_state(self):
        member = self.attach_member()
        self.assertEqual(bound_membership_ids(self.owner), [member.pk])
        self.assertEqual(service_detail(self.owner, member.public_id)['id'], str(self.binding.public_id))
        self.assertEqual(self.data(public_id=member.public_id)['resources'][0]['download_url'], self.links['android'])
        member.refresh_from_db()
        self.assertEqual(member.quota_bytes, 123)
        self.assertEqual(member.provisioning_state, 'pending_apply')
        self.assertFalse(member.eligible)
        self.assertFalse(Entitlement.objects.exists())

    def test_revoked_binding_preserves_claim_for_blocked_legacy_projection(self):
        member = self.attach_member()
        P8SourceBinding.objects.filter(pk=self.binding.pk).update(state='revoked')
        self.assertEqual(bound_membership_ids(self.owner), [])
        self.assertEqual(claimed_membership_ids(self.owner), [member.pk])
        self.assertIsNone(service_detail(self.owner, member.public_id))

    def test_other_owner_membership_association_rejected(self):
        self.attach_member(self.other)
        self.assertEqual(list_services(self.owner), [])
        self.assertEqual(self.response().status_code, 404)

    def test_existing_entitlement_or_legacy_binding_prevents_p8_merge(self):
        member = self.attach_member()
        legacy = LegacyServiceBinding.objects.create(membership=member, owner=self.owner,
            evidence_sha256='c' * 64, membership_revision=member.revision)
        self.assertEqual(list_services(self.owner), [])
        legacy.delete()
        Entitlement.objects.create(user=self.owner, quota_bytes=123)
        self.assertEqual(list_services(self.owner), [])

    def test_uuid_collision_with_old_service_cannot_shadow_it(self):
        Membership.objects.create(user=self.other, public_id=self.binding.public_id)
        self.assertEqual(list_services(self.owner), [])
        self.assertEqual(self.response().status_code, 404)

    def test_source_key_uniqueness_prevents_second_owner(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            P8SourceBinding.objects.create(source_instance='fixture-p8', source_id='fixture-service',
                owner=self.other, evidence_sha256='e' * 64, links_sha256=self.digest)

    def test_changing_owner_with_queryset_cannot_reuse_old_verification(self):
        P8SourceBinding.objects.filter(pk=self.binding.pk).update(owner=self.other, revision=2)
        with patch('portal.p8_compat._read_private_file') as read:
            self.assertEqual(self.response(user=self.other).status_code, 404)
            self.assertEqual(self.response().status_code, 404)
            read.assert_not_called()

    def test_new_association_requires_fresh_explicit_verification(self):
        member = Membership.objects.create(user=self.owner)
        P8SourceBinding.objects.filter(pk=self.binding.pk).update(legacy_membership=member)
        self.assertEqual(bound_membership_ids(self.owner), [])
        self.assertEqual(claimed_membership_ids(self.owner), [member.pk])
        self.assertEqual(self.response().status_code, 404)

    def test_changing_verifier_or_revision_requires_new_proof(self):
        for change in ({'verified_by': self.other}, {'revision': 2}, {'verification_sha256': ''}):
            with self.subTest(change=change):
                P8SourceBinding.objects.filter(pk=self.binding.pk).update(**change)
                self.assertEqual(self.response().status_code, 404)
                P8SourceBinding.objects.filter(pk=self.binding.pk).update(verified_by=self.verifier, revision=1)
                self.reverify_fixture()
