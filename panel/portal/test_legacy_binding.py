"""合成旧会员映射：核验权限、去重、独立服务和失败关闭。"""
from datetime import timedelta
import hashlib

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .legacy_binding import revoke_legacy_binding, verify_legacy_binding
from .models import (BillingCycle, DeploymentJob, DeviceSubscription, Entitlement,
                     LegacyServiceBinding, Membership, NodeIdentity, SubscriptionGrant)


@override_settings(ROOT_URLCONF='portal.test_api', PANEL_LIVE=False,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class LegacyBindingTests(TestCase):
    evidence = hashlib.sha256(b'synthetic-owner-verification').hexdigest()

    def setUp(self):
        self.admin = User.objects.create_user('核验管理员', password='Fake-test-password-12', is_staff=True)
        self.user = User.objects.create_user('本人假账号', password='Fake-test-password-12')
        self.other = User.objects.create_user('另一假账号', password='Fake-test-password-12')
        self.now = timezone.now()
        self.client.force_login(self.user)

    def membership(self, user=None, **changes):
        fields = {'user': user or self.user, 'status': 'active', 'quota_bytes': 50_000_000_000,
                  'used_bytes': 123, 'expires_at': self.now + timedelta(days=7)}
        fields.update(changes)
        return Membership.objects.create(**fields)

    def entitlement(self, user=None, **changes):
        fields = {'user': user or self.user, 'quota_bytes': 100_000_000_000, 'state': 'active',
                  'expires_at': self.now + timedelta(days=30), 'applied_revision': 1,
                  'applied_snapshot': {'quota_bytes': 100_000_000_000}}
        fields.update(changes)
        return Entitlement.objects.create(**fields)

    def verify(self, member, target=None, **changes):
        args = {'evidence_sha256': self.evidence, 'entitlement_id': target.pk if target else None}
        args.update(changes)
        return verify_legacy_binding(self.admin, member.pk, **args)

    def data(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()['error'])
        return response.json()['data']

    def counts(self):
        return tuple(model.objects.count() for model in (
            Entitlement, BillingCycle, DeviceSubscription, NodeIdentity, DeploymentJob, SubscriptionGrant))

    def test_merge_keeps_one_target_service_and_old_uuid_is_canonical(self):
        member, target = self.membership(), self.entitlement()
        cycle = BillingCycle.objects.create(entitlement=target, starts_at=self.now - timedelta(days=1),
            ends_at=self.now + timedelta(days=15), used_bytes=1234, raw_bytes=1000, weighted_remainder=77)
        before = self.counts()
        self.verify(member, target)
        rows = self.data('/api/v1/me/services')
        self.assertEqual(rows['service_count'], 1)
        self.assertEqual(rows['compatibility']['state'], 'clear')
        canonical = self.data('/api/v1/me/services/' + str(target.public_id))
        alias = self.data('/api/v1/me/services/' + str(member.public_id))
        self.assertEqual(alias, canonical)
        self.assertEqual(alias['id'], str(target.public_id))
        self.assertEqual(alias['quota_bytes'], '100000000000')
        self.assertIsNone(alias['delivery']['download_url'])
        cycle.refresh_from_db()
        member.refresh_from_db()
        self.assertEqual((cycle.used_bytes, cycle.raw_bytes, cycle.weighted_remainder), (1234, 1000, 77))
        self.assertEqual(member.used_bytes, 123)
        self.assertEqual(before, self.counts())

    def test_independent_dual_chain_has_two_rows_and_unknown_old_usage(self):
        member, target = self.membership(), self.entitlement()
        self.verify(member)
        data = self.data('/api/v1/me/services')
        self.assertEqual(data['service_count'], 2)
        self.assertEqual({row['id'] for row in data['items']}, {str(member.public_id), str(target.public_id)})
        self.assertEqual(data['compatibility']['state'], 'clear')
        old = self.data('/api/v1/me/services/' + str(member.public_id))
        self.assertEqual(old['source_type'], 'membership')
        self.assertEqual(old['compatibility']['state'], 'clear')
        for key in ('used_bytes', 'raw_bytes', 'remaining_bytes', 'next_reset_at'):
            self.assertIsNone(old[key])
        self.assertEqual(old['usage']['quality'], 'unknown')
        self.assertTrue(all(item['delivery']['download_url'] is None for item in old['clients']))

    def test_unverified_dual_chain_preserves_mapping_required_and_hidden_old_id(self):
        member, target = self.membership(), self.entitlement()
        data = self.data('/api/v1/me/services')
        self.assertEqual([row['id'] for row in data['items']], [str(target.public_id)])
        self.assertEqual(data['compatibility']['state'], 'mapping_required')
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(member.public_id)).status_code, 404)

    def test_registration_placeholder_cannot_be_verified_or_get_a_service(self):
        member = Membership.objects.create(user=self.user, quota_bytes=0)
        before = self.counts()
        with self.assertRaises(ValidationError):
            self.verify(member)
        self.assertFalse(LegacyServiceBinding.objects.exists())
        self.assertEqual(self.data('/api/v1/me/services')['items'], [])
        self.assertEqual(before, self.counts())

    def test_zero_quota_with_actual_grant_is_not_a_registration_placeholder(self):
        member = Membership.objects.create(user=self.user, quota_bytes=0)
        SubscriptionGrant.objects.create(membership=member, device='windows')
        self.verify(member)
        self.assertEqual(self.data('/api/v1/me/services')['service_count'], 1)

    def test_local_helper_requires_active_authenticated_administrator(self):
        member = self.membership()
        for actor in (self.user, AnonymousUser()):
            with self.subTest(actor=type(actor).__name__), self.assertRaises(PermissionDenied):
                verify_legacy_binding(actor, member.pk, evidence_sha256=self.evidence)
        self.admin.is_active = False
        self.admin.save(update_fields=['is_active'])
        with self.assertRaises(PermissionDenied):
            self.verify(member)
        self.assertFalse(LegacyServiceBinding.objects.exists())

    @override_settings(PANEL_LIVE=True)
    def test_production_helper_is_denied_before_writing(self):
        member = self.membership()
        with self.assertRaises(PermissionDenied):
            self.verify(member)
        self.assertFalse(LegacyServiceBinding.objects.exists())

    def test_cross_owner_target_and_invalid_evidence_rollback(self):
        member, target = self.membership(), self.entitlement(self.other)
        with self.assertRaises(ValidationError):
            self.verify(member, target)
        for evidence in ('', 'a' * 63, 'a' * 64 + '\n', 'A' * 64, 'https://fake.invalid/secret'):
            with self.subTest(evidence_length=len(evidence)), self.assertRaises(ValidationError):
                self.verify(member, evidence_sha256=evidence)
        self.assertFalse(LegacyServiceBinding.objects.exists())

    def test_stale_revision_cannot_duplicate_or_overwrite_confirmation(self):
        member, target = self.membership(), self.entitlement()
        binding = self.verify(member)
        with self.assertRaises(ValidationError):
            self.verify(member, target)
        updated = self.verify(member, target, expected_revision=binding.revision)
        with self.assertRaises(ValidationError):
            self.verify(member, expected_revision=binding.revision)
        self.assertEqual(LegacyServiceBinding.objects.count(), 1)
        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.entitlement_id, target.pk)

    def test_revocation_hides_old_service_without_falling_back(self):
        member = self.membership()
        binding = self.verify(member)
        revoke_legacy_binding(self.admin, member.pk, expected_revision=binding.revision)
        data = self.data('/api/v1/me/services')
        self.assertEqual(data['items'], [])
        self.assertEqual(data['compatibility']['state'], 'mapping_required')
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(member.public_id)).status_code, 404)

    def test_source_revision_change_requires_reverification(self):
        member, target = self.membership(), self.entitlement()
        binding = self.verify(member)
        Membership.objects.filter(pk=member.pk).update(revision=member.revision + 1)
        data = self.data('/api/v1/me/services')
        self.assertEqual([row['id'] for row in data['items']], [str(target.public_id)])
        self.assertEqual(data['compatibility']['state'], 'mapping_required')
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(member.public_id)).status_code, 404)
        self.verify(member, expected_revision=binding.revision)
        self.assertEqual(self.data('/api/v1/me/services')['service_count'], 2)

    def test_tampered_owner_target_state_or_evidence_fails_closed(self):
        member, target = self.membership(), self.entitlement()
        other_target = self.entitlement(self.other)
        binding = self.verify(member)
        cases = [{'owner_id': self.other.pk}, {'entitlement_id': other_target.pk}, {'state': 'unverified'},
                 {'evidence_sha256': 'a' * 64 + '\n'}, {'verified_at': None}, {'verified_by_id': self.other.pk}]
        for changes in cases:
            with self.subTest(field=next(iter(changes))):
                LegacyServiceBinding.objects.filter(pk=binding.pk).update(**changes)
                data = self.data('/api/v1/me/services')
                self.assertEqual([row['id'] for row in data['items']], [str(target.public_id)])
                self.assertEqual(data['compatibility']['state'], 'mapping_required')
                self.assertEqual(self.client.get('/api/v1/me/services/' + str(member.public_id)).status_code, 404)
                binding.refresh_from_db()
                binding = self.verify(member, expected_revision=binding.revision)

    def test_changed_source_or_target_owner_never_exposes_other_users_data(self):
        member, target = self.membership(), self.entitlement()
        self.verify(member, target)
        Entitlement.objects.filter(pk=target.pk).update(user_id=self.other.pk)
        self.assertEqual(self.data('/api/v1/me/services')['items'], [])
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(member.public_id)).status_code, 404)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(member.public_id)).status_code, 404)

    def test_verifier_demotion_invalidates_old_binding(self):
        member = self.membership()
        self.verify(member)
        User.objects.filter(pk=self.admin.pk).update(is_staff=False)
        self.assertEqual(self.data('/api/v1/me/services')['items'], [])

    def test_queries_do_not_write_or_export_verification_metadata(self):
        member, target = self.membership(), self.entitlement()
        self.verify(member)
        before = self.counts()
        with CaptureQueriesContext(connection) as queries:
            responses = [self.client.get('/api/v1/me/services'),
                         self.client.get('/api/v1/me/services/' + str(member.public_id)),
                         self.client.get('/api/v1/me/services/' + str(target.public_id))]
        writes = [row['sql'] for row in queries.captured_queries
                  if row['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        self.assertEqual(writes, [])
        for response in responses:
            self.assertEqual(response.status_code, 200)
            for private in ('evidence_sha256', 'membership_revision', 'verified_by', self.evidence):
                self.assertNotIn(private, response.content.decode())
        self.assertEqual(before, self.counts())

    def test_administrator_filter_and_pagination_show_independent_services(self):
        member, target = self.membership(expires_at=self.now - timedelta(days=1)), self.entitlement()
        self.verify(member)
        self.client.force_login(self.admin)
        first = self.data('/api/v1/admin/services?q=本人假账号&page_size=1')
        second = self.data('/api/v1/admin/services?q=本人假账号&page_size=1&page=2')
        self.assertEqual(first['pagination']['total'], 2)
        self.assertEqual(second['pagination']['total'], 2)
        self.assertEqual({first['items'][0]['id'], second['items'][0]['id']},
                         {str(member.public_id), str(target.public_id)})
        expired = self.data('/api/v1/admin/services?state=expired')
        self.assertEqual([row['id'] for row in expired['items']], [str(member.public_id)])
        searched = self.data('/api/v1/admin/services?q=' + str(member.public_id))
        self.assertEqual([row['id'] for row in searched['items']], [str(member.public_id)])
        self.assertEqual(searched['items'][0]['compatibility']['state'], 'clear')

    def test_administrator_merge_uses_target_filter_and_alias_search(self):
        member, target = self.membership(expires_at=self.now - timedelta(days=1)), self.entitlement(state='pending')
        self.verify(member, target)
        self.client.force_login(self.admin)
        rows = self.data('/api/v1/admin/services?q=' + str(member.public_id))
        self.assertEqual(rows['pagination']['total'], 1)
        self.assertEqual(rows['items'][0]['id'], str(target.public_id))
        self.assertEqual(rows['items'][0]['compatibility']['state'], 'clear')
        self.assertEqual(self.data('/api/v1/admin/services?state=expired')['pagination']['total'], 0)
        self.assertEqual(self.data('/api/v1/admin/services?state=pending')['pagination']['total'], 1)

    def test_other_account_cannot_read_independent_service_or_admin_index(self):
        member = self.membership()
        self.verify(member)
        self.client.force_login(self.other)
        self.assertEqual(self.data('/api/v1/me/services')['items'], [])
        self.assertEqual(self.client.get('/api/v1/me/services/' + str(member.public_id)).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/admin/services').status_code, 403)

    def test_binding_preserves_referenced_member_and_target(self):
        member, target = self.membership(), self.entitlement()
        self.verify(member, target)
        with self.assertRaises(ProtectedError):
            member.delete()
        with self.assertRaises(ProtectedError):
            target.delete()
