"""用户八身份候选的离线测试；只使用公开假数据，不连接生产。"""

import copy
import unittest
import uuid
from unittest.mock import patch

from bridge.identity import IdentityError, LANES, MAX_ACCOUNTS, PROTOCOLS, build_candidate


OWNER_A = "11111111-2222-4333-8444-555555555555"
OWNER_B = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def fake_account(number, protocol="reality", lane="home", enabled=True):
    """UUID 与密码均为可公开的测试常量，不能用于部署。"""
    credential = (str(uuid.UUID(int=number + 1, version=4)) if protocol in ("reality", "ws")
                  else f"public-test-password-not-a-secret-{number:04d}")
    return {"id": f"legacy-{number}", "protocol": protocol, "lane": lane,
            "enabled": enabled, "label": f"原有测试账号 {number}", "credential": credential}


def fake_authority():
    accounts = [fake_account(index, protocol, lane)
                for index, (lane, protocol) in enumerate((lane, protocol)
                    for lane in LANES for protocol in PROTOCOLS)]
    return {"schema_version": 1, "accounts": accounts, "active_home_upstream": "fixture-home",
            "upstreams": [{"id": "fixture-home", "host": "example.invalid",
                           "credential": "public-upstream-fixture", "future_field": {"keep": [1]}}],
            "policy": {"default": "home", "custom_rules": [{"label": "保留假规则"}]},
            "unknown_future_field": {"keep": ["完整保留"]}}


class IdentityTests(unittest.TestCase):
    def assert_rejected_unchanged(self, authority, owner, action, code=None):
        before = copy.deepcopy(authority)
        with self.assertRaises(IdentityError) as caught:
            build_candidate(authority, owner, action)
        if code:
            self.assertEqual(str(caught.exception), code)
        self.assertEqual(authority, before)
        return caught.exception

    def test_provision_adds_eight_distinct_matching_accounts(self):
        original = fake_authority()
        candidate, ids = build_candidate(original, OWNER_A, "provision")
        self.assertEqual(len(candidate["accounts"]), 16)
        self.assertEqual(len(ids), 8)
        self.assertEqual(candidate["accounts"][:8], original["accounts"])
        expected = [(lane, protocol) for lane in LANES for protocol in PROTOCOLS]
        actual = candidate["accounts"][8:]
        self.assertEqual([(a["lane"], a["protocol"]) for a in actual], expected)
        names = {"reality": "Reality", "hy2": "HY2", "ws": "WS", "anytls": "AnyTLS"}
        for account, (lane, protocol) in zip(actual, expected):
            self.assertEqual(set(account), {"id", "protocol", "lane", "enabled", "label", "credential"})
            self.assertEqual(account["id"], f"pnl-{uuid.UUID(OWNER_A).hex}-{'h' if lane == 'home' else 'b'}-{protocol}")
            self.assertLessEqual(len(account["id"]), 64)
            self.assertEqual(account["label"], f"{'HOME' if lane == 'home' else 'BWH'}-{names[protocol]}")
            self.assertIs(account["enabled"], True)
            self.assertNotEqual(account["credential"], OWNER_A)
            if protocol in ("reality", "ws"):
                self.assertEqual(uuid.UUID(account["credential"]).version, 4)
            else:
                self.assertGreaterEqual(len(account["credential"]), 48)
        self.assertEqual(len({a["credential"] for a in candidate["accounts"]}), 16)

    def test_copy_on_write_retains_unknown_fields_and_nesting(self):
        original = fake_authority()
        original["accounts"][0]["future_metadata"] = {"value": [1, 2]}
        before = copy.deepcopy(original)
        candidate, _ = build_candidate(original, OWNER_A, "provision")
        self.assertEqual(original, before)
        self.assertEqual({k: v for k, v in candidate.items() if k != "accounts"},
                         {k: v for k, v in original.items() if k != "accounts"})
        candidate["accounts"][0]["future_metadata"]["value"].append(3)
        candidate["upstreams"][0]["future_field"]["keep"].append(2)
        candidate["unknown_future_field"]["keep"].clear()
        self.assertEqual(original, before)

    def test_uuid_normalization_is_idempotent(self):
        first, ids = build_candidate(fake_authority(), OWNER_A, "provision")
        for owner in (OWNER_A.upper(), uuid.UUID(OWNER_A).hex, "{" + OWNER_A + "}", uuid.UUID(OWNER_A)):
            with self.subTest(owner=owner):
                again, again_ids = build_candidate(first, owner, "provision")
                self.assertEqual(again, first)
                self.assertEqual(again_ids, ids)
                self.assertIsNot(again, first)

    def test_multiple_owners_remain_separate_during_suspend_and_enable(self):
        base = fake_authority()
        first, a_ids = build_candidate(base, OWNER_A, "provision")
        both, b_ids = build_candidate(first, OWNER_B, "provision")
        self.assertTrue(set(a_ids).isdisjoint(b_ids))
        self.assertEqual(len({a["credential"] for a in both["accounts"]}), 24)
        before = copy.deepcopy(both)
        suspended, result_ids = build_candidate(both, OWNER_A, "suspend")
        self.assertEqual(result_ids, a_ids)
        for old, current in zip(before["accounts"], suspended["accounts"]):
            if current["id"] in a_ids:
                self.assertEqual(current, old | {"enabled": False})
            else:
                self.assertEqual(current, old)
        self.assertEqual(both, before)
        resumed, _ = build_candidate(suspended, OWNER_A, "enable")
        self.assertEqual(resumed, both)

    def test_repeated_provision_never_resurrects_suspended_owner(self):
        first, _ = build_candidate(fake_authority(), OWNER_A, "provision")
        suspended, _ = build_candidate(first, OWNER_A, "suspend")
        with patch("bridge.identity.uuid.uuid4", side_effect=AssertionError("不应生成凭据")):
            repeated, _ = build_candidate(suspended, OWNER_A, "provision")
        self.assertEqual(repeated, suspended)
        again, _ = build_candidate(suspended, OWNER_A, "suspend")
        self.assertEqual(again, suspended)

    def test_half_set_is_rejected_for_all_actions(self):
        candidate, ids = build_candidate(fake_authority(), OWNER_A, "provision")
        candidate["accounts"] = [a for a in candidate["accounts"] if a["id"] != ids[0]]
        for action in ("provision", "suspend", "enable"):
            self.assert_rejected_unchanged(candidate, OWNER_A, action, "owner_accounts_incomplete")

    def test_nonexistent_owner_cannot_modify_legacy_or_other_owner(self):
        candidate, _ = build_candidate(fake_authority(), OWNER_A, "provision")
        for action in ("suspend", "enable"):
            self.assert_rejected_unchanged(candidate, OWNER_B, action, "owner_accounts_missing")

    def test_reserved_owner_prefix_and_scope_conflicts_are_rejected(self):
        for action in ("provision", "suspend", "enable"):
            candidate, _ = build_candidate(fake_authority(), OWNER_A, "provision")
            candidate["accounts"][-1]["lane"] = "home"
            self.assert_rejected_unchanged(candidate, OWNER_A, action, "owner_account_scope_mismatch")
            candidate, _ = build_candidate(fake_authority(), OWNER_A, "provision")
            extra = fake_account(100, "hy2")
            extra["id"] = f"pnl-{uuid.UUID(OWNER_A).hex}-h-extra"
            candidate["accounts"].append(extra)
            self.assert_rejected_unchanged(candidate, OWNER_A, action, "owner_namespace_conflict")

    def test_capacity_counts_disabled_accounts_and_exact_boundary(self):
        candidate = fake_authority()
        candidate["accounts"].extend(fake_account(i, enabled=False) for i in range(8, 248))
        full, ids = build_candidate(candidate, OWNER_A, "provision")
        self.assertEqual(len(full["accounts"]), MAX_ACCOUNTS)
        self.assertEqual(build_candidate(full, OWNER_A, "provision"), (full, ids))
        self.assert_rejected_unchanged(full, OWNER_B, "provision", "accounts_capacity_exceeded")
        candidate["accounts"].append(fake_account(248, enabled=False))
        self.assert_rejected_unchanged(candidate, OWNER_A, "provision", "accounts_capacity_exceeded")

    def test_last_enabled_account_per_protocol_cannot_be_removed(self):
        candidate, ids = build_candidate(fake_authority(), OWNER_A, "provision")
        candidate["accounts"] = [a for a in candidate["accounts"] if a["id"] in ids]
        self.assert_rejected_unchanged(candidate, OWNER_A, "suspend", "protocol_without_enabled_account")
        for account in candidate["accounts"]:
            account["enabled"] = False
        repaired, _ = build_candidate(candidate, OWNER_A, "enable")
        self.assertTrue(all(a["enabled"] for a in repaired["accounts"]))

    def test_last_enabled_single_protocol_is_also_protected(self):
        candidate, ids = build_candidate(fake_authority(), OWNER_A, "provision")
        for account in candidate["accounts"]:
            if account["id"] not in ids and account["protocol"] == "hy2":
                account["enabled"] = False
        self.assert_rejected_unchanged(candidate, OWNER_A, "suspend", "protocol_without_enabled_account")

    def test_invalid_owners_and_actions_have_safe_errors(self):
        base = fake_authority()
        for owner in (None, 1, True, {}, [], "", "../root", OWNER_A + "\n", "x" * 100,
                      "00000000-0000-0000-0000-000000000000"):
            with self.subTest(owner=owner):
                self.assert_rejected_unchanged(base, owner, "provision", "owner_uuid_invalid")
        for action in (None, [], {}, True, "delete", "disable", "PROVISION", "enable;whoami"):
            with self.subTest(action=action):
                self.assert_rejected_unchanged(base, OWNER_A, action, "identity_action_invalid")

    def test_invalid_authority_and_account_shapes_are_rejected(self):
        for value in (None, [], "fixture", {}, {"accounts": {}}, {"accounts": []}):
            self.assert_rejected_unchanged(value, OWNER_A, "provision")
        mutations = (
            lambda a: a.append(copy.deepcopy(a[0])),
            lambda a: a[0].pop("credential"),
            lambda a: a[0].update(id="../legacy"),
            lambda a: a[0].update(protocol="vless"),
            lambda a: a[0].update(protocol=[]),
            lambda a: a[0].update(lane="direct"),
            lambda a: a[0].update(enabled=1),
            lambda a: a[0].update(label="bad\nlabel"),
            lambda a: a[0].update(credential="public-invalid-credential"),
            lambda a: a[1].update(credential="short"),
            lambda a: a[1].update(credential="x" * 31 + "\n"),
            lambda a: a[0].update(credential=str(uuid.UUID(int=1, version=1))),
            lambda a: a.extend(fake_account(i) for i in range(8, 257)),
        )
        for mutate in mutations:
            candidate = fake_authority()
            mutate(candidate["accounts"])
            self.assert_rejected_unchanged(candidate, OWNER_A, "provision")

    def test_existing_credential_reuse_including_disabled_is_rejected(self):
        base = fake_authority()
        base["accounts"][4]["credential"] = base["accounts"][0]["credential"]
        base["accounts"][4]["enabled"] = False
        self.assert_rejected_unchanged(base, OWNER_A, "provision", "account_credential_reused")

    def test_random_collision_has_bounded_safe_failure_and_no_mutation(self):
        base = fake_authority()
        collision = uuid.UUID(base["accounts"][0]["credential"])
        with patch("bridge.identity.uuid.uuid4", return_value=collision) as generator:
            self.assert_rejected_unchanged(base, OWNER_A, "provision", "credential_generation_failed")
        self.assertEqual(generator.call_count, 8)

    def test_collision_with_new_account_is_retried_and_not_reused(self):
        base = fake_authority()
        generated = [uuid.UUID(int=1000+i, version=4) for i in range(4)]
        with patch("bridge.identity.uuid.uuid4", side_effect=[generated[0], generated[0], *generated[1:]]):
            candidate, _ = build_candidate(base, OWNER_A, "provision")
        self.assertEqual(len({a["credential"] for a in candidate["accounts"]}), 16)

    def test_owner_identifier_is_never_used_as_credential(self):
        base = fake_authority()
        with patch("bridge.identity.uuid.uuid4", return_value=uuid.UUID(OWNER_A)):
            self.assert_rejected_unchanged(base, OWNER_A, "provision", "credential_generation_failed")


if __name__ == "__main__":
    unittest.main()
