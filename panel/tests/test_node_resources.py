"""节点白名单与资源边界：只用隔离库，绝不读取实际订阅或凭据。"""
import json
from copy import deepcopy
from django.test import TestCase

from portal.models import CoreInstance, Egress, Ingress, Line, Server
from portal.node_resources import project_egress, project_ingress


def purchase_meter():
    return {'id': 'home', 'label': 'HOME', 'scope': 'external_node', 'source_kind': 'estimate',
            'quality': 'current', 'quota_bytes': '400000000000', 'used_bytes': '100000000000',
            'remaining_bytes': '300000000000', 'upload_bytes': '20000000000', 'download_bytes': '80000000000',
            'observed_at': '2026-10-17T10:00:00+08:00', 'expires_at': '2026-10-17T10:03:00+08:00',
            'expires_on': '2027-01-19', 'cycle': {'kind': 'fixed_days',
                'starts_at': '2026-09-20T00:00:00+08:00', 'ends_at': '2026-10-20T00:00:00+08:00',
                'next_reset_at': '2026-10-20T00:00:00+08:00'}, 'alerts': [], 'details': {}}


class NodeResourcesTests(TestCase):
    def setUp(self):
        self.server = Server.objects.create(name='执行机器')
        self.core = CoreInstance.objects.create(server=self.server, instance_key='xray', name='已有核心',
                                                core_type='xray', configuration_owner='fixture-owner')
        self.ingress = Ingress.objects.create(server=self.server, name='入口', protocol='reality',
                                             config_ref='PRIVATE-CONFIG-REF')
        self.core.ingresses.add(self.ingress)
        self.egress = Egress.objects.create(server=self.server, name='HOME', kind='upstream',
                                           config_ref='PRIVATE-UPSTREAM')
        self.egress.ownership = 'external'
        self.egress.external_kind = 'node'
        self.egress.protocol = 'ss2022'
        self.egress.purchase_meter_id = 'home'

    def test_four_protocols_keep_actual_core_and_one_resource_per_endpoint(self):
        for protocol, label in [('reality', 'Reality'), ('hy2', 'Hysteria 2'), ('anytls', 'AnyTLS'), ('ws', 'WebSocket')]:
            self.ingress.protocol = protocol
            row = project_ingress(self.ingress)
            self.assertEqual(row['protocol_label'], label)
            self.assertEqual(row['core_refs']['state'], 'linked')
            self.assertEqual(row['core_refs']['items'][0]['id'], str(self.core.public_id))
            self.assertEqual(row['id'], f'ingress:{self.ingress.pk}')
            self.assertFalse(any(row['capabilities'].values()))

    def test_unknown_multiple_and_cross_server_cores_are_never_guessed(self):
        self.core.ingresses.clear()
        self.assertEqual(project_ingress(self.ingress)['core_refs']['state'], 'unknown')
        self.core.ingresses.add(self.ingress)
        second = CoreInstance.objects.create(server=self.server, instance_key='other', name='第二核心',
                                             core_type='sing_box', configuration_owner='fixture-owner')
        second.ingresses.add(self.ingress)
        self.assertEqual(project_ingress(self.ingress)['core_refs']['state'], 'conflict')
        self.core.ingresses.clear()
        second.server = Server.objects.create(name='另一机器')
        second.save()
        self.assertEqual(project_ingress(self.ingress)['core_refs']['state'], 'conflict')

    def test_external_purchase_does_not_create_owned_server_or_control(self):
        before = Server.objects.count()
        row = project_egress(self.egress, purchase_meter())
        self.assertEqual(Server.objects.count(), before)
        self.assertEqual(row['kind'], 'external')
        self.assertEqual(row['external_kind'], 'node')
        self.assertEqual(row['execution_server']['id'], str(self.server.public_id))
        self.assertEqual(row['purchase_meter']['quota_bytes'], '400000000000')
        self.assertEqual(row['purchase_meter']['expires_on'], '2027-01-19')
        self.assertEqual(row['purchase_meter']['cycle'], purchase_meter()['cycle'])
        self.assertIsNone(row['enabled'])
        self.assertFalse(any(row['capabilities'].values()))

    def test_meter_mismatch_unknown_ownership_and_display_name_do_not_bind(self):
        meter = purchase_meter()
        meter['id'] = 'bwg'
        self.assertIsNone(project_egress(self.egress, meter)['purchase_meter'])
        meter = purchase_meter()
        meter['scope'] = 'server'
        self.assertIsNone(project_egress(self.egress, meter)['purchase_meter'])
        self.egress.purchase_meter_id = None
        self.assertIsNone(project_egress(self.egress, purchase_meter())['purchase_meter'])
        self.egress.ownership = 'unknown'
        self.assertEqual(project_egress(self.egress)['kind'], 'unknown')

    def test_no_credentials_uri_or_nested_extra_fields_are_projected(self):
        self.ingress.endpoint_host = 'vless://private-credential@secret.example'
        self.ingress.endpoint_port = 65536
        self.ingress.evidence_source = 'https://secret.example/?token=PRIVATE'
        self.ingress.password = 'PRIVATE-PASSWORD'
        self.ingress.credential_ref = 'PRIVATE-UUID'
        meter = purchase_meter()
        meter['password'] = 'PRIVATE-PASSWORD'
        meter['details'] = {'node_uri': 'ss://PRIVATE@secret.example', 'nested': {'token': 'PRIVATE'}}
        meter['cycle']['password'] = 'PRIVATE'
        meter['alerts'] = [{'code': 'source_error', 'severity': 'error', 'message': 'token=PRIVATE', 'uri': 'PRIVATE'}]
        payload = json.dumps([project_ingress(self.ingress), project_egress(self.egress, meter)])
        for forbidden in ('PRIVATE', 'config_ref', 'credential_ref', 'password', 'token=', 'ss://', 'vless://'):
            self.assertNotIn(forbidden, payload)
        self.assertEqual(project_ingress(self.ingress)['endpoint'], {'host': None, 'port': None})

    def test_source_is_not_output_subscription_and_projection_is_read_only(self):
        self.egress.external_kind = 'subscription'
        meter = purchase_meter()
        original = deepcopy(meter)
        with self.assertNumQueries(0):
            row = project_egress(self.egress, meter)
        self.assertEqual(row['external_kind'], 'subscription')
        self.assertFalse(row['capabilities']['grant'])
        self.assertFalse(row['capabilities']['sync_source'])
        self.assertTrue(any('用户输出订阅' in text for text in row['limitations']))
        self.assertEqual(meter, original)
        self.egress.name = '已改别名'
        renamed = project_egress(self.egress, meter)
        self.assertEqual(row['purchase_meter'], renamed['purchase_meter'])
        self.assertEqual(row['id'], renamed['id'])
        self.assertNotIn('multiplier', renamed)

    def test_endpoint_retains_valid_ipv4_ipv6_and_hostname_but_not_url(self):
        for host in ('192.0.2.1', '2001:db8::1', 'node.example.com'):
            self.egress.endpoint_host = host
            self.egress.endpoint_port = 443
            self.assertEqual(project_egress(self.egress)['endpoint'], {'host': host, 'port': 443})

    def test_socks_and_external_resource_keep_distinct_cores_and_original_line(self):
        # 模拟真实结构，不把中转出口改成外部节点，不读取现场配置。
        self.egress.name = 'HOME 原中转'
        self.egress.protocol = 'socks'
        self.egress.ownership = 'self_owned'
        self.egress.external_kind = None
        self.egress.purchase_meter_id = None
        self.egress.core_instance = self.core
        self.egress.endpoint_host = '127.0.0.1'
        self.egress.endpoint_port = 18080
        xray = CoreInstance.objects.create(server=self.server, instance_key='relay-xray', name='Xray 出站核心',
                                           core_type='xray', configuration_owner='fixture-owner')
        external = Egress.objects.create(server=self.server, name='HOME 外部 SS2022', kind='upstream',
                                        ownership='external', external_kind='node', protocol='ss2022',
                                        core_instance=xray, purchase_meter_id='home', endpoint_port=54097)
        self.egress.next_egress = external
        self.egress.save()
        line = Line.objects.create(name='原 HOME 线路', ingress=self.ingress, egress=self.egress)
        head = project_egress(self.egress, purchase_meter())
        tail = project_egress(external, purchase_meter())
        self.assertEqual(head['kind'], 'owned')
        self.assertEqual(head['protocol_label'], 'SOCKS 中转')
        self.assertEqual(head['core_refs']['items'][0]['id'], str(self.core.public_id))
        self.assertEqual(head['next_resource'], {'id': f'egress:{external.pk}', 'resource_id': str(external.pk),
                                                 'name': external.name})
        self.assertEqual(tail['core_refs']['items'][0]['id'], str(xray.public_id))
        self.assertEqual(tail['core_refs']['state'], 'linked')
        self.assertIsNone(head['purchase_meter'])
        self.assertEqual(tail['purchase_meter']['id'], 'home')
        self.assertFalse(any(tail['capabilities'].values()))
        line.refresh_from_db()
        self.assertEqual(line.egress_id, self.egress.pk)

    def test_egress_cross_server_core_is_conflict_and_next_reference_is_not_recursive(self):
        other_server = Server.objects.create(name='另一执行机器')
        self.core.server = other_server
        self.core.save()
        self.egress.core_instance = self.core
        self.egress.next_egress = self.egress
        row = project_egress(self.egress)
        self.assertEqual(row['core_refs']['state'], 'conflict')
        self.assertEqual(set(row['next_resource']), {'id', 'resource_id', 'name'})
        self.assertTrue(any('指向自身' in text for text in row['limitations']))
        self.assertFalse(any(row['capabilities'].values()))
