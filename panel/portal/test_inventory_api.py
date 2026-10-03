"""登记资源查询的权限、去重、范围及无伪造观测证据。"""
import json
import uuid
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import path
from django.utils import timezone

from . import inventory_api
from .models import Egress, Ingress, Line, Server


urlpatterns = [
    path('api/v1/admin/servers', inventory_api.servers),
    path('api/v1/admin/servers/<str:public_id>', inventory_api.server_detail),
    path('api/v1/admin/lines', inventory_api.lines),
    path('api/v1/admin/lines/<str:public_id>', inventory_api.line_detail),
]


@override_settings(ROOT_URLCONF=__name__, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class InventoryApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('inventory-admin', is_staff=True)
        self.user = User.objects.create_user('inventory-user')
        self.start = Server.objects.create(name='入口机器', adapter='xray', last_seen_at=timezone.now(),
                                            deployment_lease_id=uuid.uuid4(), deployment_fence=27)
        self.finish = Server.objects.create(name='出口机器', adapter='unconfigured')
        self.other = Server.objects.create(name='附加机器', enabled=False)
        self.primary = Ingress.objects.create(server=self.start, name='主要入口', protocol='reality', config_ref='PRIVATE-CONFIG')
        self.extra = Ingress.objects.create(server=self.other, name='备用入口', protocol='hy2', enabled=False, config_ref='PRIVATE-INGRESS')
        self.egress = Egress.objects.create(server=self.finish, name='登记上游', kind='upstream', config_ref='PRIVATE-EGRESS')
        self.line = Line.objects.create(name='登记线路', ingress=self.primary, egress=self.egress)
        self.line.additional_ingresses.add(self.primary, self.extra)
        self.client.force_login(self.admin)

    def data(self, path):
        response = self.client.get('/api/v1/admin/' + path)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def test_roles_and_write_methods_are_rejected(self):
        paths = ['servers', 'lines', 'servers/' + str(self.start.public_id), 'lines/' + str(self.line.public_id)]
        self.client.logout()
        for route in paths:
            self.assertEqual(self.client.get('/api/v1/admin/' + route).status_code, 401)
        self.client.force_login(self.user)
        for route in paths:
            self.assertEqual(self.client.get('/api/v1/admin/' + route).status_code, 403)
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        for route in paths:
            self.assertEqual(self.client.get('/api/v1/admin/' + route).status_code, 401)
        self.client.force_login(self.admin)
        for route in paths:
            response = self.client.post('/api/v1/admin/' + route)
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response['Allow'], 'GET')

    def test_last_seen_is_a_record_and_does_not_prove_online(self):
        for observed in (timezone.now(), timezone.now() - timedelta(days=90), None):
            self.start.last_seen_at = observed
            self.start.save(update_fields=['last_seen_at'])
            server = self.data('servers/' + str(self.start.public_id))['server']
            self.assertEqual(server['monitoring'], {'state': 'not_connected', 'online': None, 'observed_at': None})
            self.assertEqual(server['last_seen_at'], observed.isoformat() if observed else None)
        rows = self.data('servers')['items']
        self.assertTrue(all(row['monitoring']['online'] is None for row in rows))

    def test_server_detail_preserves_unknown_metrics_and_capabilities(self):
        detail = self.data('servers/' + str(self.start.public_id))
        self.assertTrue(detail['read_only'])
        self.assertTrue(all(value is None for value in detail['metrics'].values()))
        self.assertEqual(detail['core'], {'actual_version': None, 'state': 'not_connected'})
        self.assertFalse(any(detail['capabilities'].values()))
        self.assertEqual(detail['ingresses']['items'][0]['protocol_label'], 'Reality')
        self.assertEqual(detail['ingresses']['items'][0]['server']['id'], str(self.start.public_id))

    def test_server_counts_do_not_multiply_independent_endpoint_sets(self):
        for index in range(2):
            Ingress.objects.create(server=self.start, name=f'入口{index}', protocol='ws')
        for index in range(3):
            Egress.objects.create(server=self.start, name=f'出口{index}', kind='direct')
        server = self.data('servers/' + str(self.start.public_id))['server']
        self.assertEqual(server['ingress_count'], 3)
        self.assertEqual(server['egress_count'], 3)

    def test_multiple_ingresses_deduplicate_and_keep_each_server(self):
        row = self.data('lines')['items'][0]
        self.assertEqual(row['ingress_count'], 2)
        self.assertEqual([item['id'] for item in row['ingresses']], [str(self.primary.pk), str(self.extra.pk)])
        self.assertEqual([item['server']['id'] for item in row['ingresses']], [str(self.start.public_id), str(self.other.public_id)])
        self.assertEqual(row['egress']['server']['id'], str(self.finish.public_id))
        self.assertTrue(row['enabled'])
        self.assertEqual(row['endpoint_registration'], 'disabled')
        self.assertEqual(row['verification'], {'state': 'not_tested', 'observed_at': None})
        detail = self.data('lines/' + str(self.line.public_id))
        self.assertFalse(any(detail['capabilities'].values()))
        self.assertIn('失败关闭仅为出口登记设置', ' '.join(detail['limitations']))

    def test_line_enabled_and_endpoint_registration_are_separate(self):
        self.line.additional_ingresses.remove(self.extra)
        self.line.enabled = False
        self.line.save(update_fields=['enabled'])
        row = self.data('lines')['items'][0]
        self.assertFalse(row['enabled'])
        self.assertEqual(row['endpoint_registration'], 'enabled')
        self.assertEqual(self.data('lines?status=disabled')['pagination']['total'], 1)

    def test_search_and_pages_use_only_registration_filters(self):
        self.assertEqual(self.data('servers?q=xray')['items'][0]['id'], str(self.start.public_id))
        self.assertEqual(self.data('servers?status=disabled')['items'][0]['id'], str(self.other.public_id))
        first, second = self.data('servers?page_size=1'), self.data('servers?page_size=1&page=2')
        self.assertEqual(first['pagination']['total'], 3)
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        for word in ('入口', '备用入口', 'hy2', '出口机器', 'upstream', '附加机器'):
            self.assertEqual(self.data('lines?q=' + word)['pagination']['total'], 1)
        for resource in ('servers', 'lines'):
            for suffix in ('?status=online', '?q=' + 'a' * 151, '?page=0', '?page_size=101'):
                self.assertEqual(self.client.get('/api/v1/admin/' + resource + suffix).status_code, 422)
            self.assertEqual(self.client.get('/api/v1/admin/' + resource + '?page=99').status_code, 404)
        self.assertEqual(self.client.get('/api/v1/admin/lines?server_id=invalid').status_code, 422)

    def test_exact_server_association_includes_primary_extra_and_exit(self):
        for server in (self.start, self.other, self.finish):
            items = self.data('lines?server_id=' + str(server.public_id))['items']
            self.assertEqual([row['id'] for row in items], [str(self.line.public_id)])
        self.assertEqual(self.data('lines?server_id=' + str(uuid.uuid4()))['items'], [])
        self.assertEqual(self.data('lines?server_id=' + str(self.other.public_id) + '&q=无此项')['items'], [])

    def test_empty_inventory_is_empty_and_does_not_create_records(self):
        self.line.delete()
        Ingress.objects.all().delete()
        Egress.objects.all().delete()
        Server.objects.all().delete()
        self.assertEqual(self.data('servers')['items'], [])
        self.assertEqual(self.data('lines')['items'], [])
        self.assertEqual(Server.objects.count(), 0)

    def test_queries_never_read_restricted_columns_or_write_rows(self):
        with CaptureQueriesContext(connection) as capture:
            values = [self.data(route) for route in ('servers', 'lines', 'servers/' + str(self.start.public_id), 'lines/' + str(self.line.public_id))]
        for query in capture:
            self.assertFalse(query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')))
            for column in ('config_ref', 'deployment_lease', 'deployment_fence'):
                self.assertNotIn(column, query['sql'])
        for column in ('config_ref', 'credential', 'password', 'PRIVATE-', 'deployment_', 'rate_version', 'multiplier'):
            self.assertNotIn(column, json.dumps(values))

    def test_endpoint_payload_is_capped_and_disabled_unshown_endpoint_is_not_lost(self):
        additional = [Ingress(server=self.start, name=f'附加{index}', protocol='ws') for index in range(105)]
        Ingress.objects.bulk_create(additional)
        self.line.additional_ingresses.add(*additional)
        extra = Ingress.objects.create(server=self.start, name='末尾停用', protocol='ws', enabled=False)
        self.line.additional_ingresses.add(extra)
        self.line.additional_ingresses.remove(self.extra)
        row = self.data('lines')['items'][0]
        self.assertEqual(row['ingress_count'], 107)
        self.assertEqual(len(row['ingresses']), inventory_api.ENDPOINT_LIMIT)
        self.assertTrue(row['ingresses_truncated'])
        self.assertEqual(row['endpoint_registration'], 'disabled')
        detail = self.data('servers/' + str(self.start.public_id))
        self.assertEqual(detail['ingresses']['total'], 107)
        self.assertTrue(detail['ingresses']['truncated'])
        self.assertEqual(len(detail['ingresses']['items']), inventory_api.ENDPOINT_LIMIT)

    def test_list_query_count_is_bounded_as_records_grow(self):
        def count_queries(view):
            request = RequestFactory().get('/api/v1/admin/resources?page_size=25')
            request.user = self.admin
            with CaptureQueriesContext(connection) as capture:
                response = view(request)
            self.assertEqual(response.status_code, 200)
            return len(capture)
        before = (count_queries(inventory_api.servers), count_queries(inventory_api.lines))
        for index in range(30):
            server = Server.objects.create(name=f'机器{index}')
            ingress = Ingress.objects.create(server=server, name=f'入口{index}', protocol='ws')
            egress = Egress.objects.create(server=self.finish, name=f'出口{index}', kind='direct')
            line = Line.objects.create(name=f'线路{index}', ingress=ingress, egress=egress)
            line.additional_ingresses.add(self.primary, self.extra)
        after = (count_queries(inventory_api.servers), count_queries(inventory_api.lines))
        self.assertEqual(before, after)
        self.assertLessEqual(after[0], 2)
        self.assertLessEqual(after[1], 3)

    def test_missing_and_malformed_identifiers_are_safe_not_found(self):
        for resource in ('servers', 'lines'):
            for identifier in ('invalid', '-1', str(uuid.uuid4()), '１２'):
                self.assertEqual(self.client.get('/api/v1/admin/' + resource + '/' + identifier).status_code, 404)

    @override_settings(ROOT_URLCONF='megabox.urls')
    def test_real_urlconf_connects_all_four_staff_only_routes(self):
        routes = ('servers', 'lines', 'servers/' + str(self.start.public_id), 'lines/' + str(self.line.public_id))
        for route in routes:
            self.assertTrue(self.data(route)['read_only'])
        self.client.force_login(self.user)
        for route in routes:
            self.assertEqual(self.client.get('/api/v1/admin/' + route).status_code, 403)
