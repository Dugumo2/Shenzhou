"""B7 独立路径反例：登记资料不能证明真实多跳、核心方向或传输能力。"""
from datetime import datetime, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from .line_projection import egress_chain_resources, line_path_projection


class LinePathProjectionTests(SimpleTestCase):
    def setUp(self):
        server = SimpleNamespace(public_id='server-1', enabled=True, name='执行服务器')
        core = SimpleNamespace(public_id='core-1', name='已登记核心', core_type='sing-box',
                               server_id=1, owner_state='verified')
        self.core = core
        self.ingress = SimpleNamespace(pk=1, name='BWH入口', server=server, server_id=1,
                                       protocol='reality', enabled=True, revision=1,
                                       core_instances=SimpleNamespace(all=lambda: [core]),
                                       evidence_source='approved-inventory',
                                       evidence_observed_at=datetime(2026, 10, 17, tzinfo=timezone.utc),
                                       config_ref='SECRET-INGRESS', endpoint_host=None, endpoint_port=None)
        self.egress = SimpleNamespace(pk=1, name='HOME出口', server=server, protocol='ss2022',
                                      fail_closed=True, ownership='external', external_kind='node',
                                      config_ref='SECRET-UPSTREAM', endpoint_host=None, endpoint_port=None)
        self.line = SimpleNamespace(egress=self.egress, all_ingresses=lambda: [self.ingress])

    def observed_chain(self):
        timestamp = datetime(2026, 10, 17, 12, 20, 5, tzinfo=timezone.utc)
        self.core.evidence_source = 'b1-observed'
        self.core.evidence_observed_at = timestamp
        xray = SimpleNamespace(**{**vars(self.core), 'public_id': 'core-2', 'name': '实际 Xray', 'core_type': 'xray'})
        for key, value in {'server_id': 1, 'evidence_source': 'b1-observed', 'evidence_observed_at': timestamp,
                           'core_instance': self.core, 'core_instance_id': 1, 'kind': 'upstream',
                           'ownership': 'self_owned', 'protocol': 'socks', 'external_kind': None,
                           'endpoint_host': '127.0.0.1', 'endpoint_port': 18080}.items():
            setattr(self.egress, key, value)
        external = SimpleNamespace(**{**vars(self.egress), 'pk': 3, 'name': '外部 SS2022', 'ownership': 'external',
                                       'external_kind': 'node', 'protocol': 'ss2022', 'core_instance': xray,
                                       'core_instance_id': 2, 'next_egress': None, 'next_egress_id': None,
                                       'endpoint_host': None, 'endpoint_port': 54097})
        self.egress.next_egress = external
        self.egress.next_egress_id = 3
        return external

    def test_existing_core_and_external_names_do_not_prove_direction_or_hop_protocol(self):
        result = line_path_projection(self.line)
        self.assertEqual(result['kind'], 'registered_association')
        self.assertEqual(result['source'], 'inventory_registration')
        self.assertIsNone(result['observed_at'])
        segment = result['segments'][0]
        self.assertEqual(segment['from']['kind'], 'ingress')
        self.assertEqual(segment['to']['kind'], 'egress')
        self.assertIsNone(segment['core'])
        self.assertIsNone(segment['protocol'])
        self.assertEqual(segment['authentication'], 'unknown')
        self.assertNotIn('SECRET-', str(result))
        # 同一主键的不同资源类型不是环路，也不能把同一服务器包装成两台机器。
        self.assertEqual(segment['id'], 'ingress:1:egress:1')
        self.assertNotIn('server', segment)

    def test_each_capability_unknown_and_fail_closed_is_only_a_requirement(self):
        segment = line_path_projection(self.line)['segments'][0]
        for kind in ('tcp', 'udp', 'dns'):
            self.assertEqual(segment[kind]['state'], 'unknown')
            self.assertEqual(segment[kind]['freshness'], 'unknown')
            self.assertIsNone(segment[kind]['observed_at'])
        self.assertEqual(segment['failure_behavior'], 'fail_closed_required')
        self.egress.fail_closed = False
        self.assertEqual(line_path_projection(self.line)['segments'][0]['failure_behavior'], 'unknown')

    def test_duplicate_primary_ingress_deduplicates_and_extra_ingress_is_a_branch(self):
        other = SimpleNamespace(**{**vars(self.ingress), 'pk': 2, 'name': '另一入口'})
        self.line.all_ingresses = lambda: [self.ingress, self.ingress, other]
        result = line_path_projection(self.line, total_ingresses=3)
        self.assertEqual(len(result['segments']), 2)
        self.assertEqual({row['to']['id'] for row in result['segments']}, {'1'})
        self.assertTrue(any('共登记 3 个' in text for text in result['warnings']))

    def test_no_ingress_never_fabricates_a_multihop_line(self):
        result = line_path_projection(self.line, ingress_nodes=[])
        self.assertEqual(result['segments'], [])
        self.assertTrue(any('不会把单独资源包装' in text for text in result['warnings']))

    def test_core_conflict_remains_explicit_and_preprojected_data_never_queries(self):
        def forbidden():
            raise AssertionError('已有节点投影时不再次读取关系')
        self.line.all_ingresses = forbidden
        nodes = [{'resource_type': 'ingress', 'resource_id': '1', 'name': '入口',
                  'core_refs': {'state': 'conflict', 'items': []}}]
        result = line_path_projection(self.line, ingress_nodes=nodes,
                                      egress_node={'resource_id': '2', 'name': '外部出口'})
        self.assertIsNone(result['segments'][0]['core'])
        self.assertTrue(any('归属存在冲突' in text for text in result['warnings']))

    def test_observed_chain_preserves_old_line_and_distinct_execution_cores(self):
        external = self.observed_chain()
        result = line_path_projection(self.line)
        self.assertIs(self.line.egress, self.egress)
        self.assertEqual(self.line.egress.pk, 1)
        self.assertEqual(result['kind'], 'observed_path')
        self.assertEqual(result['source'], 'b1-observed')
        self.assertEqual(result['terminal_egress']['id'], str(external.pk))
        self.assertEqual(result['terminal_egress']['ownership'], 'external')
        self.assertEqual(result['terminal_egress']['protocol'], 'ss2022')
        self.assertIsNone(result['terminal_egress']['endpoint_host'])
        self.assertEqual([(row['from']['kind'], row['from']['id'], row['to']['kind'], row['to']['id'])
                          for row in result['segments']], [
            ('ingress', '1', 'core', 'core-1'), ('core', 'core-1', 'egress', '1'),
            ('egress', '1', 'core', 'core-2'), ('core', 'core-2', 'egress', '3')])
        self.assertEqual([row['protocol'] for row in result['segments']], ['reality', 'socks', 'socks', 'ss2022'])
        for segment in result['segments']:
            self.assertEqual(segment['relation'], 'observed')
            for kind in ('tcp', 'udp', 'dns'):
                self.assertEqual(segment[kind]['state'], 'unknown')
            self.assertEqual(segment['authentication'], 'unknown')
        self.assertNotIn('SECRET-', str(result))

    def test_multiple_ingresses_share_egress_chain_without_socket_capability_guess(self):
        self.observed_chain()
        hy2 = SimpleNamespace(**{**vars(self.ingress), 'pk': 2, 'protocol': 'hy2', 'endpoint_port': 8443,
                                 'actual_transports': ['tcp', 'udp']})
        ws = SimpleNamespace(**{**vars(hy2), 'pk': 3, 'protocol': 'ws'})
        self.line.all_ingresses = lambda: [self.ingress, hy2, ws, self.ingress]
        result = line_path_projection(self.line)
        self.assertEqual(len(result['segments']), 6)
        self.assertEqual(len([row for row in result['segments'] if row['id'].startswith('handoff:')]), 1)
        self.assertTrue(all(row['tcp']['state'] == row['udp']['state'] == 'unknown' for row in result['segments']))

    def test_missing_evidence_core_conflict_and_internal_terminal_remain_unknown(self):
        external = self.observed_chain()
        external.evidence_source = ''
        result = line_path_projection(self.line)
        self.assertEqual(result['kind'], 'registered_association')
        self.assertIsNone(result['terminal_egress'])
        external.evidence_source = 'b1-observed'; external.core_instance.server_id = 2
        result = line_path_projection(self.line)
        self.assertEqual(result['kind'], 'registered_association')
        self.assertIsNone(result['segments'][0]['core'])
        self.egress.next_egress = None; self.egress.next_egress_id = None
        result = line_path_projection(self.line)
        self.assertEqual(result['kind'], 'registered_association')
        self.assertTrue(any('回环中转缺少后续出口' in warning for warning in result['warnings']))

    def test_cycle_and_limit_never_return_a_truncated_observed_path(self):
        external = self.observed_chain()
        external.next_egress = self.egress; external.next_egress_id = 1
        result = line_path_projection(self.line)
        self.assertEqual(result['kind'], 'registered_association')
        self.assertIsNone(result['terminal_egress'])
        self.assertTrue(any('环路' in warning for warning in result['warnings']))
        root = SimpleNamespace(pk=0, next_egress=None)
        current = root
        for value in range(1, 17):
            current.next_egress = SimpleNamespace(pk=value, next_egress=None)
            current = current.next_egress
        chain, warnings = egress_chain_resources(root)
        self.assertEqual(len(chain), 16)
        self.assertTrue(any('上限' in warning for warning in warnings))

    def test_legacy_direct_path_never_acquires_an_external_hop(self):
        self.observed_chain()
        self.egress.next_egress = None; self.egress.next_egress_id = None
        self.egress.kind = 'direct'; self.egress.protocol = 'direct'
        self.egress.endpoint_host = None; self.egress.endpoint_port = None
        result = line_path_projection(self.line)
        self.assertEqual(result['kind'], 'observed_path')
        self.assertEqual(len(result['segments']), 2)
        self.assertEqual(result['terminal_egress']['id'], '1')
        self.assertEqual(result['terminal_egress']['ownership'], 'self_owned')
        self.assertTrue(any('不代表所有流量无条件' in warning for warning in result['warnings']))
