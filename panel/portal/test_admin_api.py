"""管理员只读工作区的权限、去重、分页与无副作用。"""
from datetime import timedelta
from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from .models import Membership, Entitlement
from .legacy_binding import verify_legacy_binding


@override_settings(ROOT_URLCONF='megabox.urls', PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AdminWorkspaceTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user('admin', is_staff=True)
        self.owner = User.objects.create_user('owner')
        self.disabled = User.objects.create_user('disabled', is_active=False)
        Membership.objects.create(user=self.disabled)
        self.client.force_login(self.admin)

    def data(self, path):
        response = self.client.get('/api/v1/admin/' + path)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def test_roles_cannot_read_or_write_admin_queries(self):
        paths = ['users','overview','users/'+str(self.owner.pk)]
        self.client.logout()
        for path in paths:
            self.assertEqual(self.client.get('/api/v1/admin/'+path).status_code,401)
        self.client.force_login(self.owner)
        for path in paths:
            self.assertEqual(self.client.get('/api/v1/admin/'+path).status_code,403)
        self.client.force_login(self.admin)
        for path in paths:
            self.assertEqual(self.client.post('/api/v1/admin/'+path).status_code,405)

    def test_inventory_and_empty_accounts_never_invent_services_or_telemetry(self):
        data = self.data('overview')
        self.assertEqual(data['users'], {'total':3,'active':2,'disabled':1})
        self.assertEqual(data['services']['total'],0)
        self.assertIsNone(data['services']['statistics_incomplete'])
        user = self.data('users/'+str(self.owner.pk))
        self.assertEqual(user['services'],[])
        self.assertFalse(any(user['capabilities'].values()))

    def test_search_status_and_pagination_are_consistent(self):
        first = self.data('users?page_size=1')
        second = self.data('users?page_size=1&page=2')
        self.assertEqual(first['pagination']['total'],3)
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])
        self.assertEqual(self.data('users?q=owner')['items'][0]['id'],str(self.owner.pk))
        self.assertEqual(self.data('users?status=disabled')['items'][0]['id'],str(self.disabled.pk))
        for path in ['users?status=online','users?page_size=500','users?page=0','users?q='+'a'*151]:
            self.assertEqual(self.client.get('/api/v1/admin/'+path).status_code,422)
        self.assertEqual(self.client.get('/api/v1/admin/users?page=100').status_code,404)

    def test_merged_and_independent_sources_preserve_canonical_service_count(self):
        member = Membership.objects.create(user=self.owner,status='active',quota_bytes=50)
        target = Entitlement.objects.create(user=self.owner,quota_bytes=100,metering_gap=True,
                                          expires_at=timezone.now()-timedelta(days=1))
        self.assertEqual(self.data('overview')['services']['mapping_required'],1)
        binding=verify_legacy_binding(self.admin,member.pk,evidence_sha256='a'*64,entitlement_id=target.pk)
        self.assertEqual(self.data('users/'+str(self.owner.pk))['user']['service_count'],1)
        self.assertEqual(self.data('overview')['services']['mapping_required'],0)
        self.assertEqual(self.data('overview')['services']['expired'],1)
        verify_legacy_binding(self.admin,member.pk,evidence_sha256='b'*64,expected_revision=binding.revision)
        detail=self.data('users/'+str(self.owner.pk))
        self.assertEqual(detail['user']['service_count'],2)
        self.assertEqual(len(detail['services']),2)
        self.assertEqual(self.data('overview')['services']['total'],2)
        self.assertTrue(all(row['delivery']['download_url'] is None for row in detail['services']))

    def test_read_queries_never_mutate_or_reveal_private_columns(self):
        Entitlement.objects.create(user=self.owner,quota_bytes=100,applied_snapshot={'secret':'PRIVATE-TEST'})
        with CaptureQueriesContext(connection) as capture:
            values=[self.data(path) for path in ['users','overview','users/'+str(self.owner.pk)]]
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for q in capture))
        for word in ('password','PRIVATE-TEST','applied_snapshot','credential_ref','email'):
            self.assertNotIn(word,str(values))

    def test_invalid_user_identifiers_are_safe_not_found(self):
        for value in ['missing','-1','99999999999999999999999','99999999','１２']:
            self.assertEqual(self.client.get('/api/v1/admin/users/'+value).status_code,404)
