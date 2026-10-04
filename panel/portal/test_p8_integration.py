"""本人服务/用量/资源的真实URL接线验收；仅隔离假资料。"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Membership, P8SourceBinding
from .p8_compat import binding_verification_sha256
from .test_p8_compat import P8FixtureMixin


@override_settings(P8_COMPAT_ENABLED=True, ROOT_URLCONF='megabox.urls')
class P8IntegrationTests(P8FixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.owner = User.objects.create_user('integration-owner')
        self.verifier = User.objects.create_user('integration-verifier', is_staff=True)
        self.member = Membership.objects.create(user=self.owner, status='active', quota_bytes=10**12,
            provisioning_state='pending_apply', usage_state='not_connected')
        self.binding = P8SourceBinding.objects.create(source_instance='fixture-p8', source_id='fixture-service',
            owner=self.owner, legacy_membership=self.member, verified_by=self.verifier,
            enabled=True, state='verified', verified_at=timezone.now(), evidence_sha256='e'*64,
            links_sha256=self.digest)
        self.reverify()
        self.config_override = override_settings(P8_COMPAT_SOURCES={('fixture-p8','fixture-service'):self.config})
        self.config_override.enable()
        self.addCleanup(self.config_override.disable)
        self.client.force_login(self.owner)

    def reverify(self):
        self.binding.refresh_from_db()
        P8SourceBinding.objects.filter(pk=self.binding.pk).update(
            verification_sha256=binding_verification_sha256(self.binding))

    def read(self, path):
        response = self.client.get('/api/v1'+path)
        self.assertEqual(response.status_code, 200, response.content[:200])
        return response.json()['data']

    def test_explicit_association_lists_once_without_reading_secret(self):
        with patch('portal.p8_compat._read_private_file') as secret:
            data = self.read('/me/services')
            self.assertEqual(data['service_count'], 1)
            self.assertEqual(data['items'][0]['source_type'], 'p8')
            self.assertIsNone(data['items'][0]['quota_bytes'])
            self.assertNotIn('https://', str(data))
            secret.assert_not_called()

    def test_current_and_old_service_ids_resolve_same_unknown_service(self):
        for identifier in (self.binding.public_id, self.member.public_id):
            data = self.read('/me/services/'+str(identifier))
            self.assertEqual(data['id'], str(self.binding.public_id))
            self.assertEqual(data['source_type'], 'p8')
            for field in ('quota_bytes','used_bytes','remaining_bytes','expires_at','next_reset_at'):
                self.assertIsNone(data[field])
            usage = self.read('/me/services/'+str(identifier)+'/usage?period=7d')
            self.assertEqual(usage['service_id'], str(self.binding.public_id))
            self.assertIsNone(usage['summary']['quota_bytes'])
            self.assertEqual(usage['quality']['state'], 'unknown')

    def test_invalidated_binding_never_revives_registered_quota_or_alias_delivery(self):
        P8SourceBinding.objects.filter(pk=self.binding.pk).update(revision=2)
        data = self.read('/me/services')
        self.assertEqual(data['compatibility']['state'], 'mapping_required')
        self.assertEqual(data['service_count'], 1)
        self.assertEqual(data['items'][0]['state'], 'mapping_required')
        for path in ('/me/services/'+str(self.member.public_id),):
            item = self.read(path)
            for field in ('quota_bytes','used_bytes','raw_bytes','remaining_bytes','expires_at','next_reset_at'):
                self.assertIsNone(item[field])
            self.assertEqual(item['delivery']['state'], 'blocked')
        usage = self.read('/me/services/'+str(self.member.public_id)+'/usage')
        self.assertEqual(usage['quality']['state'], 'unknown')
        self.assertIsNone(usage['summary']['quota_bytes'])
        self.assertIsNone(usage['history']['totals'])
        self.assertEqual(self.client.get('/api/v1/me/services/'+str(self.member.public_id)+'/p8-delivery?client_adapter=android').status_code,404)

    def test_owner_can_get_declared_resource_and_windows_remains_blocked(self):
        prefix='/me/services/'+str(self.binding.public_id)+'/p8-delivery?client_adapter='
        android=self.read(prefix+'android')
        self.assertEqual(android['state'],'available')
        self.assertEqual(android['resources'][0]['download_url'],self.links['android'])
        self.assertEqual(android['runtime_acceptance'],'not_tested')
        self.assertEqual(self.read(prefix+'windows')['resources'],[])

    def test_other_staff_cannot_see_or_read_owner_resource(self):
        self.client.force_login(self.verifier)
        self.assertEqual(self.read('/me/services')['items'],[])
        with patch('portal.p8_compat._read_private_file') as secret:
            for suffix in ('','/usage','/p8-delivery?client_adapter=android'):
                response=self.client.get('/api/v1/me/services/'+str(self.binding.public_id)+suffix)
                self.assertEqual(response.status_code,404)
            secret.assert_not_called()

    def test_source_is_not_merged_without_explicit_membership_association(self):
        P8SourceBinding.objects.filter(pk=self.binding.pk).update(legacy_membership=None)
        self.reverify()
        self.assertEqual(self.read('/me/services')['service_count'],2)

    @override_settings(P8_COMPAT_ENABLED=False)
    def test_disabled_feature_preserves_old_behavior_without_secret_io(self):
        with patch('portal.p8_compat._read_private_file') as secret:
            items=self.read('/me/services')['items']
            self.assertEqual(len(items),1)
            self.assertEqual(items[0]['source_type'],'membership')
            self.assertEqual(self.client.get('/api/v1/me/services/'+str(self.binding.public_id)+'/p8-delivery?client_adapter=android').status_code,404)
            secret.assert_not_called()
