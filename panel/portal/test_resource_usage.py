"""纯假时钟/规范化假快照验证资源周期；不读取文件、数据库或连接网络。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from .resource_usage import (BWH_TTL, HOME_TTL, ResourceUsageError, project_resource_usage)


def timestamp(text):
    return datetime.fromisoformat(text).timestamp()


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


ANCHOR = timestamp('2026-09-20T00:00:00+08:00')
BOUNDARY = timestamp('2026-10-20T00:00:00+08:00')
PLANS = [
    {'id': 'home', 'label': 'HOME', 'source': 'residential', 'scope': 'external_node',
     'quota_bytes': '400000000000', 'cycle': {'kind': 'fixed_days', 'days': 30, 'anchor': '2026-09-20T00:00:00+08:00'},
     'expires_on': '2027-01-19', 'accepted_initial_gap': True},
    {'id': 'bwg', 'label': 'BWH', 'source': 'bwh', 'scope': 'server', 'quota_bytes': None,
     'cycle': {'kind': 'provider'}, 'expires_on': None, 'accepted_initial_gap': False},
]


def snapshot(now, up=100, down=200, *, start=ANCHOR+2*86400, gaps=0, provider_used=1000, reset=None):
    return {'state': 'available', 'snapshot_updated_at': iso(now),
            'residential': {'source': 'estimate', 'accounting_scope': 'shared_residential_outbound', 'quality': 'estimate',
                'updated_at': iso(now), 'expires_at': iso(now+HOME_TTL), 'last_query_ok': True,
                'used_bytes': str(up+down), 'total_bytes': '400000000000', 'upload_bytes': str(up),
                'download_bytes': str(down), 'estimate_start': iso(start), 'collection_gaps': gaps},
            'bwh': {'source': 'KiwiVM_getServiceInfo', 'accounting_scope': 'provider_vps_billing', 'quality': 'provider_reported',
                'updated_at': iso(now), 'expires_at': iso(now+BWH_TTL), 'last_query_ok': True,
                'used_bytes': str(provider_used), 'total_bytes': '10000', 'reset_at': iso(reset or now+20*86400)}}


class ResourceUsageTests(unittest.TestCase):
    def setUp(self):
        self.now = timestamp('2026-10-04T16:00:00+08:00')
        self.plans = deepcopy(PLANS)

    def tick(self, snap=None, state=None, now=None, plans=None):
        now = self.now if now is None else now
        return project_resource_usage(snapshot(now) if snap is None else snap, self.plans if plans is None else plans, state, now)

    def codes(self, dto, index=0):
        return {item['code'] for item in dto['meters'][index]['alerts']}

    def test_initial_accepted_gap_uses_lifetime_without_special_banner(self):
        state, dto = self.tick()
        home, provider = dto['meters']
        self.assertEqual(home['used_bytes'], '300')
        self.assertEqual(home['remaining_bytes'], '399999999700')
        self.assertEqual(home['quality'], 'current')
        self.assertEqual(home['source_kind'], 'estimate')
        self.assertEqual(home['alerts'], [])
        self.assertTrue(home['details']['accepted_historical_gap'])
        self.assertEqual(home['cycle']['next_reset_at'], iso(BOUNDARY))
        self.assertEqual(provider['used_bytes'], '1000'); self.assertEqual(provider['remaining_bytes'], '9000')
        self.assertEqual(provider['source_kind'], 'official')
        self.assertIsNone(provider['cycle']['starts_at'])
        self.assertEqual(set(dto), {'schema_version', 'generated_at', 'meters'})
        self.assertNotIn('total_bytes', dto)

    def test_pure_function_does_not_mutate_inputs(self):
        source = snapshot(self.now); original = deepcopy(source); plans = deepcopy(self.plans)
        state, _ = self.tick(source); old = deepcopy(state)
        self.tick(source, state, self.now+30)
        self.assertEqual(source, original); self.assertEqual(state, old); self.assertEqual(self.plans, plans)

    def test_identical_sample_repeated_is_idempotent(self):
        source = snapshot(self.now)
        state, dto = self.tick(source)
        again, repeated = self.tick(source, state)
        self.assertEqual(state, again)
        self.assertEqual(dto['meters'][0]['used_bytes'], repeated['meters'][0]['used_bytes'])
        self.assertTrue(repeated['meters'][0]['details']['sample_replayed'])

    def test_same_cycle_accumulates_from_original_baseline(self):
        state, _ = self.tick()
        state, dto = self.tick(snapshot(self.now+60, up=120, down=240), state, self.now+60)
        self.assertEqual(dto['meters'][0]['used_bytes'], '360')
        self.assertEqual(state['meters']['home']['baseline']['upload_bytes'], '0')
        self.assertEqual(state['meters']['home']['last_sample']['upload_bytes'], '120')

    def test_october_twenty_boundary_resets_view_not_lifetime(self):
        state, _ = self.tick(snapshot(BOUNDARY-60, 1000, 2000), now=BOUNDARY-60)
        state, dto = self.tick(snapshot(BOUNDARY, 1100, 2200), state, BOUNDARY)
        home = dto['meters'][0]
        self.assertEqual(home['used_bytes'], '0'); self.assertEqual(home['remaining_bytes'], '400000000000')
        self.assertEqual(home['quality'], 'current')
        self.assertEqual(home['cycle']['starts_at'], iso(BOUNDARY))
        self.assertEqual(home['cycle']['ends_at'], iso(BOUNDARY+30*86400))
        self.assertEqual(home['details']['lifetime_used_bytes'], '3300')
        state, dto = self.tick(snapshot(BOUNDARY+60, 1150, 2250), state, BOUNDARY+60)
        self.assertEqual(dto['meters'][0]['used_bytes'], '100')

    def test_boundary_duplicate_old_sample_waits_then_alerts(self):
        old = snapshot(BOUNDARY-60)
        state, _ = self.tick(old, now=BOUNDARY-60)
        state, dto = self.tick(old, state, BOUNDARY+30)
        self.assertIn('cycle_waiting', self.codes(dto))
        self.assertEqual(dto['meters'][0]['used_bytes'], '300')
        self.assertEqual(dto['meters'][0]['cycle']['starts_at'], iso(ANCHOR))
        state, dto = self.tick(old, state, BOUNDARY+181)
        self.assertIn('cycle_not_advanced', self.codes(dto))
        self.assertEqual(state['meters']['home']['cycle_index'], 0)

    def test_late_cycle_baseline_is_gap_not_fabricated_zero(self):
        state, _ = self.tick(snapshot(BOUNDARY-60), now=BOUNDARY-60)
        state, dto = self.tick(snapshot(BOUNDARY+600, 300, 500), state, BOUNDARY+600)
        self.assertEqual(dto['meters'][0]['quality'], 'gap')
        self.assertIsNone(dto['meters'][0]['used_bytes']); self.assertIsNone(dto['meters'][0]['remaining_bytes'])
        self.assertIn('late_baseline', self.codes(dto))
        state, dto = self.tick(snapshot(BOUNDARY+660, 350, 600), state, BOUNDARY+660)
        self.assertEqual(dto['meters'][0]['used_bytes'], '150')
        self.assertIsNone(dto['meters'][0]['remaining_bytes'])

    def test_cross_multiple_cycles_does_not_allocate_all_lifetime_to_current(self):
        state, _ = self.tick()
        later = BOUNDARY+60*86400+30
        state, dto = self.tick(snapshot(later, 999999, 888888), state, later)
        self.assertIn('cycle_skipped', self.codes(dto))
        self.assertIsNone(dto['meters'][0]['used_bytes'])
        self.assertEqual(state['meters']['home']['cycle_index'], 3)
        self.assertEqual(dto['meters'][0]['details']['lifetime_used_bytes'], str(999999+888888))

    def test_no_prior_state_in_later_period_does_not_claim_full_balance(self):
        state, dto = self.tick(snapshot(BOUNDARY+600), now=BOUNDARY+600)
        self.assertEqual(dto['meters'][0]['quality'], 'gap')
        self.assertIsNone(dto['meters'][0]['remaining_bytes'])
        self.assertFalse(state['meters']['home']['accepted_historical_gap'])

    def test_late_conflicting_and_regressed_samples_preserve_last_then_recover(self):
        state, _ = self.tick()
        cases = [(snapshot(self.now-1), 'late_sample'), (snapshot(self.now, 9, 9), 'conflicting_sample'),
                 (snapshot(self.now+60, 1, 2), 'counter_regression')]
        for incoming, code in cases:
            with self.subTest(code=code):
                rejected, dto = self.tick(incoming, state, self.now+60)
                self.assertIn(code, self.codes(dto)); self.assertEqual(dto['meters'][0]['used_bytes'], '300')
                self.assertEqual(rejected['meters']['home']['last_sample'], state['meters']['home']['last_sample'])
                _, recovered = self.tick(snapshot(self.now+120, 110, 210), rejected, self.now+120)
                self.assertEqual(recovered['meters'][0]['quality'], 'current')
                self.assertNotIn(code, self.codes(recovered))

    def test_estimate_start_epoch_change_preserves_observed_period_amounts(self):
        state, _ = self.tick()
        state, dto = self.tick(snapshot(self.now+60, 1, 2, start=self.now+30), state, self.now+60)
        self.assertIn('counter_epoch_changed', self.codes(dto))
        self.assertEqual(dto['meters'][0]['used_bytes'], '300')
        self.assertIsNone(dto['meters'][0]['remaining_bytes'])
        state, dto = self.tick(snapshot(self.now+120, 11, 22, start=self.now+30), state, self.now+120)
        self.assertEqual(dto['meters'][0]['used_bytes'], '330')
        self.assertEqual(dto['meters'][0]['quality'], 'gap')

    def test_new_collection_gap_not_covered_by_initial_acceptance(self):
        state, _ = self.tick()
        state, dto = self.tick(snapshot(self.now+60, 110, 220, gaps=1), state, self.now+60)
        self.assertIn('collection_gap', self.codes(dto)); self.assertEqual(dto['meters'][0]['quality'], 'gap')
        self.assertIsNone(dto['meters'][0]['remaining_bytes'])
        state, _ = self.tick(snapshot(BOUNDARY-60, 200, 400, gaps=1), state, BOUNDARY-60)
        state, dto = self.tick(snapshot(BOUNDARY, 220, 440, gaps=1), state, BOUNDARY)
        self.assertEqual(dto['meters'][0]['quality'], 'current')
        self.assertEqual(state['meters']['home']['cycle_gap_codes'], [])

    def test_source_stale_failure_missing_and_recovery_are_independent(self):
        source = snapshot(self.now)
        state, _ = self.tick(source)
        state, dto = self.tick(source, state, self.now+181)
        self.assertEqual(dto['meters'][0]['quality'], 'stale')
        self.assertEqual(dto['meters'][1]['quality'], 'current')
        failed = snapshot(self.now+200, 150, 250)
        failed['residential'].update(quality='error', last_query_ok=False)
        state, dto = self.tick(failed, state, self.now+200)
        self.assertEqual(dto['meters'][0]['used_bytes'], '300')
        self.assertEqual(dto['meters'][0]['quality'], 'error')
        state, dto = self.tick({'state': 'missing'}, state, self.now+201)
        self.assertEqual(dto['meters'][0]['quality'], 'missing'); self.assertEqual(dto['meters'][0]['used_bytes'], '300')
        _, dto = self.tick(snapshot(self.now+240, 160, 260), state, self.now+240)
        self.assertEqual(dto['meters'][0]['quality'], 'current'); self.assertEqual(dto['meters'][0]['used_bytes'], '420')

    def test_future_sample_and_clock_back_never_advance_or_zero(self):
        state, _ = self.tick()
        later, dto = self.tick(snapshot(self.now+60), state, self.now)
        self.assertIn('future_sample', self.codes(dto)); self.assertEqual(later['meters']['home']['last_sample'], state['meters']['home']['last_sample'])
        rolled, dto = self.tick(snapshot(self.now-30), state, self.now-30)
        self.assertIn('clock_regression', self.codes(dto)); self.assertEqual(dto['meters'][0]['used_bytes'], '300')
        self.assertEqual(rolled['last_generated_at'], state['last_generated_at'])

    def test_renewal_and_rename_change_neither_baseline_nor_usage(self):
        state, dto = self.tick()
        changed = deepcopy(self.plans); changed[0]['expires_on'] = '2027-05-01'; changed[0]['label'] = '家庭节点'
        new, output = self.tick(snapshot(self.now), state, self.now, changed)
        self.assertEqual(new['meters']['home']['baseline'], state['meters']['home']['baseline'])
        self.assertEqual(output['meters'][0]['used_bytes'], dto['meters'][0]['used_bytes'])
        self.assertEqual(output['meters'][0]['cycle'], dto['meters'][0]['cycle'])
        self.assertEqual(output['meters'][0]['expires_on'], '2027-05-01')
        self.assertNotEqual(new['meters']['home']['plan_fingerprint'], state['meters']['home']['plan_fingerprint'])

    def test_plan_identity_change_or_corrupt_state_fails_closed(self):
        state, _ = self.tick()
        for change in ({'days': 31}, {'anchor': '2026-09-21T00:00:00+08:00'}):
            plans = deepcopy(self.plans); plans[0]['cycle'].update(change)
            with self.assertRaisesRegex(ResourceUsageError, 'plan_identity_changed'):
                self.tick(state=state, plans=plans)
        corrupt = deepcopy(state); corrupt['schema_version'] = True
        with self.assertRaises(ResourceUsageError): self.tick(state=corrupt)
        corrupt = deepcopy(state); corrupt['meters']['home']['carry_upload_bytes'] = '-1'
        with self.assertRaises(ResourceUsageError): self.tick(state=corrupt)
        with self.assertRaises(ResourceUsageError): self.tick(plans=[self.plans[0], self.plans[0]])

    def test_official_cycle_waits_for_provider_then_new_count_accepted(self):
        reset = self.now+60
        source = snapshot(self.now, reset=reset)
        state, _ = self.tick(source)
        state, dto = self.tick(source, state, reset+30)
        self.assertIn('provider_cycle_waiting', self.codes(dto, 1)); self.assertEqual(dto['meters'][1]['quality'], 'stale')
        state, dto = self.tick(source, state, reset+BWH_TTL+1)
        self.assertIn('provider_cycle_not_advanced', self.codes(dto, 1))
        newtime = reset+BWH_TTL+60
        _, dto = self.tick(snapshot(newtime, provider_used=10, reset=reset+30*86400), state, newtime)
        self.assertEqual(dto['meters'][1]['quality'], 'current'); self.assertEqual(dto['meters'][1]['used_bytes'], '10')
        self.assertIsNone(dto['meters'][1]['cycle']['starts_at'])

    def test_official_regression_without_reset_does_not_gift_balance(self):
        reset = self.now+30*86400
        state, _ = self.tick(snapshot(self.now, reset=reset))
        _, dto = self.tick(snapshot(self.now+60, provider_used=10, reset=reset), state, self.now+60)
        self.assertIn('counter_regression', self.codes(dto, 1))
        self.assertEqual(dto['meters'][1]['used_bytes'], '1000')
        self.assertEqual(dto['meters'][1]['remaining_bytes'], '9000')

    def test_overlimit_preserved_expiry_is_resource_only(self):
        source = snapshot(self.now, 200_000_000_000, 300_000_000_000, provider_used=12000)
        _, dto = self.tick(source)
        for row in dto['meters']:
            self.assertEqual(row['remaining_bytes'], '0'); self.assertIn('over_quota', {a['code'] for a in row['alerts']})
        plans = deepcopy(self.plans); plans[0]['expires_on'] = '2026-10-03'
        _, dto = self.tick(plans=plans)
        self.assertIn('expiry_passed', self.codes(dto)); self.assertNotIn('expiry_passed', self.codes(dto, 1))
        self.assertFalse(dto['meters'][0]['details']['hard_limit_enforced'])

    def test_initial_missing_or_invalid_never_fabricates_zero(self):
        state, dto = self.tick({'state': 'missing'})
        for row in dto['meters']:
            self.assertIsNone(row['used_bytes']); self.assertIsNone(row['remaining_bytes'])
        bad = snapshot(self.now); bad['residential']['used_bytes'] = '301'
        _, dto = self.tick(bad, state)
        self.assertEqual(dto['meters'][0]['quality'], 'error'); self.assertIsNone(dto['meters'][0]['used_bytes'])

    def test_dto_has_fixed_nonsecret_fields(self):
        source = snapshot(self.now); source['residential']['api_key'] = 'private-sentinel'
        _, dto = self.tick(source)
        expected = {'id', 'label', 'scope', 'source_kind', 'quality', 'quota_bytes', 'used_bytes', 'remaining_bytes',
                    'upload_bytes', 'download_bytes', 'observed_at', 'expires_at', 'cycle', 'expires_on', 'alerts', 'details'}
        self.assertTrue(all(set(row) == expected for row in dto['meters']))
        self.assertNotIn('private-sentinel', repr(dto))

    def test_inconsistent_saved_baseline_or_counter_rejected(self):
        state, _ = self.tick()
        for kind in ('baseline_above_sample', 'baseline_epoch', 'counter_sum', 'no_baseline'):
            corrupt = deepcopy(state)
            item = corrupt['meters']['home']
            if kind == 'baseline_above_sample': item['baseline']['upload_bytes'] = '100000'
            elif kind == 'baseline_epoch': item['baseline']['estimate_start'] = iso(ANCHOR)
            elif kind == 'counter_sum': item['last_sample']['used_bytes'] = '301'
            else: item['baseline'] = None
            with self.subTest(kind=kind), self.assertRaises(ResourceUsageError):
                self.tick(state=corrupt)

    def test_equivalent_anchor_timezone_format_is_same_identity(self):
        state, _ = self.tick()
        plans = deepcopy(self.plans); plans[0]['cycle']['anchor'] = iso(ANCHOR)
        updated, dto = self.tick(state=state, plans=plans)
        self.assertEqual(updated['meters']['home']['baseline'], state['meters']['home']['baseline'])
        self.assertEqual(dto['meters'][0]['used_bytes'], '300')

    def test_pre_anchor_lifetime_is_not_all_charged_to_initial_period(self):
        _, dto = self.tick(snapshot(self.now, start=ANCHOR-86400))
        self.assertEqual(dto['meters'][0]['quality'], 'gap')
        self.assertIsNone(dto['meters'][0]['used_bytes'])
        self.assertFalse(dto['meters'][0]['details']['accepted_historical_gap'])
