"""候选/check隔离测试：不读取真实核心材料，不执行生产命令。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import shutil
import unittest
import uuid
from unittest.mock import patch

from bridge import core_candidate_check as check


class CandidateSecurityPrimitiveTests(unittest.TestCase):
    def test_duplicate_json_keys_rejected(self):
        with self.assertRaisesRegex(check.CandidateCheckError, "duplicate_json_key"):
            check._decoded(b'{"log":{},"log":{"level":"debug"}}')

    def test_non_object_and_non_finite_json_rejected(self):
        for data in (b"[]", b'{"x":NaN}', b"not json"):
            with self.subTest(data=data), self.assertRaises(check.CandidateCheckError):
                check._decoded(data)

    def test_relative_path_and_traversal_rejected(self):
        for value in ("relative.json", Path.cwd() / ".." / "config.json"):
            with self.subTest(value=value), self.assertRaisesRegex(check.CandidateCheckError, "unsafe_path"):
                check.safe_path(value)

    def test_secret_process_output_is_never_captured(self):
        with patch.object(check.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            self.assertEqual(check.run_fixed_check(["fixed-core", "check"], timeout=3, cwd=Path.cwd()), 1)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertNotIn("capture_output", run.call_args.kwargs)

    def test_windows_private_mode_is_not_claimed_as_0600(self):
        with patch.object(check.os, "name", "nt"):
            with self.assertRaisesRegex(check.CandidateCheckError, "private_permissions_unverified"):
                check.private_path_guard("unused")


class CoreCandidateCheckTests(unittest.TestCase):
    def setUp(self):
        self.test_parent = Path(__file__).resolve().parents[2] / "staging" / "core-candidate-tests"
        self.test_parent.mkdir(exist_ok=True)
        self.root = self.test_parent / uuid.uuid4().hex
        self.root.mkdir()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.source = self.root / "active.json"
        self.binary = self.root / "fixed-core.py"
        self.evidence = self.root / "owner.json"
        self.binary.write_text("import sys\nprint('FAKE_SECRET_MUST_NOT_ESCAPE')\nsys.exit(0)\n", encoding="utf-8")
        self.original = {"log": {"loglevel": "warning"}, "inbounds": [{"password": "FAKE_PRIVATE_VALUE"}],
                         "routing": {"domainStrategy": "AsIs"}}
        self.source.write_bytes(check._encoded(self.original))
        self.profile = check.CoreProfile(core_id="core-fixture", engine="xray", binary_path=str(self.binary),
            binary_sha256=check._sha(self.binary.read_bytes()), binary_version="isolated-1",
            source_path=str(self.source), active_path=str(self.source), artifact_root=str(self.artifacts),
            owner_id="isolated-owner", owner_evidence_path=str(self.evidence), owner_evidence_sha256="0" * 64,
            source_revision="source-1", identity_generation=1, service_unit="isolated.service")
        self.refresh_evidence()
        self.calls = []
        self.checker = check.CoreCandidateChecker(self.profile, private_path_guard=self.guard, runner=self.run_fake_core)

    def tearDown(self):
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, self.test_parent.resolve())
        shutil.rmtree(resolved)

    def guard(self, path):
        # 这里只证明隔离测试路径；不冒充Windows生产ACL或Linux0600验收。
        path = check.safe_path(path)
        self.assertTrue(path.is_relative_to(self.root))
        self.assertTrue(path.exists())

    def refresh_evidence(self, **changes):
        data = {key: getattr(self.profile, key) for key in ("core_id", "owner_id", "engine", "source_path",
            "active_path", "generator_mode", "binary_path", "binary_sha256", "binary_version",
            "source_revision", "identity_generation", "service_unit")}
        data["exclusive_writer"] = True
        data.update(changes)
        self.evidence.write_bytes(check._encoded(data))
        self.profile = replace(self.profile, owner_evidence_sha256=check._sha(self.evidence.read_bytes()))

    def run_fake_core(self, argv, **kwargs):
        self.calls.append(argv)
        # 只启动本测试写出的假脚本，真实核心与系统服务均未调用。
        return check.run_fixed_check([sys.executable, str(self.binary), *argv[1:]], **kwargs)

    def prepare(self, level="info"):
        return self.checker.prepare_candidate({"log": {"level": level}})

    def run_check(self, prepared, fence=1):
        return self.checker.check_candidate(prepared["artifact_ref"], check_fence=fence,
            **{key: prepared[key] for key in ("base_sha256", "candidate_sha256", "binding_fingerprint")})

    def test_prepare_preserves_secret_and_all_unrelated_fields(self):
        before = self.source.read_bytes()
        prepared = self.prepare()
        artifact = self.checker.load_artifact(prepared["artifact_ref"])
        result = json.loads(artifact["candidate"])
        self.assertEqual(result["log"]["loglevel"], "info")
        self.assertEqual(result["inbounds"], self.original["inbounds"])
        self.assertEqual(result["routing"], self.original["routing"])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(artifact["base"], before)
        self.assertEqual(prepared["previous_level"], "warning")
        self.assertRegex(prepared["artifact_ref"], r"^[a-f0-9]{32}$")
        self.assertNotIn("FAKE_PRIVATE_VALUE", json.dumps(prepared))
        self.assertNotIn(str(self.root), json.dumps(prepared))

    def test_real_fake_binary_check_succeeds_and_preserves_active(self):
        before = self.source.read_bytes()
        prepared = self.prepare()
        result = self.run_check(prepared, fence=7)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["check_fence"], 7)
        self.assertEqual(self.checker.load_check(prepared["artifact_ref"]), result)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.calls[0][1:4], ["run", "-test", "-config"])
        self.assertNotIn("FAKE_SECRET", json.dumps(result))

    def test_replayed_check_does_not_execute_twice_and_old_fence_rejected(self):
        prepared = self.prepare()
        result = self.run_check(prepared, fence=7)
        self.assertEqual(result, self.run_check(prepared, fence=7))
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(check.CandidateCheckError, "stale_check_fence"):
            self.run_check(prepared, fence=6)

    def test_failed_check_never_changes_active_and_redacts_exception(self):
        prepared = self.prepare()
        before = self.source.read_bytes()
        self.checker.runner = lambda *a, **k: 2
        result = self.run_check(prepared)
        self.assertEqual((result["status"], result["error_code"]), ("failed", "check_failed"))
        self.assertEqual(self.source.read_bytes(), before)
        self.checker.runner = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("FAKE_PRIVATE_VALUE"))
        result = self.run_check(prepared, fence=2)
        self.assertEqual(result["error_code"], "check_execution_failed")
        self.assertNotIn("FAKE_PRIVATE_VALUE", json.dumps(result))

    def test_timeout_is_unknown_and_does_not_change_active(self):
        prepared = self.prepare()
        before = self.source.read_bytes()
        self.checker.runner = lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("SECRET", 1))
        result = self.run_check(prepared)
        self.assertEqual((result["status"], result["error_code"]), ("unknown", "check_timeout"))
        self.assertEqual(before, self.source.read_bytes())
        self.assertNotIn("SECRET", json.dumps(result))

    def test_source_drift_before_check_does_not_run_binary(self):
        prepared = self.prepare()
        changed = b'{"log":{"loglevel":"error"}}'
        self.source.write_bytes(changed)
        with self.assertRaisesRegex(check.CandidateCheckError, "source_changed"):
            self.run_check(prepared)
        self.assertFalse(self.calls)
        self.assertEqual(self.source.read_bytes(), changed)

    def test_source_drift_during_check_cannot_produce_success(self):
        prepared = self.prepare()
        def mutate(*args, **kwargs):
            self.source.write_bytes(b"{}")
            return 0
        self.checker.runner = mutate
        result = self.run_check(prepared)
        self.assertEqual(result["error_code"], "check_inputs_changed")

    def test_binary_drift_rejected(self):
        prepared = self.prepare()
        self.binary.write_bytes(b"changed")
        with self.assertRaisesRegex(check.CandidateCheckError, "binary_changed"):
            self.run_check(prepared)
        self.assertFalse(self.calls)

    def test_owner_evidence_hash_or_exclusive_proof_required(self):
        self.evidence.write_bytes(b"{}")
        self.assertFalse(self.checker.binding_status()["available"])
        self.refresh_evidence(exclusive_writer=False)
        self.checker.profile = self.profile
        with self.assertRaisesRegex(check.CandidateCheckError, "ownership_unverified"):
            self.prepare()

    def test_evidence_source_and_service_unit_must_match(self):
        for change in ({"source_path": "different"}, {"service_unit": "other.service"}):
            with self.subTest(change=change):
                self.refresh_evidence(**change)
                self.checker.profile = self.profile
                with self.assertRaisesRegex(check.CandidateCheckError, "ownership_unverified"):
                    self.prepare()

    def test_mack_a_and_generated_final_config_rejected(self):
        self.checker.profile = replace(self.profile, generator_mode="mack-a-merged")
        with self.assertRaisesRegex(check.CandidateCheckError, "generator_not_supported"):
            self.prepare()
        self.checker.profile = replace(self.profile, source_path=str(self.root / "fragment.json"))
        with self.assertRaisesRegex(check.CandidateCheckError, "persistent_source_unverified"):
            self.prepare()

    def test_unknown_patch_does_not_create_artifact(self):
        for data in ({"log": {"level": "info", "password": "x"}}, {"routing": {}},
                     {"log": {"level": "trace"}}, {"log": {"level": True}}):
            with self.subTest(data=data), self.assertRaisesRegex(check.CandidateCheckError, "unsupported_patch"):
                self.checker.prepare_candidate(data)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_oversized_source_and_duplicate_config_keys_rejected(self):
        self.source.write_bytes(b"x" * (check.MAX_CONFIG_BYTES + 1))
        with self.assertRaisesRegex(check.CandidateCheckError, "material_too_large"):
            self.prepare()
        self.source.write_bytes(b'{"log":{},"log":{}}')
        with self.assertRaisesRegex(check.CandidateCheckError, "duplicate_json_key"):
            self.prepare()

    def test_artifact_tampering_is_rejected(self):
        prepared = self.prepare()
        (self.artifacts / prepared["artifact_ref"] / "candidate.json").write_bytes(b"{}")
        with self.assertRaisesRegex(check.CandidateCheckError, "artifact_hash_mismatch"):
            self.run_check(prepared)
        self.assertFalse(self.calls)

    def test_wrong_expected_hash_and_ref_rejected(self):
        prepared = self.prepare()
        prepared["candidate_sha256"] = "0" * 64
        with self.assertRaisesRegex(check.CandidateCheckError, "check_target_mismatch"):
            self.run_check(prepared)
        for ref in ("../active", "a" * 33, "A" * 32, str(self.source)):
            with self.subTest(ref=ref), self.assertRaisesRegex(check.CandidateCheckError, "invalid_artifact_ref"):
                self.checker.load_artifact(ref)

    def test_receipt_other_identity_or_hash_is_rejected(self):
        prepared = self.prepare()
        result = self.run_check(prepared)
        result["identity_generation"] = 2
        (self.artifacts / prepared["artifact_ref"] / "check.json").write_bytes(check._encoded(result))
        with self.assertRaisesRegex(check.CandidateCheckError, "check_receipt_mismatch"):
            self.checker.load_check(prepared["artifact_ref"])

    def test_receipt_extra_secret_field_is_never_returned(self):
        prepared = self.prepare()
        result = self.run_check(prepared)
        result["stderr"] = "FAKE_SECRET_MUST_NOT_ESCAPE"
        (self.artifacts / prepared["artifact_ref"] / "check.json").write_bytes(check._encoded(result))
        with self.assertRaisesRegex(check.CandidateCheckError, "check_receipt_mismatch"):
            self.checker.load_check(prepared["artifact_ref"])

    def test_fixed_service_unit_changes_binding_fingerprint(self):
        original = self.checker.verify_binding()
        self.profile = replace(self.profile, service_unit="another.service")
        self.refresh_evidence()
        self.checker.profile = self.profile
        self.assertNotEqual(original, self.checker.verify_binding())

    def test_binary_limit_is_enforced_before_execution(self):
        with patch.object(check, "MAX_BINARY_BYTES", 1):
            with self.assertRaisesRegex(check.CandidateCheckError, "material_too_large"):
                self.prepare()
        self.assertFalse(self.calls)

    def test_material_created_exclusively_with_mode_0600(self):
        with patch.object(check.os, "open", wraps=os.open) as opening:
            self.prepare()
        creation = [call for call in opening.call_args_list if call.args[1] & os.O_CREAT]
        self.assertEqual(len(creation), 3)
        self.assertTrue(all(call.args[2] == 0o600 and call.args[1] & os.O_EXCL for call in creation))

    def test_symlink_and_reparse_path_metadata_rejected(self):
        real_lstat = Path.lstat
        def linked(path, *args, **kwargs):
            original = real_lstat(path, *args, **kwargs)
            if path == self.source:
                from types import SimpleNamespace
                return SimpleNamespace(st_mode=original.st_mode, st_file_attributes=0x400)
            return original
        with patch.object(Path, "lstat", linked):
            with self.assertRaisesRegex(check.CandidateCheckError, "unsafe_path"):
                self.prepare()

    def test_hard_link_material_rejected(self):
        os.link(self.source, self.root / "hardlinked.json")
        with self.assertRaisesRegex(check.CandidateCheckError, "unsafe_file"):
            self.prepare()

    def test_sing_box_normalized_level_preserves_other_fields(self):
        self.profile = replace(self.profile, engine="sing-box")
        self.refresh_evidence()
        self.checker.profile = self.profile
        self.source.write_bytes(check._encoded({"log": {"level": "warn", "disabled": True},
                                                "inbounds": [{"password": "FAKE_PRIVATE_VALUE"}]}))
        prepared = self.prepare("warning")
        data = json.loads(self.checker.load_artifact(prepared["artifact_ref"])["candidate"])
        self.assertEqual(prepared["previous_level"], "none")
        self.assertEqual(data["log"], {"level": "warn", "disabled": False})
        self.assertEqual(self.run_check(prepared)["status"], "succeeded")
        self.assertEqual(self.calls[0][1:3], ["check", "-c"])
        disabled = self.prepare("none")
        data = json.loads(self.checker.load_artifact(disabled["artifact_ref"])["candidate"])
        self.assertTrue(data["log"]["disabled"])


if __name__ == "__main__":
    unittest.main()
