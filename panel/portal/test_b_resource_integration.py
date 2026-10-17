"""B批公共接线验收：真实API、对象分层、采购来源与检查权限。"""
from datetime import timedelta
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .inventory_purchase import InventoryPurchases
from .models import CoreInstance, Egress, Ingress, Line, Server, ServerObservation
from .observation_ingest import target_fingerprint
from . import inventory_api


@override_settings(PANEL_LIVE=False, ROOT_URLCONF='megabox.urls',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ResourceIntegrationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('b-batch-admin', is_staff=True)
        self.user = User.objects.create_user('b-batch-user')
        self.client.force_login(self.admin)
        self.server = Server.objects.create(name='测试自有服务器', adapter='sing_box', purchase_meter_id='bwg')
        self.core = CoreInstance.objects.create(server=self.server, instance_key='sing-box', name='测试核心',
            core_type='sing_box', configuration_owner='observed', owner_state='observed_only', service_unit='sing-box.service')
        self.ingress = Ingress.objects.create(server=self.server, name='测试入口', protocol='reality', endpoint_port=443)
        self.core.ingresses.add(self.ingress)
        self.egress = Egress.objects.create(server=self.server, name='测试外部出口', kind='upstream',
            ownership='external', external_kind='node', protocol='ss2022', purchase_meter_id='home')
        self.line = Line.objects.create(name='测试既有路径', ingress=self.ingress, egress=self.egress)

    def test_real_api_preserves_owned_and_external_objects_and_counts(self):
        listing = self.client.get('/api/v1/admin/servers').json()['data']['items'][0]
        self.assertEqual((listing['core_count'], listing['node_count']), (1, 1))
        self.assertIsNone(listing['observation'])
        detail = self.client.get('/api/v1/admin/servers/'+str(self.server.public_id)).json()['data']
        self.assertEqual([n['kind'] for n in detail['nodes']['items']], ['owned', 'external'])
        self.assertEqual(detail['cores']['items'][0]['service_unit'], 'sing-box.service')
        self.assertEqual(detail['purchase_meter_state'], 'forbidden')
        self.assertIsNone(detail['purchase_binding_key'])
        self.assertEqual(Server.objects.count(), 1)
        line = self.client.get('/api/v1/admin/lines/'+str(self.line.public_id)).json()['data']
        self.assertEqual(line['line']['path']['kind'], 'registered_association')
        self.assertEqual(len(line['line']['target_version']), 64)
        self.assertEqual(line['nodes']['total'], 2)

    def test_list_exposes_latest_two_sample_rates_without_boot_identifier(self):
        now = timezone.now()
        metrics = {'online': True, 'cpu_percent': 25.0, 'cpu_window_seconds': 60.0,
                   'memory_percent': 50.0, 'disk_percent': 30.0,
                   'network_interfaces': ['eth0'], 'boot_id':'test-boot'}
        for n in range(2):
            ServerObservation.objects.create(server=self.server, source='linux-host-v1', event_id='b-'+str(n),
                observed_at=now-timedelta(seconds=60*(1-n)), expires_at=now+timedelta(seconds=120),
                target_fingerprint=target_fingerprint('server',self.server),
                metrics={**metrics,'sample_monotonic_seconds':1000.0+60*n,'network_rx_bytes':1000+600*n,'network_tx_bytes':2000+1200*n})
        value = self.client.get('/api/v1/admin/servers').json()['data']['items'][0]['observation']
        self.assertEqual(value['rate_state'], 'valid')
        self.assertEqual(value['metrics']['network_rx_bytes_per_second'], 10.0)
        self.assertEqual(value['metrics']['network_tx_bytes_per_second'], 20.0)
        self.assertNotIn('boot_id', value['metrics'])

    def test_purchase_binding_survives_read_failure_but_not_authorization_loss(self):
        purchases = InventoryPurchases(self.admin)
        purchases.sources = [{'source_key':'authorized-source','meters':[{'id':'bwg','scope':'server','quality':'current','details':{'internal':'omitted'}}]}]
        first = purchases.for_resource('bwg','server')
        self.assertEqual(first['purchase_meter_state'], 'ready')
        self.assertEqual(first['purchase_meter']['details'], {})
        purchases.sources = [{'source_key':'authorized-source','meters':[]}]
        failed = purchases.for_resource('bwg','server')
        self.assertEqual(failed['purchase_meter_state'], 'unavailable')
        self.assertEqual(first['purchase_binding_key'], failed['purchase_binding_key'])
        purchases.sources = []
        self.assertEqual(purchases.for_resource('bwg','server')['purchase_meter_state'], 'forbidden')
        self.assertIsNone(purchases.for_resource('bwg','server')['purchase_binding_key'])

    def test_check_api_requires_staff_csrf_and_only_enqueues(self):
        path = '/api/v1/admin/resource-checks'
        entry = {'target_kind':'ingress','target_id':str(self.ingress.pk),'target_version':target_fingerprint('ingress',self.ingress),
                 'action':'tcp','ip':'127.0.0.1','port':443,'allow_loopback':True}
        body = {k:entry[k] for k in ('target_kind','target_id','target_version','action')}
        body['idempotency_key'] = 'b-integration-check'
        with patch('portal.resource_checks.load_policy', return_value=[entry]), patch('portal.resource_checks.execute_fixed') as execute:
            response = self.client.post(path,json.dumps(body),content_type='application/json')
            self.assertEqual(response.status_code,202,response.content)
            job = response.json()['data']
            self.assertEqual(job['state'],'queued')
            self.assertEqual(self.client.get(path+'/'+job['id']).status_code,200)
            execute.assert_not_called()
            client = Client(enforce_csrf_checks=True);client.force_login(self.admin)
            self.assertEqual(client.post(path,json.dumps(body),content_type='application/json').status_code,403)
            self.client.force_login(self.user)
            self.assertEqual(self.client.post(path,json.dumps(body),content_type='application/json').status_code,403)
            self.assertEqual(self.client.get(path+'/'+job['id']).status_code,403)

    def test_check_json_duplicate_fields_and_arbitrary_destination_are_rejected(self):
        path='/api/v1/admin/resource-checks'
        self.assertEqual(self.client.post(path,'{"action":"tcp","action":"other"}',content_type='application/json').status_code,422)
        with patch('portal.resource_checks.execute_fixed') as execute:
            self.assertEqual(self.client.post(path,json.dumps({'url':'https://example.com'}),content_type='application/json').status_code,422)
            execute.assert_not_called()

    def connect_observed_chain(self):
        now = timezone.now()
        for obj in (self.core, self.ingress, self.egress):
            obj.evidence_source = 'isolated-inventory'
            obj.evidence_observed_at = now
            obj.save(update_fields=['evidence_source', 'evidence_observed_at'])
        xray = CoreInstance.objects.create(server=self.server, instance_key='xray', name='测试Xray',
            core_type='xray', evidence_source='isolated-inventory', evidence_observed_at=now)
        self.egress.core_instance = xray
        self.egress.save(update_fields=['core_instance'])
        bridge = Egress.objects.create(server=self.server, name='既有本机中转', kind='upstream',
            ownership='self_owned', protocol='socks', endpoint_host='127.0.0.1', endpoint_port=18080,
            core_instance=self.core, next_egress=self.egress,
            evidence_source='isolated-inventory', evidence_observed_at=now)
        self.line.egress = bridge
        self.line.save(update_fields=['egress'])
        return bridge

    def test_api_keeps_original_intermediate_and_exposes_terminal_without_secret_queries(self):
        bridge = self.connect_observed_chain()
        with CaptureQueriesContext(connection) as capture:
            value = self.client.get('/api/v1/admin/lines/'+str(self.line.public_id)).json()['data']
        self.assertEqual(value['line']['egress']['id'], str(bridge.pk))
        self.assertEqual(value['line']['path']['kind'], 'observed_path')
        self.assertEqual(value['line']['path']['terminal_egress']['id'], str(self.egress.pk))
        self.assertEqual([v['resource_id'] for v in value['nodes']['items'] if v['resource_type']=='egress'],
                         [str(bridge.pk), str(self.egress.pk)])
        for query in capture:
            self.assertNotIn('deployment_lease', query['sql'])
        self.assertNotIn('config_ref', json.dumps(value))

    def test_shared_two_core_chains_do_not_create_per_line_queries(self):
        bridge = self.connect_observed_chain()
        def count():
            request = RequestFactory().get('/api/v1/admin/lines?page_size=25')
            request.user = self.admin
            with CaptureQueriesContext(connection) as capture:
                response = inventory_api.lines(request)
            self.assertEqual(response.status_code, 200)
            return len(capture)
        before = count()
        for index in range(20):
            Line.objects.create(name='共享链'+str(index), ingress=self.ingress, egress=bridge)
        self.assertEqual(count(), before)

    def test_invalid_chain_returns_explicit_incomplete_and_disables_check_version(self):
        bridge = self.connect_observed_chain()
        self.egress.next_egress = bridge
        self.egress.save(update_fields=['next_egress'])
        response = self.client.get('/api/v1/admin/lines/'+str(self.line.public_id))
        self.assertEqual(response.status_code, 200)
        value = response.json()['data']
        self.assertIsNone(value['line']['target_version'])
        self.assertTrue(value['nodes']['truncated'])
        self.assertIsNone(value['line']['path']['terminal_egress'])
        self.assertEqual(value['check_actions'], [])
