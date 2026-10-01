"""纯命令构造器测试；所有参数都是公开假数据，不连接或修改服务器。"""

import unittest

from portal.operation_policy import (
    ACTION_IMPACTS,
    ALLOWED_ACTIONS,
    OperationPolicyError,
    build_command,
)


PREFIX = [
    "/usr/bin/python3", "-B", "/etc/v2ray-agent/relay-management/relay_admin.py",
    "--runtime", "/etc/v2ray-agent/relay-management/runtime.json",
]
FAKE_SHA = "a" * 64
FAKE_CANDIDATE = "core-20260922T120000Z-0123abcd"


class OperationPolicyTests(unittest.TestCase):
    def test_all_allowed_actions_have_exact_fixed_command(self):
        cases = {
            **{name: ({}, [name]) for name in (
                "status", "check", "backup", "backups", "accounts", "upstreams"
            )},
            "core_status": ({}, ["core-status"]),
            "core_prepare": (
                {"version": "1.14.1", "archive_sha256": FAKE_SHA},
                ["core-prepare", "1.14.1", "--archive-sha256", FAKE_SHA, "--confirm"],
            ),
            "core_apply": (
                {"candidate_id": FAKE_CANDIDATE}, ["core-apply", FAKE_CANDIDATE, "--confirm"],
            ),
            "upstream_select": (
                {"upstream_id": "test-home_1"}, ["upstream", "select", "test-home_1", "--confirm"],
            ),
            "service_restart": ({}, ["service", "restart", "--confirm"]),
        }
        self.assertEqual(ALLOWED_ACTIONS, frozenset(cases))
        for action, (payload, suffix) in cases.items():
            with self.subTest(action=action):
                self.assertEqual(build_command(action, payload), PREFIX + suffix)

    def test_unknown_or_non_string_action_is_rejected(self):
        for action in ("shell", "command", "refresh", "subscription_refresh", "rollback",
                       "service_stop", "core-status", "STATUS", "status;id", "status\n", None, [], 1):
            with self.subTest(action=action):
                with self.assertRaises(OperationPolicyError):
                    build_command(action, {})

    def test_no_arguments_allowed_on_simple_actions(self):
        actions = ALLOWED_ACTIONS - {"core_prepare", "core_apply", "upstream_select"}
        for action in actions:
            for payload in ({"command": "id"}, {"runtime": "/tmp/test.json"},
                            {"online": True}, {"confirm": True}, {"service": "other.service"}):
                with self.subTest(action=action, payload=payload):
                    with self.assertRaises(OperationPolicyError):
                        build_command(action, payload)

    def test_payload_requires_plain_dictionary_with_exact_keys(self):
        class DictSubclass(dict):
            pass

        for payload in (None, [], "{}", 0, DictSubclass()):
            with self.subTest(payload=payload):
                with self.assertRaises(OperationPolicyError):
                    build_command("status", payload)
        for action, payload in (
            ("core_prepare", {"version": "1.14.1"}),
            ("core_apply", {"id": FAKE_CANDIDATE}),
            ("upstream_select", {"id": "test-home"}),
            ("core_prepare", {"version": "1.14.1", "archive_sha256": FAKE_SHA, "url": "https://example.invalid/"}),
            ("core_apply", {"candidate_id": FAKE_CANDIDATE, "runtime": "/tmp/test.json"}),
            ("upstream_select", {"upstream_id": "test-home", "credential_file": "/tmp/test"}),
        ):
            with self.subTest(action=action, payload=payload):
                with self.assertRaises(OperationPolicyError):
                    build_command(action, payload)

    def test_upstream_identifier_rejects_paths_options_and_injection(self):
        for identifier in ("../home", "/root/home", "--runtime", "test;id", "test && id",
                           "$(id)", "`id`", "test\n", "a\x00b", "测试", "a" * 65, "", 1, True, []):
            with self.subTest(identifier=identifier):
                with self.assertRaises(OperationPolicyError):
                    build_command("upstream_select", {"upstream_id": identifier})
        self.assertEqual(build_command("upstream_select", {"upstream_id": "a" * 64})[-2], "a" * 64)

    def test_core_candidate_identifier_has_fixed_format(self):
        for identifier in ("../" + FAKE_CANDIDATE, FAKE_CANDIDATE + ";id", FAKE_CANDIDATE + "\n",
                           "20260922T120000Z-0123abcd", "core-latest", "core-20260922T120000Z-0123ABCD",
                           "/root/candidate", None, {}, True):
            with self.subTest(identifier=identifier):
                with self.assertRaises(OperationPolicyError):
                    build_command("core_apply", {"candidate_id": identifier})

    def test_only_stable_version_and_lowercase_sha256_are_accepted(self):
        for version in ("latest", "v1.14.1", "1.14", "1.14.1-beta.1", "1.14.1+build", "01.14.1",
                        "1.14.1;id", "1.14.1\n", "../1.14.1", "1" * 100 + ".1.1", 1.14, None):
            with self.subTest(version=version):
                with self.assertRaises(OperationPolicyError):
                    build_command("core_prepare", {"version": version, "archive_sha256": FAKE_SHA})
        for digest in ("a" * 63, "a" * 65, "A" * 64, "g" * 64, FAKE_SHA + "\n", "$(id)", None, 1):
            with self.subTest(digest=digest):
                with self.assertRaises(OperationPolicyError):
                    build_command("core_prepare", {"version": "1.14.1", "archive_sha256": digest})

    def test_input_and_returned_commands_do_not_mutate_future_commands(self):
        payload = {"candidate_id": FAKE_CANDIDATE}
        result = build_command("core_apply", payload)
        result[0] = "/tmp/untrusted"
        result.append("--runtime=/tmp/untrusted")
        self.assertEqual(payload, {"candidate_id": FAKE_CANDIDATE})
        self.assertEqual(build_command("core_apply", payload), PREFIX + ["core-apply", FAKE_CANDIDATE, "--confirm"])

    def test_validation_errors_do_not_echo_input(self):
        marker = "DO_NOT_ECHO_FAKE_INPUT"
        with self.assertRaises(OperationPolicyError) as error:
            build_command("upstream_select", {"upstream_id": marker + ";id"})
        self.assertNotIn(marker, str(error.exception))

    def test_restart_impact_names_both_cores(self):
        self.assertEqual(frozenset(ACTION_IMPACTS), ALLOWED_ACTIONS)
        for action in ("service_restart", "core_apply"):
            with self.subTest(action=action):
                self.assertIn("sing-box", ACTION_IMPACTS[action])
                self.assertIn("Xray", ACTION_IMPACTS[action])
                self.assertIn("重启", ACTION_IMPACTS[action])
        with self.assertRaises(TypeError):
            ACTION_IMPACTS["service_restart"] = "无影响"


if __name__ == "__main__":
    unittest.main()
