"""正式规则保存与固定发布器交接：不接触真实策略或执行服务。"""
import json
from pathlib import Path
import shutil
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.conf import settings
from django.test import Client, TransactionTestCase, override_settings

from .client_rules import policy_document
from .models import ClientDirectRule
from .test_rule_api import ROOT, SETTINGS, payload


@override_settings(**{**SETTINGS, 'PANEL_LIVE': True, 'RULE_LIVE_WRITES_ENABLED': True,
                      'SECURE_SSL_REDIRECT': False, 'SESSION_COOKIE_SECURE': False})
class LiveRulePublicationTests(TransactionTestCase):
    def setUp(self):
        parent = (settings.BASE_DIR.parent / 'staging').resolve()
        root = parent / ('rule-publication-test-' + uuid.uuid4().hex)
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root) if root.resolve().is_relative_to(parent) else None)
        self.policy, self.status = root/'policy.json', root/'status.json'
        self.override = override_settings(ROUTING_POLICY_PATH=self.policy, ROUTING_STATUS_PATH=self.status)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.admin = User.objects.create_user('发布测试管理员', is_staff=True)
        self.client.force_login(self.admin)

    def send(self, method, body, path=ROOT):
        return getattr(self.client, method)(path, json.dumps(body), content_type='application/json')

    def test_default_switch_keeps_production_readonly(self):
        with override_settings(RULE_LIVE_WRITES_ENABLED=False):
            self.assertTrue(self.client.get(ROOT).json()['data']['read_only'])
            self.assertEqual(self.send('post', payload()).status_code, 403)
        self.assertFalse(self.policy.exists())

    def test_committed_crud_updates_latest_policy_and_preserves_original(self):
        old = ClientDirectRule.objects.create(kind='exact', value='original.example.com', outbound='direct', enabled=True, creator=self.admin)
        body = payload(value='new.example.com')
        created = self.send('post', body)
        self.assertEqual(created.status_code, 201, created.content)
        self.assertEqual(created.json()['data']['publication']['state'], 'queued')
        item = created.json()['data']['item']
        self.assertEqual(len(json.loads(self.policy.read_text())['rules']), 2)
        updated = self.send('patch', {'revision':1, 'enabled':False, 'idempotency_key':str(uuid.uuid4())}, ROOT+'/'+str(item['id']))
        self.assertEqual(updated.status_code, 200, updated.content)
        self.assertEqual(json.loads(self.policy.read_text()), policy_document(ClientDirectRule.objects.all())[0])
        deleted = self.send('delete', {'revision':2, 'confirm':True, 'idempotency_key':str(uuid.uuid4())}, ROOT+'/'+str(item['id']))
        self.assertEqual(deleted.status_code, 200, deleted.content)
        self.assertEqual(list(ClientDirectRule.objects.values_list('pk',flat=True)), [old.pk])

    def test_projection_failure_keeps_saved_rule_and_retry_does_not_duplicate(self):
        body = payload()
        with patch('portal.views.write_routing_policy', side_effect=OSError('不能向用户输出此路径')):
            response = self.send('post', body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['error']['code'], 'publish_queue_failed')
        self.assertEqual(ClientDirectRule.objects.count(), 1)
        # 另一位管理员在失败之后已新增条目，旧请求重试不得覆盖它。
        ClientDirectRule.objects.create(kind='exact', value='another.example.com', outbound='direct', enabled=True, creator=self.admin)
        replay = self.send('post', body)
        self.assertEqual(replay.status_code, 201, replay.content)
        self.assertTrue(replay.json()['data']['replayed'])
        self.assertEqual(ClientDirectRule.objects.count(), 2)
        self.assertEqual(len(json.loads(self.policy.read_text())['rules']), 2)

    def test_only_current_digest_can_report_publication_success(self):
        self.send('post', payload())
        _, digest = policy_document(ClientDirectRule.objects.all())
        self.status.write_text(json.dumps({'state':'succeeded','version':'release-1','policy_sha256':'old'}))
        self.assertEqual(self.client.get(ROOT).json()['data']['publication']['state'], 'queued')
        self.status.write_text(json.dumps({'state':'succeeded','version':'release-1','policy_sha256':digest}))
        result = self.client.get(ROOT).json()['data']
        self.assertFalse(result['read_only'])
        self.assertEqual(result['publication']['state'], 'succeeded')
        self.assertEqual(result['publication']['version'], 'release-1')

    def test_csrf_nonadmin_and_protected_domains_remain_blocked(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(ROOT, json.dumps(payload()), content_type='application/json').status_code, 403)
        self.assertGreaterEqual(self.send('post',payload(action='client_direct',value='openai.com')).status_code, 400)
        self.client.force_login(User.objects.create_user('普通测试用户'))
        self.assertEqual(self.send('post',payload()).status_code, 403)
        self.assertEqual(ClientDirectRule.objects.count(), 0)
        self.assertFalse(self.policy.exists())

    @override_settings(ROOT_URLCONF='megabox.urls')
    def test_legacy_page_versions_all_four_public_assets(self):
        from .context import legacy_asset_version
        version = legacy_asset_version(str(settings.BASE_DIR))
        response = self.client.get('/manage/client-routing/')
        self.assertEqual(response.status_code, 200)
        for name in ('portal.css', 'v2.css', 'portal.js', 'v2.js'):
            self.assertContains(response, '/static/portal/' + name + '?v=' + version)
