"""跨核心/订阅的故障注入测试；仅公开假身份与临时文件。"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

from bridge.identity import build_candidate
from bridge.lifecycle import Journal, Lifecycle, LifecycleError
from tests.test_identity import fake_account, fake_authority, OWNER_A, OWNER_B


class Backend:
    def __init__(self):
        self.authority = fake_authority()
        self.calls = []
        self.fail_render = self.fail_validate = self.fail_publish = False
        self.after_publish = None

    def snapshot(self):
        value = copy.deepcopy(self.authority)
        return value, hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def validate(self, candidate):
        self.calls.append('validate')
        if self.fail_validate:
            raise ValueError('公开测试异常')

    def render(self, candidate, ids):
        self.calls.append('render')
        if self.fail_render:
            raise ValueError('公开测试异常')
        if any(not x['enabled'] for x in candidate['accounts'] if x['id'] in ids):
            raise ValueError('不允许导出停用账号')
        self.last_ids = ids
        return {'windows': b'public-fake', 'android': b'{}', 'v2rayng': b'public-fake'}

    def publish(self, candidate, *, expected_hash):
        self.calls.append('publish')
        if self.fail_publish or expected_hash != self.snapshot()[1]:
            raise ValueError('公开发布失败')
        self.authority = copy.deepcopy(candidate)
        if self.after_publish:
            self.after_publish()
        return self.snapshot()[1]


class Artifacts:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.after_publish = None

    def publish(self, owner, payloads):
        if self.fail:
            raise ValueError('公开产物失败')
        self.calls.append(owner)
        if self.after_publish:
            self.after_publish()


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(__file__).resolve().parent.parent / ('.lifecycle-test-' + uuid.uuid4().hex)
        self.temp.mkdir()
        self.addCleanup(shutil.rmtree, self.temp)
        self.backend, self.artifacts = Backend(), Artifacts()
        self.journal = Journal(self.temp)
        self.lifecycle = Lifecycle(self.backend, self.artifacts, self.journal)
        self.job = str(uuid.uuid4())
        self.original = self.backend.snapshot()[0]

    def owned(self, owner=OWNER_A):
        prefix = 'pnl-' + uuid.UUID(owner).hex + '-'
        return [x for x in self.backend.authority['accounts'] if x['id'].startswith(prefix)]

    def test_prepare_before_publish_and_preserve_original(self):
        result = self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.backend.calls[:3], ['validate', 'render', 'publish'])
        self.assertEqual(len(self.backend.last_ids), 8)
        self.assertEqual(self.backend.authority['accounts'][:8], self.original['accounts'])
        self.assertEqual(self.artifacts.calls, [OWNER_A])

    def test_validation_failure_no_mutation(self):
        self.backend.fail_validate = True
        with self.assertRaises(ValueError):
            self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(self.backend.authority, self.original)
        self.assertIsNone(self.journal.read(self.job))

    def test_render_failure_no_mutation(self):
        self.backend.fail_render = True
        with self.assertRaises(ValueError):
            self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertNotIn('publish', self.backend.calls)

    def test_artifact_failure_suspends_only_owner(self):
        self.backend.authority, _ = build_candidate(self.backend.authority, OWNER_B, 'provision')
        other = copy.deepcopy(self.owned(OWNER_B))
        self.artifacts.fail = True
        with self.assertRaisesRegex(LifecycleError, '^application_failed_user_suspended$'):
            self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertTrue(all(not x['enabled'] for x in self.owned()))
        self.assertEqual(self.owned(OWNER_B), other)
        self.assertEqual(self.journal.read(self.job)['state'], 'compensated')

    def test_successful_retry_does_not_restart_or_rotate(self):
        self.lifecycle.execute(self.job, OWNER_A, 'provision')
        before, calls = self.backend.snapshot()[0], len(self.backend.calls)
        self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(self.backend.authority, before)
        self.assertEqual(len(self.backend.calls), calls)

    def test_job_reuse_rejected(self):
        self.lifecycle.execute(self.job, OWNER_A, 'provision')
        with self.assertRaisesRegex(LifecycleError, '^job_reused$'):
            self.lifecycle.execute(self.job, OWNER_B, 'provision')

    def test_suspend_does_not_render_or_republish_credentials(self):
        self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.backend.calls.clear()
        self.lifecycle.execute(str(uuid.uuid4()), OWNER_A, 'suspend')
        self.assertNotIn('render', self.backend.calls)
        self.assertTrue(all(not x['enabled'] for x in self.owned()))
        self.assertEqual(len(self.artifacts.calls), 1)

    def test_crash_after_publish_recovered_on_next_run(self):
        def crash():
            raise SystemExit('模拟进程崩溃')
        self.backend.after_publish = crash
        with self.assertRaises(SystemExit):
            self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertTrue(all(x['enabled'] for x in self.owned()))
        self.backend.after_publish = None
        other_process = Lifecycle(self.backend, self.artifacts, Journal(self.temp))
        self.assertEqual(other_process.recover()[0]['state'], 'compensated')
        self.assertTrue(all(not x['enabled'] for x in self.owned()))

    def test_unresolved_checkpoint_blocks_new_job(self):
        self.journal.write({'job': self.job, 'owner': OWNER_A, 'action': 'provision', 'state': 'applying'})
        with self.assertRaisesRegex(LifecycleError, '^recovery_required$'):
            self.lifecycle.execute(str(uuid.uuid4()), OWNER_B, 'provision')
        self.assertEqual(self.lifecycle.recover()[0]['state'], 'compensated')

    def test_failed_compensation_keeps_recovery_required(self):
        self.lifecycle.execute(self.job, OWNER_A, 'provision')
        row = self.journal.read(self.job)
        row['state'] = 'core_applied'
        self.journal.write(row)
        self.backend.fail_publish = True
        with self.assertRaisesRegex(LifecycleError, '^recovery_required$'):
            self.lifecycle.recover()
        self.assertEqual(self.journal.read(self.job)['state'], 'recovery_required')

    def test_journal_has_no_credentials(self):
        self.lifecycle.execute(self.job, OWNER_A, 'provision')
        raw = (self.temp / (self.job + '.json')).read_text()
        for account in self.backend.authority['accounts']:
            self.assertNotIn(account['credential'], raw)
        self.assertEqual(set(json.loads(raw)), {'job', 'owner', 'action', 'state'})

    def test_invalid_checkpoint_not_ignored(self):
        (self.temp / (self.job + '.json')).write_text('{broken')
        with self.assertRaisesRegex(LifecycleError, '^journal_invalid$'):
            self.lifecycle.recover()

    def test_provision_cannot_silently_reenable_suspended_user(self):
        self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.lifecycle.execute(str(uuid.uuid4()), OWNER_A, 'suspend')
        with self.assertRaises(ValueError):
            self.lifecycle.execute(str(uuid.uuid4()), OWNER_A, 'provision')
        self.assertTrue(all(not x['enabled'] for x in self.owned()))

    def test_compensating_checkpoint_failure_does_not_skip_suspension(self):
        self.backend.authority, _ = build_candidate(self.backend.authority, OWNER_B, 'provision')
        other = copy.deepcopy(self.owned(OWNER_B))
        self.artifacts.fail = True
        original_write = self.journal.write

        def failed_transition(row):
            if row['state'] == 'compensating':
                raise OSError('公开检查点异常')
            original_write(row)

        with patch.object(self.journal, 'write', side_effect=failed_transition):
            with self.assertRaisesRegex(LifecycleError, '^application_failed_user_suspended$'):
                self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertTrue(all(not x['enabled'] for x in self.owned()))
        self.assertEqual(self.owned(OWNER_B), other)
        self.assertEqual(self.journal.read(self.job)['state'], 'compensated')

    def test_persistent_checkpoint_outage_keeps_pending_and_returns_fixed_error(self):
        self.artifacts.fail = True
        original_write = self.journal.write

        def disk_outage(row):
            if row['state'] in ('compensating', 'compensated', 'recovery_required'):
                raise OSError('公开故障正文不能进入外部异常')
            original_write(row)

        with patch.object(self.journal, 'write', side_effect=disk_outage):
            with self.assertRaisesRegex(LifecycleError, '^recovery_required$'):
                self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertTrue(all(not x['enabled'] for x in self.owned()))
        self.assertEqual(self.journal.read(self.job)['state'], 'core_applied')
        self.assertEqual(self.lifecycle.recover(), [{'job': self.job, 'state': 'compensated'}])
        self.assertEqual(self.lifecycle.recover(), [])

    def test_state_memory_advances_only_after_checkpoint_success(self):
        row = {'job': self.job, 'owner': OWNER_A, 'action': 'provision', 'state': 'applying'}
        with patch.object(self.journal, 'write', side_effect=OSError('公开写入异常')):
            with self.assertRaises(OSError):
                self.lifecycle._state(row, 'complete')
        self.assertEqual(row['state'], 'applying')

    def test_core_checkpoint_failure_compensates(self):
        original_write = self.journal.write

        def failed_checkpoint(row):
            if row['state'] == 'core_applied':
                raise OSError('公开检查点异常')
            original_write(row)

        with patch.object(self.journal, 'write', side_effect=failed_checkpoint):
            with self.assertRaisesRegex(LifecycleError, '^application_failed_user_suspended$'):
                self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertTrue(all(not x['enabled'] for x in self.owned()))
        self.assertEqual(self.journal.read(self.job)['state'], 'compensated')

    def test_completion_checkpoint_failure_before_replace_is_recovered(self):
        original_write = self.journal.write

        def failed_checkpoint(row):
            if row['state'] == 'complete':
                raise OSError('公开终态检查点异常')
            original_write(row)

        with patch.object(self.journal, 'write', side_effect=failed_checkpoint):
            with self.assertRaisesRegex(LifecycleError, '^recovery_required$'):
                self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(self.journal.read(self.job)['state'], 'core_applied')
        self.assertEqual(self.artifacts.calls, [OWNER_A])
        self.assertEqual(self.lifecycle.recover(), [{'job': self.job, 'state': 'compensated'}])
        self.assertTrue(all(not x['enabled'] for x in self.owned()))

    def test_completion_checkpoint_failure_after_replace_does_not_contradict_core(self):
        original_write = self.journal.write
        states = []

        def uncertain_durability(row):
            states.append(row['state'])
            original_write(row)
            if row['state'] == 'complete':
                raise OSError('公开目录同步异常')

        with patch.object(self.journal, 'write', side_effect=uncertain_durability):
            with self.assertRaisesRegex(LifecycleError, '^recovery_required$'):
                self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(states, ['applying', 'core_applied', 'complete'])
        self.assertEqual(self.journal.read(self.job)['state'], 'complete')
        self.assertTrue(all(x['enabled'] for x in self.owned()))
        before = self.backend.snapshot()[0]
        self.assertEqual(self.lifecycle.recover(), [])
        self.assertEqual(self.lifecycle.execute(self.job, OWNER_A, 'provision')['state'], 'complete')
        self.assertEqual(self.backend.authority, before)
        self.assertEqual(self.artifacts.calls, [OWNER_A])

    def test_first_checkpoint_failure_does_not_apply_core(self):
        with patch.object(self.journal, 'write', side_effect=OSError('公开检查点异常')):
            with self.assertRaisesRegex(LifecycleError, '^checkpoint_write_failed$'):
                self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(self.backend.authority, self.original)
        self.assertNotIn('publish', self.backend.calls)
        self.assertEqual(self.artifacts.calls, [])

    def test_change_during_artifact_publish_cannot_be_marked_complete(self):
        self.backend.authority, _ = build_candidate(self.backend.authority, OWNER_B, 'provision')
        other = copy.deepcopy(self.owned(OWNER_B))

        def change_owner():
            self.owned()[0]['label'] = '公开模拟外部管理器变更'

        self.artifacts.after_publish = change_owner
        with self.assertRaisesRegex(LifecycleError, '^application_failed_user_suspended$'):
            self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertTrue(all(not x['enabled'] for x in self.owned()))
        self.assertEqual(self.owned(OWNER_B), other)
        self.assertEqual(self.journal.read(self.job)['state'], 'compensated')

    def test_extra_owner_account_does_not_escape_confirmed_namespace(self):
        self.backend.authority, _ = build_candidate(self.backend.authority, OWNER_B, 'provision')
        other = copy.deepcopy(self.owned(OWNER_B))

        def add_unexpected_account():
            account = fake_account(900, 'hy2')
            account['id'] = 'pnl-' + uuid.UUID(OWNER_A).hex + '-h-extra'
            self.backend.authority['accounts'].append(account)

        self.backend.after_publish = add_unexpected_account
        with self.assertRaisesRegex(LifecycleError, '^recovery_required$'):
            self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(self.journal.read(self.job)['state'], 'recovery_required')
        self.assertEqual(self.artifacts.calls, [])
        self.assertEqual(self.owned(OWNER_B), other)
        self.assertEqual(self.backend.authority['accounts'][:8], self.original['accounts'])

    def test_cas_conflict_preserves_unrelated_external_change(self):
        publish = self.backend.publish

        def conflict(candidate, *, expected_hash):
            self.backend.authority, _ = build_candidate(self.backend.authority, OWNER_B, 'provision')
            return publish(candidate, expected_hash=expected_hash)

        with patch.object(self.backend, 'publish', side_effect=conflict):
            with self.assertRaisesRegex(LifecycleError, '^application_failed_user_suspended$'):
                self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(self.owned(), [])
        self.assertEqual(len(self.owned(OWNER_B)), 8)
        self.assertTrue(all(x['enabled'] for x in self.owned(OWNER_B)))
        self.assertEqual(self.backend.authority['accounts'][:8], self.original['accounts'])

    def test_compensation_cas_conflict_stays_pending_until_recovered(self):
        self.backend.authority, _ = build_candidate(self.backend.authority, OWNER_B, 'provision')
        self.artifacts.fail = True
        publish = self.backend.publish
        call_count = 0

        def conflict_on_compensation(candidate, *, expected_hash):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                self.owned(OWNER_B)[0]['label'] = '公开并发保留项'
            return publish(candidate, expected_hash=expected_hash)

        with patch.object(self.backend, 'publish', side_effect=conflict_on_compensation):
            with self.assertRaisesRegex(LifecycleError, '^recovery_required$'):
                self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertTrue(all(x['enabled'] for x in self.owned()))
        self.assertEqual(self.journal.read(self.job)['state'], 'recovery_required')
        self.lifecycle.recover()
        self.assertTrue(all(not x['enabled'] for x in self.owned()))
        self.assertTrue(all(x['enabled'] for x in self.owned(OWNER_B)))
        self.assertEqual(self.owned(OWNER_B)[0]['label'], '公开并发保留项')

    def test_crash_after_artifact_publish_is_recovered_without_republish(self):
        def crash():
            raise SystemExit('公开模拟进程崩溃')

        self.artifacts.after_publish = crash
        with self.assertRaises(SystemExit):
            self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(self.journal.read(self.job)['state'], 'core_applied')
        self.assertEqual(self.artifacts.calls, [OWNER_A])
        self.artifacts.after_publish = None
        restored = Lifecycle(self.backend, self.artifacts, Journal(self.temp))
        self.assertEqual(restored.recover(), [{'job': self.job, 'state': 'compensated'}])
        self.assertTrue(all(not x['enabled'] for x in self.owned()))
        self.assertEqual(self.artifacts.calls, [OWNER_A])

    def test_crash_after_durable_complete_does_not_repeat_operation(self):
        original_write = self.journal.write

        def crash_after_completion(row):
            original_write(row)
            if row['state'] == 'complete':
                raise SystemExit('公开模拟完成后进程崩溃')

        with patch.object(self.journal, 'write', side_effect=crash_after_completion):
            with self.assertRaises(SystemExit):
                self.lifecycle.execute(self.job, OWNER_A, 'provision')
        before = self.backend.snapshot()[0]
        self.assertEqual(self.journal.read(self.job)['state'], 'complete')
        self.assertEqual(self.lifecycle.recover(), [])
        self.assertEqual(self.lifecycle.execute(self.job, OWNER_A, 'provision')['state'], 'complete')
        self.assertEqual(self.backend.authority, before)
        self.assertEqual(self.artifacts.calls, [OWNER_A])

    def test_recovery_verifies_owner_absence_before_marking_compensated(self):
        row = {'job': self.job, 'owner': OWNER_A, 'action': 'provision', 'state': 'applying'}
        self.journal.write(row)
        snapshot = self.backend.snapshot
        count = 0

        def owner_appears():
            nonlocal count
            count += 1
            if count == 2:
                self.backend.authority, _ = build_candidate(self.backend.authority, OWNER_A, 'provision')
            return snapshot()

        with patch.object(self.backend, 'snapshot', side_effect=owner_appears):
            with self.assertRaisesRegex(LifecycleError, '^recovery_required$'):
                self.lifecycle.recover()
        self.assertEqual(self.journal.read(self.job)['state'], 'recovery_required')
        self.lifecycle.recover()
        self.assertTrue(all(not x['enabled'] for x in self.owned()))

    def test_journal_write_rejects_extra_data_before_persistence(self):
        row = {'job': self.job, 'owner': OWNER_A, 'action': 'provision', 'state': 'applying'}
        invalid_rows = [row | {'credential': '公开假值'}, row | {'state': 'unexpected'},
                        row | {'owner': OWNER_A.replace('-', '')}, row | {'job': []}]
        for invalid in invalid_rows:
            with self.assertRaisesRegex(LifecycleError, '^journal_invalid$'):
                self.journal.write(invalid)
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_duplicate_json_keys_and_noncanonical_filenames_are_rejected(self):
        path = self.temp / (self.job + '.json')
        row = {'job': self.job, 'owner': OWNER_A, 'action': 'provision', 'state': 'applying'}
        raw = json.dumps(row)[:-1] + ',"owner":"' + OWNER_B + '"}'
        path.write_text(raw)
        with self.assertRaisesRegex(LifecycleError, '^journal_invalid$'):
            self.lifecycle.recover()
        path.unlink()
        path = self.temp / (uuid.UUID(self.job).hex + '.json')
        path.write_text(json.dumps(row))
        with self.assertRaisesRegex(LifecycleError, '^journal_invalid$'):
            self.lifecycle.recover()

    def test_candidate_preparation_errors_do_not_echo_backend_output(self):
        self.backend.fail_validate = True
        with self.assertRaisesRegex(LifecycleError, '^candidate_preparation_failed$'):
            self.lifecycle.execute(self.job, OWNER_A, 'provision')

    def test_terminal_job_replay_is_historical_and_does_not_undo_later_suspend(self):
        self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.lifecycle.execute(str(uuid.uuid4()), OWNER_A, 'suspend')
        previous = copy.deepcopy(self.owned())
        result = self.lifecycle.execute(self.job, OWNER_A, 'provision')
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.owned(), previous)
        self.assertTrue(all(not x['enabled'] for x in self.owned()))


if __name__ == '__main__':
    unittest.main()
