"""生产桥接调用合同测试；全部依赖注入，不执行核心、服务或网络命令。"""

import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
import uuid

from bridge.identity import build_candidate
from bridge.production import ProductionBridge, ProductionError, REQUIRED_ARTIFACTS


OWNER = "12345678-1234-4234-9234-123456789abc"
OTHER_OWNER = "22345678-1234-4234-9234-123456789abc"


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


class FakeManager:
    """仿真实签名的记录器；只有测试文件写入，没有外部进程。"""

    def __init__(self, root, admin):
        self.root, self.admin, self.events = root, admin, []
        self.paths = {name: root / name for name in ("authority", "base", "rules", "core", "dependency")}
        self.settings = {"reality_public_key": "PUBLIC_FAKE_KEY"}
        self.xray_enabled = True
        self.policy_module = SimpleNamespace(
            validate_authority=self.validate_authority,
            project_client_parameters=self.project_client_parameters,
            load_rule_documents=lambda authority, rules: {"public": True},
        )
        accounts = []
        for lane in ("home", "bwh"):
            for protocol in ("reality", "hy2", "ws", "anytls"):
                accounts.append({"id": lane + "-" + protocol, "lane": lane, "protocol": protocol,
                    "enabled": True, "label": "公开测试身份", "credential": str(uuid.uuid4())
                    if protocol in ("reality", "ws") else "PUBLIC_TEST_PASSWORD_ONLY_" + lane + "_" + protocol + "_" * 20})
        self.initial = {"schema_version": 1, "accounts": accounts, "upstreams": [], "active_home_upstream": "test",
                        "policy": {"public": True}}
        admin.atomic(self.paths["authority"], encoded(self.initial))
        admin.atomic(self.paths["base"], b"{}")
        admin.atomic(self.paths["core"], b"PUBLIC_FAKE_BINARY_NOT_EXECUTABLE")
        admin.atomic(self.paths["dependency"], b"PUBLIC_DEPENDENCY")
        self.fail_check = False
        self.fail_publish = False
        self.publish_count = 0

    def policy(self):
        return self.policy_module

    def check(self):
        self.events.append("check")
        return {}

    def authority(self):
        return self.admin.read(self.paths["authority"])

    def dependencies(self):
        return {str(self.paths[key]): self.admin.digest(self.paths[key]) for key in ("base", "dependency")}

    def validate_authority(self, candidate):
        self.events.append("validate_authority")
        if type(candidate) is not dict or type(candidate.get("accounts")) is not list:
            raise ValueError("public_fixture_invalid")

    def compiled(self, candidate):
        self.events.append("compile_sing_box")
        return {"users": candidate["accounts"]}

    def compiled_xray(self, candidate):
        self.events.append("compile_xray")
        return {"public_xray_candidate": True}

    def core_check(self, config, binary=None):
        self.events.append("core_check")
        if self.fail_check:
            raise ValueError("PUBLIC_FAKE_PRIVATE_DETAIL_NOT_FOR_ERRORS")
        if not Path(config).is_file():
            raise ValueError("check_requires_file")

    def xray_core_check(self, config):
        self.events.append("xray_core_check")

    def project_client_parameters(self, authority, base, reality_public_key):
        self.events.append("project_candidate")
        return {"accounts": copy.deepcopy(authority["accounts"]), "policy": {}}

    def publish(self, authority, *, expected_hash=None):
        self.events.append("publish")
        self.publish_count += 1
        if self.fail_publish:
            raise ValueError("PUBLIC_FAKE_PRIVATE_DETAIL_NOT_FOR_ERRORS")
        if self.admin.digest(self.paths["authority"]) != expected_hash:
            raise ValueError("authority_changed")
        self.admin.atomic(self.paths["authority"], encoded(authority))
        return self.root / "backups" / "20260923T000000Z-0123abcd"


class ProductionBridgeTests(unittest.TestCase):
    def setUp(self):
        # Windows 下继承工作区 ACL，避免 tempfile 0700 特殊权限；只删除本次目录。
        self.root = Path(__file__).resolve().parent / (".production-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup)
        self.admin = SimpleNamespace(
            read=lambda path: json.loads(Path(path).read_text(encoding="utf-8")),
            digest=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            encoded=encoded,
            atomic=lambda path, data: Path(path).write_bytes(data),
            private=lambda path, directory=False: None,
        )
        self.manager = FakeManager(self.root, self.admin)
        self.cn = self.root / "cn.json"
        self.proxy = self.root / "proxy.json"
        self.cn.write_text('{"version":3,"rules":[]}', encoding="utf-8")
        self.proxy.write_text('{"version":3,"rules":[]}', encoding="utf-8")
        self.work = self.root / "work"
        self.work.mkdir()
        self.module_file = self.root / "public-module.py"
        self.module_file.write_text("# 公开依赖注入夹具\n", encoding="utf-8")
        self.admin.__file__ = str(self.module_file)
        self.generated = {}
        self.validation_fails = False
        self.core_result = "PASS"
        self.missing_artifact = None
        self.builder = SimpleNamespace(__file__=str(self.module_file), CORE_VERSION="1.14.1", SCHEMA_VERSION=1,
            p7=SimpleNamespace(__file__=str(self.module_file), loads_unique=json.loads), build_bundle=self.build_bundle)
        # 复用真实账户筛选器，验证多用户投影未混入旧拥有者账户。
        subscription_path = Path(__file__).resolve().parents[2] / "staging" / "p8-subscriptions" / "relay_subscription.py"
        import importlib.util
        spec = importlib.util.spec_from_file_location("public_contract_subscription", subscription_path)
        real_subscription = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real_subscription)
        self.subscriptions = SimpleNamespace(__file__=str(self.module_file),
            select_subscription_accounts=real_subscription.select_subscription_accounts,
            validate_payloads=self.validate_payloads)
        self.runtime = {"cn_rules": str(self.cn), "blacklist_rules": str(self.proxy),
                        "android_core": str(self.manager.paths["core"]), "account_ids": ["owner-must-not-be-used"]}
        self.bridge = self.make_bridge()

    def cleanup(self):
        root = self.root.resolve()
        if root.parent != Path(__file__).resolve().parent or not root.name.startswith(".production-test-"):
            raise RuntimeError("test_cleanup_scope")
        if root.exists():
            shutil.rmtree(root)

    def make_bridge(self):
        return ProductionBridge(self.manager, admin=self.admin, builder=self.builder,
            subscriptions=self.subscriptions, subscription_runtime=self.runtime, work_root=self.work)

    def build_bundle(self, parameters, cn_source, documents, output, *, blacklist_source=None, fixture=False, core=None):
        self.manager.events.append("build_bundle")
        self.assertFalse(fixture)
        self.assertEqual(core, self.runtime["android_core"])
        self.assertEqual(len(parameters["accounts"]), 8)
        self.assertTrue(all(row["id"].startswith("pnl-") for row in parameters["accounts"]))
        self.generated["parameters"] = copy.deepcopy(parameters)
        output.mkdir()
        manifest = {"public_fixture": False, "core_check": {"result": self.core_result, "version": "1.14.1"}}
        self.generated[str(output)] = manifest
        return manifest

    def validate_payloads(self, export):
        self.manager.events.append("validate_payloads")
        if self.validation_fails:
            raise ValueError("PUBLIC_FAKE_PRIVATE_DETAIL_NOT_FOR_ERRORS")
        payloads = {key: b"PUBLIC_FAKE_PAYLOAD" for key in REQUIRED_ARTIFACTS if key != self.missing_artifact}
        return payloads, self.generated[str(export)]

    def prepare(self):
        authority, before_hash = self.bridge.snapshot()
        candidate, identifiers = build_candidate(authority, OWNER, "provision")
        return candidate, identifiers, before_hash

    def test_snapshot_returns_detached_authority_and_raw_file_digest(self):
        authority, digest = self.bridge.snapshot()
        self.assertEqual(digest, self.admin.digest(self.manager.paths["authority"]))
        authority["policy"]["public"] = False
        self.assertTrue(self.manager.authority()["policy"]["public"])
        self.assertTrue(self.bridge._before["policy"]["public"])

    def test_validate_render_then_publish_uses_real_signatures_and_order(self):
        candidate, identifiers, before_hash = self.prepare()
        self.bridge.validate(candidate)
        payloads = self.bridge.render(candidate, identifiers)
        self.assertEqual(set(payloads), REQUIRED_ARTIFACTS)
        self.assertEqual(self.manager.publish_count, 0)
        after_hash = self.bridge.publish(candidate, before_hash)
        self.assertEqual(after_hash, self.admin.digest(self.manager.paths["authority"]))
        self.assertEqual(self.bridge.last_backup, "20260923T000000Z-0123abcd")
        self.assertLess(self.manager.events.index("validate_payloads"), self.manager.events.index("publish"))
        self.assertEqual(set(row["id"] for row in self.generated["parameters"]["accounts"]), set(identifiers))
        self.assertEqual(self.manager.authority(), candidate)

    def test_activation_without_render_is_rejected(self):
        candidate, _, digest = self.prepare()
        self.bridge.validate(candidate)
        with self.assertRaisesRegex(ProductionError, "client_render_required"):
            self.bridge.publish(candidate, digest)
        self.assertEqual(self.manager.publish_count, 0)

    def test_render_or_publish_without_validation_is_rejected(self):
        candidate, identifiers, digest = self.prepare()
        with self.assertRaisesRegex(ProductionError, "candidate_not_validated"):
            self.bridge.render(candidate, identifiers)
        with self.assertRaisesRegex(ProductionError, "candidate_not_validated"):
            self.bridge.publish(candidate, digest)

    def test_failed_core_check_never_activates_identity_or_leaks_error(self):
        candidate, _, digest = self.prepare()
        self.manager.fail_check = True
        with self.assertRaisesRegex(ProductionError, "^candidate_validation_failed$"):
            self.bridge.validate(candidate)
        self.assertEqual(self.manager.publish_count, 0)
        self.assertEqual(self.admin.digest(self.manager.paths["authority"]), digest)

    def test_failed_payload_validation_never_activates_identity(self):
        candidate, identifiers, digest = self.prepare()
        self.bridge.validate(candidate)
        self.validation_fails = True
        with self.assertRaisesRegex(ProductionError, "^client_render_failed$"):
            self.bridge.render(candidate, identifiers)
        with self.assertRaises(ProductionError):
            self.bridge.publish(candidate, digest)
        self.assertEqual(self.manager.publish_count, 0)

    def test_syntax_not_tested_or_missing_resource_is_rejected(self):
        candidate, identifiers, _ = self.prepare()
        self.bridge.validate(candidate)
        self.core_result = "NOT TESTED"
        with self.assertRaisesRegex(ProductionError, "client_core_check_required"):
            self.bridge.render(candidate, identifiers)
        self.core_result = "PASS"
        self.missing_artifact = "v2rayng-geoip"
        with self.assertRaisesRegex(ProductionError, "client_payloads_incomplete"):
            self.bridge.render(candidate, identifiers)

    def test_stale_authority_or_rule_source_blocks_publication(self):
        candidate, identifiers, digest = self.prepare()
        self.bridge.validate(candidate)
        self.bridge.render(candidate, identifiers)
        self.cn.write_text('{"version":3,"rules":[{}]}', encoding="utf-8")
        with self.assertRaisesRegex(ProductionError, "production_sources_changed"):
            self.bridge.publish(candidate, digest)
        self.cn.write_text('{"version":3,"rules":[]}', encoding="utf-8")
        self.manager.paths["authority"].write_bytes(encoded({**self.manager.initial, "policy": {"changed": True}}))
        with self.assertRaisesRegex(ProductionError, "authority_changed"):
            self.bridge.publish(candidate, digest)
        self.assertEqual(self.manager.publish_count, 0)

    def test_changed_candidate_requires_new_validation(self):
        candidate, identifiers, digest = self.prepare()
        self.bridge.validate(candidate)
        self.bridge.render(candidate, identifiers)
        candidate["accounts"][-1]["enabled"] = False
        with self.assertRaisesRegex(ProductionError, "candidate_not_validated"):
            self.bridge.publish(candidate, digest)

    def test_only_identity_changes_and_one_owner_are_allowed(self):
        candidate, _, _ = self.prepare()
        altered = copy.deepcopy(candidate)
        altered["active_home_upstream"] = "other"
        with self.assertRaisesRegex(ProductionError, "candidate_not_identity_only"):
            self.bridge.validate(altered)
        altered, _ = build_candidate(candidate, OTHER_OWNER, "provision")
        with self.assertRaisesRegex(ProductionError, "candidate_multiple_owners"):
            self.bridge.validate(altered)
        altered = copy.deepcopy(candidate)
        altered["accounts"][0]["enabled"] = False
        with self.assertRaisesRegex(ProductionError, "candidate_legacy_account_changed"):
            self.bridge.validate(altered)

    def test_mixed_or_incomplete_owner_account_selection_is_rejected(self):
        candidate, identifiers, _ = self.prepare()
        self.bridge.validate(candidate)
        with self.assertRaisesRegex(ProductionError, "owner_accounts_required"):
            self.bridge.render(candidate, identifiers[:-1])
        mixed = identifiers[:-1] + [identifiers[-1].replace(OWNER.replace("-", ""), OTHER_OWNER.replace("-", ""))]
        with self.assertRaisesRegex(ProductionError, "owner_accounts_mixed"):
            self.bridge.render(candidate, mixed)

    def test_suspend_requires_validation_but_not_unpublishable_disabled_bundle(self):
        candidate, identifiers, digest = self.prepare()
        self.bridge.validate(candidate)
        self.bridge.render(candidate, identifiers)
        self.bridge.publish(candidate, digest)
        current, digest = self.bridge.snapshot()
        suspended, _ = build_candidate(current, OWNER, "suspend")
        self.bridge.validate(suspended)
        self.bridge.publish(suspended, digest)
        self.assertFalse(any(row["enabled"] for row in self.manager.authority()["accounts"] if row["id"] in identifiers))

    def test_unknown_builder_version_missing_module_path_or_wrong_core_rejected(self):
        self.builder.CORE_VERSION = "999.0.0"
        with self.assertRaisesRegex(ProductionError, "builder_version_unreviewed"):
            self.make_bridge()
        self.builder.CORE_VERSION = "1.14.1"
        self.runtime["android_core"] = "/unreviewed/core"
        with self.assertRaisesRegex(ProductionError, "android_core_scope_mismatch"):
            self.make_bridge()
        self.runtime["android_core"] = str(self.manager.paths["core"])
        self.builder.__file__ = None
        with self.assertRaisesRegex(ProductionError, "production_module_path_unconfirmed"):
            self.make_bridge().snapshot()

    def test_publish_errors_are_sanitized_and_not_blindly_retried(self):
        candidate, identifiers, digest = self.prepare()
        self.bridge.validate(candidate)
        self.bridge.render(candidate, identifiers)
        self.manager.fail_publish = True
        with self.assertRaisesRegex(ProductionError, "^production_publish_failed$") as caught:
            self.bridge.publish(candidate, digest)
        self.assertIsNone(caught.exception.applied)
        self.assertEqual(self.manager.publish_count, 1)
        self.assertEqual(self.admin.digest(self.manager.paths["authority"]), digest)


if __name__ == "__main__":
    unittest.main()
