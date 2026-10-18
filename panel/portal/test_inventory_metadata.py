"""资料维护隔离反例：人工资料、既有身份与账本不因编辑或导入改变。"""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings
from django.urls import path
from django.utils import timezone

from . import inventory_api
from .core_control import _finish_job
from .core_control_models import CoreConfigCandidate, CoreControlJob, CoreControlState
from .inventory_service import InventoryError, edit_metadata
from .models import (AuditEvent, BillingCycle, CoreInstance, DeviceSubscription, Egress,
                     Entitlement, Ingress, InventoryMutationReceipt, Line, NodeIdentity, Server)
from .resource_import import SCHEMA_VERSION, apply_resources, preview_resources
from .observation_ingest import target_fingerprint


urlpatterns = [
    path('api/v1/admin/servers/<str:public_id>', inventory_api.server_detail),
    path('api/v1/admin/lines/<str:public_id>', inventory_api.line_detail),
    path('api/v1/admin/cores/<str:public_id>', inventory_api.core_detail),
    path('api/v1/admin/ingresses/<str:resource_id>', inventory_api.ingress_detail),
    path('api/v1/admin/egresses/<str:resource_id>', inventory_api.egress_detail),
]


@override_settings(PANEL_LIVE=False, INVENTORY_METADATA_WRITE_ENABLED=True,
                   ROOT_URLCONF=__name__,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class MetadataPreservationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('metadata-admin', is_staff=True)
        self.server = Server.objects.create(name='原服务器', notes='原备注', deployment_fence=9)
        self.ingress = Ingress.objects.create(server=self.server, name='原入口', protocol='reality',
                                              endpoint_host='192.0.2.1', endpoint_port=443, config_ref='private-reference')
        self.core = CoreInstance.objects.create(server=self.server, instance_key='main', name='原核心',
            core_type='xray', configuration_owner='operator', configuration_version='v1', notes='核心备注')
        self.core.ingresses.add(self.ingress)
        self.egress = Egress.objects.create(server=self.server, name='原出口', kind='upstream',
                                             core_instance=self.core, fail_closed=True)
        self.line = Line.objects.create(name='原线路', notes='线路备注', ingress=self.ingress, egress=self.egress)
        self.entitlement = Entitlement.objects.create(user=self.admin, quota_bytes=1000 * 1000 ** 3)
        self.entitlement.lines.add(self.line)
        self.now = timezone.now() - timedelta(seconds=1)
        BillingCycle.objects.create(entitlement=self.entitlement, starts_at=self.now - timedelta(days=1),
            ends_at=self.now + timedelta(days=29), used_bytes=98253546067)
        subscription = DeviceSubscription.objects.create(entitlement=self.entitlement, client='windows', name='原订阅')
        NodeIdentity.objects.create(subscription=subscription, line=self.line, ingress=self.ingress,
                                    generation=1, state='active')
        self.targets = [('server', self.server), ('line', self.line), ('core', self.core),
                        ('ingress', self.ingress), ('egress', self.egress)]
        self.client.force_login(self.admin)

    def identifier(self, kind, item):
        return str(item.pk if kind in ('ingress', 'egress') else item.public_id)

    def path(self, kind, item=None, identifier=None):
        collection = {'server': 'servers', 'line': 'lines', 'core': 'cores',
                      'ingress': 'ingresses', 'egress': 'egresses'}[kind]
        return f'/api/v1/admin/{collection}/{identifier or self.identifier(kind, item)}'

    def patch(self, kind, item, payload):
        return self.client.patch(self.path(kind, item), json.dumps(payload), content_type='application/json')

    def draft(self, **changes):
        return {'name': '人工名称', 'revision': 1, 'idempotency_key': 'metadata-edit-001', **changes}

    def protected_snapshot(self):
        return {model.__name__: list(model.objects.order_by('pk').values()) for model in (
            Entitlement, BillingCycle, DeviceSubscription, NodeIdentity,
            Entitlement.lines.through, CoreInstance.ingresses.through, Line.additional_ingresses.through)}

    def control_job(self, status):
        self.core.refresh_from_db()
        serial = CoreControlJob.objects.count() + 1
        candidate = CoreConfigCandidate.objects.create(core=self.core, base_revision=self.core.revision,
            patch={'log': {'level': 'warning'}}, base_sha256='a' * 64, candidate_sha256='b' * 64,
            binding_fingerprint='c' * 64, artifact_ref='d' * 32, creator=self.admin,
            idempotency_key=f'metadata-candidate-{serial}', request_sha256='e' * 64)
        CoreControlState.objects.update_or_create(core=self.core, defaults={'fence': serial})
        return CoreControlJob.objects.create(core=self.core, candidate=candidate, actor=self.admin,
            action='check', status=status, expected_revision=self.core.revision, fence=serial,
            idempotency_key=f'metadata-control-{serial}', request_sha256='f' * 64,
            expires_at=timezone.now() + timedelta(minutes=10))

    def test_active_control_states_refuse_metadata_without_mutating_job_or_revision(self):
        job = self.control_job('queued')
        before_core = CoreInstance.objects.values().get(pk=self.core.pk)
        before_protected = self.protected_snapshot()
        payload = self.draft(notes='待保存输入')
        original_payload = dict(payload)
        for status in ('queued', 'running', 'unknown'):
            CoreControlJob.objects.filter(pk=job.pk).update(status=status)
            before_job = CoreControlJob.objects.values().get(pk=job.pk)
            response = self.patch('core', self.core, payload)
            self.assertEqual(response.status_code, 409, response.content)
            self.assertEqual(response.json()['error']['code'], 'core_busy')
            self.assertIn('保留输入', response.json()['error']['message'])
            self.assertEqual(CoreInstance.objects.values().get(pk=self.core.pk), before_core)
            self.assertEqual(CoreControlJob.objects.values().get(pk=job.pk), before_job)
            self.assertEqual(payload, original_payload)
        self.assertEqual(self.protected_snapshot(), before_protected)
        self.assertFalse(InventoryMutationReceipt.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_control_completion_releases_metadata_edit_for_same_request(self):
        for active_status in ('running', 'unknown'):
            for terminal_status in ('succeeded', 'failed'):
                job = self.control_job(active_status)
                payload = self.draft(revision=job.expected_revision,
                    idempotency_key=f'after-{active_status}-{terminal_status}')
                self.assertEqual(self.patch('core', self.core, payload).status_code, 409)
                result = {'status': terminal_status, 'job_id': str(job.public_id),
                    'core_id': str(self.core.public_id), 'action': job.action, 'fence': job.fence,
                    'candidate_sha256': job.candidate.candidate_sha256,
                    'binding_fingerprint': job.candidate.binding_fingerprint,
                    'config_sha256': job.candidate.candidate_sha256}
                _finish_job(job.public_id, result)
                job.refresh_from_db()
                self.assertEqual(job.status, terminal_status)
                self.assertNotEqual(job.error_code, 'revision_changed_during_execution')
                saved = self.patch('core', self.core, payload)
                self.assertEqual(saved.status_code, 200, saved.content)
                self.assertEqual(saved.json()['data']['item']['revision'], job.expected_revision + 1)

    def test_busy_core_does_not_block_other_resources_or_other_core(self):
        self.control_job('unknown')
        other = CoreInstance.objects.create(server=self.server, instance_key='other', name='另一核心',
            core_type='sing_box', configuration_owner='operator')
        for kind, item in [target for target in self.targets if target[0] != 'core'] + [('core', other)]:
            response = self.patch(kind, item, self.draft(idempotency_key=f'other-{kind}-001'))
            self.assertEqual(response.status_code, 200, response.content)
        self.core.refresh_from_db()
        self.assertEqual((self.core.name, self.core.revision), ('原核心', 1))

    def test_successful_metadata_receipt_can_be_replayed_while_core_is_busy(self):
        payload = self.draft(notes='已保存备注')
        first = self.patch('core', self.core, payload).json()['data']
        job = self.control_job('running')
        before_job = CoreControlJob.objects.values().get(pk=job.pk)
        before_core = CoreInstance.objects.values().get(pk=self.core.pk)
        replay = self.patch('core', self.core, payload)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertTrue(replay.json()['data']['replayed'])
        self.assertEqual(replay.json()['data']['item'], first['item'])
        self.assertEqual(CoreInstance.objects.values().get(pk=self.core.pk), before_core)
        self.assertEqual(CoreControlJob.objects.values().get(pk=job.pk), before_job)
        self.assertEqual(InventoryMutationReceipt.objects.count(), 1)

    def test_import_after_manual_edit_preserves_name_notes_and_ledger(self):
        before = self.protected_snapshot()
        for kind, item in self.targets:
            changes = {} if kind in ('ingress', 'egress') else {'notes': '人工备注'}
            edit_metadata(self.admin, kind, self.identifier(kind, item),
                          self.draft(**changes, idempotency_key=f'manual-{kind}-001'))
            item.refresh_from_db()
            payload = {'schema_version': SCHEMA_VERSION, 'source': f'metadata-{kind}', 'revision': 1,
                'observed_at': self.now.isoformat(), 'expires_at': (self.now + timedelta(hours=1)).isoformat(),
                'resources': [{'kind': kind, 'source_id': 'existing', 'target_pk': item.pk,
                    'expected_revision': item.revision, 'data': {'name': '盘点来源旧名称'}}]}
            preview = preview_resources(payload)
            self.assertTrue(preview['can_apply'])
            self.assertIn('name', preview['rows'][0]['preserved_fields'])
            apply_resources(self.admin, payload, expected_preview_sha256=preview['preview_sha256'])
            item.refresh_from_db()
            self.assertEqual(item.name, '人工名称')
            if kind not in ('ingress', 'egress'):
                self.assertEqual(item.notes, '人工备注')
        self.assertEqual(self.protected_snapshot(), before)

    def test_audit_failure_rolls_back_metadata_and_receipt(self):
        for kind, item in self.targets:
            before = type(item).objects.values().get(pk=item.pk)
            with patch('portal.inventory_service.audit', side_effect=IntegrityError('isolated failure')):
                with self.assertRaises(InventoryError) as failure:
                    edit_metadata(self.admin, kind, self.identifier(kind, item), self.draft())
            self.assertEqual(failure.exception.code, 'write_conflict')
            self.assertEqual(type(item).objects.values().get(pk=item.pk), before)
        self.assertFalse(InventoryMutationReceipt.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_revoked_actor_cannot_replay_existing_receipt(self):
        for kind, item in self.targets:
            edit_metadata(self.admin, kind, self.identifier(kind, item), self.draft(idempotency_key=f'{kind}-edit-001'))
        User.objects.filter(pk=self.admin.pk).update(is_staff=False)
        for kind, item in self.targets:
            with self.assertRaises(InventoryError) as failure:
                edit_metadata(self.admin, kind, self.identifier(kind, item), self.draft(idempotency_key=f'{kind}-edit-001'))
            self.assertEqual((failure.exception.code, failure.exception.status), ('permission_denied', 403))
        self.assertEqual(InventoryMutationReceipt.objects.count(), 5)

    def test_inactive_actor_cannot_write_or_replay(self):
        payload = self.draft()
        edit_metadata(self.admin, 'core', self.core.public_id, payload)
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        for request in (payload, self.draft(revision=2, idempotency_key='metadata-edit-002')):
            with self.assertRaises(InventoryError) as failure:
                edit_metadata(self.admin, 'core', self.core.public_id, request)
            self.assertEqual(failure.exception.status, 403)
        self.assertEqual(self.client.get(self.path('core', self.core)).status_code, 401)
        self.assertEqual(InventoryMutationReceipt.objects.count(), 1)

    def test_all_resources_change_only_metadata_and_keep_runtime_fingerprint(self):
        before_protected = self.protected_snapshot()
        for kind, item in self.targets:
            with self.subTest(kind=kind):
                before = type(item).objects.values().get(pk=item.pk)
                fingerprint = target_fingerprint(kind, item)
                payload = self.draft(idempotency_key=f'{kind}-metadata-001')
                if kind not in ('ingress', 'egress'):
                    payload['notes'] = '维护备注'
                response = self.patch(kind, item, payload)
                self.assertEqual(response.status_code, 200, response.content)
                result = response.json()['data']
                self.assertEqual(result['item']['id'], self.identifier(kind, item))
                self.assertFalse(result['replayed'])
                item.refresh_from_db()
                self.assertEqual((item.name, item.revision), ('人工名称', 2))
                after = type(item).objects.values().get(pk=item.pk)
                for field in set(before) - {'name', 'notes', 'revision'}:
                    self.assertEqual(after[field], before[field], field)
                self.assertEqual(target_fingerprint(kind, item), fingerprint)
        self.assertEqual(self.protected_snapshot(), before_protected)

    def test_minimal_get_returns_raw_metadata_without_runtime_fields(self):
        for kind, item in self.targets[2:]:
            item.name = 'https://名称示例'
            item.save(update_fields=['name'])
            response = self.client.get(self.path(kind, item))
            self.assertEqual(response.status_code, 200, response.content)
            result = response.json()['data']
            expected_fields = {'id', 'name', 'revision'} | ({'notes'} if kind == 'core' else set())
            self.assertEqual(set(result['item']), expected_fields)
            self.assertEqual(result['item']['name'], item.name)
            self.assertFalse(result['read_only'])
            self.assertTrue(result['capabilities']['edit'])
        self.assertFalse(InventoryMutationReceipt.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_replay_and_conflict_are_scoped_to_kind_target_and_exact_payload(self):
        for kind, item in self.targets:
            with self.subTest(kind=kind):
                payload = self.draft(idempotency_key=f'{kind}-metadata-001')
                first = self.patch(kind, item, payload).json()['data']
                second = self.patch(kind, item, {**payload, 'revision': 2, 'name': '更新名称',
                    'idempotency_key': f'{kind}-metadata-002'})
                self.assertEqual(second.status_code, 200)
                replay = self.patch(kind, item, payload).json()['data']
                self.assertTrue(replay['replayed'])
                self.assertEqual(replay['item'], first['item'])
                for changed in ({**payload, 'name': '同键异内容'},
                                {**payload, 'idempotency_key': f'{kind}-metadata-003'}):
                    response = self.patch(kind, item, changed)
                    self.assertEqual(response.status_code, 409, response.content)
                item.refresh_from_db()
                self.assertEqual((item.name, item.revision), ('更新名称', 3))
        self.assertEqual(InventoryMutationReceipt.objects.count(), 10)
        response = self.patch('egress', self.egress, self.draft(idempotency_key='ingress-metadata-001'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'idempotency_conflict')

    def test_new_endpoints_require_active_admin_csrf_and_allow_only_get_patch(self):
        member = User.objects.create_user('metadata-member')
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        for kind, item in self.targets[2:]:
            route = self.path(kind, item)
            self.client.logout()
            self.assertEqual(self.client.get(route).status_code, 401)
            self.assertEqual(self.patch(kind, item, self.draft()).status_code, 401)
            self.client.force_login(member)
            self.assertEqual(self.client.get(route).status_code, 403)
            self.assertEqual(self.patch(kind, item, self.draft()).status_code, 403)
            self.client.force_login(self.admin)
            self.assertEqual(self.client.post(route).status_code, 405)
            self.assertEqual(csrf_client.patch(route, json.dumps(self.draft()),
                                              content_type='application/json').status_code, 403)
        self.assertFalse(InventoryMutationReceipt.objects.exists())

    def test_production_gate_disables_new_writes_and_replays_but_keeps_read(self):
        for kind, item in self.targets[2:]:
            payload = self.draft(idempotency_key=f'{kind}-metadata-001')
            self.assertEqual(self.patch(kind, item, payload).status_code, 200)
            with override_settings(PANEL_LIVE=True, INVENTORY_METADATA_WRITE_ENABLED=False):
                response = self.patch(kind, item, payload)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()['error']['code'], 'metadata_read_only')
                result = self.client.get(self.path(kind, item)).json()['data']
                self.assertTrue(result['read_only'])
                self.assertFalse(result['capabilities']['edit'])

    def test_invalid_metadata_and_runtime_fields_do_not_change_anything(self):
        forbidden = {'endpoint_host': '192.0.2.2', 'endpoint_port': 80, 'server': self.server.pk,
            'configuration_owner': 'panel', 'instance_key': 'changed', 'core_type': 'other',
            'config_ref': 'new', 'enabled': False, 'ownership': 'external', 'next_egress': None,
            'core_instance': None, 'quota_bytes': 1, 'revision': True, 'idempotency_key': 'short'}
        for kind, item in self.targets:
            with self.subTest(kind=kind):
                before = type(item).objects.values().get(pk=item.pk)
                changes = [{key: value} for key, value in forbidden.items()]
                changes.extend({'name': value} for value in ('', '   ', 'x' * 101, '名称\n', None, 1, []))
                changes.extend([{'notes': 'x' * 1001}, {'notes': '\x00'}])
                if kind in ('ingress', 'egress'):
                    changes.append({'notes': ''})
                for change in changes:
                    response = self.patch(kind, item, self.draft(**change))
                    self.assertEqual(response.status_code, 422, (kind, change, response.content))
                self.assertEqual(type(item).objects.values().get(pk=item.pk), before)
        self.assertFalse(InventoryMutationReceipt.objects.exists())
        self.assertFalse(AuditEvent.objects.exists())

    def test_invalid_missing_identifiers_and_json_fail_without_writes(self):
        invalid = ('0', '-1', '01', '1.0', 'ingress:1', 'true', '9223372036854775808', '9' * 100)
        for kind in ('core', 'ingress', 'egress'):
            for identifier in invalid + (str(uuid.uuid4()) if kind == 'core' else '9223372036854775807',):
                route = self.path(kind, identifier=identifier)
                self.assertEqual(self.client.get(route).status_code, 404, route)
                self.assertEqual(self.client.patch(route, json.dumps(self.draft()),
                                                   content_type='application/json').status_code, 404)
        for kind, item in self.targets[2:]:
            route = self.path(kind, item)
            for raw in ('[]', '{"name":"一","name":"二"}', '{"revision":NaN}', '{', ' ' * 8193):
                self.assertEqual(self.client.patch(route, raw, content_type='application/json').status_code, 422)
            self.assertEqual(self.client.patch(route, '{}', content_type='text/plain').status_code, 415)
        self.assertFalse(InventoryMutationReceipt.objects.exists())

    def test_notes_can_be_cleared_and_maximum_lengths_are_accepted(self):
        for kind, item in self.targets:
            payload = self.draft(name='名' * 100, idempotency_key=f'{kind}-metadata-001')
            if kind not in ('ingress', 'egress'):
                payload['notes'] = '注' * 1000
            self.assertEqual(self.patch(kind, item, payload).status_code, 200)
            if kind not in ('ingress', 'egress'):
                cleared = self.patch(kind, item, {'notes': '', 'revision': 2,
                                                  'idempotency_key': f'{kind}-metadata-002'})
                self.assertEqual(cleared.status_code, 200, cleared.content)
                self.assertEqual(cleared.json()['data']['item']['notes'], '')
