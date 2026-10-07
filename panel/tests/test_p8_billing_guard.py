"""直接请求账期API也不能绕过失效P8映射；全部使用隔离元数据。"""
from datetime import timedelta
import json
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from portal.models import Entitlement, Membership, P8EntitlementBinding, P8SourceBinding
from portal.p8_compat import binding_verification_sha256
from portal.p8_entitlement import verify_p8_entitlement


@override_settings(PANEL_LIVE=False, P8_COMPAT_ENABLED=True,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class P8BillingGuardTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user('billing-p8-admin', is_staff=True)
        self.owner = get_user_model().objects.create_user('billing-p8-owner')
        member = Membership.objects.create(user=self.owner, status='active', quota_bytes=1000)
        self.p8 = P8SourceBinding.objects.create(owner=self.owner, legacy_membership=member,
            source_instance='fixture', source_id='billing', enabled=True, state='verified',
            verified_by=self.admin, verified_at=timezone.now(), evidence_sha256='e' * 64,
            links_sha256='a' * 64)
        self.p8.verification_sha256 = binding_verification_sha256(self.p8)
        self.p8.save(update_fields=['verification_sha256'])
        settings = override_settings(P8_COMPAT_SOURCES={('fixture', 'billing'): {
            'source_instance': 'fixture', 'source_id': 'billing', 'evidence_sha256': 'e' * 64}})
        settings.enable()
        self.addCleanup(settings.disable)
        self.target = Entitlement.objects.create(user=self.owner, public_id=self.p8.public_id, quota_bytes=1000)
        self.binding = verify_p8_entitlement(self.admin, self.p8.public_id, self.target.pk,
                                             evidence_sha256='f' * 64, expected_revision=0)
        self.url = '/api/v1/admin/services/' + str(self.p8.public_id) + '/billing'
        self.client.force_login(self.admin)

    def test_invalid_mapping_blocks_direct_read_preview_and_save(self):
        P8EntitlementBinding.objects.filter(pk=self.binding.pk).update(state='revoked')
        before = list(Entitlement.objects.values())
        payload = {'next_reset_at': (timezone.now() + timedelta(days=10)).strftime('%Y-%m-%dT%H:%M'),
            'expected_billing_revision': 0, 'preview_token': 'fake-only', 'idempotency_key': uuid.uuid4().hex}
        self.assertEqual(self.client.get(self.url).status_code, 404)
        preview = {key: payload[key] for key in ('next_reset_at', 'expected_billing_revision')}
        self.assertEqual(self.client.post(self.url + '/preview', json.dumps(preview),
                                          content_type='application/json').status_code, 404)
        self.assertEqual(self.client.patch(self.url, json.dumps(payload),
                                           content_type='application/json').status_code, 404)
        self.assertEqual(list(Entitlement.objects.values()), before)

    def test_valid_mapping_keeps_metadata_but_does_not_create_cycle(self):
        data = self.client.get(self.url)
        self.assertEqual(data.status_code, 200)
        self.assertFalse(data.json()['data']['can_modify'])
        self.assertFalse(self.target.cycles.exists())

    def test_corrupt_cross_owner_target_does_not_block_innocent_service(self):
        other = get_user_model().objects.create_user('billing-p8-other')
        innocent = Entitlement.objects.create(user=other, quota_bytes=2000)
        P8EntitlementBinding.objects.filter(pk=self.binding.pk).update(entitlement=innocent)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/admin/services/' + str(innocent.public_id) + '/billing').status_code, 200)
