"""采购独立变更与消费证据的隔离测试，不读取现场资料。"""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from .models import Egress, Server
from .resource_plan_models import ResourcePlanBinding, ResourcePlanConsumption, ResourcePlanVersion
from .resource_plan_service import (ResourcePlanError, confirm_resource_plan, export_resource_plan_overrides,
    preview_resource_plan, record_resource_plan_consumption, register_resource_plan, resource_plan_state)
from .resource_usage import _digest, project_resource_usage
from .test_resource_usage import PLANS, snapshot, timestamp


@override_settings(PANEL_LIVE=False, RESOURCE_PLAN_WRITE_ENABLED=True)
class ResourcePlanServiceTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.created_tables = []
        with connection.schema_editor() as editor:
            for model in (ResourcePlanBinding, ResourcePlanVersion, ResourcePlanConsumption):
                if model._meta.db_table not in connection.introspection.table_names():
                    editor.create_model(model)
                    cls.created_tables.append(model)

    @classmethod
    def tearDownClass(cls):
        with connection.schema_editor() as editor:
            for model in reversed(cls.created_tables):
                editor.delete_model(model)
        super().tearDownClass()

    def setUp(self):
        self.admin = get_user_model().objects.create_user('purchase-owner', is_staff=True)
        self.other = get_user_model().objects.create_user('other-staff', is_staff=True)
        self.server = Server.objects.create(name='采购测试机', purchase_meter_id='bwg')
        self.egress = Egress.objects.create(server=self.server, name='采购测试外部节点', kind='upstream',
                                           ownership='external', purchase_meter_id='home')
        self.source = deepcopy(PLANS)
        self.source_settings = override_settings(RESOURCE_PLAN_SOURCE_READER=lambda key: next(p for p in self.source if p['id'] == key))
        self.source_settings.enable()
        self.addCleanup(self.source_settings.disable)
        self.keys = {'home': 'a' * 64, 'bwg': 'b' * 64}
        self.purchase = patch('portal.resource_plan_service.InventoryPurchases.for_resource', side_effect=lambda meter, scope: {
            'purchase_meter_state': 'ready', 'purchase_binding_key': self.keys[meter]})
        self.purchase.start()
        self.addCleanup(self.purchase.stop)
        self.binding = register_resource_plan(self.source[0], target_kind='egress', target_id=self.egress.pk, actor=self.admin)
        register_resource_plan(self.source[1], target_kind='server', target_id=self.server.pk, actor=self.admin)

    def request(self, operation='quota', changes=None, meter='home'):
        current = resource_plan_state(self.admin, self.keys[meter])
        return {'expected_revision': current['revision'], 'source_fingerprint': current['source_fingerprint'],
                'operation': operation, 'changes': changes if changes is not None else {'add_bytes': '1000'}}

    def save(self, payload=None, key='purchase-request-1', meter='home'):
        payload = payload or self.request(meter=meter)
        preview = preview_resource_plan(self.admin, self.keys[meter], payload)
        confirm = {**payload, 'preview_token': preview['preview_token'], 'idempotency_key': key}
        return confirm_resource_plan(self.admin, self.keys[meter], confirm), confirm

    def test_original_registration_preserves_home_and_provider_facts(self):
        self.assertEqual(resource_plan_state(self.admin, self.keys['home'])['plan'], PLANS[0])
        self.assertEqual(resource_plan_state(self.admin, self.keys['home'])['effective_plan'], PLANS[0])
        self.assertEqual(resource_plan_state(self.admin, self.keys['bwg'])['allowed_operations'], ['expiry'])
        again = register_resource_plan(self.source[0], target_kind='egress', target_id=self.egress.pk, actor=self.admin)
        self.assertEqual(again.pk, self.binding.pk)
        self.assertFalse(ResourcePlanVersion.objects.exists())

    def test_quota_save_is_pending_and_other_fields_unchanged(self):
        result, _ = self.save()
        self.assertEqual(result['revision'], 2)
        self.assertEqual(result['plan']['quota_bytes'], '400000001000')
        self.assertEqual(result['effective_plan'], PLANS[0])
        self.assertEqual(result['plan']['cycle'], PLANS[0]['cycle'])
        self.assertEqual(result['plan']['expires_on'], PLANS[0]['expires_on'])
        self.assertEqual(result['consumption']['status'], 'pending')
        self.assertEqual(result['receipt']['status'], 'pending')
        self.assertFalse(ResourcePlanConsumption.objects.exists())

    def test_renewal_and_quota_keep_existing_cycle_and_counters(self):
        now = timestamp('2026-10-10T00:00:00+08:00')
        state, view = project_resource_usage(snapshot(now), deepcopy(PLANS), now=now)
        original = deepcopy(state)
        self.save(self.request('expiry', {'expires_on': '2028-01-19'}))
        result, _ = self.save(key='purchase-request-2')
        _, updated = project_resource_usage(snapshot(now), [result['plan'], PLANS[1]], state, now=now)
        self.assertEqual(state, original)
        for field in ('used_bytes', 'upload_bytes', 'download_bytes', 'cycle'):
            self.assertEqual(updated['meters'][0][field], view['meters'][0][field])

    def test_cycle_preview_changes_only_future_schedule(self):
        reset = (timezone.now() + timedelta(days=10)).isoformat()
        result, _ = self.save(self.request('cycle', {'days': 31, 'next_reset_at': reset}))
        self.assertEqual(result['plan']['cycle'], {'kind': 'fixed_days', 'days': 31, 'anchor': reset})
        self.assertEqual(result['plan']['quota_bytes'], PLANS[0]['quota_bytes'])
        self.assertEqual(result['plan']['expires_on'], PLANS[0]['expires_on'])
        self.assertEqual(ResourcePlanVersion.objects.get().before, PLANS[0])
        self.assertEqual(result['receipt']['status'], 'pending')

    def test_official_reset_and_quota_cannot_be_overridden(self):
        for operation, changes in [('quota', {'quota_bytes': '60000'}), ('cycle', {'days': 30, 'next_reset_at': '2030-01-01T00:00:00+08:00'})]:
            with self.subTest(operation=operation), self.assertRaisesRegex(ResourcePlanError, 'official_plan_read_only'):
                preview_resource_plan(self.admin, self.keys['bwg'], self.request(operation, changes, 'bwg'))
        result, _ = self.save(self.request('expiry', {'expires_on': '2028-01-01'}, 'bwg'), meter='bwg')
        self.assertEqual(result['plan']['cycle'], {'kind': 'provider'})
        self.assertIsNone(result['plan']['quota_bytes'])

    def test_same_request_retry_does_not_add_quota_twice(self):
        result, payload = self.save()
        replay = confirm_resource_plan(self.admin, self.keys['home'], payload)
        self.assertEqual(replay['receipt']['id'], result['receipt']['id'])
        self.assertTrue(replay['receipt']['replayed'])
        self.assertEqual(ResourcePlanVersion.objects.count(), 1)
        self.assertEqual(replay['plan']['quota_bytes'], '400000001000')

    def test_same_key_different_payload_rejected(self):
        self.save()
        with self.assertRaisesRegex(ResourcePlanError, 'idempotency_conflict'):
            self.save(self.request('expiry', {'expires_on': '2028-01-01'}))

    def test_old_preview_and_different_preview_payload_rejected(self):
        request = self.request()
        preview = preview_resource_plan(self.admin, self.keys['home'], request)
        payload = {**request, 'preview_token': preview['preview_token'], 'idempotency_key': 'old-preview-request'}
        with self.assertRaisesRegex(ResourcePlanError, 'preview_changed'):
            confirm_resource_plan(self.admin, self.keys['home'], {**payload, 'changes': {'add_bytes': '2000'}})
        self.save(key='new-request-saves-first')
        with self.assertRaisesRegex(ResourcePlanError, 'revision_conflict'):
            confirm_resource_plan(self.admin, self.keys['home'], payload)

    def test_expired_preview_rejected(self):
        request = self.request()
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() - 1000):
            preview = preview_resource_plan(self.admin, self.keys['home'], request)
        with self.assertRaisesRegex(ResourcePlanError, 'preview_expired_or_changed'):
            confirm_resource_plan(self.admin, self.keys['home'], {**request, 'preview_token': preview['preview_token'], 'idempotency_key': 'expired-request'})

    def test_source_drift_and_target_drift_rejected(self):
        self.source[0]['expires_on'] = '2029-01-01'
        with self.assertRaisesRegex(ResourcePlanError, 'purchase_source_changed'):
            resource_plan_state(self.admin, self.keys['home'])
        self.source[0] = deepcopy(PLANS[0])
        self.egress.purchase_meter_id = 'different'
        self.egress.save(update_fields=['purchase_meter_id'])
        with self.assertRaisesRegex(ResourcePlanError, 'purchase_target_changed'):
            resource_plan_state(self.admin, self.keys['home'])

    def test_staff_cannot_cross_owner_or_keep_revoked_authorization(self):
        with self.assertRaisesRegex(ResourcePlanError, 'permission_denied'):
            resource_plan_state(self.other, self.keys['home'])
        get_user_model().objects.filter(pk=self.admin.pk).update(is_staff=False)
        with self.assertRaisesRegex(ResourcePlanError, 'permission_denied'):
            resource_plan_state(self.admin, self.keys['home'])

    def test_source_generation_change_rejected_even_same_plan(self):
        with patch('portal.resource_plan_service.InventoryPurchases.for_resource', return_value={
                'purchase_meter_state': 'ready', 'purchase_binding_key': 'b' * 64}):
            with self.assertRaisesRegex(ResourcePlanError, 'purchase_source_changed'):
                resource_plan_state(self.admin, self.keys['home'])

    def test_unknown_fields_numeric_bytes_and_past_reset_rejected(self):
        for request in [self.request(changes={'add_bytes': 1000}), self.request(changes={'add_bytes': '-1'}),
                        self.request(changes={'quota_bytes': str(2**63)}), self.request('cycle', {'days': 0, 'next_reset_at': '2030-01-01T00:00:00Z'}),
                        self.request('cycle', {'days': 30, 'next_reset_at': '2020-01-01T00:00:00Z'}),
                        {**self.request(), 'owner_id': self.other.pk}]:
            with self.subTest(request=request), self.assertRaises(ResourcePlanError):
                preview_resource_plan(self.admin, self.keys['home'], request)
        self.assertFalse(ResourcePlanVersion.objects.exists())

    def test_production_requires_source_reader_and_separate_write_switch(self):
        with override_settings(PANEL_LIVE=True, RESOURCE_PLAN_SOURCE_READER=None):
            with self.assertRaisesRegex(ResourcePlanError, 'purchase_source_not_configured'):
                resource_plan_state(self.admin, self.keys['home'])
        with override_settings(RESOURCE_PLAN_WRITE_ENABLED=False):
            with self.assertRaisesRegex(ResourcePlanError, 'purchase_plan_read_only'):
                self.save()

    def test_export_preserves_order_and_receipt_required_for_consumed(self):
        first, _ = self.save()
        self.save(self.request('expiry', {'expires_on': '2028-01-01'}), key='second-request')
        entries = export_resource_plan_overrides()['entries']
        home = next(e for e in entries if e['meter_id'] == 'home')
        self.assertEqual([v['revision'] for v in home['versions']], [2, 3])
        self.assertEqual(home['source_plan'], PLANS[0])
        version = ResourcePlanVersion.objects.get(revision=2)
        receipt = {'version_id': first['receipt']['id'], 'revision': 2, 'source_fingerprint': version.source_fingerprint,
            'plan_fingerprint': _digest(version.plan), 'state_before_sha256': 'a' * 64,
            'state_after_sha256': 'b' * 64, 'consumed_at': timezone.now().isoformat(), 'status': 'consumed',
            'error_code': '', 'effective_plan_fingerprint': _digest(version.plan)}
        record_resource_plan_consumption(receipt)
        record_resource_plan_consumption(receipt)
        current = resource_plan_state(self.admin, self.keys['home'])
        self.assertEqual(current['consumption']['revision'], 2)
        self.assertEqual(current['consumption']['status'], 'pending')
        with self.assertRaisesRegex(ResourcePlanError, 'consumption_conflict'):
            record_resource_plan_consumption({**receipt, 'state_after_sha256': 'c' * 64})
        with self.assertRaisesRegex(ResourcePlanError, 'consumption_version_conflict'):
            record_resource_plan_consumption({**receipt, 'plan_fingerprint': 'd' * 64})
        self.assertEqual(ResourcePlanConsumption.objects.count(), 1)

    def test_registration_never_overwrites_source_or_versions(self):
        self.save()
        changed = {**PLANS[0], 'quota_bytes': '900000000000'}
        with self.assertRaisesRegex(ResourcePlanError, 'purchase_registration_conflict'):
            register_resource_plan(changed, target_kind='egress', target_id=self.egress.pk, actor=self.admin)
        self.binding.refresh_from_db()
        self.assertEqual(self.binding.source_plan, PLANS[0])
        self.assertEqual(self.binding.revision, 2)

    def test_consumption_requires_order_and_exact_latest_version(self):
        self.save()
        self.save(self.request('expiry', {'expires_on': '2028-01-01'}), key='second-consumption')
        versions = list(ResourcePlanVersion.objects.order_by('revision'))
        def receipt(version):
            return {'version_id': str(version.public_id), 'revision': version.revision,
                'source_fingerprint': version.source_fingerprint, 'plan_fingerprint': _digest(version.plan),
                'state_before_sha256': 'a' * 64, 'state_after_sha256': 'b' * 64,
                'consumed_at': timezone.now().isoformat(), 'status': 'consumed', 'error_code': '',
                'effective_plan_fingerprint': _digest(version.plan)}
        with self.assertRaisesRegex(ResourcePlanError, 'consumption_out_of_order'):
            record_resource_plan_consumption(receipt(versions[1]))
        record_resource_plan_consumption(receipt(versions[0]))
        record_resource_plan_consumption(receipt(versions[1]))
        current = resource_plan_state(self.admin, self.keys['home'])
        self.assertEqual(current['consumption']['status'], 'consumed')
        self.assertEqual(current['consumption']['revision'], 3)
        self.assertIsNotNone(current['consumption']['consumed_at'])
        with self.assertRaisesRegex(ResourcePlanError, 'invalid_consumption'):
            record_resource_plan_consumption({**receipt(versions[1]), 'version_id': []})

    def consumption_receipt(self, version, effective, *, rejected=False):
        return {'version_id': str(version.public_id), 'revision': version.revision,
            'source_fingerprint': version.source_fingerprint, 'plan_fingerprint': _digest(version.plan),
            'state_before_sha256': 'a' * 64, 'state_after_sha256': 'b' * 64,
            'consumed_at': timezone.now().isoformat(), 'status': 'rejected' if rejected else 'consumed',
            'error_code': 'cycle_reset_expired' if rejected else '', 'effective_plan_fingerprint': _digest(effective)}

    def test_expired_cycle_rejection_allows_following_quota_and_corrected_cycle(self):
        now = timezone.now()
        with patch('portal.resource_plan_service.timezone.now', return_value=now - timedelta(days=3)):
            result, original_request = self.save(self.request('cycle', {
                'days': 31, 'next_reset_at': (now - timedelta(days=1)).isoformat()}))
        first = ResourcePlanVersion.objects.get(revision=2)
        effective = deepcopy(PLANS[0])
        rejection = self.consumption_receipt(first, effective, rejected=True)
        record_resource_plan_consumption(rejection)
        record_resource_plan_consumption(rejection)
        state = resource_plan_state(self.admin, self.keys['home'])
        self.assertEqual(state['consumption']['status'], 'rejected')
        self.assertEqual(state['consumption']['error_code'], 'cycle_reset_expired')
        self.assertEqual(state['consumption']['effective_plan_fingerprint'], _digest(PLANS[0]))
        self.assertEqual(state['effective_plan'], PLANS[0])
        self.assertNotEqual(state['plan']['cycle'], state['effective_plan']['cycle'])
        replay = confirm_resource_plan(self.admin, self.keys['home'], original_request)
        self.assertEqual(replay['receipt']['status'], 'rejected')
        self.assertEqual(replay['receipt']['error_code'], 'cycle_reset_expired')

        self.save(key='quota-after-rejected-cycle')
        quota = ResourcePlanVersion.objects.get(revision=3)
        effective['quota_bytes'] = quota.plan['quota_bytes']
        with self.assertRaisesRegex(ResourcePlanError, 'effective_plan_conflict'):
            record_resource_plan_consumption(self.consumption_receipt(quota, quota.plan))
        record_resource_plan_consumption(self.consumption_receipt(quota, effective))
        self.assertEqual(effective['cycle'], PLANS[0]['cycle'])
        actual = resource_plan_state(self.admin, self.keys['home'])
        self.assertEqual(actual['consumption']['status'], 'consumed')
        self.assertEqual(actual['effective_plan'], effective)
        self.assertNotEqual(actual['plan']['cycle'], actual['effective_plan']['cycle'])

        reset = (now + timedelta(days=10)).isoformat()
        self.save(self.request('cycle', {'days': 28, 'next_reset_at': reset}), key='corrected-cycle-request')
        corrected = ResourcePlanVersion.objects.get(revision=4)
        effective['cycle'] = corrected.plan['cycle']
        record_resource_plan_consumption(self.consumption_receipt(corrected, effective))
        state = resource_plan_state(self.admin, self.keys['home'])
        self.assertEqual(state['consumption']['revision'], 4)
        self.assertEqual(state['consumption']['error_code'], '')
        self.assertEqual(state['consumption']['effective_plan_fingerprint'], _digest(effective))
        self.assertEqual(state['effective_plan'], effective)
        self.assertEqual(ResourcePlanConsumption.objects.count(), 3)

    def test_consumption_cannot_reject_quota_or_future_cycle_or_accept_expired_cycle(self):
        self.save()
        quota = ResourcePlanVersion.objects.get(revision=2)
        with self.assertRaisesRegex(ResourcePlanError, 'invalid_rejection'):
            record_resource_plan_consumption(self.consumption_receipt(quota, PLANS[0], rejected=True))
        record_resource_plan_consumption(self.consumption_receipt(quota, quota.plan))
        now = timezone.now()
        with patch('portal.resource_plan_service.timezone.now', return_value=now - timedelta(days=3)):
            self.save(self.request('cycle', {'days': 30, 'next_reset_at': (now - timedelta(days=1)).isoformat()}), key='late-cycle-acceptance')
        expired = ResourcePlanVersion.objects.get(revision=3)
        with self.assertRaisesRegex(ResourcePlanError, 'expired_cycle_cannot_be_consumed'):
            record_resource_plan_consumption(self.consumption_receipt(expired, expired.plan))
        record_resource_plan_consumption(self.consumption_receipt(expired, quota.plan, rejected=True))
        self.save(self.request('cycle', {'days': 30, 'next_reset_at': (now + timedelta(days=1)).isoformat()}), key='future-cycle-rejection')
        future = ResourcePlanVersion.objects.get(revision=4)
        with self.assertRaisesRegex(ResourcePlanError, 'invalid_rejection'):
            record_resource_plan_consumption(self.consumption_receipt(future, quota.plan, rejected=True))

    def test_read_rejects_corrupt_effective_receipt_instead_of_displaying_desired(self):
        self.save()
        version = ResourcePlanVersion.objects.get(revision=2)
        receipt = record_resource_plan_consumption(self.consumption_receipt(version, version.plan))
        ResourcePlanConsumption.objects.filter(pk=receipt.pk).update(effective_plan_fingerprint='f' * 64)
        with self.assertRaisesRegex(ResourcePlanError, 'effective_plan_conflict'):
            resource_plan_state(self.admin, self.keys['home'])
