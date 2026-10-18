"""固定核心的私有候选与配置检查；不负责接管、应用或重启。"""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid
from dataclasses import asdict, dataclass


MAX_CONFIG_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 16384
MAX_BINARY_BYTES = 128 * 1024 * 1024
HASH = re.compile(r"[a-f0-9]{64}")
ARTIFACT_REF = re.compile(r"[a-f0-9]{32}")
LEVELS = ("debug", "info", "warning", "error", "none")


class CandidateCheckError(Exception):
    """仅携带固定错误码，禁止将配置、路径或子进程输出带到API。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise CandidateCheckError(code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _object(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _decoded(data):
    try:
        result = json.loads(data, object_pairs_hook=_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise CandidateCheckError("invalid_json") from None
    _need(type(result) is dict, "invalid_json")
    return result


def safe_path(value):
    """不解析软链；逐级拒绝软链、junction及路径穿越。"""
    path = Path(value)
    _need(path.is_absolute() and ".." not in path.parts, "unsafe_path")
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise CandidateCheckError("unsafe_path") from None
        _need(not stat.S_ISLNK(info.st_mode)
              and not getattr(info, "st_file_attributes", 0) & 0x400, "unsafe_path")
    return path


def private_path_guard(path):
    """默认只接受可证明的POSIX私有文件；Windows需要另行审核ACL适配。"""
    _need(os.name == "posix", "private_permissions_unverified")
    try:
        info = safe_path(path).stat()
    except OSError:
        raise CandidateCheckError("private_permissions_unverified") from None
    _need(info.st_uid == os.geteuid(), "private_owner_mismatch")
    expected = 0o700 if stat.S_ISDIR(info.st_mode) else 0o600
    _need(stat.S_IMODE(info.st_mode) == expected, "private_permissions_unverified")


def _read(path, limit, guard=None):
    path = safe_path(path)
    if guard:
        guard(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_file")
            _need(info.st_size <= limit, "material_too_large")
            data = stream.read(limit + 1)
            _need(len(data) <= limit, "material_too_large")
            _need(os.path.samestat(info, path.stat()), "file_changed")
            return data
    except OSError:
        raise CandidateCheckError("material_unavailable") from None


def _write_new(path, data, guard):
    path = safe_path(path)
    guard(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            guard(path)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise CandidateCheckError("material_write_failed") from None


def _binary_digest(path):
    """分块哈希，避免小机器每次核验都把整个核心二进制载入内存。"""
    path = safe_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            _need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_file")
            _need(info.st_size <= MAX_BINARY_BYTES, "material_too_large")
            digest, total = hashlib.sha256(), 0
            while data := stream.read(65536):
                total += len(data)
                _need(total <= MAX_BINARY_BYTES, "material_too_large")
                digest.update(data)
            _need(os.path.samestat(info, path.stat()), "file_changed")
            return digest.hexdigest()
    except OSError:
        raise CandidateCheckError("binary_unavailable") from None


def run_fixed_check(argv, *, timeout, cwd):
    """输出直接丢弃，不采集秘密、不让失败输出造成内存无限增长。"""
    result = subprocess.run(argv, cwd=cwd, timeout=timeout, shell=False, check=False,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            env={"LANG": "C", "LC_ALL": "C", "HOME": str(cwd)})
    return result.returncode


@dataclass(frozen=True)
class CoreProfile:
    """只能由已审本地绑定构造，不能从网页请求或数据库自由字段拼装。"""

    core_id: str
    engine: str
    binary_path: str
    binary_sha256: str
    binary_version: str
    source_path: str
    active_path: str
    artifact_root: str
    owner_id: str
    owner_evidence_path: str
    owner_evidence_sha256: str
    source_revision: str
    identity_generation: int
    service_unit: str
    generator_mode: str = "single-json"
    check_timeout_seconds: int = 10


class CoreCandidateChecker:
    """只准备和检查候选；不存在默认生产实例或环境变量权限旁路。"""

    def __init__(self, profile, *, private_path_guard=private_path_guard, runner=run_fixed_check):
        self.profile = profile
        self.private_path_guard = private_path_guard
        self.runner = runner

    def verify_binding(self):
        profile = self.profile
        _need(isinstance(profile, CoreProfile), "binding_unavailable")
        _need(profile.engine in ("sing-box", "xray"), "unsupported_engine")
        _need(profile.generator_mode == "single-json", "generator_not_supported")
        _need(type(profile.identity_generation) is int and profile.identity_generation > 0,
              "identity_unverified")
        _need(type(profile.check_timeout_seconds) is int and 1 <= profile.check_timeout_seconds <= 30,
              "invalid_check_timeout")
        for value in (profile.binary_sha256, profile.owner_evidence_sha256):
            _need(type(value) is str and HASH.fullmatch(value), "invalid_binding_hash")
        for value in (profile.core_id, profile.owner_id, profile.source_revision, profile.binary_version):
            _need(type(value) is str and 0 < len(value) <= 128 and not any(ord(c) < 32 for c in value),
                  "invalid_binding")
        _need(type(profile.service_unit) is str
              and re.fullmatch(r"[A-Za-z0-9_.@-]{1,100}\.service", profile.service_unit),
              "invalid_service_unit")
        source, active = safe_path(profile.source_path), safe_path(profile.active_path)
        _need(source == active, "persistent_source_unverified")
        root = safe_path(profile.artifact_root)
        _need(root.is_dir(), "artifact_root_unavailable")
        self.private_path_guard(root)
        _need(not source.is_relative_to(root), "source_inside_artifacts")
        self.private_path_guard(source)
        evidence = _read(profile.owner_evidence_path, MAX_METADATA_BYTES, self.private_path_guard)
        _need(_sha(evidence) == profile.owner_evidence_sha256, "owner_evidence_changed")
        proof = _decoded(evidence)
        fields = ("core_id", "owner_id", "engine", "source_path", "active_path", "generator_mode",
                  "binary_path", "binary_sha256", "binary_version", "source_revision",
                  "identity_generation", "service_unit")
        _need(all(proof.get(key) == getattr(profile, key) for key in fields)
              and proof.get("exclusive_writer") is True, "ownership_unverified")
        binary = safe_path(profile.binary_path)
        if os.name == "posix":
            try:
                info = binary.stat()
            except OSError:
                raise CandidateCheckError("binary_unavailable") from None
            _need(not stat.S_IMODE(info.st_mode) & 0o022 and info.st_uid in (0, os.geteuid()),
                  "unsafe_binary_permissions")
        _need(_binary_digest(binary) == profile.binary_sha256, "binary_changed")
        return _sha(_encoded(asdict(profile)))

    def binding_status(self):
        try:
            self.verify_binding()
        except (CandidateCheckError, OSError, TypeError, ValueError):
            return {"available": False, "reason": "binding_unavailable"}
        return {"available": True, "reason": ""}

    def _directory(self, artifact_ref):
        _need(type(artifact_ref) is str and ARTIFACT_REF.fullmatch(artifact_ref), "invalid_artifact_ref")
        root = safe_path(self.profile.artifact_root)
        self.private_path_guard(root)
        directory = safe_path(root / artifact_ref)
        self.private_path_guard(directory)
        return directory

    def prepare_candidate(self, patch):
        _need(type(patch) is dict and set(patch) == {"log"}
              and type(patch["log"]) is dict and set(patch["log"]) == {"level"}
              and type(patch["log"]["level"]) is str and patch["log"]["level"] in LEVELS,
              "unsupported_patch")
        fingerprint = self.verify_binding()
        base = _read(self.profile.source_path, MAX_CONFIG_BYTES, self.private_path_guard)
        candidate = _decoded(base)
        log = candidate.setdefault("log", {})
        _need(type(log) is dict, "invalid_log_config")
        level = patch["log"]["level"]
        if self.profile.engine == "xray":
            previous = log.get("loglevel", "")
            log["loglevel"] = level
        else:
            previous = "none" if log.get("disabled") is True else log.get("level", "")
            previous = "warning" if previous == "warn" else previous
            if level == "none":
                log["disabled"] = True
            else:
                log["level"] = "warn" if level == "warning" else level
                if "disabled" in log:
                    log["disabled"] = False
        _need(type(previous) is str and previous in ("", *LEVELS), "unsupported_previous_level")
        data = _encoded(candidate)
        _need(len(data) <= MAX_CONFIG_BYTES, "material_too_large")
        _need(self.verify_binding() == fingerprint, "binding_changed")
        _need(_sha(_read(self.profile.source_path, MAX_CONFIG_BYTES, self.private_path_guard)) == _sha(base),
              "source_changed")
        artifact_ref = uuid.uuid4().hex
        directory = safe_path(Path(self.profile.artifact_root) / artifact_ref)
        try:
            # Windows权限由受信guard验证继承ACL，默认guard在此前已拒绝该平台。
            directory.mkdir(mode=0o700 if os.name == "posix" else 0o777)
        except OSError:
            raise CandidateCheckError("material_write_failed") from None
        self.private_path_guard(directory)
        manifest = {"schema_version": 1, "artifact_ref": artifact_ref, "core_id": self.profile.core_id,
                    "base_sha256": _sha(base), "candidate_sha256": _sha(data),
                    "binding_fingerprint": fingerprint, "previous_level": previous,
                    "identity_generation": self.profile.identity_generation,
                    "source_revision": self.profile.source_revision, "patch": patch}
        for name, content in (("base.json", base), ("candidate.json", data),
                              ("manifest.json", _encoded(manifest))):
            _write_new(directory / name, content, self.private_path_guard)
        return {key: manifest[key] for key in ("base_sha256", "candidate_sha256", "binding_fingerprint",
                                               "artifact_ref", "previous_level")}

    def load_artifact(self, artifact_ref):
        fingerprint = self.verify_binding()
        directory = self._directory(artifact_ref)
        manifest = _decoded(_read(directory / "manifest.json", MAX_METADATA_BYTES, self.private_path_guard))
        _need(manifest.get("schema_version") == 1 and manifest.get("artifact_ref") == artifact_ref
              and manifest.get("core_id") == self.profile.core_id
              and manifest.get("binding_fingerprint") == fingerprint
              and manifest.get("identity_generation") == self.profile.identity_generation
              and manifest.get("source_revision") == self.profile.source_revision, "artifact_binding_mismatch")
        base = _read(directory / "base.json", MAX_CONFIG_BYTES, self.private_path_guard)
        candidate = _read(directory / "candidate.json", MAX_CONFIG_BYTES, self.private_path_guard)
        _need(_sha(base) == manifest.get("base_sha256")
              and _sha(candidate) == manifest.get("candidate_sha256"), "artifact_hash_mismatch")
        return {"manifest": manifest, "base": base, "candidate": candidate}

    def load_check(self, artifact_ref):
        manifest = self.load_artifact(artifact_ref)["manifest"]
        result = _decoded(_read(self._directory(artifact_ref) / "check.json", MAX_METADATA_BYTES,
                                self.private_path_guard))
        keys = ("base_sha256", "candidate_sha256", "binding_fingerprint", "identity_generation", "core_id")
        _need(set(result) == {*keys, "check_fence", "status", "error_code"}
              and all(result.get(key) == manifest[key] for key in keys)
              and type(result.get("check_fence")) is int and result["check_fence"] > 0
              and result.get("status") in ("succeeded", "failed", "unknown")
              and result.get("error_code") in ("", "check_failed", "check_timeout",
                                                "check_execution_failed", "check_inputs_changed"),
              "check_receipt_mismatch")
        return result

    def check_candidate(self, artifact_ref, *, base_sha256, candidate_sha256,
                        binding_fingerprint, check_fence):
        _need(type(check_fence) is int and check_fence > 0, "invalid_check_fence")
        artifact = self.load_artifact(artifact_ref)
        manifest = artifact["manifest"]
        expected = {"base_sha256": base_sha256, "candidate_sha256": candidate_sha256,
                    "binding_fingerprint": binding_fingerprint}
        _need(all(manifest[key] == value for key, value in expected.items()), "check_target_mismatch")
        _need(_sha(_read(self.profile.source_path, MAX_CONFIG_BYTES, self.private_path_guard)) == base_sha256,
              "source_changed")
        directory = self._directory(artifact_ref)
        lock = directory / ".checking"
        _write_new(lock, b"", self.private_path_guard)
        try:
            receipt_path = directory / "check.json"
            if receipt_path.exists():
                previous = self.load_check(artifact_ref)
                _need(previous["check_fence"] <= check_fence, "stale_check_fence")
                if previous["check_fence"] == check_fence:
                    return previous
            args = [str(self.profile.binary_path)]
            args += (["check", "-c"] if self.profile.engine == "sing-box" else ["run", "-test", "-config"])
            args.append(str(directory / "candidate.json"))
            result = {**expected, "core_id": self.profile.core_id,
                      "identity_generation": self.profile.identity_generation,
                      "check_fence": check_fence, "status": "failed", "error_code": "check_failed"}
            try:
                code = self.runner(args, timeout=self.profile.check_timeout_seconds, cwd=directory)
                _need(type(code) is int, "invalid_check_result")
                if code == 0:
                    result.update(status="succeeded", error_code="")
            except subprocess.TimeoutExpired:
                result.update(status="unknown", error_code="check_timeout")
            except Exception:
                # 受信适配器也不能把异常原文（可能含配置/秘密）传播到上层。
                result.update(status="failed", error_code="check_execution_failed")
            try:
                self.load_artifact(artifact_ref)
                _need(_sha(_read(self.profile.source_path, MAX_CONFIG_BYTES, self.private_path_guard))
                      == base_sha256, "source_changed")
            except (CandidateCheckError, OSError):
                result.update(status="failed", error_code="check_inputs_changed")
            temporary = directory / (".check-" + uuid.uuid4().hex)
            _write_new(temporary, _encoded(result), self.private_path_guard)
            safe_path(receipt_path)
            os.replace(temporary, receipt_path)
            return result
        except OSError:
            raise CandidateCheckError("check_receipt_write_failed") from None
        finally:
            try:
                lock.unlink()
            except OSError:
                pass
