"""资源导入隔离反例：保旧资产、幂等、冲突、失败回滚与输入边界。"""
import copy
import hashlib
import io
import json
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import (BillingCycle, ClientDirectRule, CoreInstance, DeviceSubscription, Egress, Entitlement,
                     Ingress, Line, NodeIdentity, ResourceImportBinding, ResourceImportReceipt, Server,
                     ServerObservation)
from .resource_import import (ResourceImportError, SCHEMA_VERSION, apply_resources, load_manifest,
                              parse_manifest, preview_resources)


@override_settings(PANEL_LIVE=False, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ResourceImportTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('import-admin', is_staff=True)
        self.now = timezone.now() - timedelta(seconds=1)
        self.server = Server.objects.create(name='人工服务器别名', notes='保留备注', adapter='xray', deployment_fence=9)
        self.ingress = Ingress.objects.create(server=self.server, name='原入口', protocol='reality', config_ref='restricted-reference')
        self.home = Egress.objects.create(server=self.server, name='HOME', kind='upstream')
        self.direct = Egress.objects.create(server=self.server, name='BWH', kind='direct')
        self.home_line = Line.objects.create(name='人工HOME别名', notes='线路备注', ingress=self.ingress, egress=self.home)
        self.direct_line = Line.objects.create(name='人工BWH别名', ingress=self.ingress, egress=self.direct)
        self.entitlement = Entitlement.objects.create(user=self.admin, quota_bytes=1024**4)
        self.entitlement.lines.add(self.home_line, self.direct_line)
        self.cycle = BillingCycle.objects.create(entitlement=self.entitlement, starts_at=self.now - timedelta(days=20),
            ends_at=self.now + timedelta(days=10), used_bytes=98253546067)
        subscription = DeviceSubscription.objects.create(entitlement=self.entitlement, client='windows', name='原订阅')
        for generation in range(1, 9):
            NodeIdentity.objects.create(subscription=subscription, line=self.home_line if generation % 2 else self.direct_line,
                                        ingress=self.ingress, generation=generation, state='active')
        for index in range(22):
            ClientDirectRule.objects.create(kind='suffix', value=f'kept-{index}.example', creator=self.admin)
        ServerObservation.objects.create(server=self.server, source='previous', event_id='old-1',
            observed_at=self.now, expires_at=self.now + timedelta(minutes=5), target_fingerprint='a'*64,
            metrics={'cpu_percent': 2})

    def manifest(self, resources=None, revision=1):
        return {'schema_version': SCHEMA_VERSION, 'source': 'approved-inventory', 'revision': revision,
            'observed_at': self.now.isoformat(), 'expires_at': (self.now + timedelta(hours=1)).isoformat(),
            'resources': resources or [
                {'kind': 'server', 'source_id': 'bwh', 'target_pk': self.server.pk, 'expected_revision': self.server.revision,
                 'data': {'name': '来源名称不覆盖人工别名', 'purchase_meter_id': 'bwh-meter'}},
                {'kind': 'egress', 'source_id': 'home', 'target_pk': self.home.pk, 'expected_revision': self.home.revision,
                 'data': {'server': 'bwh', 'ownership': 'external', 'external_kind': 'node', 'protocol': 'ss2022',
                          'endpoint_host': None, 'endpoint_port': None}},
            ]}

    def apply(self, payload):
        return apply_resources(self.admin, payload, expected_preview_sha256=preview_resources(payload)['preview_sha256'])

    def protected_snapshot(self):
        return {model.__name__: list(model.objects.order_by('pk').values()) for model in (
            NodeIdentity, BillingCycle, Entitlement, ClientDirectRule, ServerObservation,
            Entitlement.lines.through, Line.additional_ingresses.through)}

    def test_preview_and_apply_preserve_existing_assets_and_ledger(self):
        before = self.protected_snapshot()
        payload = self.manifest()
        preview = preview_resources(payload)
        self.assertTrue(preview['can_apply'])
        self.assertEqual(preview['rows'][1]['unknown_fields'], ['endpoint_host', 'endpoint_port'])
        self.assertEqual(ResourceImportBinding.objects.count(), 0)
        result = self.apply(payload)
        self.assertTrue(result['applied'])
        self.assertEqual(self.protected_snapshot(), before)
        self.assertEqual((Server.objects.count(), Line.objects.count(), NodeIdentity.objects.count()), (1, 2, 8))
        self.server.refresh_from_db()
        self.assertEqual((self.server.name, self.server.notes, self.server.adapter, self.server.deployment_fence),
                         ('人工服务器别名', '保留备注', 'xray', 9))
        self.home.refresh_from_db()
        self.assertEqual((self.home.ownership, self.home.protocol, self.home.server_id), ('external', 'ss2022', self.server.pk))

    def test_exact_retry_returns_receipt_without_more_rows_or_revision(self):
        payload = self.manifest()
        first = self.apply(payload)
        replay = self.apply(payload)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['targets'], first['targets'])
        self.assertEqual((ResourceImportBinding.objects.count(), ResourceImportReceipt.objects.count()), (2, 1))
        self.server.refresh_from_db()
        self.assertEqual(self.server.revision, 2)

    def test_missing_resource_is_not_deleted_and_unknown_does_not_clear_facts(self):
        self.apply(self.manifest())
        self.server.refresh_from_db()
        payload = self.manifest(revision=2)
        payload['resources'] = payload['resources'][:1]
        payload['resources'][0]['data'] = {'purchase_meter_id': None}
        self.apply(payload)
        self.server.refresh_from_db()
        self.assertEqual(self.server.purchase_meter_id, 'bwh-meter')
        self.assertEqual(Egress.objects.count(), 2)
        self.assertEqual(ResourceImportBinding.objects.count(), 2)

    def test_user_edit_invalidates_preview_and_preserves_alias(self):
        payload = self.manifest()
        preview = preview_resources(payload)
        Server.objects.filter(pk=self.server.pk).update(name='审核后人工改名', revision=2)
        with self.assertRaisesMessage(ResourceImportError, 'preview_changed'):
            apply_resources(self.admin, payload, expected_preview_sha256=preview['preview_sha256'])
        self.assertFalse(ResourceImportBinding.objects.exists())
        payload['resources'][0]['expected_revision'] = 2
        self.apply(payload)
        self.server.refresh_from_db()
        self.assertEqual(self.server.name, '审核后人工改名')

    def test_receipt_conflict_and_out_of_order_are_rejected(self):
        payload = self.manifest(revision=3)
        self.apply(payload)
        changed = copy.deepcopy(payload)
        changed['resources'][0]['data']['name'] = '篡改相同版本'
        with self.assertRaisesMessage(ResourceImportError, 'source_revision_conflict'):
            preview_resources(changed)
        with self.assertRaisesMessage(ResourceImportError, 'source_revision_out_of_order'):
            preview_resources(self.manifest(revision=2))

    def test_rollback_after_first_resource_write_is_atomic(self):
        before = self.protected_snapshot()
        original = ResourceImportBinding.objects.update_or_create
        calls = []
        def fail_second(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise IntegrityError('模拟第二项失败')
            return original(*args, **kwargs)
        with patch.object(ResourceImportBinding.objects, 'update_or_create', side_effect=fail_second):
            with self.assertRaisesMessage(ResourceImportError, 'atomic_import_failed'):
                self.apply(self.manifest())
        self.server.refresh_from_db()
        self.assertEqual((self.server.revision, self.server.purchase_meter_id), (1, None))
        self.assertFalse(ResourceImportBinding.objects.exists())
        self.assertFalse(ResourceImportReceipt.objects.exists())
        self.assertEqual(self.protected_snapshot(), before)

    def test_relationship_and_owner_conflicts_do_not_reassign(self):
        other = Server.objects.create(name='另一台')
        payload = self.manifest()
        payload['resources'][0].update(target_pk=other.pk, data={'name': '另一台'})
        preview = preview_resources(payload)
        self.assertFalse(preview['can_apply'])
        self.assertIn('relationship_change_forbidden', preview['rows'][1]['conflicts'])
        with self.assertRaisesMessage(ResourceImportError, 'unresolved_conflicts'):
            self.apply(payload)
        self.assertFalse(ResourceImportBinding.objects.exists())

    def test_new_resources_and_core_association_use_source_ids_without_grants(self):
        resources = [
            {'kind': 'server', 'source_id': 'new-server', 'data': {'name': '新登记机器'}},
            {'kind': 'ingress', 'source_id': 'new-in', 'data': {'name': '新登记端点', 'server': 'new-server', 'protocol': 'ws'}},
            {'kind': 'core', 'source_id': 'new-core', 'data': {'name': '登记核心', 'server': 'new-server',
                'instance_key': 'main', 'core_type': 'xray', 'configuration_owner': 'unverified', 'ingresses': ['new-in']}},
        ]
        before = self.protected_snapshot()
        self.apply(self.manifest(resources))
        self.apply(self.manifest(resources))
        self.assertEqual((Server.objects.count(), CoreInstance.objects.count(), Ingress.objects.count()), (2, 1, 2))
        self.assertEqual(CoreInstance.objects.get().ingresses.count(), 1)
        self.assertEqual(self.protected_snapshot(), before)

    def test_name_is_only_a_warning_never_an_automatic_match(self):
        payload = self.manifest([{'kind': 'server', 'source_id': 'unknown', 'data': {'name': self.server.name}}])
        preview = preview_resources(payload)
        self.assertFalse(preview['can_apply'])
        self.assertIn('possible_existing_resource_requires_mapping', preview['rows'][0]['conflicts'])
        self.assertIsNone(preview['rows'][0]['target_pk'])

    def test_existing_ownership_and_core_owner_cannot_be_replaced(self):
        self.home.ownership = 'external'
        self.home.save(update_fields=['ownership'])
        payload = self.manifest()
        payload['resources'][1]['data'] = {'ownership': 'self_owned'}
        self.assertFalse(preview_resources(payload)['can_apply'])
        core = CoreInstance.objects.create(server=self.server, name='现有核心', core_type='xray',
                                           instance_key='main', configuration_owner='existing-owner')
        payload['resources'] = [{'kind': 'core', 'source_id': 'main', 'target_pk': core.pk, 'expected_revision': 1,
                                 'data': {'configuration_owner': 'new-owner'}}]
        self.assertIn('protected_field_conflict', preview_resources(payload)['rows'][0]['conflicts'])

    def test_core_relationships_cannot_be_removed_by_new_snapshot(self):
        core = CoreInstance.objects.create(server=self.server, name='现有核心', core_type='xray',
                                           instance_key='main', configuration_owner='existing-owner')
        core.ingresses.add(self.ingress)
        payload = self.manifest([{'kind': 'core', 'source_id': 'main', 'target_pk': core.pk, 'expected_revision': 1,
                                 'data': {'ingresses': []}}])
        self.assertIn('relationship_removal_forbidden', preview_resources(payload)['rows'][0]['conflicts'])
        with self.assertRaisesMessage(ResourceImportError, 'unresolved_conflicts'):
            self.apply(payload)
        self.assertEqual(list(core.ingresses.values_list('pk', flat=True)), [self.ingress.pk])

    def test_import_cannot_change_revision_of_active_core_job(self):
        from .models import CoreConfigCandidate, CoreControlJob
        core = CoreInstance.objects.create(server=self.server, name='活动核心', core_type='xray',
            instance_key='busy', configuration_owner='existing-owner')
        candidate = CoreConfigCandidate.objects.create(core=core, base_revision=1,
            patch={'log': {'level': 'info'}}, base_sha256='a'*64, candidate_sha256='b'*64,
            binding_fingerprint='c'*64, artifact_ref='d'*32, creator=self.admin,
            idempotency_key='candidate-import', request_sha256='e'*64)
        job = CoreControlJob.objects.create(core=core, candidate=candidate, actor=self.admin,
            action='apply', status='queued', expected_revision=1, fence=1,
            idempotency_key='job-import', request_sha256='f'*64,
            expires_at=timezone.now()+timedelta(minutes=5))
        payload = self.manifest([{'kind': 'core', 'source_id': 'busy', 'target_pk': core.pk,
            'expected_revision': 1, 'data': {'registered_version': '1.14.1'}}])
        for state in ('queued', 'running', 'unknown'):
            job.status=state; job.save(update_fields=['status'])
            with self.assertRaisesMessage(ResourceImportError, 'core_busy'):
                self.apply(payload)
            core.refresh_from_db()
            self.assertEqual(core.revision, 1)
            self.assertFalse(ResourceImportReceipt.objects.exists())
        job.status='failed'; job.save(update_fields=['status'])
        self.apply(payload)
        core.refresh_from_db()
        self.assertEqual(core.revision, 2)

    def test_explicit_binding_cannot_be_stolen_by_another_source(self):
        self.apply(self.manifest())
        self.server.refresh_from_db()
        self.home.refresh_from_db()
        payload = self.manifest()
        payload['source'] = 'another-source'
        self.assertIn('target_already_bound', preview_resources(payload)['rows'][0]['conflicts'])
        self.assertEqual(ResourceImportBinding.objects.count(), 2)

    def test_duplicate_unknown_reference_and_invalid_external_shape_block_preview(self):
        payload = self.manifest()
        payload['resources'].append(copy.deepcopy(payload['resources'][0]))
        with self.assertRaisesMessage(ResourceImportError, 'duplicate_source_id'):
            preview_resources(payload)
        payload = self.manifest()
        payload['resources'][1]['data']['server'] = 'missing'
        self.assertIn('unresolved_relation', preview_resources(payload)['rows'][1]['conflicts'])
        payload = self.manifest()
        payload['resources'][1]['data']['ownership'] = 'self_owned'
        self.assertIn('external_kind_requires_external_ownership', preview_resources(payload)['rows'][1]['conflicts'])

    def test_file_command_apply_requires_reviewed_digest_and_reuses_receipt(self):
        path = Path(settings.DATA_ROOT) / ('b3-test-' + uuid.uuid4().hex + '.json')
        try:
            payload = self.manifest()
            raw = json.dumps(payload).encode()
            path.write_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            preview = preview_resources(payload)
            output = io.StringIO()
            call_command('import_resources', manifest=str(path), expected_sha256=sha, apply=True,
                         actor_id=self.admin.pk, expected_preview_sha256=preview['preview_sha256'], stdout=output)
            self.assertTrue(json.loads(output.getvalue())['applied'])
            self.assertEqual(ResourceImportReceipt.objects.count(), 1)
        finally:
            path.unlink(missing_ok=True)

    def test_expired_and_invalid_host_inputs_never_apply(self):
        payload = self.manifest()
        with self.assertRaisesMessage(ResourceImportError, 'expired_evidence'):
            preview_resources(payload, now=self.now + timedelta(hours=2))
        for host in ('example.com/secret', 'user@example.com', '[::1]:443', '-bad.example', 'bad..example'):
            payload['resources'][1]['data']['endpoint_host'] = host
            with self.assertRaisesMessage(ResourceImportError, 'invalid_endpoint_host'):
                preview_resources(payload)

    def test_service_unit_and_source_fields_are_safe_identifiers(self):
        core = CoreInstance.objects.create(server=self.server, name='现有核心', core_type='xray',
                                           instance_key='main', configuration_owner='existing-owner')
        for data in ({'service_unit': 'xray.service;reboot'}, {'service_unit': '/etc/xray.service'},
                     {'evidence_source': 'user:password@host'}, {'evidence_source': '/private/config'},
                     {'configuration_owner': 'user:password@host'},
                     {'evidence_observed_at': (self.now + timedelta(hours=1)).isoformat()}):
            payload = self.manifest([{'kind': 'core', 'source_id': 'main', 'target_pk': core.pk,
                                     'expected_revision': 1, 'data': data}])
            with self.assertRaises(ResourceImportError):
                preview_resources(payload)
        payload['resources'][0]['data'] = {'service_unit': 'xray@main.service', 'evidence_source': 'approved-inventory'}
        self.assertTrue(preview_resources(payload)['can_apply'])

    def linked_manifest(self):
        payload = self.manifest()
        payload['resources'][1]['data'] = {'server': 'bwh', 'ownership': 'self_owned', 'protocol': 'socks',
            'endpoint_host': '127.0.0.1', 'endpoint_port': 18080, 'core_instance': 'sing-box', 'next_egress': 'external'}
        payload['resources'].extend([
            {'kind': 'core', 'source_id': 'sing-box', 'data': {'name': '我方入口核心', 'server': 'bwh',
                'instance_key': 'sing-box', 'core_type': 'sing_box', 'configuration_owner': 'observed'}},
            {'kind': 'core', 'source_id': 'xray', 'data': {'name': '我方中转核心', 'server': 'bwh',
                'instance_key': 'xray', 'core_type': 'xray', 'configuration_owner': 'observed'}},
            {'kind': 'egress', 'source_id': 'external', 'data': {'name': 'HOME外部节点', 'server': 'bwh',
                'kind': 'upstream', 'ownership': 'external', 'external_kind': 'node', 'protocol': 'ss2022',
                'endpoint_host': None, 'endpoint_port': 54097, 'core_instance': 'xray', 'purchase_meter_id': 'home'}},
        ])
        return payload

    def test_unknown_core_and_next_links_are_filled_without_replacing_line_endpoint(self):
        before = self.protected_snapshot()
        payload = self.linked_manifest()
        self.apply(payload)
        self.home.refresh_from_db()
        self.home_line.refresh_from_db()
        self.assertEqual(self.home_line.egress_id, self.home.pk)
        self.assertEqual((self.home.config_ref, self.home.protocol, self.home.core_instance.core_type), ('', 'socks', 'sing_box'))
        self.assertEqual((self.home.next_egress.protocol, self.home.next_egress.core_instance.core_type), ('ss2022', 'xray'))
        self.assertEqual(self.home.next_egress.purchase_meter_id, 'home')
        self.assertIsNone(self.home.purchase_meter_id)
        self.assertEqual(self.protected_snapshot(), before)
        self.assertTrue(self.apply(payload)['replayed'])

    def test_known_execution_core_cannot_be_reassigned(self):
        core = CoreInstance.objects.create(server=self.server, name='固定核心', core_type='xray',
            instance_key='fixed', configuration_owner='existing')
        self.home.core_instance = core
        self.home.save(update_fields=['core_instance'])
        preview = preview_resources(self.linked_manifest())
        row = next(row for row in preview['rows'] if row['source_id'] == 'home')
        self.assertIn('relationship_change_forbidden', row['conflicts'])

    def test_self_loop_and_batch_cycle_are_rejected(self):
        payload = self.linked_manifest()
        payload['resources'][1]['data']['next_egress'] = 'home'
        with self.assertRaisesMessage(ResourceImportError, 'resource_relation_cycle'):
            preview_resources(payload)
        payload = self.linked_manifest()
        payload['resources'][-1]['data']['next_egress'] = 'home'
        with self.assertRaisesMessage(ResourceImportError, 'resource_relation_cycle'):
            preview_resources(payload)

    def test_existing_graph_cycle_and_seventeen_hops_are_rejected(self):
        chain = [Egress.objects.create(server=self.server, name=f'已有段{i}', kind='upstream') for i in range(16)]
        for current, following in zip(chain, chain[1:]):
            current.next_egress = following
            current.save(update_fields=['next_egress'])
        ResourceImportBinding.objects.create(source='approved-inventory', resource_kind='egress', source_id='chain',
                                            target_pk=chain[0].pk, source_revision=1, fingerprint='a'*64)
        payload = self.manifest()
        payload['resources'][1]['data'] = {'next_egress': 'chain'}
        preview = preview_resources(payload)
        self.assertIn('egress_chain_too_long', next(row for row in preview['rows'] if row['source_id'] == 'home')['conflicts'])
        chain[0].next_egress = self.home
        chain[0].save(update_fields=['next_egress'])
        preview = preview_resources(payload)
        self.assertIn('egress_chain_cycle', next(row for row in preview['rows'] if row['source_id'] == 'home')['conflicts'])

    def test_execution_core_server_and_source_boundaries(self):
        other = Server.objects.create(name='另一机器')
        core = CoreInstance.objects.create(server=other, name='异机核心', core_type='xray',
                                          instance_key='other', configuration_owner='observed')
        ResourceImportBinding.objects.create(source='approved-inventory', resource_kind='core', source_id='other',
                                            target_pk=core.pk, source_revision=1, fingerprint='a'*64)
        payload = self.manifest()
        payload['resources'][1]['data'] = {'core_instance': 'other'}
        preview = preview_resources(payload)
        self.assertIn('execution_core_server_mismatch', preview['rows'][1]['conflicts'])
        ResourceImportBinding.objects.filter(resource_kind='core').update(source='another-source')
        preview = preview_resources(payload)
        self.assertIn('unresolved_relation', preview['rows'][1]['conflicts'])

    def test_invalid_fields_expiry_and_boolean_integer_are_rejected_before_write(self):
        for change in ({'password': 'never-log-this'}, {'notes': '不能覆盖人工备注'}, {'enabled': True}, {'endpoint_port': True}):
            payload = self.manifest()
            payload['resources'][1]['data'].update(change)
            with self.assertRaises(ResourceImportError):
                self.apply(payload)
        for field, value in [('revision', True), ('expires_at', self.now.isoformat()), ('source', 'https://not-allowed')]:
            payload = self.manifest()
            payload[field] = value
            with self.assertRaises(ResourceImportError):
                parse_manifest(payload)
        self.assertFalse(ResourceImportBinding.objects.exists())

    def test_admin_and_live_gates(self):
        payload = self.manifest()
        preview = preview_resources(payload)
        user = User.objects.create_user('ordinary')
        with self.assertRaisesMessage(ResourceImportError, 'admin_required'):
            apply_resources(user, payload, expected_preview_sha256=preview['preview_sha256'])
        with override_settings(PANEL_LIVE=True):
            with self.assertRaisesMessage(ResourceImportError, 'live_import_disabled'):
                self.apply(payload)

    def test_file_hash_duplicate_keys_and_default_command_preview(self):
        path = Path(settings.DATA_ROOT) / ('b3-test-' + uuid.uuid4().hex + '.json')
        try:
            raw = json.dumps(self.manifest()).encode()
            path.write_bytes(raw)
            sha = hashlib.sha256(raw).hexdigest()
            self.assertEqual(load_manifest(path, sha), self.manifest())
            with self.assertRaisesMessage(ResourceImportError, 'file_digest_mismatch'):
                load_manifest(path, '0'*64)
            output = io.StringIO()
            call_command('import_resources', manifest=str(path), expected_sha256=sha, stdout=output)
            self.assertTrue(json.loads(output.getvalue())['can_apply'])
            self.assertFalse(ResourceImportReceipt.objects.exists())
            raw = b'{"source": "a", "source": "b"}'
            path.write_bytes(raw)
            with self.assertRaisesMessage(ResourceImportError, 'duplicate_json_key'):
                load_manifest(path, hashlib.sha256(raw).hexdigest())
        finally:
            path.unlink(missing_ok=True)
