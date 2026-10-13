"""确认差分可以入账；窗口/覆盖/历史未知不能被改成完整套餐。"""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from .api_helpers import current_recorded_totals, project_service
from .ingress_metering import record_ingress_sample
from .models import BillingCycle, IngressUsageSample, ServiceRateVersion, UsageLedger, UsageStream
from .subscription_usage_headers import userinfo_for_service
from . import test_ingress_metering as metering_fixture
from .usage_timeseries import SHANGHAI, build_timeseries
from .usage_api import _confirmed


class PartialObservedTests(TestCase):
    service = metering_fixture.IngressMeteringTests.service
    rate_for = metering_fixture.IngressMeteringTests.rate_for

    def setUp(self):
        metering_fixture.IngressMeteringTests.setUp(self)
        self.record.applied_snapshot = {'quota_bytes': self.record.quota_bytes}
        self.record.save(update_fields=['applied_snapshot'])

    def sample(self, sequence=1, upload=1000, download=2000, at=None, **changes):
        end = at or self.now-timedelta(seconds=120-sequence*10)
        start = end-timedelta(milliseconds=4)
        windows = {'read_started_at':start,'read_ended_at':end,'upload_read_started_at':start,
            'upload_read_ended_at':start+timedelta(milliseconds=1),
            'download_read_started_at':start+timedelta(milliseconds=2),'download_read_ended_at':end}
        args = dict(identity_id=self.identity.pk,server_id=self.server.pk,core_instance_id=self.core.pk,
            epoch='partial-observed-a',sequence=sequence,upload_bytes=upload,download_bytes=download,
            observed_at=end,coverage_verified=False,counter_kind='cumulative',observation_verified=True,
            read_windows=windows,source_config_fingerprint='a'*64)
        args.update(changes)
        return record_ingress_sample(**args)

    def test_first_baseline_preserves_old_cycle_amount_and_does_not_charge_lifetime(self):
        self.cycle.used_bytes,self.cycle.raw_bytes=500,400
        self.cycle.save()
        first=self.sample(upload=999999999)
        self.assertEqual(first.status,'baseline')
        self.assertFalse(first.coverage_verified)
        self.assertTrue(first.observation_verified)
        self.cycle.refresh_from_db()
        self.assertEqual((self.cycle.used_bytes,self.cycle.raw_bytes),(500,400))
        self.assertFalse(UsageLedger.objects.exists())

    def test_confirmed_diff_enters_existing_cycle_without_full_quality_or_header(self):
        self.sample()
        sample=self.sample(2,1040,2040)
        row=UsageLedger.objects.get()
        self.assertEqual((row.upload_delta,row.download_delta,row.weighted_bytes),(40,40,100))
        self.assertEqual(row.quality,'partial_observed')
        self.assertEqual(row.ingress_sample_id,sample.pk)
        self.assertFalse(sample.coverage_verified)
        self.assertEqual(len(sample.read_windows),6)
        self.record.refresh_from_db()
        projection=project_service(self.record,'entitlement')
        self.assertEqual(projection['recorded_used_bytes'],'100')
        self.assertIsNone(projection['used_bytes'])
        self.assertIsNone(projection['remaining_bytes'])
        self.assertEqual(projection['usage']['quality'],'gap')
        self.assertIsNone(userinfo_for_service(self.record.user,self.record.public_id))

    def test_repeat_same_window_is_idempotent_and_conflicting_window_is_not(self):
        self.sample()
        second=self.sample(2,1040,2040)
        self.assertEqual(self.sample(2,1040,2040).pk,second.pk)
        self.assertEqual(UsageLedger.objects.count(),1)
        with self.assertRaises(ValidationError):
            self.sample(2,1040,2040,at=second.observed_at+timedelta(milliseconds=1))

    def test_zero_is_known_after_two_observations_but_missing_is_not_zero(self):
        self.sample()
        self.sample(2)
        self.assertEqual(current_recorded_totals(self.record,self.cycle,self.now)['recorded_used_bytes'],'0')
        with self.assertRaises(ValidationError):
            self.sample(3,upload=None)
        self.assertEqual(UsageLedger.objects.count(),1)

    def test_partial_observation_cannot_flip_coverage_or_use_unverified_config(self):
        for changes in ({'coverage_verified':True},{'source_config_fingerprint':''},{'read_windows':{}}):
            with self.assertRaises(ValidationError):
                self.sample(**changes)
        self.assertFalse(IngressUsageSample.objects.exists())

    def test_same_epoch_config_change_is_gap_and_not_charged(self):
        self.sample()
        value=self.sample(2,1040,2040,source_config_fingerprint='b'*64)
        self.assertEqual(value.reason_code,'observation_contract_changed')
        self.assertFalse(UsageLedger.objects.exists())

    def test_cycle_boundary_inside_previous_read_window_cannot_charge_new_cycle(self):
        point=self.now-timedelta(seconds=100)
        boundary=point-timedelta(milliseconds=2)
        self.cycle.ends_at=boundary
        self.cycle.save()
        new=BillingCycle.objects.create(entitlement=self.record,starts_at=boundary,ends_at=self.now+timedelta(days=30))
        self.sample(at=point)
        value=self.sample(2,1040,2040,at=point+timedelta(seconds=10))
        self.assertEqual(value.reason_code,'cycle_boundary')
        self.assertFalse(UsageLedger.objects.exists())
        new.refresh_from_db()
        self.assertEqual(new.used_bytes,0)

    def test_rate_boundary_in_previous_window_keeps_gap_without_guess(self):
        point=self.now-timedelta(seconds=100)
        self.sample(at=point)
        ServiceRateVersion.objects.create(entitlement=self.record,line=self.line,ingress=self.ingress,
            multiplier='2',effective_at=point-timedelta(milliseconds=2),actor=self.admin,reason='假倍率边界')
        value=self.sample(2,1040,2040,at=point+timedelta(seconds=10))
        self.assertEqual(value.reason_code,'rate_boundary')
        self.assertFalse(UsageLedger.objects.exists())

    def test_overlapping_uncertainty_envelopes_are_not_duplicate_counter_diffs(self):
        self.sample()
        self.sample(2,1040,2040)
        self.sample(3,1080,2080)
        value=build_timeseries(self.record,'month',self.now)
        self.assertEqual(value['totals']['charged_bytes'],'200')
        self.assertEqual(value['totals']['state'],'partial')
        self.assertFalse(any(bucket['state']=='complete' for bucket in value['buckets']))

    def test_midnight_envelope_is_unallocated_once_without_fictitious_daily_split(self):
        midnight=self.now.astimezone(SHANGHAI).replace(hour=0,minute=0,second=0,microsecond=0)
        self.sample(at=midnight-timedelta(seconds=1))
        self.sample(2,1040,2040,at=midnight+timedelta(seconds=1))
        value=build_timeseries(self.record,'month',self.now)
        self.assertEqual(value['totals']['charged_bytes'],'100')
        self.assertEqual(value['totals']['unallocated_charged_bytes'],'100')
        self.assertEqual(len(value['unallocated']),1)
        self.assertEqual(sum(int(bucket['charged_bytes'] or 0) for bucket in value['buckets']),0)

    def test_partial_quality_without_real_sample_is_not_user_recorded_amount(self):
        self.sample()
        stream=UsageStream.objects.get(identity=self.identity)
        UsageLedger.objects.create(cycle=self.cycle,stream=stream,sequence=55,upload_delta=99,download_delta=99,
            weighted_bytes=999,quality='partial_observed',observed_at=self.now,grant_rate_version=self.rate,
            interval_start=self.now-timedelta(seconds=5),interval_end=self.now)
        self.assertIsNone(current_recorded_totals(self.record,self.cycle,self.now)['recorded_used_bytes'])
        self.assertIsNone(build_timeseries(self.record,'month',self.now)['totals']['charged_bytes'])

    def test_bad_sample_status_or_non_sha_is_rejected_by_all_user_amount_views(self):
        self.sample()
        second=self.sample(2,1040,2040)
        for changed in ({'status':'gap'},{'status':'baseline'},{'source_config_fingerprint':'not-a-sha'}):
            IngressUsageSample.objects.filter(pk=second.pk).update(status='accepted',source_config_fingerprint='a'*64)
            IngressUsageSample.objects.filter(pk=second.pk).update(**changed)
            self.assertIsNone(current_recorded_totals(self.record,self.cycle,self.now)['recorded_used_bytes'])
            self.assertEqual(_confirmed(UsageLedger.objects.all(),self.now).count(),0)
            self.assertIsNone(build_timeseries(self.record,'month',self.now)['totals']['charged_bytes'])
