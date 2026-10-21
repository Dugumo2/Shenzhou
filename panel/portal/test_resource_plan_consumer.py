"""真实 Django 保存→文件投影→持久回执反例，全部使用独立假库/假 P11 文件。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone as datetime_timezone
import io
import json
from pathlib import Path
import shutil
import uuid
from contextlib import ExitStack
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from bridge.resource_plan_consumer import ProjectionStore, consume_once, file_digest, run_as_database_owner
from .provider_usage import HOME_ACCOUNTING, BWH_ACCOUNTING
from .resource_plan_models import ResourcePlanConsumption
from .resource_plan_service import export_resource_plan_overrides, record_resource_plan_consumption, resource_plan_state
from .resource_usage import _time
from . import test_resource_plan_service as service_tests
from .test_resource_usage import ANCHOR


@override_settings(PANEL_LIVE=False, RESOURCE_PLAN_WRITE_ENABLED=True)
class ResourcePlanConsumerTests(TransactionTestCase):
    request = service_tests.ResourcePlanServiceTests.request
    save = service_tests.ResourcePlanServiceTests.save

    def setUp(self):
        # 本组夹具的初值属于9/20起的首个采购周期，不能随真实日期越过10/20而改变语义。
        clock = patch('django.utils.timezone.now', return_value=datetime(2026, 10, 18, 4, tzinfo=datetime_timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)
        service_tests.ResourcePlanServiceTests.setUp(self)
        self.now = timezone.now()
        self.base = Path(__file__).resolve().parents[2]/'staging'
        self.root = self.base/('purchase-projection-'+uuid.uuid4().hex)
        self.root.mkdir()
        (self.root/'.isolated-purchase-test').touch()
        self.addCleanup(self.clean)
        self.write('plans.json', {'schema_version': 1, 'plans': self.source})
        self.source_sample()
        self.setting = override_settings(RESOURCE_PLAN_PROJECTION_TEST_ROOT=str(self.root))
        self.setting.enable()
        self.addCleanup(self.setting.disable)
        self.store = ProjectionStore(self.root)
        self.run_command()

    def clean(self):
        if self.root.resolve().parent != self.base.resolve() or not self.root.name.startswith('purchase-projection-') or self.root.is_symlink():
            raise RuntimeError('fixture_scope_refused')
        shutil.rmtree(self.root)

    def write(self, name, document):
        path = self.root/name
        path.write_text(json.dumps(document), encoding='utf-8')
        path.chmod(0o600)

    def read(self, name):
        return json.loads((self.root/name).read_text(encoding='utf-8'))

    def source_sample(self, *, up=100, down=200):
        stamp = int(self.now.timestamp())
        self.write('source.json', {'schema_version': 1, 'updated_at': stamp,
            'residential': {'source': 'estimate', 'accounting': HOME_ACCOUNTING, 'last_query_ok': True,
                'updated_at': stamp, 'upload_bytes': up, 'download_bytes': down, 'used_bytes': up+down,
                'total_bytes': 400_000_000_000, 'estimate_start': int(ANCHOR+86400),
                'historical_usage_known': False, 'collection_gaps': 0, 'period_start': None,
                'reset_day': 19, 'reset_day_confirmed': False, 'expire': None,
                'quota_unit_assumption': '400_decimal_GB', 'auto_period_reset': False, 'header_eligible': True},
            'bwh': {'source': 'KiwiVM_getServiceInfo', 'accounting': BWH_ACCOUNTING, 'last_query_ok': True,
                'updated_at': stamp, 'used_bytes': 1000, 'total_bytes': 10000,
                'reset_at': int(self.now.timestamp()+864000)}})

    def run_command(self):
        with patch('portal.management.commands.project_resource_usage.timezone.now', return_value=self.now):
            output = io.StringIO()
            call_command('project_resource_usage', stdout=output)
        return json.loads(output.getvalue())

    def tick(self):
        return consume_once(self.store, now=self.now.timestamp(), export_overrides=export_resource_plan_overrides,
                            record=record_resource_plan_consumption)

    def test_real_command_quota_expiry_and_repeated_sample(self):
        original = self.read('usage.json')['meters'][0]
        self.save()
        self.save(self.request('expiry', {'expires_on': '2028-01-19'}), key='renewal-request-2')
        self.now = timezone.now()
        result = self.run_command()
        self.assertEqual(result['consumed'], 2)
        self.assertEqual(ResourcePlanConsumption.objects.count(), 2)
        home = self.read('usage.json')['meters'][0]
        self.assertEqual(home['quota_bytes'], '400000001000')
        self.assertEqual(home['expires_on'], '2028-01-19')
        for key in ('cycle', 'used_bytes', 'upload_bytes', 'download_bytes'):
            self.assertEqual(home[key], original[key])
        self.assertEqual(self.run_command()['consumed'], 0)
        self.assertEqual(ResourcePlanConsumption.objects.count(), 2)
        self.assertEqual(resource_plan_state(self.admin, self.keys['home'])['consumption']['status'], 'consumed')

    def test_cycle_preserves_current_and_resets_once_then_uses_new_days(self):
        original = self.read('usage.json')['meters'][0]
        reset = timezone.now()+timedelta(minutes=2)
        self.save(self.request('cycle', {'days': 12, 'next_reset_at': reset.isoformat()}))
        self.now = timezone.now()
        self.run_command()
        home = self.read('usage.json')['meters'][0]
        self.assertEqual(home['used_bytes'], original['used_bytes'])
        self.assertEqual(home['cycle']['starts_at'], original['cycle']['starts_at'])
        self.assertEqual(_time(home['cycle']['ends_at']), reset.timestamp())
        self.now = reset+timedelta(seconds=1)
        self.source_sample(up=150, down=250)
        self.run_command()
        home = self.read('usage.json')['meters'][0]
        self.assertEqual(home['used_bytes'], '0')
        self.assertEqual(_time(home['cycle']['starts_at']), reset.timestamp())
        self.assertEqual(_time(home['cycle']['ends_at']), reset.timestamp()+12*86400)
        self.now += timedelta(seconds=30)
        self.source_sample(up=170, down=260)
        self.run_command()
        self.assertEqual(self.read('usage.json')['meters'][0]['used_bytes'], '30')
        self.run_command()
        self.assertEqual(self.read('usage.json')['meters'][0]['used_bytes'], '30')
        history = self.read('cycle-state.json')['purchase_tracking']['home']['history']
        self.assertEqual(history[0]['cycle'], original['cycle'])
        self.assertEqual(history[0]['state']['last_sample']['used_bytes'], '300')

    def test_view_failure_has_no_ack_and_recovers_exact_same_state(self):
        self.save()
        self.now = timezone.now()
        actual = self.store.write
        def failing(path, document):
            if path == self.store.view and document['meters'][0]['quota_bytes'] == '400000001000':
                raise OSError('simulated_view_failure')
            return actual(path, document)
        with patch.object(self.store, 'write', side_effect=failing):
            with self.assertRaises(OSError):
                self.tick()
        self.assertFalse(ResourcePlanConsumption.objects.exists())
        pending = self.read('purchase-journal.json')
        self.assertEqual(self.read('usage.json')['meters'][0]['quota_bytes'], '400000000000')
        self.tick()
        receipt = ResourcePlanConsumption.objects.get()
        self.assertEqual(receipt.state_after_sha256, file_digest(pending['state']))
        self.assertFalse(self.store.journal.exists())
        self.assertEqual(self.read('usage.json')['meters'][0]['used_bytes'], '300')

    def test_ack_failure_after_files_recovers_original_receipt(self):
        self.save()
        self.now = timezone.now()
        with self.assertRaises(OSError):
            consume_once(self.store, now=self.now.timestamp(), export_overrides=export_resource_plan_overrides,
                         record=lambda _: (_ for _ in ()).throw(OSError('simulated_db_failure')))
        pending = self.read('purchase-journal.json')
        self.assertEqual(self.read('usage.json')['meters'][0]['quota_bytes'], '400000001000')
        self.assertFalse(ResourcePlanConsumption.objects.exists())
        self.now += timedelta(seconds=1)
        self.tick()
        self.assertEqual(ResourcePlanConsumption.objects.get().state_after_sha256, file_digest(pending['state']))
        self.assertFalse(self.store.journal.exists())

    def test_source_drift_and_corrupt_state_preserve_files(self):
        self.save()
        original = (self.root/'cycle-state.json').read_bytes()
        plans = self.read('plans.json')
        plans['plans'][0]['quota_bytes'] = '2'
        self.write('plans.json', plans)
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual((self.root/'cycle-state.json').read_bytes(), original)
        self.write('plans.json', {'schema_version': 1, 'plans': self.source})
        (self.root/'cycle-state.json').write_text('{bad', encoding='utf-8')
        view = (self.root/'usage.json').read_bytes()
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual((self.root/'usage.json').read_bytes(), view)
        self.assertFalse(ResourcePlanConsumption.objects.exists())

    def test_expired_reset_is_rejected_and_original_statistics_continue(self):
        original = self.read('usage.json')['meters'][0]
        reset = timezone.now()+timedelta(seconds=30)
        self.save(self.request('cycle', {'days': 12, 'next_reset_at': reset.isoformat()}))
        self.now = reset+timedelta(seconds=1)
        self.source_sample(up=150, down=250)
        result = self.run_command()
        self.assertEqual((result['consumed'], result['rejected']), (0, 1))
        receipt = ResourcePlanConsumption.objects.get()
        self.assertEqual((receipt.status, receipt.error_code), ('rejected', 'cycle_reset_expired'))
        current = self.read('usage.json')['meters'][0]
        self.assertEqual(current['cycle'], original['cycle'])
        self.assertEqual(current['used_bytes'], '400')
        self.now += timedelta(seconds=30)
        self.source_sample(up=170, down=260)
        self.run_command()
        self.assertEqual(self.read('usage.json')['meters'][0]['used_bytes'], '430')
        self.assertEqual(ResourcePlanConsumption.objects.count(), 1)

    def test_rejected_cycle_followed_by_quota_does_not_resurrect_cycle_and_future_cycle_recovers(self):
        original = self.read('usage.json')['meters'][0]
        reset = timezone.now()+timedelta(seconds=30)
        self.save(self.request('cycle', {'days': 12, 'next_reset_at': reset.isoformat()}))
        self.save(key='quota-after-expiry')
        self.now = reset+timedelta(seconds=1)
        self.source_sample(up=150, down=250)
        result = self.run_command()
        self.assertEqual((result['consumed'], result['rejected']), (1, 1))
        current = self.read('usage.json')['meters'][0]
        self.assertEqual(current['cycle'], original['cycle'])
        self.assertEqual(current['quota_bytes'], '400000001000')
        self.assertEqual(current['used_bytes'], '400')
        future = self.now+timedelta(days=3)
        self.save(self.request('cycle', {'days': 10, 'next_reset_at': future.isoformat()}), key='cycle-after-rejection')
        self.run_command()
        current = self.read('usage.json')['meters'][0]
        self.assertEqual(_time(current['cycle']['ends_at']), future.timestamp())
        self.assertEqual(current['cycle']['starts_at'], original['cycle']['starts_at'])
        self.assertEqual(current['used_bytes'], '400')
        self.assertEqual(ResourcePlanConsumption.objects.filter(status='consumed').count(), 2)

    def test_rejected_cycle_receipt_crash_replays_exactly_once(self):
        reset = timezone.now()+timedelta(seconds=30)
        self.save(self.request('cycle', {'days': 12, 'next_reset_at': reset.isoformat()}))
        self.now = reset+timedelta(seconds=1)
        def recorded_then_crash(receipt):
            record_resource_plan_consumption(receipt)
            raise OSError('simulated_rejection_ack_death')
        with patch('portal.resource_plan_service.timezone.now', return_value=self.now):
            with self.assertRaises(OSError):
                consume_once(self.store, now=self.now.timestamp(), export_overrides=export_resource_plan_overrides,
                             record=recorded_then_crash)
        stored = ResourcePlanConsumption.objects.get()
        self.assertEqual(stored.status, 'rejected')
        fingerprint = stored.receipt_fingerprint
        self.run_command()
        self.assertEqual(ResourcePlanConsumption.objects.get().receipt_fingerprint, fingerprint)
        self.assertFalse(self.store.journal.exists())

    def test_delayed_rejected_change_keeps_original_boundary_without_fabricating_missing_usage(self):
        original = self.read('usage.json')['meters'][0]
        reset = timezone.now()+timedelta(seconds=30)
        self.save(self.request('cycle', {'days': 12, 'next_reset_at': reset.isoformat()}))
        self.now = timezone.datetime.fromtimestamp(_time(original['cycle']['ends_at'])+1, timezone.get_current_timezone())
        self.source_sample(up=150, down=250)
        self.run_command()
        current = self.read('usage.json')['meters'][0]
        self.assertEqual(current['cycle']['starts_at'], original['cycle']['ends_at'])
        self.assertEqual(_time(current['cycle']['ends_at'])-_time(current['cycle']['starts_at']), 30*86400)
        self.assertIsNone(current['used_bytes'])
        self.assertEqual(ResourcePlanConsumption.objects.get().status, 'rejected')
        self.now += timedelta(seconds=30)
        self.source_sample(up=170, down=260)
        self.run_command()
        self.assertEqual(self.read('usage.json')['meters'][0]['used_bytes'], '30')

    def test_bwg_only_expiry_keeps_official_reset_and_used(self):
        original = self.read('usage.json')['meters'][1]
        self.save(self.request('expiry', {'expires_on': '2028-01-19'}, meter='bwg'), meter='bwg')
        self.now = timezone.now()
        self.run_command()
        current = self.read('usage.json')['meters'][1]
        for key in ('used_bytes', 'quota_bytes', 'cycle'):
            self.assertEqual(current[key], original[key])

    def test_revoked_owner_never_publishes(self):
        self.save()
        self.admin.is_staff = False
        self.admin.save(update_fields=['is_staff'])
        original = (self.root/'cycle-state.json').read_bytes()
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual((self.root/'cycle-state.json').read_bytes(), original)
        self.assertFalse(ResourcePlanConsumption.objects.exists())

    def test_live_cannot_inject_test_directory(self):
        with override_settings(PANEL_LIVE=True):
            with self.assertRaises(ValueError):
                ProjectionStore(self.root)

    def test_production_default_refuses_non_root_before_reading_files(self):
        with patch('bridge.resource_plan_consumer.os.name', 'posix'), patch('bridge.resource_plan_consumer.os.geteuid', return_value=1001, create=True):
            with self.assertRaises(ValueError):
                ProjectionStore()

    def test_command_has_no_arbitrary_path_option(self):
        with self.assertRaises(CommandError):
            call_command('project_resource_usage', '--root', str(self.root), stdout=io.StringIO(), stderr=io.StringIO())

    def test_old_state_cannot_reapply_already_confirmed_version(self):
        old = self.read('cycle-state.json')
        self.save()
        self.now = timezone.now()
        self.run_command()
        self.write('cycle-state.json', old)
        view = (self.root/'usage.json').read_bytes()
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual((self.root/'usage.json').read_bytes(), view)
        self.assertEqual(ResourcePlanConsumption.objects.count(), 1)

    def test_out_of_order_version_is_rejected_before_writes(self):
        self.save()
        overrides = export_resource_plan_overrides()
        next(e for e in overrides['entries'] if e['meter_id'] == 'home')['versions'][0]['revision'] = 3
        original = (self.root/'cycle-state.json').read_bytes()
        with self.assertRaises(ValueError):
            consume_once(self.store, now=self.now.timestamp(), export_overrides=lambda: overrides,
                         record=record_resource_plan_consumption)
        self.assertEqual((self.root/'cycle-state.json').read_bytes(), original)

    def test_existing_lock_blocks_concurrent_command(self):
        with self.store.lock():
            with self.assertRaises(CommandError):
                self.run_command()

    def test_ack_persisted_then_process_dies_is_idempotent(self):
        self.save()
        self.now = timezone.now()
        def recorded_then_crash(receipt):
            record_resource_plan_consumption(receipt)
            raise OSError('simulated_process_death')
        with self.assertRaises(OSError):
            consume_once(self.store, now=self.now.timestamp(), export_overrides=export_resource_plan_overrides,
                         record=recorded_then_crash)
        self.assertEqual(ResourcePlanConsumption.objects.count(), 1)
        self.tick()
        self.assertEqual(ResourcePlanConsumption.objects.count(), 1)
        self.assertFalse(self.store.journal.exists())

    def test_corrupt_source_preserves_view_and_has_no_receipt(self):
        self.save()
        (self.root/'source.json').write_text('{invalid', encoding='utf-8')
        view = (self.root/'usage.json').read_bytes()
        authority = (self.root/'plan-authority.json').read_bytes()
        with self.assertRaises(CommandError):
            self.run_command()
        self.assertEqual((self.root/'usage.json').read_bytes(), view)
        self.assertEqual((self.root/'plan-authority.json').read_bytes(), authority)
        self.assertFalse(ResourcePlanConsumption.objects.exists())

    def test_authority_mirror_keeps_original_plan_not_current_override(self):
        self.save()
        self.now = timezone.now()
        self.run_command()
        mirror = self.read('plan-authority.json')
        self.assertEqual(set(mirror), {'schema_version', 'observed_at', 'plans'})
        self.assertEqual(mirror['plans'], self.source)
        self.assertEqual(self.read('usage.json')['meters'][0]['quota_bytes'], '400000001000')

    def test_second_cycle_change_keeps_same_current_start_and_history(self):
        original = self.read('usage.json')['meters'][0]
        reset = timezone.now()+timedelta(days=2)
        self.save(self.request('cycle', {'days': 12, 'next_reset_at': reset.isoformat()}))
        self.now = timezone.now()
        self.run_command()
        second = reset+timedelta(days=1)
        self.save(self.request('cycle', {'days': 15, 'next_reset_at': second.isoformat()}), key='cycle-request-second')
        self.now = timezone.now()
        self.run_command()
        home = self.read('usage.json')['meters'][0]
        self.assertEqual(home['used_bytes'], original['used_bytes'])
        self.assertEqual(home['cycle']['starts_at'], original['cycle']['starts_at'])
        self.assertEqual(_time(home['cycle']['ends_at']), second.timestamp())
        history = self.read('cycle-state.json')['purchase_tracking']['home']['history']
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['cycle'], original['cycle'])
        self.assertEqual(_time(history[1]['cycle']['ends_at']), reset.timestamp())


@override_settings(PANEL_LIVE=False, RESOURCE_PLAN_PROJECTION_TEST_ROOT=None)
class DatabaseOwnerScopeTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.identity = {'uid': 0, 'gid': 0, 'groups': [0, 77]}
        self.closes = []
        self.stack.enter_context(patch('bridge.resource_plan_consumer._database_boundary'))
        for method, field in (('geteuid', 'uid'), ('getegid', 'gid'), ('getgroups', 'groups')):
            self.stack.enter_context(patch('bridge.resource_plan_consumer.os.'+method,
                side_effect=lambda field=field: deepcopy(self.identity[field]), create=True))
        for method, field in (('seteuid', 'uid'), ('setegid', 'gid'), ('setgroups', 'groups')):
            self.stack.enter_context(patch('bridge.resource_plan_consumer.os.'+method,
                side_effect=lambda value, field=field: self.identity.update({field: deepcopy(value)}), create=True))
        self.close = self.stack.enter_context(patch('django.db.connections.close_all', side_effect=self.closed))

    def closed(self):
        self.closes.append(deepcopy(self.identity))

    def assert_restored(self):
        self.assertEqual(self.identity, {'uid': 0, 'gid': 0, 'groups': [0, 77]})

    def test_fixed_owner_and_connection_boundaries(self):
        def callback():
            self.assertEqual(self.identity, {'uid': 998, 'gid': 998, 'groups': [998]})
            return {'status': 'recorded'}
        self.assertEqual(run_as_database_owner(callback), {'status': 'recorded'})
        self.assertEqual([item['uid'] for item in self.closes], [0, 998])
        self.assert_restored()

    def test_receipt_exception_closes_as_owner_and_restores_root(self):
        def failed_receipt():
            self.assertEqual(self.identity['uid'], 998)
            raise OSError('simulated_receipt_failure')
        with self.assertRaises(OSError):
            run_as_database_owner(failed_receipt)
        self.assertEqual([item['uid'] for item in self.closes], [0, 998])
        self.assert_restored()

    def test_connection_close_failure_still_restores_all_identity_fields(self):
        def failing_close():
            self.closed()
            if len(self.closes) == 2:
                raise OSError('simulated_close_failure')
        self.close.side_effect = failing_close
        with self.assertRaises(OSError):
            run_as_database_owner(lambda: 'complete')
        self.assert_restored()

    def test_uid_switch_failure_restores_gid_and_groups_without_calling_database(self):
        called = []
        def switch_uid(value):
            if value == 998:
                raise PermissionError('simulated_seteuid_failure')
            self.identity['uid'] = value
        with patch('bridge.resource_plan_consumer.os.seteuid', side_effect=switch_uid, create=True):
            with self.assertRaises(PermissionError):
                run_as_database_owner(lambda: called.append(True))
        self.assertFalse(called)
        self.assert_restored()

    def test_gid_switch_failure_restores_groups_without_calling_database(self):
        called = []
        def switch_gid(value):
            if value == 998:
                raise PermissionError('simulated_setegid_failure')
            self.identity['gid'] = value
        with patch('bridge.resource_plan_consumer.os.setegid', side_effect=switch_gid, create=True):
            with self.assertRaises(PermissionError):
                run_as_database_owner(lambda: called.append(True))
        self.assertFalse(called)
        self.assert_restored()

    def test_live_test_override_is_rejected_before_callback(self):
        called = []
        with override_settings(PANEL_LIVE=True, RESOURCE_PLAN_PROJECTION_TEST_ROOT='not-allowed'):
            with self.assertRaises(ValueError):
                run_as_database_owner(lambda: called.append(True))
        self.assertFalse(called)
        self.assertFalse(self.closes)
