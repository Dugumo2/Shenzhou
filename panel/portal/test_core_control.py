"""核心状态服务反例：独立测试库，不接真实配置、核心或账本。"""
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import core_control as control
from .core_control_models import CoreConfigCandidate, CoreControlJob, CoreControlState
from .models import CoreInstance, Server


class CoreReceiptProjectionTests(SimpleTestCase):
    def test_no_free_text_or_exception_can_escape_receipt(self):
        result = control._safe_result({'status': 'failed', 'stdout': 'private-secret',
            'error_code': 'private-secret', 'stage': 'private-secret', 'epoch': 'private-secret',
            'core_version': 'private-secret', 'job_id': 'private-secret', 'fence': True})
        self.assertEqual(result, {'status': 'failed', 'error_code': 'executor_failed'})

    def test_malformed_types_become_safe_unknown_or_error(self):
        for value in (None, [], '', {'status': 'passed'}, {'status': ['succeeded']}):
            self.assertEqual(control._safe_result(value), {'status': 'unknown', 'error_code': 'invalid_receipt'})
        self.assertEqual(control._safe_result({'status': 'failed', 'error_code': {'secret': 'never'}}),
                         {'status': 'failed', 'error_code': 'executor_failed'})

    def test_only_digest_and_version_shapes_are_projected(self):
        result = control._safe_result({'status': 'succeeded', 'config_sha256': 'a' * 64,
            'candidate_sha256': '/private', 'core_version': '1.12.0', 'fence': 3})
        self.assertEqual(result, {'status': 'succeeded', 'config_sha256': 'a' * 64, 'core_version': '1.12.0', 'fence': 3})


@override_settings(CORE_CONTROL_WRITE_ENABLED=True)
class CoreControlTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(username='core-admin', is_staff=True)
        self.user = get_user_model().objects.create_user(username='core-user')
        self.server = Server.objects.create(name='隔离服务器')
        self.core = CoreInstance.objects.create(server=self.server, name='隔离核心', instance_key='core',
            core_type='sing_box', configuration_owner='isolated-test', owner_state='verified')
        self.executor = Mock()
        self.executor.binding_status.return_value = {'available': True}
        self.executor.prepare_candidate.return_value = {
            'base_sha256': 'a' * 64, 'candidate_sha256': 'b' * 64,
            'binding_fingerprint': 'c' * 64, 'artifact_ref': 'd' * 32, 'previous_level': 'info'}
        self.executor.execute_job.side_effect = self.receipt
        self.executor.reconcile_job.side_effect = self.receipt
        self.patch_executor = patch.object(control, '_executor', return_value=self.executor)
        self.patch_executor.start()
        self.addCleanup(self.patch_executor.stop)
        self.serial = 0

    def key(self):
        self.serial += 1
        return f'isolated-request-{self.serial}'

    def receipt(self, job, status='succeeded', **extra):
        return {'status': status, 'job_id': str(job.public_id), 'core_id': str(job.core.public_id),
            'action': job.action, 'fence': job.fence, 'candidate_sha256': job.candidate.candidate_sha256,
            'binding_fingerprint': job.candidate.binding_fingerprint,
            'config_sha256': job.candidate.base_sha256 if job.action == 'rollback' else job.candidate.candidate_sha256,
            **extra}

    def candidate(self, **extra):
        body = {'patch': {'log': {'level': 'warning'}}, 'expected_revision': 1,
                'idempotency_key': self.key(), **extra}
        return control.create_candidate(self.admin, self.core.public_id, body)

    def enqueue(self, candidate, action='check', revision=None, **extra):
        self.core.refresh_from_db()
        return control.enqueue_action(self.admin, self.core.public_id, candidate['candidate']['id'], action,
            {'expected_revision': revision or self.core.revision, 'idempotency_key': self.key(), **extra})

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(control.CoreControlError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_full_check_apply_rollback_versions_and_no_repeat(self):
        candidate = self.candidate()
        self.assertEqual(candidate['candidate']['state'], 'prepared')
        checked = control.run_job(self.enqueue(candidate)['job']['id'])
        self.assertEqual(checked['status'], 'succeeded')
        row = CoreConfigCandidate.objects.get(public_id=candidate['candidate']['id'])
        self.assertEqual(row.checked_fence, checked['fence'])
        self.assertIsNotNone(row.checked_at)
        self.core.refresh_from_db()
        self.assertEqual(self.core.revision, 1)
        applied = self.enqueue(candidate, 'apply')
        control.run_job(applied['job']['id'])
        control.run_job(applied['job']['id'])
        self.core.refresh_from_db()
        self.assertEqual(self.core.revision, 2)
        self.assertEqual(self.core.configuration_version, 'b' * 64)
        control.run_job(self.enqueue(candidate, 'rollback')['job']['id'])
        self.core.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual((self.core.revision, self.core.configuration_version, row.state), (3, 'a' * 64, 'rolled_back'))
        self.assertEqual(self.executor.execute_job.call_count, 3)
        self.assert_code('rollback_unavailable', self.enqueue, candidate, 'rollback')

    def test_candidate_idempotency_replay_and_conflict(self):
        original = self.candidate(idempotency_key='same-request-key')
        self.assertTrue(self.candidate(idempotency_key='same-request-key')['replayed'])
        self.assertEqual(CoreConfigCandidate.objects.count(), 1)
        self.assertEqual(self.executor.prepare_candidate.call_count, 1)
        self.assert_code('idempotency_conflict', self.candidate, idempotency_key='same-request-key',
                         patch={'log': {'level': 'debug'}})
        self.assertEqual(original['candidate']['base_revision'], 1)

    def test_action_replay_before_busy_and_change_rejected(self):
        candidate = self.candidate()
        first = self.enqueue(candidate, idempotency_key='same-action-key')
        again = self.enqueue(candidate, idempotency_key='same-action-key')
        self.assertTrue(again['replayed'])
        self.assertEqual(first['job']['id'], again['job']['id'])
        self.assert_code('idempotency_conflict', self.enqueue, candidate, 'apply', idempotency_key='same-action-key')

    def test_patch_fields_and_revision_rejected_before_prepare(self):
        for body in ({'patch': {'outbounds': []}}, {'patch': {'log': {'level': 'info', 'secret': 'never'}}},
                     {'expected_revision': True}, {'shell': 'never'}):
            with self.assertRaises(control.CoreControlError):
                self.candidate(**body)
        self.assert_code('revision_conflict', self.candidate, expected_revision=2)
        self.executor.prepare_candidate.assert_not_called()

    def test_permission_owner_and_disabled_boundaries(self):
        self.assert_code('permission_denied', control.control_detail, self.user, self.core.public_id)
        with override_settings(CORE_CONTROL_WRITE_ENABLED=False):
            self.assert_code('control_disabled', self.candidate)
        self.core.owner_state = 'unknown'
        self.core.save()
        self.assert_code('binding_unavailable', self.candidate)
        self.assertFalse(control.control_detail(self.admin, self.core.public_id)['capabilities']['create_candidate'])

    def test_no_apply_without_check_and_stale_candidate(self):
        candidate = self.candidate()
        self.assert_code('check_required', self.enqueue, candidate, 'apply')
        self.core.revision = 2
        self.core.save()
        self.assert_code('candidate_stale', self.enqueue, candidate)

    def test_unknown_blocks_and_read_only_recovery_does_not_execute_twice(self):
        candidate = self.candidate()
        control.run_job(self.enqueue(candidate)['job']['id'])
        job = self.enqueue(candidate, 'apply')['job']
        self.executor.execute_job.side_effect = TimeoutError('private-path-and-secret')
        self.assertEqual(control.run_job(job['id'])['status'], 'unknown')
        self.assert_code('core_busy', self.candidate)
        self.assert_code('core_busy', self.enqueue, candidate, 'apply')
        control.run_job(job['id'])
        recovered = control.reconcile_job(job['id'])
        self.assertEqual(recovered['status'], 'succeeded')
        self.core.refresh_from_db()
        self.assertEqual(self.core.revision, 2)
        control.reconcile_job(job['id'])
        self.assertEqual(self.executor.execute_job.call_count, 2)
        self.assertEqual(self.executor.reconcile_job.call_count, 1)

    def test_check_failure_keeps_revision_and_requires_new_check(self):
        candidate = self.candidate()
        self.executor.execute_job.side_effect = lambda job: self.receipt(job, 'failed', error_code='check_failed')
        self.assertEqual(control.run_job(self.enqueue(candidate)['job']['id'])['status'], 'failed')
        self.assert_code('check_required', self.enqueue, candidate, 'apply')
        self.core.refresh_from_db()
        self.assertEqual(self.core.revision, 1)

    def test_wrong_receipt_identity_fence_hash_or_binding_stays_unknown(self):
        for field, value in [('job_id', 'wrong'), ('core_id', 'wrong'), ('action', 'rollback'), ('fence', True),
                             ('candidate_sha256', 'e' * 64), ('config_sha256', 'e' * 64),
                             ('binding_fingerprint', 'e' * 64)]:
            with self.subTest(field=field):
                candidate = self.candidate()
                job = self.enqueue(candidate)['job']
                self.executor.execute_job.side_effect = lambda row: self.receipt(row, **{field: value})
                result = control.run_job(job['id'])
                self.assertEqual((result['status'], result['error_code']), ('unknown', 'receipt_mismatch'))
                self.executor.reconcile_job.side_effect = self.receipt
                self.assertEqual(control.reconcile_job(job['id'])['status'], 'succeeded')

    def test_revoked_expired_revision_or_fence_prevents_executor(self):
        for reason in ('permission_revoked', 'job_expired', 'revision_conflict', 'fence_conflict'):
            candidate = self.candidate()
            job = self.enqueue(candidate)['job']
            if reason == 'permission_revoked':
                get_user_model().objects.filter(pk=self.admin.pk).update(is_staff=False)
            elif reason == 'job_expired':
                CoreControlJob.objects.filter(public_id=job['id']).update(expires_at=timezone.now() - timedelta(seconds=1))
            elif reason == 'revision_conflict':
                CoreInstance.objects.filter(pk=self.core.pk).update(revision=2)
            else:
                CoreControlState.objects.filter(core=self.core).update(fence=999)
            result = control.run_job(job['id'])
            self.assertEqual((result['status'], result['error_code']), ('failed', reason))
            get_user_model().objects.filter(pk=self.admin.pk).update(is_staff=True)
            CoreInstance.objects.filter(pk=self.core.pk).update(revision=1)
        self.executor.execute_job.assert_not_called()

    def test_revision_or_fence_change_after_execute_cannot_advance_candidate(self):
        candidate = self.candidate()
        def changed(job):
            CoreControlState.objects.filter(core=self.core).update(fence=999)
            return self.receipt(job)
        self.executor.execute_job.side_effect = changed
        job = control.run_job(self.enqueue(candidate)['job']['id'])
        self.assertEqual((job['status'], job['error_code']), ('unknown', 'fence_changed_during_execution'))
        self.core.refresh_from_db()
        self.assertEqual(self.core.revision, 1)

    def test_running_crash_is_reconciled_only_and_queue_claim_is_single(self):
        candidate = self.candidate()
        job = self.enqueue(candidate)['job']
        def during_execute(row):
            self.assertEqual(control.run_job(row.public_id)['status'], 'running')
            return self.receipt(row)
        self.executor.execute_job.side_effect = during_execute
        self.assertEqual(control.run_next_job()['status'], 'succeeded')
        self.assertIsNone(control.run_next_job())
        self.assertEqual(self.executor.execute_job.call_count, 1)
        new = self.enqueue(candidate)['job']
        CoreControlJob.objects.filter(public_id=new['id']).update(status='running', started_at=timezone.now())
        self.assertEqual(control.reconcile_job(new['id'])['status'], 'succeeded')
        self.assertEqual(self.executor.execute_job.call_count, 1)

    def test_database_blocks_second_active_job(self):
        candidate = self.candidate()
        job = CoreControlJob.objects.get(public_id=self.enqueue(candidate)['job']['id'])
        with self.assertRaises(IntegrityError), transaction.atomic():
            CoreControlJob.objects.create(core=self.core, candidate=job.candidate, actor=self.admin,
                action='check', expected_revision=1, fence=2, idempotency_key=self.key(),
                request_sha256='f' * 64, expires_at=timezone.now() + timedelta(minutes=1))

    def test_job_lookup_core_scope_and_receipt_secret_projection(self):
        candidate = self.candidate()
        job = self.enqueue(candidate)['job']
        other = CoreInstance.objects.create(server=self.server, instance_key='other', name='另一核心',
            core_type='sing_box', configuration_owner='isolated')
        self.assert_code('not_found', control.get_job, self.admin, job['id'], other.public_id)
        self.executor.execute_job.side_effect = lambda row: self.receipt(row, stdout='secret', path='/private', config={'secret': 'never'})
        result = control.run_job(job['id'])
        self.assertNotIn('secret', str(result))
        self.assertNotIn('artifact_ref', str(control.control_detail(self.admin, self.core.public_id)))

    def test_invalid_artifact_rejected_before_persist(self):
        self.executor.prepare_candidate.return_value['artifact_ref'] = '../../secret'
        self.assert_code('invalid_artifact', self.candidate)
        self.assertEqual(CoreConfigCandidate.objects.count(), 0)

    def test_candidate_capabilities_track_check_busy_revision_and_current_rollback(self):
        candidate = self.candidate()
        self.assertEqual(candidate['candidate']['capabilities'], {'check': True, 'apply': False, 'rollback': False})
        job = self.enqueue(candidate)['job']
        detail = control.control_detail(self.admin, self.core.public_id)
        self.assertEqual(detail['candidates'][0]['capabilities'], {'check': False, 'apply': False, 'rollback': False})
        control.run_job(job['id'])
        self.assertTrue(control.control_detail(self.admin, self.core.public_id)['candidates'][0]['capabilities']['apply'])
        control.run_job(self.enqueue(candidate, 'apply')['job']['id'])
        detail = control.control_detail(self.admin, self.core.public_id)
        self.assertEqual(detail['current_applied_candidate_id'], candidate['candidate']['id'])
        self.assertEqual(detail['candidates'][0]['capabilities'], {'check': False, 'apply': False, 'rollback': True})
        newer = self.candidate(expected_revision=2)
        control.run_job(self.enqueue(newer)['job']['id'])
        control.run_job(self.enqueue(newer, 'apply')['job']['id'])
        detail = control.control_detail(self.admin, self.core.public_id)
        self.assertEqual(detail['current_applied_candidate_id'], newer['candidate']['id'])
        old = next(row for row in detail['candidates'] if row['id'] == candidate['candidate']['id'])
        self.assertFalse(old['capabilities']['rollback'])
        self.assert_code('rollback_unavailable', self.enqueue, candidate, 'rollback')

    def test_database_pollution_is_not_returned_as_patch_or_receipt(self):
        candidate = self.candidate()
        job = self.enqueue(candidate)['job']
        CoreConfigCandidate.objects.filter(public_id=candidate['candidate']['id']).update(
            patch={'log': {'level': 'info', 'secret': 'never'}, 'secret': 'never'},
            base_sha256='private-secret', previous_level='private-secret')
        CoreControlJob.objects.filter(public_id=job['id']).update(
            result={'status': 'unknown', 'stdout': 'private-secret', 'error_code': 'private-secret'},
            error_code='private-secret')
        detail = control.control_detail(self.admin, self.core.public_id)
        self.assertNotIn('private-secret', str(detail))
        self.assertNotIn('never', str(detail))
        self.assertEqual(detail['candidates'][0]['patch'], {'log': {'level': 'info'}})

    def test_missing_or_future_check_fence_cannot_apply(self):
        candidate = self.candidate()
        control.run_job(self.enqueue(candidate)['job']['id'])
        CoreConfigCandidate.objects.filter(public_id=candidate['candidate']['id']).update(checked_fence=999)
        self.assert_code('check_required', self.enqueue, candidate, 'apply')
        detail = control.control_detail(self.admin, self.core.public_id)
        self.assertFalse(detail['candidates'][0]['capabilities']['apply'])
