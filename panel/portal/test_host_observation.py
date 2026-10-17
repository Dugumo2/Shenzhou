"""B4 隔离样本测试；不接网络、不读取生产主机。"""
import math
import json
import shutil
import uuid
from contextlib import nullcontext
from datetime import timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.models import Prefetch
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .host_observer import HostSampler
from .models import CoreInstance, Egress, Ingress, Line, Server, ServerObservation
from .observation_ingest import (METRICS, ObservationError, ingest_observation,
                                 freshness, observation_projection, target_fingerprint, target_key)

BOOT = '12345678-1234-1234-1234-123456789012'
OTHER_BOOT = '12345678-1234-1234-1234-123456789013'


class SamplerTests(SimpleTestCase):
    def setUp(self):
        fixture_root = (settings.DATA_ROOT / 'b4-fixtures').resolve()
        self.root = fixture_root / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        def cleanup():
            if self.root.resolve().parent != fixture_root:
                raise ValueError('fixture_path_changed')
            shutil.rmtree(self.root)
        self.addCleanup(cleanup)
        self.proc, self.sys = self.root / 'proc', self.root / 'sys'
        self.stamp = 1000.0
        self.write(self.proc / 'sys/kernel/random/boot_id', BOOT)
        self.write(self.proc / 'stat', 'cpu 100 0 100 800 0 0 0 0 90 0\n')
        self.write(self.proc / 'meminfo', 'MemTotal: 1024 kB\nMemAvailable: 256 kB\n')
        self.nic('eth0', 1000, 2000)
        self.sampler = HostSampler(['eth0'], proc_root=self.proc, sys_root=self.sys,
                                   monotonic=lambda: self.stamp)
        disk = patch.object(self.sampler, '_disk', return_value=25.0)
        disk.start()
        self.addCleanup(disk.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding='ascii')

    def nic(self, name, rx, tx, *, physical=True, index='2', link='2', kind='1'):
        root = self.sys / 'class/net' / name
        if physical:
            (root / 'device').mkdir(parents=True, exist_ok=True)
        for file, value in {'type': kind, 'ifindex': index, 'iflink': link,
                            'statistics/rx_bytes': str(rx), 'statistics/tx_bytes': str(tx)}.items():
            self.write(root / file, value)

    def test_three_rounds_cpu_memory_and_real_counter_scope(self):
        _, first = self.sampler.collect()
        self.assertIsNone(first['cpu_percent'])
        self.assertEqual(first['memory_percent'], 75)
        self.assertEqual(first['disk_percent'], 25)
        for turn in (1, 2):
            self.stamp += 60
            self.write(self.proc / 'stat', f'cpu {100 + turn * 50} 0 {100 + turn * 50} {800 + turn * 100} 0 0 0 0 90 0\n')
            self.nic('eth0', 1000 + turn * 6000, 2000 + turn * 12000)
            _, sample = self.sampler.collect()
            self.assertEqual(sample['cpu_percent'], 50)
            self.assertEqual(sample['cpu_window_seconds'], 60)
            self.assertEqual(sample['network_rx_bytes'], 1000 + turn * 6000)
        self.assertEqual(sample['network_interfaces'], ['eth0'])

    def test_virtual_interfaces_are_not_counted(self):
        for name, properties in [('lo', {'physical': False, 'kind': '772'}),
                                 ('tun0', {'physical': False, 'kind': '65534'}),
                                 ('sit0', {'physical': False, 'kind': '776'}),
                                 ('veth0', {'index': '3', 'link': '4'}),
                                 ('br0', {'physical': False})]:
            with self.subTest(name=name):
                self.nic(name, 999999, 999999, **properties)
                sampler = HostSampler([name], proc_root=self.proc, sys_root=self.sys)
                with patch.object(sampler, '_disk', return_value=None):
                    _, sample = sampler.collect()
                self.assertIsNone(sample['network_rx_bytes'])
                self.assertIsNone(sample['network_interfaces'])

    def test_missing_one_selected_nic_never_silently_shrinks_scope(self):
        self.sampler.interfaces.append('missing0')
        _, sample = self.sampler.collect()
        self.assertIsNone(sample['network_rx_bytes'])

    def test_cpu_boot_reset_missing_and_gap(self):
        self.sampler.collect()
        for boot, stamp, cpu in [(OTHER_BOOT, 1060, 'cpu 1 0 1 2 0'),
                                 (OTHER_BOOT, 1120, 'cpu 0 0 0 1 0'),
                                 (OTHER_BOOT, 1400, 'cpu 10 0 10 20 0')]:
            self.write(self.proc / 'sys/kernel/random/boot_id', boot)
            self.write(self.proc / 'stat', cpu)
            self.stamp = stamp
            _, sample = self.sampler.collect()
            self.assertIsNone(sample['cpu_percent'])
        (self.proc / 'meminfo').unlink()
        self.assertIsNone(self.sampler.collect()[1]['memory_percent'])

    def test_per_interface_reset_cannot_hide_behind_other_growth(self):
        self.nic('eth1', 9000, 9000)
        self.sampler.interfaces.append('eth1')
        self.sampler.collect()
        self.nic('eth0', 100000, 200000)
        self.nic('eth1', 1, 1)
        self.assertIsNone(self.sampler.collect()[1]['network_rx_bytes'])

    def test_bounded_reads_invalid_input_and_timeout_propagate(self):
        self.write(self.proc / 'meminfo', 'x' * 65537)
        self.assertIsNone(self.sampler.collect()[1]['memory_percent'])
        for interfaces in ([], ['../secret'], ['.'], ['eth0', 'eth0']):
            with self.assertRaises(ValueError):
                HostSampler(interfaces)
        with patch.object(self.sampler, '_cpu', side_effect=TimeoutError), self.assertRaises(TimeoutError):
            self.sampler.collect()


class ObservationFixtureMixin:
    def setUp(self):
        self.server = Server.objects.create(name='隔离B4主机', adapter='xray')
        self.now = timezone.now() - timedelta(seconds=1)
        self.policy = {'linux-host-v1': {'targets': [target_key('server', self.server)], 'check_kinds': [],
                                        'metrics': list(METRICS), 'max_ttl_seconds': 180}}

    def payload(self, turn=0, **changes):
        observed = self.now - timedelta(seconds=60 * (2 - turn))
        metrics = {'online': True, 'boot_id': BOOT, 'sample_monotonic_seconds': 1000 + turn * 60,
                   'network_rx_bytes': 1000 + turn * 6000, 'network_tx_bytes': 2000 + turn * 12000,
                   'network_interfaces': ['eth0']}
        metrics.update(changes.pop('metrics', {}))
        return {'source': 'linux-host-v1', 'event_id': f'event-{turn}', 'target_kind': 'server',
                'target_id': str(self.server.public_id), 'target_fingerprint': target_fingerprint('server', self.server),
                'observed_at': observed.isoformat(), 'expires_at': (observed + timedelta(seconds=180)).isoformat(),
                'metrics': metrics, **changes}

    def ingest(self, turn=0, **changes):
        return ingest_observation(self.payload(turn, **changes), trusted_sources=self.policy)[0]

    def project(self, row, previous=None, **kwargs):
        return observation_projection(row, self.server, previous, **kwargs)


class ProjectionTests(ObservationFixtureMixin, TestCase):

    def test_three_samples_expiry_and_recovery(self):
        rows = [self.ingest(turn) for turn in range(3)]
        self.assertEqual(self.project(rows[0])['rate_state'], 'unavailable')
        for index in (1, 2):
            view = self.project(rows[index], rows[index - 1])
            self.assertEqual(view['rate_state'], 'valid')
            self.assertEqual(view['metrics']['network_rx_bytes_per_second'], 100)
            self.assertEqual(view['metrics']['network_tx_bytes_per_second'], 200)
        expired = self.project(rows[2], rows[1], now=self.now + timedelta(seconds=181))
        self.assertEqual(expired['freshness'], 'stale')
        self.assertIsNone(expired['metrics']['network_rx_bytes_per_second'])
        with patch('portal.observation_ingest.timezone.now', return_value=self.now + timedelta(seconds=360)):
            resumed = self.ingest(7)
            self.assertEqual(self.project(resumed, rows[2])['rate_state'], 'gap')
            recovered = self.ingest(8)
            self.assertEqual(self.project(recovered, resumed)['rate_state'], 'valid')

    def test_generation_scope_counter_missing_and_monotonic_failures(self):
        before = self.ingest()
        cases = [({'boot_id': OTHER_BOOT}, 'generation_changed'),
                 ({'network_interfaces': ['eth1']}, 'scope_changed'),
                 ({'network_rx_bytes': 10}, 'counter_reset'), ({'boot_id': None}, 'unavailable'),
                 ({'network_tx_bytes': None}, 'unavailable'), ({'sample_monotonic_seconds': 999}, 'gap'),
                 ({'online': False}, 'unavailable')]
        for index, (metrics, expected) in enumerate(cases):
            row = self.ingest(1, metrics=metrics, event_id=f'case-{index}')
            self.assertEqual(self.project(row, before)['rate_state'], expected)

    def test_future_invalid_and_replay_conflict(self):
        payload = self.payload()
        first, created = ingest_observation(payload, trusted_sources=self.policy)
        replay, created = ingest_observation(payload, trusted_sources=self.policy)
        self.assertFalse(created)
        self.assertEqual(first.pk, replay.pk)
        for change in ({'observed_at': (self.now + timedelta(days=1)).isoformat()},
                       {'metrics': {'boot_id': 'bad'}}, {'metrics': {'sample_monotonic_seconds': math.nan}},
                       {'metrics': {'network_rx_bytes_per_second': 12}}):
            with self.assertRaises(ObservationError):
                self.ingest(1, **change)
        with self.assertRaises(ObservationError):
            self.ingest(0, metrics={'network_rx_bytes': 9999})

    def test_pre_upgrade_sample_replay_keeps_original_json_shape(self):
        payload = self.payload()
        del payload['metrics']['boot_id']
        del payload['metrics']['sample_monotonic_seconds']
        row, _ = ingest_observation(payload, trusted_sources=self.policy)
        self.assertNotIn('boot_id', row.metrics)
        replay, created = ingest_observation(payload, trusted_sources=self.policy)
        self.assertFalse(created)
        self.assertEqual(replay.pk, row.pk)

    def test_target_change_and_late_sample_are_not_fresh_rates(self):
        before, row = self.ingest(0), self.ingest(1)
        before.received_at = before.expires_at + timedelta(seconds=1)
        self.assertEqual(self.project(row, before)['rate_state'], 'gap')
        self.server.enabled = False
        self.assertEqual(self.project(row, before)['freshness'], 'target_changed')

    def test_projection_explicit_previous_has_no_queries(self):
        before, row = self.ingest(0), self.ingest(1)
        with self.assertNumQueries(0):
            projection = self.project(row, before)
            self.project(row)
        self.assertNotIn('boot_id', projection['metrics'])
        self.assertNotIn('sample_monotonic_seconds', projection['metrics'])
        with self.assertNumQueries(1):
            self.assertEqual(observation_projection(row, self.server)['rate_state'], 'valid')

    def test_late_insertion_does_not_replace_latest_and_missing_breaks_adjacency(self):
        current = self.ingest(2)
        self.ingest(0)
        missing = self.ingest(1, metrics={'network_rx_bytes': None})
        self.assertEqual(ServerObservation.objects.order_by('-observed_at', '-pk').first().pk, current.pk)
        self.assertEqual(observation_projection(current, self.server)['rate_state'], 'unavailable')
        self.assertEqual(self.project(current, missing)['metrics']['network_rx_bytes'], 13000)

    def test_endpoint_and_unit_change_version_metadata_does_not(self):
        ingress = Ingress.objects.create(server=self.server, name='入口', protocol='ws')
        core = CoreInstance.objects.create(server=self.server, name='核心', core_type='xray', instance_key='b4')
        core.ingresses.add(ingress)
        original = target_fingerprint('ingress', ingress)
        ingress.name, ingress.notes = '别名', '说明'
        self.assertEqual(target_fingerprint('ingress', ingress), original)
        ingress.endpoint_host = 'node.example.invalid'
        self.assertNotEqual(target_fingerprint('ingress', ingress), original)
        original_core = target_fingerprint('core', core)
        core.service_unit = 'approved-core.service'
        self.assertNotEqual(target_fingerprint('core', core), original_core)

    def test_fingerprint_uses_prefetch_but_not_truncated_display_scope(self):
        ingress = Ingress.objects.create(server=self.server, name='入口', protocol='ws')
        extra = Ingress.objects.create(server=self.server, name='另一入口', protocol='hy2')
        core = CoreInstance.objects.create(server=self.server, name='核心', core_type='xray', instance_key='b4')
        core.ingresses.add(ingress)
        expected = target_fingerprint('ingress', ingress)
        loaded = Ingress.objects.select_related('server').prefetch_related(Prefetch(
            'core_instances', queryset=CoreInstance.objects.select_related('server').prefetch_related('ingresses'))).get(pk=ingress.pk)
        with self.assertNumQueries(0):
            self.assertEqual(target_fingerprint('ingress', loaded), expected)
        egress = Egress.objects.create(server=self.server, name='出口', kind='direct')
        line = Line.objects.create(name='线路', ingress=ingress, egress=egress)
        line.additional_ingresses.add(extra)
        expected_line = target_fingerprint('line', line)
        line.registered_additional_ingresses = []
        self.assertEqual(target_fingerprint('line', line), expected_line)
        line.registered_additional_ingresses = [extra, ingress, extra]
        line.registered_additional_ingresses_complete = True
        self.assertEqual(target_fingerprint('line', line), expected_line)

    def test_retention_is_scoped_and_command_rejects_non_linux(self):
        from .management.commands.observe_host import prune_samples
        self.ingest(0)
        other = self.ingest(1, event_id='keep')
        ServerObservation.objects.filter(pk=other.pk).update(source='another-trusted-source')
        with patch('portal.management.commands.observe_host.RETAIN_SAMPLES', 0):
            prune_samples(self.server)
        self.assertEqual(list(ServerObservation.objects.values_list('source', flat=True)), ['another-trusted-source'])
        with patch('portal.management.commands.observe_host.sys.platform', 'win32'), self.assertRaises(CommandError):
            call_command('observe_host', server_id=str(self.server.public_id), fingerprint='0' * 64, interface=['eth0'])


class CommandTests(ObservationFixtureMixin, TransactionTestCase):
    """命令会真实回收数据库连接，不能包在TestCase的外层事务中。"""

    def test_sample_command_three_cycles_failure_recovers_with_bounded_status(self):
        from .management.commands.observe_host import Command
        sampler = Mock()
        sampler.previous = {'old': 'state'}
        sampler.collect.side_effect = [TimeoutError(),
                                       (self.now - timedelta(seconds=60), self.payload(1)['metrics']),
                                       (self.now, self.payload(2)['metrics'])]
        output = StringIO()
        command = Command(stdout=output)
        fake_resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_maxrss=12345))
        with patch.dict('sys.modules', resource=fake_resource), \
                patch('portal.management.commands.observe_host.collection_deadline', return_value=nullcontext()), \
                patch('portal.management.commands.observe_host.time.sleep') as sleep:
            command.run_samples(self.server, sampler, target_fingerprint('server', self.server), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIsNone(sampler.previous)
        self.assertEqual(ServerObservation.objects.count(), 2)
        statuses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([row['status'] for row in statuses], ['collection_failed', 'stored', 'stored'])
        self.assertEqual(set(statuses[0]), {'status', 'cycle', 'elapsed_ms', 'cpu_ms', 'max_rss_kib'})

    def test_sample_command_stops_when_pinned_target_changes(self):
        from .management.commands.observe_host import Command
        sampler = Mock()
        sampler.collect.return_value = (self.now, self.payload(2)['metrics'])
        fake_resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_maxrss=1))
        with patch.dict('sys.modules', resource=fake_resource), \
                patch('portal.management.commands.observe_host.collection_deadline', return_value=nullcontext()), \
                self.assertRaises(CommandError):
            Command(stdout=StringIO()).run_samples(self.server, sampler, '0' * 64, 3)
        self.assertFalse(ServerObservation.objects.exists())


class EgressChainFingerprintTests(TestCase):
    """完整已登记出口链参与指纹，非法或截断链不生成可检查版本。"""

    def setUp(self):
        self.server = Server.objects.create(name='链指纹隔离主机')
        self.core = CoreInstance.objects.create(server=self.server, name='执行核心', core_type='xray',
                                               instance_key='chain-core', configuration_owner='operator')
        self.external = Egress.objects.create(server=self.server, name='外部节点', kind='residential',
                                               endpoint_host='upstream.example.invalid', endpoint_port=443,
                                               ownership='external', external_kind='node')
        self.middle = Egress.objects.create(server=self.server, name='本地SOCKS', kind='direct',
                                            endpoint_host='127.0.0.1', endpoint_port=28003,
                                            core_instance=self.core, next_egress=self.external,
                                            ownership='self_owned')
        self.root = Egress.objects.create(server=self.server, name='入口出口', kind='direct',
                                          next_egress=self.middle, ownership='self_owned')

    def loaded_root(self):
        return Egress.objects.select_related('server', 'core_instance__server').prefetch_related(
            'core_instance__ingresses').get(pk=self.root.pk)

    def fingerprint(self):
        return target_fingerprint('egress', self.loaded_root())

    def receipt(self):
        now = timezone.now()
        return SimpleNamespace(target_fingerprint=self.fingerprint(), observed_at=now,
                               expires_at=now + timedelta(seconds=180))

    def test_each_intermediate_target_configuration_and_owner_change_expires_receipt(self):
        changes = [(self.middle, 'endpoint_port', 28004), (self.external, 'config_ref', 'revised-reference'),
                   (self.middle, 'ownership', 'unknown'), (self.core, 'configuration_version', 'v2'),
                   (self.core, 'configuration_owner', 'new-owner'), (self.core, 'owner_state', 'managed')]
        for target, field, value in changes:
            with self.subTest(field=field):
                receipt = self.receipt()
                setattr(target, field, value)
                target.save(update_fields=[field])
                self.assertEqual(freshness(receipt, 'egress', self.loaded_root()), 'target_changed')

    def test_middle_metadata_does_not_expire_but_rewire_and_core_replace_do(self):
        receipt = self.receipt()
        self.middle.name = '新别名'
        self.middle.save(update_fields=['name'])
        self.assertEqual(freshness(receipt, 'egress', self.loaded_root()), 'fresh')
        replacement = CoreInstance.objects.create(server=self.server, name='另一个核心', core_type='xray', instance_key='other')
        self.middle.core_instance = replacement
        self.middle.save(update_fields=['core_instance'])
        self.assertEqual(freshness(receipt, 'egress', self.loaded_root()), 'target_changed')
        receipt = self.receipt()
        self.middle.next_egress = None
        self.middle.save(update_fields=['next_egress'])
        self.assertEqual(freshness(receipt, 'egress', self.loaded_root()), 'target_changed')

    def test_full_chain_changes_line_fingerprint_and_ignores_display_truncation(self):
        ingress = Ingress.objects.create(server=self.server, name='入口', protocol='ws')
        line = Line.objects.create(name='完整链', ingress=ingress, egress=self.root)
        before = target_fingerprint('line', line)
        line.registered_egress_chain = [self.root]
        self.assertEqual(target_fingerprint('line', line), before)
        self.external.endpoint_host = 'replacement.example.invalid'
        self.external.save(update_fields=['endpoint_host'])
        loaded = Line.objects.select_related('egress__server', 'ingress__server').get(pk=line.pk)
        self.assertNotEqual(target_fingerprint('line', loaded), before)

    def test_prefetched_complete_chain_needs_no_extra_queries(self):
        expected = self.fingerprint()
        prefixes = ['', 'next_egress__', 'next_egress__next_egress__']
        loaded = Egress.objects.select_related(*[
            prefix + suffix for prefix in prefixes for suffix in ('server', 'core_instance__server')
        ]).prefetch_related(*[prefix + 'core_instance__ingresses' for prefix in prefixes]).get(pk=self.root.pk)
        with self.assertNumQueries(0):
            self.assertEqual(target_fingerprint('egress', loaded), expected)

    def test_cycle_rejected_and_previous_receipt_never_fresh(self):
        receipt = self.receipt()
        self.external.next_egress = self.root
        self.external.save(update_fields=['next_egress'])
        with self.assertRaisesRegex(ObservationError, '^egress_chain_cycle$'):
            self.fingerprint()
        self.assertEqual(freshness(receipt, 'egress', self.loaded_root()), 'target_changed')

    def test_sixteen_segments_allowed_seventeen_rejected_with_bounded_queries(self):
        tail = Egress.objects.create(server=self.server, name='末端', kind='direct')
        for index in range(15):
            tail = Egress.objects.create(server=self.server, name=f'段{index}', kind='direct', next_egress=tail)
        loaded = Egress.objects.select_related('server').get(pk=tail.pk)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(len(target_fingerprint('egress', loaded)), 64)
        self.assertLessEqual(len(queries), 15)
        head = Egress.objects.create(server=self.server, name='第17段', kind='direct', next_egress=tail)
        loaded = Egress.objects.select_related('server').get(pk=head.pk)
        with CaptureQueriesContext(connection) as queries, \
                self.assertRaisesRegex(ObservationError, '^egress_chain_too_deep$'):
            target_fingerprint('egress', loaded)
        self.assertLessEqual(len(queries), 15)

    def test_plain_exit_keeps_legacy_identity_without_chain(self):
        from .inventory_service import digest
        plain = Egress.objects.create(server=self.server, name='普通出口', kind='direct')
        expected = digest({'kind': 'egress', 'id': target_key('egress', plain),
                           'server': {'kind': 'server', 'id': target_key('server', self.server),
                                      'adapter': self.server.adapter, 'enabled': self.server.enabled},
                           'reference_digest': digest(plain.config_ref), 'endpoint_host': plain.endpoint_host,
                           'endpoint_port': plain.endpoint_port, 'type': plain.kind, 'fail_closed': plain.fail_closed})
        self.assertEqual(target_fingerprint('egress', plain), expected)
