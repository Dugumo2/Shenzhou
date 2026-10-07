"""Q3 独立业务反例：仅假账号、假链接和隔离数据库，不读取现场秘密。"""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from portal.models import (BillingCycle, BillingPlan, DeploymentJob, DeviceSubscription,
    Egress, Entitlement, Ingress, Line, LineRateVersion, Membership, NodeIdentity,
    P8EntitlementBinding, P8SourceBinding, Server, UsageLedger, UsageStream)
from portal.p8_compat import RESOURCE_FORMATS, binding_verification_sha256, load_p8_source
from portal.p8_entitlement import (_digest, preview_p8_entitlement, revoke_p8_entitlement,
                                   verify_p8_entitlement)
from portal.test_p8_compat import P8FixtureMixin


@override_settings(ROOT_URLCONF='megabox.urls', PANEL_LIVE=False, P8_COMPAT_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class P8EntitlementBindingTests(P8FixtureMixin, TestCase):
    def setUp(self):
        TestCase.setUp(self)
        # 在隔离数据根创建单个假文件，避免依赖 Windows 临时目录的继承权限。
        self.path = Path(settings.DATA_ROOT) / ('q3-fixture-links-' + uuid4().hex + '.json')
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.origin = 'https://fixture.example.invalid:2053'
        self.links = {kind: self.origin + '/s/' + ('w' if kind.startswith('windows') else 'f') * 43 + '/' + kind
                      for kind in RESOURCE_FORMATS}
        self.config = {'source_instance': 'fixture-p8', 'source_id': 'fixture-service',
            'links_path': str(self.path), 'origin': self.origin, 'evidence_sha256': 'e' * 64}
        users = get_user_model()
        self.admin = users.objects.create_user('q3-verifier', is_staff=True)
        self.owner = users.objects.create_user('q3-owner')
        self.other = users.objects.create_user('q3-other')
        self.other_admin = users.objects.create_user('q3-other-admin', is_staff=True)
        self.member = Membership.objects.create(user=self.owner, status='active',
            quota_bytes=10**12, revision=2, provisioning_state='pending_apply',
            usage_state='not_connected')
        self.links['windows-routing'] = self.origin + '/s/' + 'w' * 43 + '/windows-routing'
        self.config['resources'] = {kind: {'format': fmt, 'verified': True}
                                    for kind, fmt in RESOURCE_FORMATS.items()}
        self.write(self.links)
        self.source = P8SourceBinding.objects.create(owner=self.owner, legacy_membership=self.member,
            source_instance='fixture-p8', source_id='fixture-service', verified_by=self.admin,
            verified_at=timezone.now(), enabled=True, state='verified',
            evidence_sha256='e' * 64, links_sha256=self.digest)
        self.source.verification_sha256 = binding_verification_sha256(self.source)
        self.source.save(update_fields=['verification_sha256'])
        self.configs = {('fixture-p8', 'fixture-service'): self.config}
        setting = override_settings(P8_COMPAT_SOURCES=self.configs)
        setting.enable()
        self.addCleanup(setting.disable)

    def target(self, *, owner=None, public_id=None):
        return Entitlement.objects.create(user=owner or self.owner,
            public_id=public_id or self.source.public_id, quota_bytes=10**12,
            revision=2, applied_revision=0, state='pending')

    def verify(self, target, revision=0, actor=None):
        return verify_p8_entitlement(actor or self.admin, self.source.public_id,
            target.pk, expected_revision=revision, evidence_sha256='b' * 64)

    def read(self, path, *, actor=None):
        self.client.force_login(actor or self.owner)
        response = self.client.get('/api/v1/' + path)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def protected(self):
        # 全量仅限本测试创建的假记录，验证关联不暗改权益、身份和历史。
        tables = (Membership, Entitlement, BillingCycle, BillingPlan, DeploymentJob,
                  DeviceSubscription, NodeIdentity, UsageStream, UsageLedger, P8SourceBinding)
        return {model.__name__: list(model.objects.order_by('pk').values()) for model in tables}

    def assert_one_service(self):
        canonical = str(self.source.public_id)
        mine = self.read('me/services')['items']
        admin = self.read('admin/services', actor=self.admin)
        owner_admin = self.read('admin/users/' + str(self.owner.pk), actor=self.admin)
        self.assertEqual([row['id'] for row in mine], [canonical])
        self.assertEqual(admin['pagination']['total'], 1)
        self.assertEqual([row['id'] for row in admin['items']], [canonical])
        self.assertEqual(owner_admin['user']['service_count'], 1)
        self.assertEqual([row['id'] for row in owner_admin['services']], [canonical])
        for public_id in (self.source.public_id, self.member.public_id):
            detail = self.read('me/services/' + str(public_id))
            self.assertEqual(detail['id'], canonical)
            self.assertEqual(detail['source_type'], 'p8')
        return mine[0]

    def assert_original_delivery(self):
        for public_id in (self.source.public_id, self.member.public_id):
            for adapter, expected in (('android', ['android']), ('windows', ['windows', 'windows-routing']),
                    ('v2rayng', ['v2rayng', 'v2rayng-routes', 'v2rayng-geosite', 'v2rayng-geoip'])):
                with self.subTest(alias=public_id == self.member.public_id, adapter=adapter):
                    data = self.read(f'me/services/{public_id}/p8-delivery?client_adapter={adapter}')
                    self.assertEqual(data['state'], 'available')
                    self.assertEqual(data['service_id'], str(self.source.public_id))
                    self.assertEqual({row['key']: row['download_url'] for row in data['resources']},
                                     {kind: self.links[kind] for kind in expected})
        self.assertEqual(set(self.links), set(RESOURCE_FORMATS))
        source = load_p8_source(self.config, self.digest, 'e' * 64)
        self.assertEqual({resource.kind: resource.url for resource in source.resources}, self.links)

    def assert_mapping_closed(self):
        row = self.assert_one_service()
        self.assertEqual(row['state'], 'mapping_required')
        self.assertIsNone(row['quota_bytes'])
        self.assertIsNone(row['used_bytes'])
        for public_id in (self.source.public_id, self.member.public_id):
            data = self.read(f'me/services/{public_id}/usage')
            self.assertIsNone(data['summary']['quota_bytes'])
            self.assertIsNone(data['summary']['charged_bytes'])
            self.client.force_login(self.owner)
            with patch('portal.p8_compat._read_private_file') as private:
                response = self.client.get(f'/api/v1/me/services/{public_id}/p8-delivery?client_adapter=android')
                self.assertEqual(response.status_code, 404)
                private.assert_not_called()

    def seed_usage(self, target):
        now = timezone.now()
        target.activated_at = now - timedelta(days=1)
        target.expires_at = now + timedelta(days=90)
        target.applied_revision = target.revision
        target.applied_snapshot = {'quota_bytes': target.quota_bytes}
        target.state = 'simulated'
        target.usage_updated_at = now
        target.save()
        cycle = BillingCycle.objects.create(entitlement=target, starts_at=now-timedelta(days=1),
            ends_at=now+timedelta(days=29), used_bytes=375, raw_bytes=300, weighted_remainder=23)
        server = Server.objects.create(name='Q3隔离机器')
        ingress = Ingress.objects.create(server=server, name='Q3假入口', protocol='reality')
        egress = Egress.objects.create(server=server, name='Q3假出口', kind='direct')
        line = Line.objects.create(name='Q3假线路', ingress=ingress, egress=egress)
        target.lines.set([line])
        subscription = DeviceSubscription.objects.create(entitlement=target, name='Q3假身份',
            client='windows', state='simulated')
        identity = NodeIdentity.objects.create(subscription=subscription, line=line,
            ingress=ingress, generation=1, state='simulated')
        stream = UsageStream.objects.create(identity=identity, epoch='q3-fixture', sequence=1,
            upload_bytes=100, download_bytes=200)
        rate = LineRateVersion.objects.create(line=line, actor=self.admin, multiplier='1.25',
            effective_at=target.activated_at, reason='Q3独立假账本')
        UsageLedger.objects.create(cycle=cycle, stream=stream, sequence=1, upload_delta=100,
            download_delta=200, weighted_bytes=375, quality='metered', observed_at=now, rate_version=rate)
        return cycle

    def test_unmapped_original_service_and_all_original_links_remain_available(self):
        before = self.protected()
        row = self.assert_one_service()
        self.assertEqual(row['quota_state'], 'unknown')
        self.assertIsNone(row['quota_bytes'])
        self.assert_original_delivery()
        self.assertEqual(before, self.protected())
        self.assertEqual(P8EntitlementBinding.objects.count(), 0)

    def test_preview_is_read_only_and_does_not_consume_private_link_file(self):
        target = self.target()
        before = self.protected()
        with CaptureQueriesContext(connection) as queries, patch('portal.p8_compat._read_private_file') as private:
            preview = preview_p8_entitlement(self.admin, self.source.public_id, target.pk)
        self.assertEqual(str(preview['p8_public_id']), str(self.source.public_id))
        self.assertEqual(str(preview['entitlement_id']), str(target.pk))
        self.assertEqual(preview['revision'], 0)
        self.assertEqual(len(preview['snapshot_sha256']), 64)
        self.assertEqual(preview['changes'], [])
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        private.assert_not_called()
        self.assertEqual(before, self.protected())
        self.assertEqual(P8EntitlementBinding.objects.count(), 0)

    def test_binding_keeps_one_id_alias_all_links_and_does_not_apply_configured_quota(self):
        self.assert_one_service()
        self.assert_original_delivery()
        target = self.target()
        before = self.protected()
        self.verify(target)
        row = self.assert_one_service()
        self.assertEqual(row['quota_bytes'], str(target.quota_bytes))
        self.assertEqual(row['quota_state'], 'configured')
        self.assertIsNone(row['used_bytes'])
        self.assertIsNone(row['remaining_bytes'])
        self.assert_original_delivery()
        for public_id in (self.source.public_id, self.member.public_id):
            data = self.read(f'me/services/{public_id}/usage')
            self.assertEqual(data['service_id'], str(self.source.public_id))
            self.assertEqual(data['source_type'], 'p8')
            self.assertEqual(data['summary']['quota_state'], 'configured')
            self.assertEqual(data['summary']['quota_bytes'], str(target.quota_bytes))
            self.assertIsNone(data['summary']['charged_bytes'])
            self.assertIsNone(data['summary']['remaining_bytes'])
            self.assertIsNone(data['current_cycle'])
        self.assertEqual(before, self.protected())

    def test_binding_uses_existing_entitlement_usage_without_regranting_or_clearing_history(self):
        target = self.target()
        self.seed_usage(target)
        before = self.protected()
        binding = self.verify(target)
        first_binding = deepcopy(list(P8EntitlementBinding.objects.values()))
        row = self.assert_one_service()
        self.assertEqual(row['used_bytes'], '375')
        for public_id in (self.source.public_id, self.member.public_id):
            data = self.read(f'me/services/{public_id}/usage')
            self.assertEqual(data['summary']['charged_bytes'], '375')
            self.assertEqual(data['summary']['upload_bytes'], '100')
            self.assertEqual(data['summary']['download_bytes'], '200')
            self.assertEqual(data['history']['totals']['charged_bytes'], '375')
        repeated = self.verify(target, binding.revision)
        self.assertEqual(repeated.pk, binding.pk)
        self.assertEqual(repeated.revision, binding.revision)
        self.assertEqual(first_binding, list(P8EntitlementBinding.objects.values()))
        self.assertEqual(before, self.protected())

    def test_stale_expected_revision_and_invalid_evidence_cannot_partially_write(self):
        target = self.target()
        binding = self.verify(target)
        before = list(P8EntitlementBinding.objects.values())
        with self.assertRaises(ValidationError):
            self.verify(target, binding.revision + 1)
        for evidence in ('', 'not-a-sha', 'A' * 64):
            with self.subTest(evidence_length=len(evidence)), self.assertRaises(ValidationError):
                verify_p8_entitlement(self.admin, self.source.public_id, target.pk,
                    expected_revision=binding.revision, evidence_sha256=evidence)
        self.assertEqual(before, list(P8EntitlementBinding.objects.values()))

    def test_exact_original_creation_request_replay_returns_same_revision_without_writes(self):
        target = self.target()
        self.seed_usage(target)
        original = self.verify(target, revision=0)
        self.assertEqual(original.revision, 1)
        before = self.protected()
        mapping_before = list(P8EntitlementBinding.objects.values())
        with CaptureQueriesContext(connection) as queries:
            repeated = self.verify(target, revision=0)
        self.assertEqual(repeated.pk, original.pk)
        self.assertEqual(repeated.revision, 1)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(before, self.protected())
        self.assertEqual(mapping_before, list(P8EntitlementBinding.objects.values()))

    def test_creation_replay_with_changed_actor_evidence_or_target_is_rejected(self):
        target = self.target()
        self.verify(target)
        foreign = Entitlement.objects.create(user=self.other, quota_bytes=123456)
        before = self.protected()
        mapping_before = list(P8EntitlementBinding.objects.values())
        requests = ((self.other_admin, target.pk, 'b' * 64),
                    (self.admin, target.pk, 'c' * 64),
                    (self.admin, foreign.pk, 'b' * 64))
        for actor, target_id, evidence in requests:
            with self.subTest(actor=actor.pk, target=target_id, changed_evidence=evidence != 'b' * 64):
                with CaptureQueriesContext(connection) as queries, self.assertRaises(ValidationError):
                    verify_p8_entitlement(actor, self.source.public_id, target_id,
                        expected_revision=0, evidence_sha256=evidence)
                self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(before, self.protected())
        self.assertEqual(mapping_before, list(P8EntitlementBinding.objects.values()))

    def test_original_creation_replay_does_not_restore_revoked_mapping(self):
        target = self.target()
        binding = self.verify(target)
        revoke_p8_entitlement(self.admin, self.source.public_id, expected_revision=binding.revision)
        mapping_before = list(P8EntitlementBinding.objects.values())
        with CaptureQueriesContext(connection) as queries, self.assertRaises(ValidationError):
            self.verify(target, revision=0)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(mapping_before, list(P8EntitlementBinding.objects.values()))
        self.assert_mapping_closed()

    def test_original_creation_replay_does_not_resign_membership_revision_drift(self):
        target = self.target()
        self.verify(target)
        Membership.objects.filter(pk=self.member.pk).update(revision=3)
        mapping_before = list(P8EntitlementBinding.objects.values())
        with CaptureQueriesContext(connection) as queries, self.assertRaises(ValidationError):
            self.verify(target, revision=0)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(mapping_before, list(P8EntitlementBinding.objects.values()))
        self.assert_mapping_closed()

    def test_revoke_cannot_resign_tampered_target_and_restore_original_independent_quota(self):
        target = self.target()
        binding = self.verify(target)
        foreign = Entitlement.objects.create(user=self.other, quota_bytes=987654321)
        P8EntitlementBinding.objects.filter(pk=binding.pk).update(entitlement=foreign)
        before = self.protected()
        mapping_before = list(P8EntitlementBinding.objects.values())
        with CaptureQueriesContext(connection) as queries, self.assertRaises(ValidationError):
            revoke_p8_entitlement(self.admin, self.source.public_id, expected_revision=binding.revision)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        self.assertEqual(before, self.protected())
        self.assertEqual(mapping_before, list(P8EntitlementBinding.objects.values()))
        owner_rows = self.read('me/services')['items']
        self.assertEqual([row['id'] for row in owner_rows], [str(self.source.public_id)])
        self.assertEqual(owner_rows[0]['source_type'], 'p8')
        self.assertEqual(owner_rows[0]['state'], 'mapping_required')
        self.assertIsNone(owner_rows[0]['quota_bytes'])
        foreign_rows = self.read('me/services', actor=self.other)['items']
        self.assertEqual([row['id'] for row in foreign_rows], [str(foreign.public_id)])
        self.assertEqual(foreign_rows[0]['quota_bytes'], '987654321')

    def test_foreign_target_and_wrong_canonical_id_are_rejected_without_changes(self):
        for owner, public_id in ((self.other, self.source.public_id), (self.owner, uuid4())):
            target = self.target(owner=owner, public_id=public_id)
            before = self.protected()
            with self.subTest(foreign=owner == self.other), self.assertRaises(ValidationError):
                self.verify(target)
            self.assertEqual(before, self.protected())
        self.assertEqual(P8EntitlementBinding.objects.count(), 0)

    def test_old_uuid_collision_does_not_authorize_owner_to_read_foreign_entitlement(self):
        foreign = self.target(owner=self.other)
        self.client.force_login(self.owner)
        with patch('portal.p8_compat._read_private_file') as private:
            for suffix in ('', '/usage', '/p8-delivery?client_adapter=android'):
                response = self.client.get(f'/api/v1/me/services/{foreign.public_id}{suffix}')
                self.assertEqual(response.status_code, 404)
            private.assert_not_called()
        rows = self.read('me/services', actor=self.other)['items']
        self.assertEqual([row['id'] for row in rows], [str(foreign.public_id)])

    def test_invalid_binding_never_hides_foreign_independent_entitlement(self):
        target = self.target()
        binding = self.verify(target)
        foreign = Entitlement.objects.create(user=self.other, quota_bytes=987654321)
        P8EntitlementBinding.objects.filter(pk=binding.pk).update(entitlement=foreign)
        rows = self.read('me/services', actor=self.other)['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], str(foreign.public_id))
        self.assertEqual(rows[0]['quota_bytes'], '987654321')
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f'/api/v1/me/services/{foreign.public_id}').status_code, 404)
        owner_rows = self.read('me/services')['items']
        self.assertTrue(all(row.get('quota_bytes') != '987654321' for row in owner_rows))
        self.assertTrue(all(row['state'] == 'mapping_required' for row in owner_rows))

    def test_source_drift_does_not_fallback_to_second_entitlement_or_old_resources(self):
        target = self.target()
        self.verify(target)
        P8SourceBinding.objects.filter(pk=self.source.pk).update(source_id='changed-source')
        self.assert_mapping_closed()

    def test_source_evidence_drift_does_not_fallback_or_expose_resources(self):
        target = self.target()
        self.verify(target)
        self.config['evidence_sha256'] = 'f' * 64
        self.assert_mapping_closed()

    def test_saved_snapshot_tamper_cannot_reenable_accounting_or_resources(self):
        target = self.target()
        binding = self.verify(target)
        snapshot = deepcopy(binding.snapshot)
        snapshot['owner_id'] = self.other.pk
        P8EntitlementBinding.objects.filter(pk=binding.pk).update(snapshot=snapshot)
        self.assert_mapping_closed()

    def assert_malformed_snapshot_isolated(self, modifications, *, raw_snapshots=False):
        target = self.target()
        binding = self.verify(target)
        foreign = Entitlement.objects.create(user=self.other, quota_bytes=987654321)
        self.seed_usage(foreign)
        original = deepcopy(binding.snapshot)
        for label, changes in modifications:
            with self.subTest(malformed=label):
                snapshot = changes if raw_snapshots else {**deepcopy(original), **changes}
                snapshot_sha = _digest(snapshot)
                fields = snapshot if type(snapshot) is dict else {}
                # 摘要不是秘密签名。重算自洽的历史回执，确保类型检查先于 ORM 查询。
                verification_sha = _digest({'p8_id': fields.get('p8_id'),
                    'entitlement_id': fields.get('entitlement_id'), 'owner_id': fields.get('owner_id'),
                    'revision': binding.revision, 'snapshot_sha256': snapshot_sha,
                    'evidence_sha256': binding.evidence_sha256, 'verified_by_id': binding.verified_by_id,
                    'verified_at': binding.verified_at.isoformat()})
                P8EntitlementBinding.objects.filter(pk=binding.pk).update(snapshot=snapshot,
                    snapshot_sha256=snapshot_sha, verification_sha256=verification_sha)
                # 使用真实 QuerySet 和真实接口；坏 A 不能使全局遍历击穿正常 B。
                foreign_rows = self.read('me/services', actor=self.other)['items']
                self.assertEqual([row['id'] for row in foreign_rows], [str(foreign.public_id)])
                self.assertEqual(foreign_rows[0]['quota_bytes'], '987654321')
                detail = self.read(f'me/services/{foreign.public_id}', actor=self.other)
                self.assertEqual(detail['id'], str(foreign.public_id))
                self.assertEqual(detail['quota_bytes'], '987654321')
                self.read(f'admin/services/{foreign.public_id}/billing', actor=self.admin)
                admin_rows = self.read('admin/services', actor=self.admin)['items']
                self.assertEqual({row['id'] for row in admin_rows},
                                 {str(self.source.public_id), str(foreign.public_id)})
                own_rows = self.read('me/services')['items']
                self.assertEqual([row['id'] for row in own_rows], [str(self.source.public_id)])
                self.assertEqual(own_rows[0]['state'], 'mapping_required')
                self.assertIsNone(own_rows[0]['quota_bytes'])

    def test_malformed_snapshot_entitlement_id_never_breaks_other_users_real_queries(self):
        self.assert_malformed_snapshot_isolated([
            ('dict', {'entitlement_id': {}}),
            ('list', {'entitlement_id': []}),
            ('bool', {'entitlement_id': True}),
            ('oversize_integer', {'entitlement_id': 2**100}),
        ])

    def test_malformed_snapshot_owner_id_never_hides_other_users_real_services(self):
        values = ({}, [], True, str(self.owner.pk), 2**100)
        self.assert_malformed_snapshot_isolated([
            (type(value).__name__, {'owner_id': value, 'entitlement_owner_id': value})
            for value in values])

    def test_malformed_snapshot_uuid_never_breaks_other_users_detail_or_billing(self):
        self.assert_malformed_snapshot_isolated([
            (type(value).__name__, {'public_id': value, 'entitlement_public_id': value})
            for value in ({}, [], True, 123, 'not-a-uuid')])

    def test_snapshot_top_level_json_shapes_fail_closed_without_global_query_failure(self):
        self.assert_malformed_snapshot_isolated([
            (type(value).__name__, value) for value in ({}, [], 'invalid', True, 123)
        ], raw_snapshots=True)

    def test_valid_snapshot_without_membership_still_projects_existing_entitlement(self):
        self.source.legacy_membership = None
        self.source.verification_sha256 = binding_verification_sha256(self.source)
        self.source.save(update_fields=['legacy_membership', 'verification_sha256'])
        target = self.target()
        before = self.protected()
        binding = self.verify(target)
        for key in ('membership_id', 'membership_revision', 'membership_public_id', 'membership_owner_id'):
            self.assertIsNone(binding.snapshot[key])
        row = self.read(f'me/services/{self.source.public_id}')
        self.assertEqual(row['id'], str(self.source.public_id))
        self.assertEqual(row['source_type'], 'p8')
        self.assertEqual(row['state'], 'pending')
        self.assertEqual(row['quota_state'], 'configured')
        self.assertEqual(row['quota_bytes'], str(target.quota_bytes))
        delivery = self.read(f'me/services/{self.source.public_id}/p8-delivery?client_adapter=android')
        self.assertEqual(delivery['state'], 'available')
        self.assertEqual(delivery['resources'][0]['download_url'], self.links['android'])
        self.assertEqual(before, self.protected())

    def test_target_public_id_drift_keeps_original_single_unknown_service(self):
        target = self.target()
        self.verify(target)
        changed_id = uuid4()
        Entitlement.objects.filter(pk=target.pk).update(public_id=changed_id)
        self.assert_mapping_closed()
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f'/api/v1/me/services/{changed_id}').status_code, 404)

    def test_member_owner_drift_does_not_reveal_foreign_alias_or_resources(self):
        target = self.target()
        self.verify(target)
        Membership.objects.filter(pk=self.member.pk).update(user=self.other)
        self.client.force_login(self.owner)
        with patch('portal.p8_compat._read_private_file') as private:
            for path in (f'me/services/{self.member.public_id}',
                         f'me/services/{self.member.public_id}/usage',
                         f'me/services/{self.source.public_id}/p8-delivery?client_adapter=android'):
                self.assertEqual(self.client.get('/api/v1/' + path).status_code, 404)
            private.assert_not_called()
        rows = self.read('me/services')['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], str(self.source.public_id))
        self.assertEqual(rows[0]['state'], 'mapping_required')

    def test_another_admin_has_no_owner_delivery_or_usage_permission_after_binding(self):
        target = self.target()
        self.seed_usage(target)
        self.verify(target)
        for actor in (self.other, self.other_admin, self.admin):
            self.client.force_login(actor)
            with self.subTest(actor=actor.pk), patch('portal.p8_compat._read_private_file') as private:
                for public_id in (self.source.public_id, self.member.public_id):
                    for suffix in ('', '/usage', '/p8-delivery?client_adapter=android'):
                        response = self.client.get(f'/api/v1/me/services/{public_id}{suffix}')
                        self.assertEqual(response.status_code, 404)
                private.assert_not_called()

    def test_database_disabled_owner_cannot_use_stale_session_for_downloads(self):
        target = self.target()
        self.verify(target)
        self.client.force_login(self.owner)
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        with patch('portal.p8_compat._read_private_file') as private:
            response = self.client.get(f'/api/v1/me/services/{self.source.public_id}/p8-delivery?client_adapter=android')
            self.assertIn(response.status_code, (401, 403, 404))
            private.assert_not_called()

    def test_member_revision_drift_requires_reverification_and_keeps_one_unknown(self):
        target = self.target()
        self.verify(target)
        Membership.objects.filter(pk=self.member.pk).update(revision=3)
        self.assert_mapping_closed()

    def test_target_owner_drift_never_borrows_foreign_quota_or_history(self):
        target = self.target()
        self.seed_usage(target)
        self.verify(target)
        Entitlement.objects.filter(pk=target.pk).update(user=self.other)
        rows = self.read('me/services')['items']
        for row in rows:
            self.assertIsNone(row['quota_bytes'])
            self.assertIsNone(row['used_bytes'])
        self.client.force_login(self.owner)
        with patch('portal.p8_compat._read_private_file') as private:
            self.assertEqual(self.client.get(f'/api/v1/me/services/{self.source.public_id}/p8-delivery?client_adapter=android').status_code, 404)
            private.assert_not_called()
        # 已核套餐被直接改归属不能给新用户恢复为独立权益；这是原套餐漂移，
        # 与坏绑定误指另一用户既有、合法套餐的情形不同。
        self.assertEqual(self.read('me/services', actor=self.other)['items'], [])
        self.client.force_login(self.other)
        for suffix in ('', '/usage', '/p8-delivery?client_adapter=android'):
            self.assertEqual(self.client.get(f'/api/v1/me/services/{target.public_id}{suffix}').status_code, 404)

    def test_revocation_keeps_one_unknown_service_and_preserves_underlying_account(self):
        target = self.target()
        self.seed_usage(target)
        binding = self.verify(target)
        before = self.protected()
        revoke_p8_entitlement(self.admin, self.source.public_id, expected_revision=binding.revision)
        self.assert_mapping_closed()
        self.assertEqual(before, self.protected())

    def test_recalled_verifier_is_rechecked_from_database(self):
        target = self.target()
        self.verify(target)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_staff=False)
        self.assertTrue(self.admin.is_staff)
        # 使用另一位仍有效的管理员检查列表，不能靠旧会话属性续权。
        self.admin = self.other_admin
        self.assert_mapping_closed()

    def test_nonadmin_and_stale_admin_cannot_preview_or_write(self):
        target = self.target()
        for actor in (self.owner, self.other):
            with self.subTest(actor=actor.pk):
                with self.assertRaises(PermissionDenied):
                    preview_p8_entitlement(actor, self.source.public_id, target.pk)
                with self.assertRaises(PermissionDenied):
                    self.verify(target, actor=actor)
        get_user_model().objects.filter(pk=self.admin.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.verify(target)
        self.assertEqual(P8EntitlementBinding.objects.count(), 0)

    def test_live_mode_rejects_mutation_but_keeps_read_compatibility(self):
        target = self.target()
        binding = self.verify(target)
        before = self.protected()
        mapping_before = list(P8EntitlementBinding.objects.values())
        with override_settings(PANEL_LIVE=True):
            with self.assertRaises(PermissionDenied):
                self.verify(target, binding.revision)
            with self.assertRaises(PermissionDenied):
                revoke_p8_entitlement(self.admin, self.source.public_id, expected_revision=binding.revision)
            preview_p8_entitlement(self.admin, self.source.public_id, target.pk, binding.revision)
            self.assert_one_service()
            self.assert_original_delivery()
        self.assertEqual(before, self.protected())
        self.assertEqual(mapping_before, list(P8EntitlementBinding.objects.values()))
