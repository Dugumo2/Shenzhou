"""资料保存、受信观测及目标时效的产品场景；不连接现场。"""
import copy
import json
import math
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .inventory_service import InventoryError, edit_metadata
from .models import CheckResult, CoreInstance, Egress, Ingress, InventoryMutationReceipt, Line, Server, ServerObservation
from .observation_ingest import (METRICS, ObservationError, freshness, ingest_check, ingest_observation,
                                 target_fingerprint, target_key)


@override_settings(ROOT_URLCONF='portal.test_inventory_api', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
                   INVENTORY_METADATA_WRITE_ENABLED=True)
class InventoryManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('inventory-writer', is_staff=True)
        self.user = User.objects.create_user('inventory-reader')
        self.server = Server.objects.create(name='原机器', adapter='xray', deployment_fence=7)
        self.ingress = Ingress.objects.create(server=self.server, name='入口', protocol='ws', config_ref='private-reference')
        self.egress = Egress.objects.create(server=self.server, name='出口', kind='direct')
        self.line = Line.objects.create(name='原线路', ingress=self.ingress, egress=self.egress)
        self.core = CoreInstance.objects.create(server=self.server, name='登记核心', core_type='xray', instance_key='main',
                                               configuration_owner='operator', configuration_version='release-1')
        self.core.ingresses.add(self.ingress)
        self.client.force_login(self.admin)
        self.now = timezone.now() - timedelta(seconds=1)

    def path(self, kind='servers', target=None):
        return '/api/v1/admin/' + kind + '/' + str((target or self.server).public_id)

    def patch(self, payload, **kwargs):
        return self.client.patch(self.path(**kwargs), json.dumps(payload), content_type='application/json')

    def draft(self, **changes):
        return {'name': '新别名', 'notes': '记录用途', 'revision': 1, 'idempotency_key': 'edit-request-001', **changes}

    def policy(self, kind='server', target=None):
        target = target or self.server
        return {'approved-local': {'targets': [target_key(kind, target)], 'check_kinds': ['service_status', 'core_version', 'whole_path', 'tcp', 'udp', 'tls', 'dns', 'egress'],
                                  'metrics': list(METRICS), 'max_ttl_seconds': 600}}

    def observation(self, **changes):
        return {'source': 'approved-local', 'event_id': 'event-1', 'target_kind': 'server', 'target_id': str(self.server.public_id),
                'target_fingerprint': target_fingerprint('server', self.server), 'observed_at': self.now.isoformat(),
                'expires_at': (self.now + timedelta(minutes=5)).isoformat(),
                'metrics': {'online': True, 'cpu_percent': 0, 'cpu_window_seconds': 1,
                            'network_rx_bytes': 100, 'network_tx_bytes': 200, 'network_interfaces': ['eth0']}, **changes}

    def check(self, kind='line', target=None, **changes):
        target = target or self.line
        return {**{k: v for k, v in self.observation().items() if k != 'metrics'},
                'target_kind': kind, 'target_id': target_key(kind, target).split(':', 1)[1],
                'target_fingerprint': target_fingerprint(kind, target), 'check_kind': 'whole_path' if kind == 'line' else 'tcp',
                'result': 'pass', 'latency_ms': 12.5, 'error_stage': '', 'error_code': '', **changes}

    def test_metadata_changes_only_name_notes_and_revision(self):
        before = Server.objects.values().get(pk=self.server.pk)
        response = self.patch(self.draft())
        self.assertEqual(response.status_code, 200, response.content)
        self.server.refresh_from_db()
        self.assertEqual((self.server.name, self.server.notes, self.server.revision), ('新别名', '记录用途', 2))
        after = Server.objects.values().get(pk=self.server.pk)
        for key in set(before) - {'name', 'notes', 'revision'}:
            self.assertEqual(before[key], after[key])
        self.assertEqual(InventoryMutationReceipt.objects.count(), 1)
        response = self.patch(self.draft(idempotency_key='line-edit-001'), kind='lines', target=self.line)
        self.assertEqual(response.status_code, 200)
        self.line.refresh_from_db()
        self.assertEqual((self.line.ingress_id, self.line.egress_id, self.line.enabled), (self.ingress.pk, self.egress.pk, True))

    def test_replay_and_competing_revision_do_not_duplicate_or_overwrite(self):
        first = self.patch(self.draft())
        retry = self.patch(self.draft())
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.json()['data']['replayed'])
        self.assertEqual(first.json()['data']['item'], retry.json()['data']['item'])
        self.assertEqual(self.patch(self.draft(name='其他内容')).status_code, 409)
        self.assertEqual(self.patch(self.draft(idempotency_key='other-request-001')).status_code, 409)
        self.server.refresh_from_db()
        self.assertEqual(self.server.revision, 2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            InventoryMutationReceipt.objects.create(actor=self.admin, key='edit-request-001', fingerprint='a'*64)

    def test_revision_retry_is_historical_receipt_not_current_state(self):
        self.patch(self.draft())
        self.patch(self.draft(name='第三版', revision=2, idempotency_key='edit-request-002'))
        retry = self.patch(self.draft()).json()['data']
        self.assertEqual(retry['item']['revision'], 2)
        self.assertTrue(retry['replayed'])
        self.assertEqual(self.client.get(self.path()).json()['data']['server']['revision'], 3)

    def test_permission_csrf_and_independent_live_gate(self):
        self.client.force_login(self.user)
        self.assertEqual(self.patch(self.draft()).status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.patch(self.path(), json.dumps(self.draft()), content_type='application/json').status_code, 403)
        with override_settings(PANEL_LIVE=True, INVENTORY_METADATA_WRITE_ENABLED=True):
            result = edit_metadata(self.admin, 'server', self.server.public_id, self.draft())
            self.assertEqual(result['item']['revision'], 2)
        with override_settings(INVENTORY_METADATA_WRITE_ENABLED=False):
            self.client.force_login(self.admin)
            self.assertEqual(self.patch(self.draft()).status_code, 403)
            self.assertFalse(self.client.get(self.path()).json()['data']['capabilities']['edit'])

    def test_stale_actor_and_forbidden_fields_refuse_before_write(self):
        for change in ({'enabled': False}, {'adapter': 'something'}, {'config_ref': 'x'}, {'revision': True}, {'name': ''}):
            self.assertEqual(self.patch(self.draft(**change)).status_code, 422)
        self.admin.is_staff = False
        self.admin.save(update_fields=['is_staff'])
        with self.assertRaises(InventoryError):
            edit_metadata(self.admin, 'server', self.server.public_id, self.draft())
        self.assertFalse(InventoryMutationReceipt.objects.exists())

    def test_duplicate_json_fields_and_wrong_body_are_rejected(self):
        for raw in ('[]', '{"name":"a","name":"b"}', '{"revision":NaN}'):
            self.assertEqual(self.client.patch(self.path(), raw, content_type='application/json').status_code, 422)

    def test_trusted_sample_keeps_null_and_cumulative_scope(self):
        row, created = ingest_observation(self.observation(), trusted_sources=self.policy())
        self.assertTrue(created)
        self.assertIsNone(row.metrics['memory_percent'])
        detail = self.client.get(self.path()).json()['data']
        self.assertEqual(detail['server']['monitoring']['state'], 'fresh')
        self.assertTrue(detail['server']['monitoring']['online'])
        self.assertEqual(detail['observation']['metrics']['cpu_percent'], 0)
        self.assertIsNone(detail['metrics']['total_transfer_bytes'])
        self.assertIn('不是月用量', detail['observation']['scope'])
        self.assertEqual(detail['cores']['items'][0]['configuration_owner'], 'operator')
        self.assertIsNone(detail['cores']['items'][0]['registered_version'])

    def test_sample_replay_immutable_and_older_arrival_not_latest(self):
        row, _ = ingest_observation(self.observation(), trusted_sources=self.policy())
        replay, created = ingest_observation(self.observation(), trusted_sources=self.policy())
        self.assertEqual(row.pk, replay.pk); self.assertFalse(created)
        with self.assertRaises(ObservationError):
            ingest_observation(self.observation(metrics={'online': False}), trusted_sources=self.policy())
        ingest_observation(self.observation(event_id='old', observed_at=(self.now-timedelta(hours=1)).isoformat(),
                           expires_at=(self.now-timedelta(minutes=59)).isoformat()), trusted_sources=self.policy())
        self.assertEqual(self.client.get(self.path()).json()['data']['observation']['observed_at'], self.now.isoformat())

    def test_stale_latest_does_not_claim_online(self):
        ingest_observation(self.observation(observed_at=(self.now-timedelta(minutes=10)).isoformat(),
                           expires_at=(self.now-timedelta(seconds=1)).isoformat()), trusted_sources=self.policy())
        body = self.client.get(self.path()).json()['data']
        self.assertEqual(body['server']['monitoring']['state'], 'stale')
        self.assertIsNone(body['server']['monitoring']['online'])
        self.assertTrue(body['observation']['metrics']['online'])

    def test_untrusted_source_wrong_target_unknown_fields_and_times_rejected(self):
        cases = [dict(source='unknown'), dict(target_id='bad'), dict(target_kind=[]), dict(target_fingerprint='0'*64),
                 dict(observed_at=(timezone.now()+timedelta(days=1)).isoformat()),
                 dict(expires_at=(self.now+timedelta(days=2)).isoformat()), dict(expires_at=self.now.isoformat()),
                 dict(observed_at='2026-10-01T00:00:00'), dict(command='uname')]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ObservationError):
                ingest_observation(self.observation(**change), trusted_sources=self.policy())
        with self.assertRaises(ObservationError):
            policy = self.policy(); policy['approved-local']['targets'] = []
            ingest_observation(self.observation(), trusted_sources=policy)
        self.assertFalse(ServerObservation.objects.exists())

    def test_metrics_bounds_cpu_window_and_interface_scope(self):
        invalid = [{'cpu_percent': 50}, {'cpu_percent': 50, 'cpu_window_seconds': 0}, {'cpu_percent': math.nan},
                   {'memory_percent': True}, {'disk_percent': 101}, {'network_rx_bytes': 5},
                   {'network_rx_bytes': -1, 'network_interfaces': ['eth0']}, {'network_interfaces': [{}]},
                   {'online': 'yes'}, {'unknown_metric': 1}, {'network_rx_bytes': 10**500}]
        for metrics in invalid:
            with self.subTest(metrics=str(metrics)[:80]), self.assertRaises(ObservationError):
                ingest_observation(self.observation(metrics=metrics), trusted_sources=self.policy())
        self.assertFalse(ServerObservation.objects.exists())

    def test_alias_does_not_expire_sample_or_check_but_configuration_does(self):
        observation, _ = ingest_observation(self.observation(), trusted_sources=self.policy())
        check, _ = ingest_check(self.check(), trusted_sources=self.policy('line', self.line))
        self.patch(self.draft())
        self.server.refresh_from_db()
        self.assertEqual(freshness(observation, 'server', self.server), 'fresh')
        self.line.refresh_from_db()
        self.assertEqual(freshness(check, 'line', self.line), 'fresh')
        self.core.configuration_version = 'release-2'; self.core.save(update_fields=['configuration_version'])
        self.assertEqual(freshness(check, 'line', self.line), 'target_changed')

    def test_endpoint_and_line_association_change_invalidate(self):
        check, _ = ingest_check(self.check(), trusted_sources=self.policy('line', self.line))
        extra = Ingress.objects.create(server=self.server, name='新增入口', protocol='hy2')
        self.line.additional_ingresses.add(extra)
        self.assertEqual(freshness(check, 'line', self.line), 'target_changed')
        self.line.additional_ingresses.remove(extra)
        self.ingress.config_ref = 'new-reference'; self.ingress.save(update_fields=['config_ref'])
        self.line.refresh_from_db()
        self.assertEqual(freshness(check, 'line', self.line), 'target_changed')

    def test_endpoint_success_never_promotes_entire_line(self):
        row, _ = ingest_check(self.check('ingress', self.ingress), trusted_sources=self.policy('ingress', self.ingress))
        response = self.client.get(self.path('lines', self.line)).json()['data']
        self.assertEqual(response['line']['verification']['state'], 'not_tested')
        self.assertEqual(response['checks']['items'][0]['target_kind'], 'ingress')
        self.assertEqual(response['checks']['items'][0]['result'], 'pass')
        with self.assertRaises(ObservationError):
            ingest_check(self.check('ingress', self.ingress, check_kind='whole_path'), trusted_sources=self.policy('ingress', self.ingress))

    def test_failed_and_timeout_checks_show_exact_scope_not_online(self):
        for index, result in enumerate(('fail', 'timeout')):
            ingest_check(self.check(event_id=f'line-event-{index}', result=result, error_stage='connect',
                                   error_code='timeout', latency_ms=None), trusted_sources=self.policy('line', self.line))
        detail = self.client.get(self.path('lines', self.line)).json()['data']
        self.assertEqual(detail['line']['verification']['result'], 'timeout')
        self.assertEqual(len(detail['checks']['items']), 2)
        self.assertEqual(self.client.get(self.path()).json()['data']['server']['monitoring']['state'], 'not_connected')

    def test_foreign_checks_not_exposed_and_core_mismatch_rejected(self):
        other = Server.objects.create(name='别的服务器')
        endpoint = Ingress.objects.create(server=other, name='其他入口', protocol='ws')
        ingest_check(self.check('ingress', endpoint), trusted_sources=self.policy('ingress', endpoint))
        self.assertEqual(self.client.get(self.path()).json()['data']['checks']['items'], [])
        self.assertEqual(self.client.get(self.path('lines', self.line)).json()['data']['checks']['items'], [])
        self.core.ingresses.add(endpoint)
        with self.assertRaises(ObservationError):
            ingest_check(self.check('core', self.core, check_kind='core_version'), trusted_sources=self.policy('core', self.core))

    def test_arbitrary_error_text_is_not_saved(self):
        for change in ({'error_code': 'https://secret'}, {'error_stage': 'token'}, {'latency_ms': float('inf')}, {'result': 'maybe'},
                       {'error_code': 'timeout', 'result': 'pass'}):
            with self.assertRaises(ObservationError):
                ingest_check(self.check(**change), trusted_sources=self.policy('line', self.line))
        self.assertFalse(CheckResult.objects.exists())

    def test_no_http_observation_or_check_write_entry(self):
        for body in ({'metrics': {'online': True}}, {'result': 'pass'}, {'notes': 'test', 'probe': 'https://example.invalid'}):
            self.assertEqual(self.patch({**self.draft(), **body}).status_code, 422)
        self.assertFalse(ServerObservation.objects.exists()); self.assertFalse(CheckResult.objects.exists())
