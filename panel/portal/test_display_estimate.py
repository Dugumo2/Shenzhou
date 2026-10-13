"""接受的展示估算不能改账、串用户、重复叠加或流入历史时间图。"""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from .api_helpers import project_service
from .display_estimate import project_estimate
from .models import UsageLedger
from .subscription_usage_headers import userinfo_for_service
from .test_billing_schedule import BillingFixture, GB


class DisplayEstimateTests(BillingFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.record.metering_gap = True
        self.value = {'version': 1, 'owner_id': self.record.user_id, 'service_id': str(self.record.public_id),
            'cycle_starts_at': self.cycle.starts_at.isoformat(), 'cycle_ends_at': self.cycle.ends_at.isoformat(),
            'accepted_at': self.now.isoformat(), 'source_observed_at': (self.now-timedelta(minutes=1)).isoformat(),
            'source_sha256': 'a'*64, 'approval_ref': 'user-confirmed-estimate',
            'charged_bytes': str(10*GB), 'upload_bytes': str(4*GB), 'download_bytes': str(6*GB),
            'ledger_baseline': {'charged_bytes': str(30*GB), 'upload_bytes': str(12*GB), 'download_bytes': str(12*GB)}}
        self.record.applied_snapshot = {**self.record.applied_snapshot, 'accepted_display_estimate': self.value}
        self.record.save(update_fields=['metering_gap','applied_snapshot'])

    def result(self):
        return project_service(self.record, 'entitlement', now=self.now)

    def test_only_explicit_estimate_is_projected_without_mutating_account_or_header(self):
        before=(self.cycle.used_bytes,UsageLedger.objects.count())
        view=self.result()
        self.assertEqual(view['usage_estimate']['used_bytes'],str(10*GB))
        self.assertEqual(view['usage_estimate']['remaining_bytes'],str(90*GB))
        self.assertIsNone(view['used_bytes']);self.assertIsNone(view['remaining_bytes'])
        self.assertEqual(view['recorded_used_bytes'],str(30*GB))
        self.assertIsNone(userinfo_for_service(self.user,self.record.public_id,now=self.now))
        self.cycle.refresh_from_db()
        self.assertEqual(before,(self.cycle.used_bytes,UsageLedger.objects.count()))

    def test_adds_only_new_confirmed_amount_and_repeated_projection_does_not_add_twice(self):
        UsageLedger.objects.create(cycle=self.cycle,stream=self.stream,sequence=2,
            upload_delta=GB,download_delta=GB,weighted_bytes=2*GB,quality='metered',observed_at=self.now)
        self.assertEqual(self.result()['usage_estimate']['used_bytes'],str(12*GB))
        self.assertEqual(self.result()['usage_estimate']['used_bytes'],str(12*GB))

    def test_missing_current_personal_rows_can_show_accepted_opening_but_not_an_unproven_zero(self):
        self.ledger.delete()
        self.value['ledger_baseline']=dict.fromkeys(self.value['ledger_baseline'],'0')
        view=self.result()
        self.assertIsNone(view['recorded_used_bytes'])
        self.assertEqual(view['usage_estimate']['used_bytes'],str(10*GB))
        self.record.applied_snapshot.pop('accepted_display_estimate')
        self.assertIsNone(self.result()['usage_estimate'])

    def test_other_owner_cycle_time_and_invalid_source_fail_closed(self):
        for key,value in [('owner_id',self.record.user_id+1),('service_id','wrong'),('source_sha256','no'),
            ('accepted_at',(self.now+timedelta(hours=1)).isoformat()),('cycle_ends_at',self.now.isoformat()),
            ('upload_bytes','999'),('charged_bytes','-1'),('approval_ref','')]:
            with self.subTest(key=key):
                original=self.value[key];self.value[key]=value
                self.assertIsNone(self.result()['usage_estimate']);self.value[key]=original
        self.assertIsNone(project_service(self.record,'entitlement',now=self.cycle.ends_at)['usage_estimate'])

    def test_baseline_disappearance_does_not_create_negative_delta_or_fake_remaining(self):
        self.ledger.delete()
        self.assertIsNone(self.result()['usage_estimate'])

    def test_changing_next_reset_on_same_cycle_preserves_opening_but_next_cycle_does_not_inherit(self):
        original_end=self.cycle.ends_at
        self.cycle.ends_at += timedelta(days=2)
        self.cycle.save(update_fields=['ends_at'])
        self.assertEqual(self.result()['usage_estimate']['used_bytes'],str(10*GB))
        self.assertEqual(project_service(self.record,'entitlement',now=original_end+timedelta(hours=1))['usage_estimate']['used_bytes'],str(10*GB))
        from .models import BillingCycle
        BillingCycle.objects.create(entitlement=self.record,starts_at=self.cycle.ends_at,
                                    ends_at=self.cycle.ends_at+timedelta(days=30))
        self.assertIsNone(project_service(self.record,'entitlement',now=self.cycle.ends_at)['usage_estimate'])

    def test_actual_zero_and_over_quota_are_explicit_and_bounded(self):
        for key in ('charged_bytes','upload_bytes','download_bytes'):self.value[key]='0'
        self.assertEqual(self.result()['usage_estimate']['used_bytes'],'0')
        self.value.update(charged_bytes=str(120*GB),upload_bytes=str(40*GB),download_bytes=str(80*GB))
        self.assertEqual(self.result()['usage_estimate']['remaining_bytes'],'0')
