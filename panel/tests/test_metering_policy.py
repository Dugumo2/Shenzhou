"""纯额度策略离线测试；不读网站数据库，不连接 API 或修改代理。"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import unittest

from bridge.metering_policy import MAX_INTEGER, MeteringPolicyError, evaluate_access


NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def values(**changes):
    return {
        "now": NOW, "user_active": True, "status": "active",
        "provisioning_state": "applied", "usage_state": "measured",
        "quota_bytes": 1000, "used_bytes": 200, "expires_at": NOW + timedelta(days=1),
        "usage_updated_at": NOW, "known_owner": True, "gap_count": 0,
        "current_observation_healthy": True,
    } | changes


class MeteringPolicyTests(unittest.TestCase):
    def test_eligible_observed_usage_allows_subscription_without_precision_claim(self):
        decision = evaluate_access(**values())
        self.assertEqual(decision.state, "allowed")
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.subscription_allowed)
        self.assertFalse(decision.requires_suspension)
        self.assertEqual(decision.primary_reason, "eligible_observed_usage")
        self.assertFalse(decision.usage_exact)
        self.assertFalse(decision.enforcement_verified)
        self.assertIn("observed_usage_only", decision.warnings)
        self.assertIn("decision_not_enforcement", decision.warnings)

    def test_current_health_is_false_by_default_even_when_membership_eligible(self):
        fixture = values()
        fixture.pop("current_observation_healthy")
        decision = evaluate_access(**fixture)
        self.assertEqual(decision.state, "suspended")
        self.assertEqual(decision.reasons, ("observation_unhealthy",))
        self.assertFalse(decision.subscription_allowed)
        self.assertTrue(decision.requires_suspension)

    def test_historical_gaps_do_not_permanently_suspend_current_healthy_user(self):
        for gap_count in (1, 2, 1000, MAX_INTEGER):
            with self.subTest(gap_count=gap_count):
                decision = evaluate_access(**values(gap_count=gap_count))
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.gap_count, gap_count)
                self.assertIn("historical_gaps", decision.warnings)
                self.assertFalse(decision.usage_exact)

    def test_health_failure_requires_suspension_regardless_of_historical_gaps(self):
        for gaps in (0, 1, 100):
            with self.subTest(gaps=gaps):
                decision = evaluate_access(**values(gap_count=gaps, current_observation_healthy=False))
                self.assertFalse(decision.allowed)
                self.assertTrue(decision.requires_suspension)
                self.assertIn("observation_unhealthy", decision.reasons)

    def test_freshness_boundaries_match_three_minutes_and_thirty_seconds(self):
        for offset, allowed, reason in (
            (-180.000001, False, "usage_stale"), (-180, True, None), (-179, True, None),
            (0, True, None), (30, True, None), (30.000001, False, "usage_from_future"),
        ):
            with self.subTest(offset=offset):
                decision = evaluate_access(**values(usage_updated_at=NOW + timedelta(seconds=offset)))
                self.assertEqual(decision.allowed, allowed)
                self.assertEqual(decision.requires_suspension, not allowed)
                if reason:
                    self.assertIn(reason, decision.reasons)

    def test_expiry_must_be_strictly_in_future(self):
        for offset, allowed in ((-1, False), (0, False), (0.000001, True)):
            with self.subTest(offset=offset):
                decision = evaluate_access(**values(expires_at=NOW + timedelta(seconds=offset)))
                self.assertEqual(decision.allowed, allowed)
                if not allowed:
                    self.assertEqual(decision.primary_reason, "expired")
                    self.assertTrue(decision.requires_suspension)

    def test_quota_equal_or_exceeded_is_suspended_and_never_rounded(self):
        for used, allowed in ((999, True), (1000, False), (1001, False)):
            with self.subTest(used=used):
                decision = evaluate_access(**values(used_bytes=used))
                self.assertEqual(decision.allowed, allowed)
                if not allowed:
                    self.assertEqual(decision.primary_reason, "quota_exhausted")
        self.assertTrue(evaluate_access(**values(quota_bytes=MAX_INTEGER, used_bytes=MAX_INTEGER - 1)).allowed)
        self.assertFalse(evaluate_access(**values(quota_bytes=MAX_INTEGER, used_bytes=MAX_INTEGER)).allowed)

    def test_new_registration_remains_pending_and_does_not_request_core_suspend(self):
        decision = evaluate_access(**values(
            status="pending", provisioning_state="not_provisioned", quota_bytes=0,
            used_bytes=0, expires_at=None, usage_state="not_connected", usage_updated_at=None,
            known_owner=False, current_observation_healthy=False,
        ))
        self.assertEqual(decision.state, "pending")
        self.assertEqual(decision.primary_reason, "membership_pending")
        self.assertFalse(decision.subscription_allowed)
        self.assertFalse(decision.requires_suspension)
        self.assertIn("expiry_not_set", decision.reasons)
        self.assertIn("quota_not_allocated", decision.reasons)

    def test_applied_but_pending_membership_is_not_treated_as_harmless(self):
        decision = evaluate_access(**values(status="pending"))
        self.assertEqual(decision.state, "suspended")
        self.assertTrue(decision.requires_suspension)
        self.assertIn("membership_pending", decision.reasons)

    def test_active_but_uncertain_provisioning_requests_idempotent_suspension(self):
        for state in ("not_provisioned", "pending_apply", "failed", "future_unknown"):
            with self.subTest(state=state):
                decision = evaluate_access(**values(provisioning_state=state))
                self.assertFalse(decision.allowed)
                self.assertTrue(decision.requires_suspension)
                self.assertIn("provisioning_not_applied", decision.reasons)

    def test_unready_metering_and_unknown_owner_cannot_issue_subscription(self):
        for changes, reason in (
            ({"known_owner": False}, "account_mapping_missing"),
            ({"usage_state": "not_connected"}, "usage_not_measured"),
            ({"usage_state": "estimated"}, "usage_not_measured"),
            ({"usage_state": "future_unknown"}, "usage_not_measured"),
            ({"usage_updated_at": None}, "usage_timestamp_missing"),
        ):
            with self.subTest(changes=changes):
                decision = evaluate_access(**values(**changes))
                self.assertFalse(decision.subscription_allowed)
                self.assertTrue(decision.requires_suspension)
                self.assertIn(reason, decision.reasons)

    def test_missing_expiry_and_zero_quota_do_not_mean_unlimited(self):
        for changes, reason in (({"expires_at": None}, "expiry_not_set"),
                                ({"quota_bytes": 0}, "quota_not_allocated")):
            decision = evaluate_access(**values(**changes))
            self.assertFalse(decision.allowed)
            self.assertTrue(decision.requires_suspension)
            self.assertIn(reason, decision.reasons)

    def test_manual_disable_has_priority_over_secondary_expiry_and_health_failures(self):
        decision = evaluate_access(**values(user_active=False, status="suspended", expires_at=NOW,
            quota_bytes=100, used_bytes=100, current_observation_healthy=False))
        self.assertEqual(decision.reasons, (
            "user_disabled", "membership_suspended", "expired", "quota_exhausted", "observation_unhealthy"))
        self.assertEqual(decision.primary_reason, "user_disabled")
        self.assertTrue(decision.requires_suspension)
        self.assertFalse(decision.enforcement_verified)

    def test_hard_denial_before_provisioning_does_not_claim_core_action_needed(self):
        decision = evaluate_access(**values(status="pending", provisioning_state="not_provisioned", expires_at=NOW))
        self.assertEqual(decision.state, "suspended")
        self.assertFalse(decision.requires_suspension)
        self.assertIn("expired", decision.reasons)

    def test_timezones_compare_the_same_instant_without_clock_access(self):
        taipei = timezone(timedelta(hours=8))
        shifted = values(now=NOW.astimezone(taipei), usage_updated_at=NOW.astimezone(taipei),
                         expires_at=(NOW + timedelta(days=1)).astimezone(taipei))
        self.assertEqual(evaluate_access(**shifted), evaluate_access(**values()))

    def test_datetime_extremes_do_not_overflow_when_comparing_freshness(self):
        early = datetime.min.replace(tzinfo=timezone.utc)
        late = datetime.max.replace(tzinfo=timezone.utc)
        self.assertTrue(evaluate_access(**values(now=early, usage_updated_at=early, expires_at=late)).allowed)
        decision = evaluate_access(**values(now=late, usage_updated_at=early, expires_at=late))
        self.assertIn("usage_stale", decision.reasons)
        self.assertIn("expired", decision.reasons)

    def test_invalid_numbers_never_become_zero_or_unlimited(self):
        for field, code in (("quota_bytes", "quota_invalid"), ("used_bytes", "usage_invalid"),
                            ("gap_count", "gap_count_invalid")):
            for value in (-1, True, False, 1.5, "100", None, MAX_INTEGER + 1):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(MeteringPolicyError, "^" + code + "$"):
                        evaluate_access(**values(**{field: value}))

    def test_naive_and_non_datetime_inputs_are_rejected(self):
        for field, code in (("now", "now_invalid"), ("expires_at", "expiry_invalid"),
                            ("usage_updated_at", "usage_timestamp_invalid")):
            for value in (NOW.replace(tzinfo=None), "2026-09-23", 1790000000, True):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(MeteringPolicyError, "^" + code + "$"):
                        evaluate_access(**values(**{field: value}))
        with self.assertRaisesRegex(MeteringPolicyError, "^now_invalid$"):
            evaluate_access(**values(now=None))

    def test_boolean_flags_and_state_inputs_are_strict_and_safe(self):
        for field, code in (("user_active", "user_active_invalid"), ("known_owner", "known_owner_invalid"),
                            ("current_observation_healthy", "observation_health_invalid")):
            for value in (1, 0, "true", None):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(MeteringPolicyError, "^" + code + "$"):
                        evaluate_access(**values(**{field: value}))
        for field, code in (("status", "membership_status_invalid"),
                            ("provisioning_state", "provisioning_state_invalid"),
                            ("usage_state", "usage_state_invalid")):
            for value in ("", "active\n", "x" * 33, None, [], True, "TOKEN-or-URL"):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(MeteringPolicyError, "^" + code + "$"):
                        evaluate_access(**values(**{field: value}))
        with self.assertRaisesRegex(MeteringPolicyError, "^membership_status_invalid$"):
            evaluate_access(**values(status="unknown"))

    def test_result_is_immutable_and_labels_do_not_claim_completed_enforcement(self):
        decision = evaluate_access(**values(current_observation_healthy=False, gap_count=1))
        with self.assertRaises(FrozenInstanceError):
            decision.state = "allowed"
        self.assertTrue(all(isinstance(text, str) and text for text in decision.reason_text))
        self.assertTrue(all(isinstance(text, str) and text for text in decision.warning_text))
        self.assertIn("此为策略决定，停用是否生效须核验执行结果", decision.warning_text)

    def test_common_membership_eligible_cases_preserve_original_boolean_contract(self):
        # 明确的现有模型验收样例；附加健康/归属条件全部满足时不放宽原门槛。
        for changes, expected in (
            ({}, True), ({"user_active": False}, False), ({"status": "pending"}, False),
            ({"status": "suspended"}, False), ({"provisioning_state": "pending_apply"}, False),
            ({"usage_state": "not_connected"}, False), ({"expires_at": None}, False),
            ({"used_bytes": 1000}, False), ({"usage_updated_at": None}, False),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(evaluate_access(**values(**changes)).allowed, expected)


if __name__ == "__main__":
    unittest.main()
