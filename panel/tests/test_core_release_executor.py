"""隔离文件与假核心适配器验证；不运行systemctl或连接生产。"""
from contextlib import nullcontext
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from bridge.core_release_executor import CoreReleaseExecutor, ReleaseError


def digest(data):
    return hashlib.sha256(data).hexdigest()


class FakeChecker:
    def __init__(self, root):
        self.base = b'{"log":{"level":"info"},"identity":"kept"}'
        self.candidate = b'{"log":{"level":"error"},"identity":"kept"}'
        self.profile = SimpleNamespace(core_id=str(uuid.uuid4()), artifact_root=root / 'artifacts',
                                       source_path=root / 'config.json', active_path=root / 'config.json',
                                       generator_mode='single-json', identity_generation=7,
                                       service_unit='isolated-test.service')
        self.profile.artifact_root.mkdir()
        self.profile.source_path.write_bytes(self.base)
        self.profile.source_path.chmod(0o600)
        self.fingerprint = 'a' * 64
        self.check = {'status': 'succeeded', 'base_sha256': digest(self.base),
                      'candidate_sha256': digest(self.candidate), 'binding_fingerprint': self.fingerprint,
                      'identity_generation': 7, 'check_fence': 1}

    def verify_binding(self):
        return self.fingerprint

    def load_artifact(self, ref):
        return {'manifest': {}, 'base': self.base, 'candidate': self.candidate}

    def load_check(self, ref):
        return self.check


class FakeAdapter:
    def __init__(self):
        self.restarts = 0
        self.observations = 0
        self.timeout = False
        self.confirmed = True
        self.loaded_hash = ''
        self.loaded_job_id = ''

    def restart(self, profile, job_id):
        self.restarts += 1
        self.loaded_job_id = job_id
        self.loaded_hash = digest(Path(profile.active_path).read_bytes())
        if self.timeout:
            raise TimeoutError('此文本不可回传 secret=do-not-leak')

    def observe(self, profile, job_id):
        self.observations += 1
        return {'job_id': self.loaded_job_id, 'status': 'succeeded' if self.confirmed else 'unknown',
                'config_sha256': self.loaded_hash, 'running': True}


class CoreReleaseExecutorTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parents[2] / 'staging' / 'core-release-tests'
        self.parent.mkdir(exist_ok=True)
        self.root = self.parent / uuid.uuid4().hex
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.checker = FakeChecker(self.root)
        self.adapter = FakeAdapter()
        self.valid_fence = True
        self.valid_identity = True
        self.executor = self.make_executor()
        self.job = SimpleNamespace(public_id=uuid.uuid4(), core=SimpleNamespace(public_id=self.checker.profile.core_id),
                                   action='apply', fence=2, expected_revision=1,
                                   candidate=SimpleNamespace(artifact_ref='b' * 32, checked_fence=1,
                                                             base_sha256=digest(self.checker.base),
                                                             candidate_sha256=digest(self.checker.candidate),
                                                             binding_fingerprint='a' * 64))

    def cleanup(self):
        if self.root.resolve().parent != self.parent.resolve():
            raise RuntimeError('清理路径越界')
        shutil.rmtree(self.root)

    def private_guard(self, path):
        # Windows测试只证明隔离路径；不声称验证了POSIX权限。
        path = Path(path)
        if not path.is_relative_to(self.root):
            raise ReleaseError('unsafe_path')

    def make_executor(self):
        return CoreReleaseExecutor(self.checker, self.adapter, control_guard=lambda job: nullcontext(),
                                   fence_guard=lambda job: self.valid_fence,
                                   identity_guard=lambda profile, data: self.valid_identity,
                                   private_path_guard=self.private_guard)

    def next_job(self, action='apply'):
        job = copy.copy(self.job)
        job.public_id = uuid.uuid4()
        job.fence += 1
        job.action = action
        return job

    def assert_rejected(self, code):
        result = self.executor.execute_job(self.job)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], code)
        self.assertEqual(self.adapter.restarts, 0)
        return result

    def test_apply_receipt_and_replay_across_executor_restart(self):
        result = self.executor.execute_job(self.job)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['config_sha256'], digest(self.checker.candidate))
        self.assertEqual(self.checker.profile.active_path.read_bytes(), self.checker.candidate)
        self.assertEqual(self.make_executor().execute_job(self.job), result)
        self.assertEqual(self.adapter.restarts, 1)
        self.assertEqual(result['fence'], 2)

    def test_atomic_failure_keeps_old_config_and_blocks_replay(self):
        original = os.replace
        def replace(source, target):
            if Path(target) == self.checker.profile.active_path:
                raise OSError('secret path details')
            return original(source, target)
        with patch('bridge.core_release_executor.os.replace', side_effect=replace):
            result = self.executor.execute_job(self.job)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.checker.profile.active_path.read_bytes(), self.checker.base)
        self.assertEqual(self.make_executor().execute_job(self.job)['status'], 'unknown')
        self.assertEqual(self.adapter.restarts, 0)
        self.assertNotIn('secret', json.dumps(result))

    def test_check_then_source_drift_is_rejected(self):
        self.checker.profile.source_path.write_bytes(b'changed')
        self.assert_rejected('source_changed')

    def test_checked_candidate_tamper_is_rejected(self):
        self.checker.candidate += b' '
        self.assert_rejected('artifact_changed')

    def test_binding_change_is_rejected(self):
        self.checker.fingerprint = 'f' * 64
        self.assert_rejected('binding_changed')

    def test_missing_owner_evidence_is_closed(self):
        self.checker.verify_binding = lambda: (_ for _ in ()).throw(ValueError('secret-owner'))
        result = self.assert_rejected('validation_failed')
        self.assertNotIn('secret-owner', json.dumps(result))

    def test_wrong_checked_fence_is_rejected(self):
        self.job.candidate.checked_fence = 9
        self.assert_rejected('check_mismatch')

    def test_check_unknown_cannot_apply(self):
        self.checker.check['status'] = 'unknown'
        self.assert_rejected('check_required')

    def test_stale_authority_fence_is_rejected(self):
        self.valid_fence = False
        self.assert_rejected('stale_fence')

    def test_old_identity_cannot_be_applied(self):
        self.valid_identity = False
        self.assert_rejected('identity_changed')

    def test_missing_authority_guard_is_closed(self):
        self.executor.control_guard = None
        self.assert_rejected('authority_guard_unavailable')

    def test_generator_owned_config_cannot_be_overwritten(self):
        self.checker.profile.generator_mode = 'mack-a'
        self.assert_rejected('unsupported_generator')

    def test_timeout_stays_unknown_until_read_only_reconcile(self):
        self.adapter.timeout = True
        self.adapter.confirmed = False
        result = self.executor.execute_job(self.job)
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('do-not-leak', json.dumps(result))
        self.assertEqual(self.make_executor().execute_job(self.job)['status'], 'unknown')
        self.assertEqual(self.executor.execute_job(self.next_job())['error_code'], 'unresolved_job')
        self.assertEqual(self.executor.reconcile_job(self.job)['status'], 'unknown')
        self.adapter.confirmed = True
        self.assertEqual(self.executor.reconcile_job(self.job)['status'], 'succeeded')
        self.assertEqual(self.adapter.restarts, 1)

    def test_file_hash_without_running_proof_is_unknown(self):
        self.adapter.confirmed = False
        self.assertEqual(self.executor.execute_job(self.job)['status'], 'unknown')
        self.assertEqual(self.executor.reconcile_job(self.job)['status'], 'unknown')
        self.assertEqual(self.adapter.restarts, 1)

    def test_reconcile_cannot_confirm_revoked_identity(self):
        self.adapter.timeout = True
        self.executor.execute_job(self.job)
        self.valid_identity = False
        result = self.executor.reconcile_job(self.job)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['error_code'], 'identity_changed')
        self.assertEqual(self.adapter.restarts, 1)

    def test_wrong_job_running_proof_cannot_confirm(self):
        self.adapter.timeout = True
        self.executor.execute_job(self.job)
        with patch.object(self.adapter, 'observe', return_value={
                'job_id': str(uuid.uuid4()), 'status': 'succeeded', 'running': True,
                'config_sha256': digest(self.checker.candidate)}):
            self.assertEqual(self.executor.reconcile_job(self.job)['status'], 'unknown')
        self.assertEqual(self.adapter.restarts, 1)

    def test_symlink_material_is_rejected(self):
        original = Path.is_symlink
        def symlink(path):
            return path == self.checker.profile.source_path or original(path)
        with patch.object(Path, 'is_symlink', symlink):
            self.assert_rejected('unsafe_path')

    def test_rollback_restores_only_config_and_preserves_ledger(self):
        ledger = self.root / 'ledger.json'
        ledger.write_bytes(b'{"usage":12345}')
        self.assertEqual(self.executor.execute_job(self.job)['status'], 'succeeded')
        rollback = self.next_job('rollback')
        self.assertEqual(self.executor.execute_job(rollback)['status'], 'succeeded')
        self.assertEqual(self.checker.profile.active_path.read_bytes(), self.checker.base)
        self.assertEqual(ledger.read_bytes(), b'{"usage":12345}')
        self.assertEqual(self.adapter.restarts, 2)

    def test_rollback_cannot_revive_revoked_identity(self):
        self.assertEqual(self.executor.execute_job(self.job)['status'], 'succeeded')
        self.valid_identity = False
        self.assertEqual(self.executor.execute_job(self.next_job('rollback'))['error_code'], 'identity_changed')
        self.assertEqual(self.checker.profile.active_path.read_bytes(), self.checker.candidate)
        self.assertEqual(self.adapter.restarts, 1)

    def test_older_fence_blocked_even_if_source_restored(self):
        self.assertEqual(self.executor.execute_job(self.job)['status'], 'succeeded')
        stale = self.next_job()
        stale.fence = 1
        self.assertEqual(self.executor.execute_job(stale)['error_code'], 'stale_fence')

    def test_same_job_changed_base_request_is_rejected(self):
        self.executor.execute_job(self.job)
        self.job.candidate.base_sha256 = 'f' * 64
        self.assertEqual(self.executor.execute_job(self.job)['error_code'], 'job_replay_mismatch')
        self.assertEqual(self.adapter.restarts, 1)

    def test_crash_after_config_replace_does_not_reexecute(self):
        with patch.object(self.adapter, 'restart', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.executor.execute_job(self.job)
        self.assertEqual(self.make_executor().execute_job(self.job)['status'], 'unknown')
        self.assertEqual(self.checker.profile.active_path.read_bytes(), self.checker.candidate)
        self.assertEqual(self.adapter.restarts, 0)

    def test_crash_before_job_journal_cannot_be_bypassed(self):
        original = self.executor._save
        def save(name, value):
            if name == str(self.job.public_id) + '.json':
                raise KeyboardInterrupt()
            return original(name, value)
        with patch.object(self.executor, '_save', side_effect=save):
            with self.assertRaises(KeyboardInterrupt):
                self.executor.execute_job(self.job)
        self.assertEqual(self.make_executor().execute_job(self.job)['error_code'], 'unresolved_job')
        self.assertEqual(self.executor.reconcile_job(self.job)['status'], 'unknown')
        self.assertEqual(self.adapter.restarts, 0)

    def test_success_receipt_crash_before_unlock_can_recover(self):
        with patch.object(self.executor, '_finish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.executor.execute_job(self.job)
        self.assertEqual(self.make_executor().execute_job(self.job)['status'], 'succeeded')
        state = json.loads((self.executor.directory / 'state.json').read_bytes())
        self.assertEqual(state['pending'], '')
        self.assertEqual(self.adapter.restarts, 1)

    def test_private_files_are_created_with_mode_0600(self):
        original = os.open
        modes = []
        def opened(path, flags, mode=0o777, **kwargs):
            if flags & os.O_CREAT:
                modes.append(mode)
            return original(path, flags, mode, **kwargs)
        with patch('bridge.core_release_executor.os.open', side_effect=opened):
            self.assertEqual(self.executor.execute_job(self.job)['status'], 'succeeded')
        self.assertGreater(len(modes), 3)
        self.assertEqual(set(modes), {0o600})
        if os.name == 'posix':
            self.assertEqual(self.checker.profile.active_path.stat().st_mode & 0o777, 0o600)

    def test_unverified_windows_private_permissions_fail_closed(self):
        self.executor.private_path_guard = __import__('bridge.core_release_executor', fromlist=['_private'])._private
        with patch('bridge.core_release_executor.os.name', 'nt'):
            self.assert_rejected('private_permissions_unverified')

    def test_lock_excludes_second_executor(self):
        with self.executor._lock():
            self.assertEqual(self.make_executor().execute_job(self.job)['error_code'], 'executor_busy')
        self.assertEqual(self.adapter.restarts, 0)

    def real_checker(self):
        from bridge.core_candidate_check import CoreProfile, CoreCandidateChecker
        binary = self.root / 'isolated-core'
        binary.write_bytes(b'isolated fixed checker binary')
        binary.chmod(0o700)
        evidence = self.root / 'owner-evidence.json'
        fields = {'core_id': self.checker.profile.core_id, 'owner_id': 'isolated-owner',
                  'engine': 'sing-box', 'binary_path': str(binary),
                  'binary_sha256': digest(binary.read_bytes()), 'binary_version': 'isolated-v1',
                  'source_path': str(self.checker.profile.source_path),
                  'active_path': str(self.checker.profile.active_path),
                  'source_revision': 'revision-1', 'identity_generation': 7,
                  'service_unit': 'isolated-test.service', 'generator_mode': 'single-json'}
        evidence.write_text(json.dumps(dict(fields, exclusive_writer=True)), encoding='utf-8')
        evidence.chmod(0o600)
        profile = CoreProfile(**fields, artifact_root=str(self.checker.profile.artifact_root),
                              owner_evidence_path=str(evidence), owner_evidence_sha256=digest(evidence.read_bytes()))
        return CoreCandidateChecker(profile, private_path_guard=self.private_guard,
                                    runner=lambda argv, **kwargs: 0)

    def prepare_real_candidate(self, checker):
        prepared = checker.prepare_candidate({'log': {'level': 'error'}})
        result = checker.check_candidate(prepared['artifact_ref'], base_sha256=prepared['base_sha256'],
                                         candidate_sha256=prepared['candidate_sha256'],
                                         binding_fingerprint=prepared['binding_fingerprint'], check_fence=1)
        self.assertEqual(result['status'], 'succeeded')
        self.job.candidate = SimpleNamespace(**prepared, checked_fence=1)

    def test_real_checker_artifact_check_apply_and_rollback_contract(self):
        checker = self.real_checker()
        self.prepare_real_candidate(checker)
        original = Path(checker.profile.source_path).read_bytes()
        executor = CoreReleaseExecutor(checker, self.adapter, control_guard=lambda job: nullcontext(),
                                       fence_guard=lambda job: True, identity_guard=lambda profile, data: True,
                                       private_path_guard=self.private_guard)
        result = executor.execute_job(self.job)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['config_sha256'], self.job.candidate.candidate_sha256)
        self.assertEqual(executor.execute_job(self.next_job('rollback'))['status'], 'succeeded')
        self.assertEqual(Path(checker.profile.source_path).read_bytes(), original)
        self.assertEqual(self.adapter.restarts, 2)

    def test_real_checker_source_drift_after_check_is_rejected(self):
        checker = self.real_checker()
        self.prepare_real_candidate(checker)
        Path(checker.profile.source_path).write_bytes(b'{"log":{"level":"debug"}}')
        executor = CoreReleaseExecutor(checker, self.adapter, control_guard=lambda job: nullcontext(),
                                       fence_guard=lambda job: True, identity_guard=lambda profile, data: True,
                                       private_path_guard=self.private_guard)
        self.assertEqual(executor.execute_job(self.job)['error_code'], 'source_changed')
        self.assertEqual(self.adapter.restarts, 0)


if __name__ == '__main__':
    unittest.main()
