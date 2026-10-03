"""隔离核对业务主状态、应用状态与管理员真实接口筛选的一致性。"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .api_helpers import service_facts
from .models import (BillingCycle, DeploymentJob, DeviceSubscription, Egress, Entitlement,
                     Ingress, Line, Membership, NodeIdentity, Server, UsageLedger, UsageStream)


@override_settings(ROOT_URLCONF='portal.test_api', PANEL_LIVE=False,
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ServiceStatusAPITests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.admin = User.objects.create_user('状态管理员', is_staff=True)
        self.client.force_login(self.admin)
        self.sequence = 0
        server = Server.objects.create(name='状态测试假服务器')
        self.ingress = Ingress.objects.create(server=server, name='假入口', protocol='ws')
        egress = Egress.objects.create(server=server, name='假出口', kind='upstream')
        self.line = Line.objects.create(name='假线路', ingress=self.ingress, egress=egress)

    def service(self, **changes):
        self.sequence += 1
        user = changes.pop('user', None) or User.objects.create_user('状态用户%02d' % self.sequence)
        fields = {'user': user, 'quota_bytes': 100, 'state': 'active', 'enabled': True,
                  'activated_at': self.now - timedelta(days=1), 'expires_at': self.now + timedelta(days=30),
                  'revision': 1, 'applied_revision': 1, 'applied_snapshot': {'quota_bytes': 100},
                  'usage_updated_at': self.now}
        fields.update(changes)
        return Entitlement.objects.create(**fields)

    def cycle(self, record, *, evidence=True, used=30, observed=None, **changes):
        fields = {'entitlement': record, 'starts_at': self.now - timedelta(days=1),
                  'ends_at': self.now + timedelta(days=15), 'used_bytes': used, 'raw_bytes': used}
        fields.update(changes)
        cycle = BillingCycle.objects.create(**fields)
        if evidence:
            subscription = DeviceSubscription.objects.create(entitlement=record, client='windows', name='假来源')
            identity = NodeIdentity.objects.create(subscription=subscription, line=self.line,
                                                   ingress=self.ingress, generation=1)
            stream = UsageStream.objects.create(identity=identity, epoch='status-test-only')
            UsageLedger.objects.create(cycle=cycle, stream=stream, sequence=1, upload_delta=used,
                download_delta=0, weighted_bytes=used, quality='metered', observed_at=observed or self.now)
        return cycle

    def data(self, query=''):
        with patch('portal.api.timezone.now', return_value=self.now):
            response = self.client.get('/api/v1/admin/services' + query)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()['error'])
        return response.json()['data']

    def row(self, record):
        rows = self.data('?q=' + str(record.public_id))['items']
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_business_reasons_override_active_state_and_match_active_filter(self):
        cases = [({}, 30, 'active', '已应用'),
                 ({'enabled': False}, 30, 'disabled', '已停用'),
                 ({'expires_at': self.now}, 30, 'expired', '已到期'),
                 ({'revision': 2}, 30, 'pending', '等待应用'),
                 ({}, 100, 'exhausted', '本期额度已用完'),
                 ({'metering_gap': True}, 30, 'metering_gap', '用量待核算'),
                 ({'expires_at': None}, 30, 'verification_required', '有效期待核验'),
                 ({'applied_snapshot': {}}, 30, 'verification_required', '额度待核验')]
        valid = None
        for changes, used, business_state, label in cases:
            with self.subTest(business_state=business_state, changes=changes):
                record = self.service(**changes)
                self.cycle(record, used=used)
                row = self.row(record)
                self.assertEqual(row['state'], 'active')
                self.assertEqual(row['business_state'], business_state)
                self.assertEqual(row['status_label'], label)
                self.assertEqual(row['application']['state'], 'waiting' if changes.get('revision') == 2 else 'applied')
                filtered = self.data('?state=active&q=' + str(record.public_id))
                self.assertEqual(filtered['pagination']['total'], int(business_state == 'active'))
                if business_state == 'active':
                    valid = record
                    self.assertEqual(row['delivery']['state'], 'verification_required')
                    self.assertIsNone(row['delivery']['download_url'])
                else:
                    self.assertEqual(filtered['items'], [])
                    self.assertEqual(row['delivery']['state'], 'blocked')
                    self.assertTrue(row['delivery']['message'])
        self.assertEqual([row['id'] for row in self.data('?state=active')['items']], [str(valid.public_id)])

    def test_inactive_account_is_blocked_in_admin_projection_and_owner_session(self):
        record = self.service()
        self.cycle(record)
        record.user.is_active = False
        record.user.save(update_fields=['is_active'])
        row = self.row(record)
        self.assertEqual(row['business_state'], 'account_disabled')
        self.assertEqual(row['status_label'], '账号已停用')
        self.assertEqual(row['application']['state'], 'applied')
        self.assertEqual(row['delivery']['state'], 'blocked')
        self.assertEqual(self.data('?state=active')['items'], [])
        self.client.force_login(record.user)
        response = self.client.get('/api/v1/me/services')
        self.assertEqual(response.status_code, 401)

    def test_unknown_and_stale_full_counters_never_become_exhausted_or_active(self):
        unknown = self.service()
        self.cycle(unknown, evidence=False, used=100)
        stale = self.service(usage_updated_at=self.now - timedelta(minutes=4))
        self.cycle(stale, used=100, observed=self.now - timedelta(minutes=4))
        missing_cycle = self.service()
        for record, quality in [(unknown, 'unknown'), (stale, 'stale'), (missing_cycle, 'unknown')]:
            with self.subTest(quality=quality, service=str(record.public_id)):
                row = self.row(record)
                self.assertEqual(row['business_state'], 'metering_' + quality)
                self.assertEqual(row['status_label'], '用量待核算')
                self.assertEqual(row['usage']['quality'], quality)
                self.assertIsNone(row['remaining_bytes'])
                self.assertEqual(row['delivery']['state'], 'blocked')
                self.assertEqual(self.data('?state=active&q=' + str(record.public_id))['items'], [])
        self.assertEqual(self.data('?state=metering_gap')['items'], [])

    def test_applied_quota_is_authoritative_for_exhaustion_and_active_filter(self):
        exhausted = self.service(quota_bytes=1000)
        self.cycle(exhausted, used=150)
        valid = self.service(quota_bytes=100, applied_snapshot={'quota_bytes': 1000})
        self.cycle(valid, used=150)
        self.assertEqual(self.row(exhausted)['business_state'], 'exhausted')
        row = self.row(valid)
        self.assertEqual(row['business_state'], 'active')
        self.assertEqual(row['remaining_bytes'], '850')
        self.assertEqual([row['id'] for row in self.data('?state=active')['items']], [str(valid.public_id)])

    def test_failed_isolated_and_unapplied_zero_revision_never_become_active(self):
        for changes, business in [({'state': 'failed'}, 'failed'), ({'state': 'simulated'}, 'simulated'),
                                  ({'revision': 0, 'applied_revision': 0}, 'pending')]:
            with self.subTest(business=business):
                record = self.service(**changes)
                self.cycle(record)
                row = self.row(record)
                self.assertEqual(row['business_state'], business)
                self.assertNotEqual(row['status_label'], '已应用')
                self.assertEqual(row['delivery']['state'], 'blocked')
        self.assertEqual(self.data('?state=active')['items'], [])

    def test_legacy_unknown_usage_is_not_exhaustion_or_active_entitlement(self):
        user = User.objects.create_user('独立旧会员')
        record = Membership.objects.create(user=user, status='active', provisioning_state='applied',
            quota_bytes=100, used_bytes=100, usage_state='measured', usage_updated_at=self.now,
            expires_at=self.now + timedelta(days=30))
        row = self.row(record)
        self.assertEqual(row['business_state'], 'verification_required')
        self.assertEqual(row['status_label'], '待核验')
        self.assertEqual(row['application']['state'], 'verification_required')
        self.assertEqual(row['usage']['quality'], 'unknown')
        self.assertIsNone(row['used_bytes'])
        self.assertIsNone(row['remaining_bytes'])
        self.assertEqual(self.data('?state=active')['items'], [])

    def test_gap_filter_includes_detected_overlap_and_missing_identity_without_persisting(self):
        overlap = self.service()
        self.cycle(overlap)
        self.cycle(overlap, evidence=False, starts_at=self.now - timedelta(hours=1))
        partial = self.service()
        self.cycle(partial)
        subscription = DeviceSubscription.objects.create(entitlement=partial, client='android', name='未采样假来源')
        NodeIdentity.objects.create(subscription=subscription, line=self.line, ingress=self.ingress, generation=1)
        for record in (overlap, partial):
            row = self.row(record)
            self.assertEqual(row['business_state'], 'metering_gap')
            self.assertEqual(row['status_label'], '用量待核算')
            record.refresh_from_db()
            self.assertFalse(record.metering_gap)
        data = self.data('?state=metering_gap')
        self.assertEqual(data['pagination']['total'], 2)
        self.assertEqual({row['id'] for row in data['items']}, {str(overlap.public_id), str(partial.public_id)})
        self.assertEqual(self.data('?state=active')['items'], [])

    def test_active_sql_prefilter_skips_known_blockers_before_metering_evaluation(self):
        valid = self.service()
        self.cycle(valid)
        for changes, used in [({'enabled': False}, 30), ({'expires_at': self.now}, 30),
                              ({'revision': 2}, 30), ({}, 100)]:
            record = self.service(**changes)
            self.cycle(record, used=used)
        inactive = self.service()
        self.cycle(inactive)
        inactive.user.is_active = False
        inactive.user.save(update_fields=['is_active'])
        with patch('portal.api_helpers.service_facts', wraps=service_facts) as facts:
            data = self.data('?state=active')
        self.assertEqual([row['id'] for row in data['items']], [str(valid.public_id)])
        self.assertEqual({call.args[0].pk for call in facts.call_args_list}, {valid.pk})

    def test_active_filter_paginates_only_matching_services_and_keeps_stable_counts(self):
        valid_ids = set()
        for _ in range(5):
            record = self.service()
            self.cycle(record)
            valid_ids.add(str(record.public_id))
        self.cycle(self.service(enabled=False))
        self.cycle(self.service(expires_at=self.now))
        found = set()
        for page in range(1, 4):
            data = self.data('?state=active&page_size=2&page=%s' % page)
            self.assertEqual(data['pagination']['total'], 5)
            self.assertEqual(data['pagination']['pages'], 3)
            self.assertEqual(data['filters']['state'], 'active')
            page_ids = {row['id'] for row in data['items']}
            self.assertFalse(found & page_ids)
            self.assertTrue(all(row['status_label'] == '已应用' for row in data['items']))
            found |= page_ids
        self.assertEqual(found, valid_ids)

    def test_filtered_reads_create_no_cycles_identity_delivery_or_jobs(self):
        record = self.service()
        models = (BillingCycle, DeviceSubscription, NodeIdentity, UsageStream, UsageLedger, DeploymentJob)
        before = [model.objects.count() for model in models]
        with CaptureQueriesContext(connection) as queries:
            for query in ['', '?state=active', '?state=metering_gap', '?state=expired',
                          '?q=' + str(record.public_id)]:
                self.data(query)
        self.assertEqual([model.objects.count() for model in models], before)
        writes = [query['sql'] for query in queries.captured_queries
                  if query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
        self.assertEqual(writes, [])
