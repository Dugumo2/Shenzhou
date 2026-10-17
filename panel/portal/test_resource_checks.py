"""固定检查的隔离验证；仅使用内存桩或本机临时TCP监听。"""
import socket
import subprocess
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from . import resource_checks as checks
from .inventory_service import InventoryError
from .models import CheckResult, CoreInstance, Egress, Ingress, ResourceCheckJob, Server
from .observation_ingest import target_fingerprint


class ResourceCheckTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('checks-admin', is_staff=True)
        self.user = User.objects.create_user('checks-user')
        self.server = Server.objects.create(name='隔离机器')
        self.core = CoreInstance.objects.create(server=self.server, instance_key='isolated', name='隔离核心',
            core_type='other', configuration_owner='local-test')
        self.ingress = Ingress.objects.create(server=self.server, name='隔离入口', protocol='ws')
        self.entry = {'target_kind': 'ingress', 'target_id': str(self.ingress.pk),
            'target_version': target_fingerprint('ingress', self.ingress), 'action': 'tcp',
            'ip': '127.0.0.1', 'port': 54321, 'allow_loopback': True}
        self.payload = {key: self.entry[key] for key in ('target_kind', 'target_id', 'target_version', 'action')}
        self.payload['idempotency_key'] = 'isolated-check-001'
        self.policy = patch.object(checks, 'load_policy', return_value=[self.entry])
        self.policy.start()
        self.addCleanup(self.policy.stop)
        # 非root约束单独验证；CI执行环境不影响隔离执行器测试。
        self.uid = patch.object(checks.os, 'geteuid', return_value=1000, create=True)
        self.uid.start()
        self.addCleanup(self.uid.stop)

    def enqueue(self, **changes):
        return checks.enqueue_check(self.admin, {**self.payload, **changes})

    def test_read_catalog_and_job_never_run_probe(self):
        with patch.object(checks, 'execute_fixed') as execute:
            actions = checks.actions_for_targets([('ingress', self.ingress)])
            self.assertEqual(len(actions), 1)
            self.assertNotIn('ip', actions[0])
            result = self.enqueue()
            self.assertEqual(checks.get_job(self.admin, result['id'])['state'], 'queued')
            execute.assert_not_called()

    def test_permissions_and_unknown_payload_are_denied(self):
        for actor in (self.user, None):
            with self.assertRaises(InventoryError) as caught:
                checks.enqueue_check(actor, self.payload)
            self.assertEqual(caught.exception.status, 403)
        for extra in ('host', 'url', 'shell', 'script', 'port', 'unit'):
            with self.assertRaises(InventoryError):
                self.enqueue(**{extra: 'do-not-execute'})
        self.assertFalse(ResourceCheckJob.objects.exists())

    def test_same_request_replays_conflict_does_not_change_job(self):
        first, replay = self.enqueue(), self.enqueue()
        self.assertEqual(first['id'], replay['id'])
        self.assertTrue(replay['replayed'])
        with self.assertRaises(InventoryError) as caught:
            self.enqueue(action='service_status')
        self.assertEqual(caught.exception.code, 'idempotency_conflict')
        self.assertEqual(ResourceCheckJob.objects.count(), 1)

    def test_different_requests_same_target_are_busy_then_rate_limited(self):
        self.enqueue()
        with self.assertRaises(InventoryError) as caught:
            self.enqueue(idempotency_key='isolated-check-002')
        self.assertEqual(caught.exception.code, 'check_in_progress')
        ResourceCheckJob.objects.update(state='completed')
        with self.assertRaises(InventoryError) as caught:
            self.enqueue(idempotency_key='isolated-check-003')
        self.assertEqual(caught.exception.status, 429)

    def test_database_enforces_single_active_target(self):
        self.enqueue()
        values = ResourceCheckJob.objects.values().first()
        for key in ('id', 'public_id'):
            values.pop(key)
        values['idempotency_key'] = 'isolated-race-key'
        with self.assertRaises(IntegrityError), transaction.atomic():
            ResourceCheckJob.objects.create(**values)

    def test_stale_and_unapproved_targets_do_not_enqueue(self):
        with self.assertRaises(InventoryError) as caught:
            self.enqueue(target_version='0' * 64)
        self.assertEqual(caught.exception.code, 'target_version_changed')
        with patch.object(checks, 'load_policy', return_value=[]), self.assertRaises(InventoryError):
            self.enqueue()
        self.assertFalse(ResourceCheckJob.objects.exists())

    def test_expired_lease_releases_target_without_rerunning_old_task(self):
        first = self.enqueue()
        ResourceCheckJob.objects.update(lease_until=timezone.now()-timedelta(seconds=1),
            created_at=timezone.now()-timedelta(seconds=180))
        second = self.enqueue(idempotency_key='isolated-check-new')
        old = checks.get_job(self.admin, first['id'])
        self.assertEqual(old['failure_code'], 'worker_deadline_exceeded')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(old['not_tested'], checks.NOT_TESTED)

    def test_revoked_role_and_changed_target_abort_before_execution(self):
        self.enqueue()
        User.objects.filter(pk=self.admin.pk).update(is_staff=False)
        with patch.object(checks, 'execute_fixed') as execute:
            result = checks.run_next()
            self.assertEqual(result['failure_code'], 'permission_denied')
            execute.assert_not_called()
        self.assertFalse(CheckResult.objects.exists())

    def test_changed_target_before_execution_is_rejected(self):
        self.enqueue()
        Ingress.objects.filter(pk=self.ingress.pk).update(enabled=False)
        with patch.object(checks, 'execute_fixed') as execute:
            self.assertEqual(checks.run_next()['failure_code'], 'target_version_changed')
            execute.assert_not_called()

    def test_changed_target_during_execution_cannot_save_pass(self):
        self.enqueue()
        def change(_):
            Ingress.objects.filter(pk=self.ingress.pk).update(enabled=False)
            return {'result': 'pass', 'latency_ms': 1, 'error_stage': '', 'error_code': ''}
        with patch.object(checks, 'execute_fixed', side_effect=change):
            result = checks.run_next()
        self.assertEqual(result['failure_code'], 'target_version_changed')
        self.assertIsNone(result['result'])
        self.assertFalse(CheckResult.objects.exists())

    def test_revoked_role_during_execution_cannot_save_pass(self):
        self.enqueue()
        def revoke(_):
            User.objects.filter(pk=self.admin.pk).update(is_active=False)
            return {'result': 'pass', 'latency_ms': 1, 'error_stage': '', 'error_code': ''}
        with patch.object(checks, 'execute_fixed', side_effect=revoke):
            self.assertEqual(checks.run_next()['failure_code'], 'permission_denied')
        self.assertFalse(CheckResult.objects.exists())

    def test_tcp_connection_to_local_isolated_listener_saves_only_transport(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            self.entry['port'] = listener.getsockname()[1]
            first = self.enqueue()
            result = checks.run_next()
        self.assertEqual(result['result']['result'], 'pass')
        self.assertEqual(result['result']['check_kind'], 'tcp')
        self.assertIsNone(CheckResult.objects.get().line_id)
        self.assertIn('authentication', result['not_tested'])
        self.assertEqual(self.enqueue()['id'], first['id'])
        self.assertIsNone(checks.run_next())

    def test_timeout_and_error_do_not_expose_command_output(self):
        entry = {'action': 'service_status', 'unit': 'isolated.service'}
        with patch.object(checks.subprocess, 'run', side_effect=subprocess.TimeoutExpired('PRIVATE', 5)) as run:
            result = checks.execute_fixed(entry)
        self.assertEqual(result['result'], 'timeout')
        self.assertEqual(result['error_stage'], 'collect')
        self.assertNotIn('PRIVATE', str(result))
        self.assertEqual(run.call_args.args[0], ['/usr/bin/systemctl', 'is-active', '--quiet', 'isolated.service'])
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_policy_rejects_metadata_dns_private_and_shell_inputs(self):
        for change in ({'ip': '169.254.169.254'}, {'ip': '10.0.0.1'}, {'ip': 'example.com'},
                       {'ip': '127.0.0.1', 'allow_loopback': False}, {'ip': '::ffff:127.0.0.1'}, {'port': True}):
            with self.assertRaises(InventoryError):
                checks._validate_policy({'version': 1, 'actions': [{**self.entry, **change}]})
        core_entry = {'target_kind': 'core', 'target_id': str(self.core.public_id),
            'target_version': target_fingerprint('core', self.core), 'action': 'service_status', 'unit': 'safe.service; bad'}
        with self.assertRaises(InventoryError):
            checks._validate_policy({'version': 1, 'actions': [core_entry]})

    def test_policy_change_during_check_rejects_result(self):
        self.enqueue()
        def revoke(_):
            self.policy.return_value = []
            return {'result': 'pass', 'latency_ms': 1, 'error_stage': '', 'error_code': ''}
        with patch.object(checks, 'execute_fixed', side_effect=revoke):
            # 修改当前目录读取桩模拟管理员撤销动作。
            with patch.object(checks, 'load_policy', side_effect=[[self.entry], []]):
                result = checks.run_next()
        self.assertEqual(result['failure_code'], 'action_not_allowed')
        self.assertFalse(CheckResult.objects.exists())

    def test_root_worker_is_rejected_before_claiming(self):
        self.enqueue()
        with patch.object(checks.os, 'name', 'posix'), patch.object(checks.os, 'geteuid', return_value=0):
            with self.assertRaises(InventoryError) as caught:
                checks.run_next()
        self.assertEqual(caught.exception.code, 'root_worker_forbidden')
        self.assertEqual(ResourceCheckJob.objects.get().state, 'queued')

    def test_core_service_timeout_has_target_version_and_stage(self):
        entry = {'target_kind': 'core', 'target_id': str(self.core.public_id),
            'target_version': target_fingerprint('core', self.core), 'action': 'service_status', 'unit': 'isolated.service'}
        payload = {key: entry[key] for key in self.payload if key != 'idempotency_key'}
        payload['idempotency_key'] = 'core-timeout-001'
        with patch.object(checks, 'load_policy', return_value=[entry]), patch.object(checks.subprocess, 'run', side_effect=subprocess.TimeoutExpired('hidden', 5)):
            checks.enqueue_check(self.admin, payload)
            result = checks.run_next()
        self.assertEqual(result['result']['target_kind'], 'core')
        self.assertEqual(result['result']['target_version'], entry['target_version'])
        self.assertEqual(result['result']['result'], 'timeout')
        self.assertEqual(result['result']['error_stage'], 'collect')

    def test_changed_policy_address_during_execution_is_not_accepted(self):
        self.enqueue()
        with patch.object(checks, 'load_policy', side_effect=[[self.entry], [{**self.entry, 'port': 54322}]]), \
             patch.object(checks, 'execute_fixed', return_value={'result': 'pass', 'latency_ms': 1, 'error_stage': '', 'error_code': ''}):
            result = checks.run_next()
        self.assertEqual(result['failure_code'], 'action_policy_changed')
        self.assertFalse(CheckResult.objects.exists())

    def egress_action(self, target):
        entry = {**self.entry, 'target_kind': 'egress', 'target_id': str(target.pk),
                 'target_version': target_fingerprint('egress', target)}
        payload = {key: entry[key] for key in self.payload if key != 'idempotency_key'}
        payload['idempotency_key'] = 'egress-chain-check'
        return entry, payload

    def test_empty_policy_does_not_iterate_targets_or_compute_fingerprint(self):
        def unneeded_targets():
            raise AssertionError('空目录不得遍历目标')
            yield None
        with patch.object(checks, 'load_policy', return_value=[]), patch.object(checks, 'target_fingerprint') as fingerprint:
            self.assertEqual(checks.actions_for_targets(unneeded_targets()), [])
            fingerprint.assert_not_called()

    def test_cyclic_chain_hides_action_and_rejects_enqueue_with_fixed_error(self):
        first = Egress.objects.create(server=self.server, name='链首', kind='upstream')
        second = Egress.objects.create(server=self.server, name='链尾', kind='upstream', next_egress=first)
        entry, payload = self.egress_action(first)
        Egress.objects.filter(pk=first.pk).update(next_egress=second)
        first.refresh_from_db()
        with patch.object(checks, 'load_policy', return_value=[entry]):
            self.assertEqual(checks.actions_for_targets([('egress', first)]), [])
            with self.assertRaises(InventoryError) as caught:
                checks.enqueue_check(self.admin, payload)
        self.assertEqual(caught.exception.code, 'target_unavailable')
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(ResourceCheckJob.objects.exists())

    def test_too_deep_chain_hides_action_and_rejects_enqueue(self):
        first = Egress.objects.create(server=self.server, name='链首', kind='upstream')
        entry, payload = self.egress_action(first)
        previous = first
        for index in range(16):
            following = Egress.objects.create(server=self.server, name=f'第{index+2}段', kind='upstream')
            Egress.objects.filter(pk=previous.pk).update(next_egress=following)
            previous = following
        first.refresh_from_db()
        with patch.object(checks, 'load_policy', return_value=[entry]):
            self.assertEqual(checks.actions_for_targets([('egress', first)]), [])
            with self.assertRaises(InventoryError) as caught:
                checks.enqueue_check(self.admin, payload)
        self.assertEqual(caught.exception.code, 'target_unavailable')
        self.assertFalse(ResourceCheckJob.objects.exists())

    def test_chain_invalidated_while_running_cannot_save_success(self):
        first = Egress.objects.create(server=self.server, name='链首', kind='upstream')
        second = Egress.objects.create(server=self.server, name='链尾', kind='upstream', next_egress=first)
        entry, payload = self.egress_action(first)
        def invalidate(_):
            Egress.objects.filter(pk=first.pk).update(next_egress=second)
            return {'result': 'pass', 'latency_ms': 1, 'error_stage': '', 'error_code': ''}
        with patch.object(checks, 'load_policy', return_value=[entry]), patch.object(checks, 'execute_fixed', side_effect=invalidate):
            checks.enqueue_check(self.admin, payload)
            result = checks.run_next()
        self.assertEqual(result['failure_code'], 'target_unavailable')
        self.assertEqual(result['state'], 'completed')
        self.assertFalse(CheckResult.objects.exists())

    def test_idle_worker_does_not_start_write_transaction(self):
        with patch.object(checks.transaction, 'atomic', side_effect=AssertionError('空队列不得开启写事务')):
            self.assertIsNone(checks.run_next())
        self.enqueue()
        ResourceCheckJob.objects.update(state='running')
        with patch.object(checks.transaction, 'atomic', side_effect=AssertionError('未过期执行中无需写事务')):
            self.assertIsNone(checks.run_next())

    def test_expired_running_task_is_recovered_without_queued_task(self):
        self.enqueue()
        ResourceCheckJob.objects.update(state='running', lease_until=timezone.now()-timedelta(seconds=1))
        self.assertIsNone(checks.run_next())
        job = ResourceCheckJob.objects.get()
        self.assertEqual(job.state, 'completed')
        self.assertEqual(job.failure_code, 'worker_deadline_exceeded')
