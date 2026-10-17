"""B9独立API反例；仅合成两核关系、内存SQLite与固定检测模拟。"""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from portal import inventory_api
from portal.models import CheckResult, CoreInstance, Egress, Ingress, Line, P8SourceBinding, ResourceCheckJob, Server
from portal.observation_ingest import target_fingerprint


@override_settings(PANEL_LIVE=False, ROOT_URLCONF='megabox.urls', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class IndependentReview(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('independent-review-admin', is_staff=True)
        self.viewer = User.objects.create_user('independent-review-viewer')
        self.client.force_login(self.admin)
        now = timezone.now()
        self.server = Server.objects.create(name='合成BWH', adapter='sing_box', purchase_meter_id='bwg')
        evidence = dict(evidence_source='isolated-review', evidence_observed_at=now)
        self.sing = CoreInstance.objects.create(server=self.server, name='合成sing-box', instance_key='sing', core_type='sing_box', owner_state='observed_only', service_unit='sing-box.service', **evidence)
        self.xray = CoreInstance.objects.create(server=self.server, name='合成Xray', instance_key='xray', core_type='xray', owner_state='observed_only', service_unit='relay-xray.service', **evidence)
        self.ingress = Ingress.objects.create(server=self.server, name='合成入口', protocol='reality', config_ref='PRIVATE_CONFIG_REF', endpoint_port=443, **evidence)
        self.sing.ingresses.add(self.ingress)
        self.external = Egress.objects.create(server=self.server, name='合成外部HOME', kind='upstream', ownership='external', external_kind='node', protocol='ss2022', endpoint_port=54097, purchase_meter_id='home', core_instance=self.xray, **evidence)
        self.bridge = Egress.objects.create(server=self.server, name='原HOME中转', kind='upstream', ownership='self_owned', protocol='socks', endpoint_host='127.0.0.1', endpoint_port=18080, core_instance=self.sing, next_egress=self.external, **evidence)
        self.line = Line.objects.create(name='原HOME', ingress=self.ingress, egress=self.bridge)

    def call(self, handler, path, *args):
        request = RequestFactory().get(path)
        request.user = self.admin
        with patch('portal.resource_checks.load_policy', return_value=[]), CaptureQueriesContext(connection) as capture:
            response = handler(request, *args)
        self.assertEqual(response.status_code, 200, response.content)
        return json.loads(response.content)['data'], list(capture)

    def old_result(self, **target):
        now = timezone.now()
        return CheckResult.objects.create(**target, source='review', event_id='old-'+str(CheckResult.objects.count()), check_kind='whole_path', observed_at=now, expires_at=now+timedelta(minutes=5), target_fingerprint='a'*64, result='pass')

    def test_home_public_api_keeps_original_bridge_and_external(self):
        data, queries = self.call(inventory_api.line_detail, '/', str(self.line.public_id))
        self.assertEqual(data['line']['egress']['id'], str(self.bridge.pk))
        self.assertEqual(data['line']['path']['terminal_egress']['id'], str(self.external.pk))
        self.assertEqual(data['line']['path']['kind'], 'observed_path')
        self.assertEqual([n['resource_id'] for n in data['nodes']['items'] if n['resource_type']=='egress'], [str(self.bridge.pk), str(self.external.pk)])
        self.assertNotIn('PRIVATE_CONFIG_REF', json.dumps(data))
        self.assertFalse(any('deployment_lease' in q['sql'] for q in queries))

    def test_whole_path_results_keep_list_query_count_constant(self):
        self.old_result(line=self.line)
        _, first = self.call(inventory_api.lines, '/?page_size=25')
        for index in range(10):
            line = Line.objects.create(name='共享HOME'+str(index), ingress=self.ingress, egress=self.bridge)
            self.old_result(line=line)
        _, many = self.call(inventory_api.lines, '/?page_size=25')
        self.assertEqual(len(first), len(many))

    def test_existing_check_history_does_not_read_server_lease(self):
        self.old_result(egress=self.external)
        _, queries = self.call(inventory_api.line_detail, '/', str(self.line.public_id))
        bad = [q['sql'] for q in queries if 'deployment_lease' in q['sql']]
        self.assertEqual(bad, [])

    def test_downstream_protocol_change_invalidates_version(self):
        before = target_fingerprint('line', self.line)
        self.external.protocol = 'socks'
        self.external.save(update_fields=['protocol'])
        after = target_fingerprint('line', Line.objects.get(pk=self.line.pk))
        self.assertNotEqual(before, after)

    def test_cycle_with_old_result_is_safe_in_list_and_detail(self):
        self.old_result(line=self.line)
        self.external.next_egress = self.bridge
        self.external.save(update_fields=['next_egress'])
        data, _ = self.call(inventory_api.line_detail, '/', str(self.line.public_id))
        self.assertIsNone(data['line']['target_version'])
        self.assertIsNone(data['line']['path']['terminal_egress'])
        self.assertEqual(data['check_actions'], [])
        self.assertTrue(data['nodes']['truncated'])
        listing, _ = self.call(inventory_api.lines, '/')
        self.assertEqual(listing['items'][0]['verification']['state'], 'target_changed')

    def test_nonstaff_cannot_read_resources_or_enqueue(self):
        self.client.force_login(self.viewer)
        for path in ('servers','lines','servers/'+str(self.server.public_id),'lines/'+str(self.line.public_id)):
            self.assertEqual(self.client.get('/api/v1/admin/'+path).status_code,403)
        with patch('portal.resource_checks.execute_fixed') as execute:
            self.assertEqual(self.client.post('/api/v1/admin/resource-checks','{}',content_type='application/json').status_code,403)
            execute.assert_not_called()

    def test_purchase_without_own_source_is_forbidden(self):
        data, _ = self.call(inventory_api.server_detail, '/', str(self.server.public_id))
        self.assertEqual(data['purchase_meter_state'],'forbidden')
        self.assertIsNone(data['purchase_meter'])
        self.assertIsNone(data['purchase_binding_key'])
        self.assertTrue(all(node['purchase_meter'] is None for node in data['nodes']['items'] if node['resource_type']=='egress'))

    def test_too_deep_chain_with_old_result_has_no_fabricated_terminal(self):
        self.old_result(line=self.line)
        tail = self.external
        for index in range(15):
            row = Egress.objects.create(server=self.server, name='合成后段'+str(index), kind='upstream', ownership='external', external_kind='node', protocol='ss2022', core_instance=self.xray, evidence_source='review', evidence_observed_at=timezone.now())
            tail.next_egress = row
            tail.save(update_fields=['next_egress'])
            tail = row
        data, _ = self.call(inventory_api.line_detail, '/', str(self.line.public_id))
        self.assertIsNone(data['line']['target_version'])
        self.assertIsNone(data['line']['path']['terminal_egress'])
        self.assertTrue(data['nodes']['truncated'])
        self.assertEqual(data['check_actions'], [])

    @override_settings(P8_COMPAT_ENABLED=True, PROVIDER_USAGE_SOURCE={'path':'never-read-fixture','source_instance':'authorized','source_id':'one'})
    def test_purchase_source_isolation_and_revoke_clear_binding(self):
        record = P8SourceBinding.objects.create(owner=self.viewer, source_instance='authorized', source_id='one', enabled=True, state='verified', evidence_sha256='a'*64, links_sha256='b'*64)
        snapshot = {'meters':[{'id':'bwg','scope':'server','quality':'current','details':{'private':'must_strip'}}]}
        with patch('portal.inventory_purchase.valid_p8', return_value=True), patch('portal.resource_usage_view.read_resource_usage_view', return_value=snapshot) as read:
            data, _ = self.call(inventory_api.server_detail, '/', str(self.server.public_id))
            self.assertEqual(data['purchase_meter_state'],'forbidden')
            read.assert_not_called()
            record.owner = self.admin
            record.source_id = 'wrong-source'
            record.save(update_fields=['owner','source_id'])
            data, _ = self.call(inventory_api.server_detail, '/', str(self.server.public_id))
            self.assertEqual(data['purchase_meter_state'],'forbidden')
            read.assert_not_called()
            record.source_id = 'one'
            record.save(update_fields=['source_id'])
            data, _ = self.call(inventory_api.server_detail, '/', str(self.server.public_id))
            self.assertEqual(data['purchase_meter_state'],'ready')
            self.assertEqual(data['purchase_meter']['details'],{})
            old_key = data['purchase_binding_key']
            read.return_value = {'meters':[]}
            data, _ = self.call(inventory_api.server_detail, '/', str(self.server.public_id))
            self.assertEqual(data['purchase_meter_state'],'unavailable')
            self.assertEqual(data['purchase_binding_key'],old_key)
            record.owner = self.viewer
            record.save(update_fields=['owner'])
            data, _ = self.call(inventory_api.server_detail, '/', str(self.server.public_id))
            self.assertEqual(data['purchase_meter_state'],'forbidden')
            self.assertIsNone(data['purchase_binding_key'])

    def test_check_directory_and_enqueue_do_not_execute_io(self):
        version = target_fingerprint('ingress',self.ingress)
        entry = {'target_kind':'ingress','target_id':str(self.ingress.pk),'target_version':version,'action':'tcp','ip':'127.0.0.1','port':443,'allow_loopback':True}
        request = RequestFactory().get('/')
        request.user = self.admin
        with patch('portal.resource_checks.load_policy', return_value=[entry]), patch('portal.resource_checks.execute_fixed') as execute:
            data = json.loads(inventory_api.line_detail(request,str(self.line.public_id)).content)['data']
            self.assertEqual(len(data['check_actions']),1)
            self.assertNotIn('ip',data['check_actions'][0])
            payload = {k:entry[k] for k in ('target_kind','target_id','target_version','action')}
            payload['idempotency_key'] = 'review-request-001'
            self.assertEqual(self.client.post('/api/v1/admin/resource-checks',json.dumps(payload),content_type='application/json').status_code,202)
            strict = Client(enforce_csrf_checks=True)
            strict.force_login(self.admin)
            self.assertEqual(strict.post('/api/v1/admin/resource-checks',json.dumps(payload),content_type='application/json').status_code,403)
            execute.assert_not_called()

    def test_completed_check_job_detail_does_not_read_server_lease(self):
        result = self.old_result(egress=self.external)
        job = ResourceCheckJob.objects.create(actor=self.admin,idempotency_key='completed-review',request_fingerprint='a'*64,target_kind='egress',target_id=str(self.external.pk),target_version='a'*64,action='tcp',state='completed',result=result)
        _, queries = self.call(inventory_api.resource_check_detail,'/',str(job.public_id))
        bad = [q['sql'] for q in queries if 'deployment_lease' in q['sql']]
        self.assertEqual(bad,[])
