"""三来源服务的端点集成反例；仅元数据假夹具，不消费来源秘密。"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import BillingCycle, Entitlement, Membership, P8SourceBinding
from .p8_compat import binding_verification_sha256
from .service_projection import resolve


@override_settings(ROOT_URLCONF='megabox.urls', P8_COMPAT_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ServiceProjectionTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.admin = users.objects.create_user('projection-admin', is_staff=True)
        self.owner = users.objects.create_user('projection-owner')
        self.empty = users.objects.create_user('projection-empty')
        self.other = users.objects.create_user('projection-other')
        self.configs = {}
        self.override = override_settings(P8_COMPAT_SOURCES=self.configs)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.client.force_login(self.admin)

    def binding(self, owner=None, member=None, suffix='one'):
        record = P8SourceBinding.objects.create(owner=owner or self.owner, legacy_membership=member,
            source_instance='fixture', source_id=suffix, verified_by=self.admin, verified_at=timezone.now(),
            enabled=True, state='verified', evidence_sha256='e'*64, links_sha256='a'*64)
        record.verification_sha256 = binding_verification_sha256(record)
        record.save(update_fields=['verification_sha256'])
        self.configs[('fixture', suffix)] = {'source_instance': 'fixture', 'source_id': suffix,
            'evidence_sha256': 'e'*64}
        return record

    def read(self, path):
        response = self.client.get('/api/v1/' + path)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()['data']

    def test_three_sources_share_count_identity_and_no_secret_reads(self):
        p8 = self.binding()
        member = Membership.objects.create(user=self.other, status='active', quota_bytes=400)
        ent = Entitlement.objects.create(user=self.admin, quota_bytes=100)
        with patch('portal.p8_compat._read_private_file') as private:
            listing = self.read('admin/services')
            self.assertEqual(listing['pagination']['total'], 3)
            self.assertEqual({x['id'] for x in listing['items']}, {str(x.public_id) for x in (p8, member, ent)})
            detail = self.read('admin/users/' + str(self.owner.pk))
            self.assertEqual(detail['user']['service_count'], 1)
            self.assertEqual(detail['services'][0]['id'], str(p8.public_id))
            self.assertEqual(self.read('admin/overview')['services']['total'], 3)
            self.client.force_login(self.owner)
            mine = self.read('me/services')['items'][0]
            self.assertEqual(mine['id'], detail['services'][0]['id'])
            self.assertEqual(mine['state'], detail['services'][0]['state'])
            self.assertFalse(detail['services'][0]['capabilities']['p8_delivery'])
            self.assertIsNone(detail['services'][0]['operation_targets']['p8_delivery'])
            private.assert_not_called()

    def test_explicit_association_and_invalid_proof_keep_one_canonical_unknown(self):
        member = Membership.objects.create(user=self.owner, status='active', quota_bytes=10**12)
        p8 = self.binding(member=member)
        for invalid in (False, True):
            if invalid:
                P8SourceBinding.objects.filter(pk=p8.pk).update(revision=2)
            admin = self.read('admin/users/' + str(self.owner.pk))
            self.assertEqual(admin['user']['service_count'], 1)
            self.assertEqual(admin['services'][0]['id'], str(p8.public_id))
            self.client.force_login(self.owner)
            for identifier in (p8.public_id, member.public_id):
                row = self.read('me/services/' + str(identifier))
                usage = self.read('me/services/' + str(identifier) + '/usage')
                self.assertEqual(row['id'], str(p8.public_id))
                self.assertEqual(usage['service_id'], str(p8.public_id))
                self.assertIsNone(row['quota_bytes'])
                self.assertIsNone(usage['summary']['quota_bytes'])
                self.assertEqual(row['state'], 'mapping_required' if invalid else 'verification_required')
            self.client.force_login(self.admin)

    def test_missing_source_and_revocation_filter_unknown_before_pagination(self):
        first = self.binding()
        second = self.binding(suffix='two')
        self.configs.clear()
        P8SourceBinding.objects.filter(pk=second.pk).update(enabled=False, state='revoked')
        data = self.read('admin/services?state=mapping_required&page_size=1')
        following = self.read('admin/services?state=mapping_required&page_size=1&page=2')
        self.assertEqual(data['pagination']['total'], 2)
        self.assertNotEqual(data['items'][0]['id'], following['items'][0]['id'])
        self.assertEqual({data['items'][0]['id'], following['items'][0]['id']}, {str(first.public_id), str(second.public_id)})
        self.assertTrue(all(not any(row['capabilities'].values()) for row in data['items']))

    def test_service_filter_uses_deduplicated_count_and_validates_input(self):
        self.binding()
        member = Membership.objects.create(user=self.other, status='active', quota_bytes=50)
        self.binding(owner=self.other, member=member, suffix='linked')
        data = self.read('admin/users?service=with&page_size=1')
        self.assertEqual(data['pagination']['total'], 2)
        self.assertEqual(data['items'][0]['service_count'], 1)
        no_service = self.read('admin/users?service=none')['items']
        self.assertEqual({x['id'] for x in no_service}, {str(self.admin.pk), str(self.empty.pk)})
        self.assertEqual(self.client.get('/api/v1/admin/users?service=garbage').status_code, 422)

    def test_uuid_collision_does_not_alias_entitlement_or_enable_p8_billing(self):
        ent = Entitlement.objects.create(user=self.owner, quota_bytes=100)
        p8 = self.binding(owner=self.other)
        P8SourceBinding.objects.filter(pk=p8.pk).update(public_id=ent.public_id)
        self.assertEqual(self.read('admin/services')['pagination']['total'], 1)
        self.assertIsNone(resolve(self.other, ent.public_id))
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(ent.public_id)).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(ent.public_id) + '/p8-delivery?client_adapter=android').status_code, 404)

    def test_foreign_membership_claim_never_deduplicates_or_aliases_owner(self):
        member = Membership.objects.create(user=self.other, status='active', quota_bytes=300)
        p8 = self.binding(member=member)
        self.assertEqual(self.read('admin/services')['pagination']['total'], 2)
        self.assertIsNone(resolve(self.owner, member.public_id))
        row = self.read('admin/users/' + str(self.owner.pk))['services'][0]
        self.assertEqual(row['id'], str(p8.public_id))
        self.assertEqual(row['state'], 'mapping_required')

    def test_three_source_conflict_preserves_entitlement_and_unknown_p8_without_member_quota(self):
        member = Membership.objects.create(user=self.owner, status='active', quota_bytes=10**12)
        ent = Entitlement.objects.create(user=self.owner, quota_bytes=100)
        p8 = self.binding(member=member)
        rows = self.read('admin/users/' + str(self.owner.pk))['services']
        self.assertEqual({row['id'] for row in rows}, {str(ent.public_id), str(p8.public_id)})
        disputed = next(row for row in rows if row['source_type'] == 'p8')
        self.assertEqual(disputed['state'], 'mapping_required')
        self.assertIsNone(disputed['quota_bytes'])
        self.assertFalse(any(disputed['capabilities'].values()))
        self.client.force_login(self.owner)
        alias = self.read('me/services/' + str(member.public_id))
        self.assertEqual(alias['id'], str(p8.public_id))
        self.assertEqual(alias['state'], 'mapping_required')

    def test_user_count_and_no_service_filter_do_not_build_service_projections(self):
        self.binding()
        with patch('portal.service_projection.project', side_effect=AssertionError('计数不能全量构造详情')):
            data = self.read('admin/users?service=with')
            self.assertEqual(data['pagination']['total'], 1)
            self.assertEqual(data['items'][0]['service_count'], 1)

    def test_disabled_operations_rejected_by_actual_endpoints_and_other_staff_no_secret(self):
        p8 = self.binding()
        member = Membership.objects.create(user=self.other, status='active', quota_bytes=200)
        with patch('portal.p8_compat._read_private_file') as private:
            for record in (p8, member):
                path = '/api/v1/admin/services/' + str(record.public_id) + '/billing'
                self.assertEqual(self.client.get(path).status_code, 404)
                self.assertEqual(self.client.patch(path, data={
                    'next_reset_at': '2028-01-01T00:00', 'expected_billing_revision': 0,
                    'preview_token': 'fixture-only', 'idempotency_key': 'fixture-request',
                }, content_type='application/json').status_code, 404)
                self.assertEqual(self.client.post('/api/v1/admin/services', {}).status_code, 405)
            self.assertEqual(self.client.get('/api/v1/me/services/' + str(p8.public_id) + '/p8-delivery?client_adapter=android').status_code, 404)
            private.assert_not_called()

    def test_billing_capability_only_points_to_confirmed_entitlement_cycle(self):
        ent = Entitlement.objects.create(user=self.owner, quota_bytes=100)
        first = self.read('admin/services')['items'][0]
        self.assertFalse(first['actions']['billing'])
        self.assertIsNone(first['operation_targets']['billing'])
        now = timezone.now()
        ent.activated_at = now - timedelta(days=1)
        ent.save(update_fields=['activated_at'])
        BillingCycle.objects.create(entitlement=ent, starts_at=now-timedelta(days=1), ends_at=now+timedelta(days=20))
        row = self.read('admin/services')['items'][0]
        self.assertTrue(row['capabilities']['billing'])
        self.assertEqual(row['operation_targets']['billing'], str(ent.public_id))

    def test_old_member_alias_search_finds_only_one_p8_row(self):
        member = Membership.objects.create(user=self.owner, status='active', quota_bytes=300)
        p8 = self.binding(member=member)
        data = self.read('admin/services?q=' + str(member.public_id))
        self.assertEqual(data['pagination']['total'], 1)
        self.assertEqual(data['items'][0]['id'], str(p8.public_id))

    @override_settings(P8_COMPAT_ENABLED=False)
    def test_feature_off_preserves_existing_membership_projection(self):
        member = Membership.objects.create(user=self.owner, status='active', quota_bytes=300)
        self.binding(member=member)
        rows = self.read('admin/services')['items']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_type'], 'membership')
