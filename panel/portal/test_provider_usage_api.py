"""来源账单只对明确绑定的管理员本人开放，不能冒充用户套餐余额。"""
import json
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from .models import P8SourceBinding
from .p8_compat import binding_verification_sha256, service_detail
from .usage_api import service_usage


@override_settings(P8_COMPAT_ENABLED=True)
class ProviderUsageAPITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user('source-owner', is_staff=True)
        self.other = User.objects.create_user('source-other', is_staff=True)
        self.binding = P8SourceBinding.objects.create(source_instance='fixture-p8', source_id='fixture-service',
            owner=self.owner, verified_by=self.other, enabled=True, state='verified',
            evidence_sha256='e'*64, links_sha256='a'*64, verified_at=timezone.now())
        self.binding.verification_sha256 = binding_verification_sha256(self.binding)
        self.binding.save(update_fields=['verification_sha256'])
        self.source = {'source_instance':'fixture-p8', 'source_id':'fixture-service', 'path':'/fixture/provider-usage.json'}
        # 资源摘要只核来源元数据，不需要构造或消费订阅链接文件。
        self.config = {'source_instance':'fixture-p8', 'source_id':'fixture-service', 'evidence_sha256':'e'*64}
        context = override_settings(P8_COMPAT_SOURCES={('fixture-p8','fixture-service'):self.config}, PROVIDER_USAGE_SOURCE=self.source)
        context.enable(); self.addCleanup(context.disable)

    def response(self, user=None):
        request = RequestFactory().get('/fixture/usage')
        request.user = user or self.owner
        return service_usage(request, self.binding.public_id)

    def test_admin_owner_gets_separate_source_statistics_without_filling_quota(self):
        with patch('portal.resource_usage_view.read_resource_usage_view', return_value={'schema_version':2,'meters':[]}) as read:
            result = json.loads(self.response().content)['data']
            self.assertNotIn('provider_usage', result)
            read.assert_not_called()
            from .admin_api import resource_usage
            request = RequestFactory().get('/api/v1/admin/resource-usage')
            request.user = self.owner
            resources = json.loads(resource_usage(request).content)['data']
        self.assertEqual(resources['items'][0]['provider_usage']['schema_version'], 2)
        read.assert_called_once_with(self.source['path'])
        self.assertIsNone(result['summary']['remaining_bytes'])
        self.assertIsNone(result['summary']['charged_bytes'])
        self.assertEqual(result['history']['days'], [])
        metadata = service_detail(self.owner, self.binding.public_id)
        self.assertEqual(metadata['status_label'], '订阅已接入')
        self.assertEqual(metadata['application']['state'], 'verification_required')

    def test_other_staff_does_not_read_provider_file(self):
        with patch('portal.resource_usage_view.read_resource_usage_view') as read:
            self.assertEqual(self.response(self.other).status_code, 404)
            read.assert_not_called()

    def test_nonstaff_owner_does_not_receive_whole_server_bill(self):
        self.owner.is_staff = False; self.owner.save(update_fields=['is_staff'])
        with patch('portal.resource_usage_view.read_resource_usage_view') as read:
            result = json.loads(self.response().content)['data']
            self.assertNotIn('provider_usage', result)
            read.assert_not_called()

    def test_wrong_source_disabled_feature_and_invalid_binding_never_read_file(self):
        for override in ({'PROVIDER_USAGE_SOURCE':{**self.source,'source_id':'other'}}, {'PROVIDER_USAGE_SOURCE':{}}, {'P8_COMPAT_ENABLED':False}):
            with override_settings(**override), patch('portal.resource_usage_view.read_resource_usage_view') as read:
                self.response(); read.assert_not_called()
        P8SourceBinding.objects.filter(pk=self.binding.pk).update(enabled=False)
        with patch('portal.resource_usage_view.read_resource_usage_view') as read:
            self.response(); read.assert_not_called()
