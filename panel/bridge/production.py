"""已审核生产模块的身份适配器；不自动加载模块、联网或执行任务。

仅 root 生命周期执行器可构造本类。调用方先按固定安装路径和审核摘要
加载依赖；本类不接受网页路径，也不把源码存在当成版本已获审核。
生成和检查均发生在发布前，不调用旧的全局订阅 Store。
"""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import re
import secrets


REQUIRED_ARTIFACTS = frozenset({
    "windows", "android", "v2rayng", "windows-rules", "v2rayng-routes",
    "v2rayng-geosite", "v2rayng-geoip",
})
EXPECTED_CORE_VERSION = "1.14.1"
_OWNER_ACCOUNT = re.compile(r"pnl-([0-9a-f]{32})-([hb])-(reality|hy2|ws|anytls)")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class ProductionError(RuntimeError):
    """固定错误代号；applied 为真表示已提交，None 表示提交结果未知。"""

    def __init__(self, code, *, applied=False):
        super().__init__(code)
        self.applied = applied


def _require(value, code):
    if not value:
        raise ProductionError(code)


class ProductionBridge:
    """依赖必须来自同一批已经审核的安装模块；不提供猜测路径的默认加载器。

    admin 是真实 relay_admin 模块，builder 是 P8 client_bundle 模块，
    subscriptions 是 relay_subscription 校验模块。subscription_runtime 只
    使用 cn_rules、blacklist_rules、android_core，绝不沿用其全局账号绑定。
    work_root 必须是已准备的受限目录；Linux 位于 /root/relay-exports 下。
    last_backup 仅保存 publish 返回的备份编号，上层负责持久日志和补偿。
    """

    def __init__(self, manager, *, admin, builder, subscriptions, subscription_runtime, work_root):
        _require(type(subscription_runtime) is dict, "subscription_runtime_required")
        _require(all(isinstance(subscription_runtime.get(key), (str, Path))
                     for key in ("cn_rules", "blacklist_rules", "android_core")), "subscription_sources_required")
        _require(getattr(builder, "CORE_VERSION", None) == EXPECTED_CORE_VERSION
                 and getattr(builder, "SCHEMA_VERSION", None) == 1, "builder_version_unreviewed")
        _require(getattr(manager, "xray_enabled", False), "dual_core_manager_required")
        for dependency, methods in (
            (admin, ("read", "digest", "encoded", "atomic", "private")),
            (manager, ("authority", "policy", "check", "dependencies", "compiled", "compiled_xray",
                       "core_check", "xray_core_check", "publish")),
            (builder, ("build_bundle",)),
            (subscriptions, ("select_subscription_accounts", "validate_payloads")),
            (getattr(builder, "p7", None), ("loads_unique",)),
        ):
            _require(all(callable(getattr(dependency, method, None)) for method in methods), "production_api_missing")
        self.manager, self.admin, self.builder, self.subscriptions = manager, admin, builder, subscriptions
        self.runtime = copy.deepcopy(subscription_runtime)
        self.work_root = Path(work_root)
        _require(Path(self.runtime["android_core"]) == Path(manager.paths["core"]), "android_core_scope_mismatch")
        self._before = self._before_hash = self._sources = None
        self._validated = self._rendered = None
        self.last_backup = None

    def _candidate_digest(self, candidate):
        return hashlib.sha256(self.admin.encoded(candidate)).hexdigest()

    def _fingerprint(self):
        result = dict(self.manager.dependencies())
        paths = [self.manager.paths["core"], self.runtime["cn_rules"], self.runtime["blacklist_rules"]]
        for module in (self.admin, self.builder, self.builder.p7, self.subscriptions):
            value = getattr(module, "__file__", None)
            _require(value is not None, "production_module_path_unconfirmed")
            paths.append(value)
        for raw in paths:
            path = Path(raw)
            _require(path.is_file(), "production_source_missing")
            result[str(path)] = self.admin.digest(path)
        return result

    def _guard_sources(self):
        _require(self._before is not None, "snapshot_required")
        _require(self.admin.digest(self.manager.paths["authority"]) == self._before_hash,
                 "authority_changed_retry_snapshot")
        _require(self._fingerprint() == self._sources, "production_sources_changed")

    def _stage(self, label):
        root = self.work_root
        _require(root.is_dir() and not root.is_symlink(), "private_work_root_required")
        if os.name == "posix":
            _require(root.resolve().is_relative_to(Path("/root/relay-exports")), "private_work_scope")
        self.admin.private(root, True)
        stage = root / (label + "-" + secrets.token_hex(16))
        stage.mkdir(mode=0o700 if os.name == "posix" else 0o777)
        return stage

    def _identity_scope(self, candidate):
        """只允许一个面板用户的新增／启停；不改旧身份凭据或网络权威字段。"""
        _require(type(candidate) is dict and set(candidate) == set(self._before), "candidate_schema_changed")
        _require({k: v for k, v in candidate.items() if k != "accounts"}
                 == {k: v for k, v in self._before.items() if k != "accounts"}, "candidate_not_identity_only")
        old = {row["id"]: row for row in self._before["accounts"]}
        new = {row["id"]: row for row in candidate["accounts"]}
        _require(len(new) == len(candidate["accounts"]) and old.keys() <= new.keys(), "candidate_account_removed_or_duplicate")
        changed, activating = set(), False
        for identifier, row in new.items():
            previous = old.get(identifier)
            if previous == row:
                continue
            match = _OWNER_ACCOUNT.fullmatch(identifier)
            _require(match is not None, "candidate_legacy_account_changed")
            changed.add(match[1])
            if previous is not None:
                _require({k: v for k, v in previous.items() if k != "enabled"}
                         == {k: v for k, v in row.items() if k != "enabled"}, "candidate_existing_credential_changed")
            activating = activating or (row["enabled"] and (previous is None or not previous["enabled"]))
        _require(len(changed) <= 1, "candidate_multiple_owners")
        return activating

    def snapshot(self):
        """返回独立权威副本与文件原始摘要；不输出或记录内容。"""
        try:
            before_hash = self.admin.digest(self.manager.paths["authority"])
            sources = self._fingerprint()
            self.manager.check()
            authority = self.manager.authority()
            _require(before_hash == self.admin.digest(self.manager.paths["authority"]), "authority_changed_retry_snapshot")
            _require(sources == self._fingerprint(), "production_sources_changed")
            self._before, self._before_hash, self._sources = copy.deepcopy(authority), before_hash, sources
            self._validated = self._rendered = None
            return copy.deepcopy(authority), before_hash
        except ProductionError:
            raise
        except Exception:
            raise ProductionError("snapshot_failed") from None

    def validate(self, candidate):
        """在受限 staging 中执行真实双核心候选检查，不修改活动配置。"""
        self._validated = self._rendered = None
        try:
            self._guard_sources()
            self.manager.policy().validate_authority(candidate)
            self._identity_scope(candidate)
            stage = self._stage("validate")
            config, xray = self.manager.compiled(candidate), self.manager.compiled_xray(candidate)
            self.admin.atomic(stage / "sing-box.json", self.admin.encoded(config))
            self.admin.atomic(stage / "xray.json", self.admin.encoded(xray))
            self.manager.core_check(stage / "sing-box.json")
            self.manager.xray_core_check(stage / "xray.json")
            self._guard_sources()
            self._validated = self._candidate_digest(candidate)
        except ProductionError:
            raise
        except Exception:
            raise ProductionError("candidate_validation_failed") from None

    def render(self, candidate, account_ids):
        """从尚未发布的候选生成并校验七项独立用户产物，返回有界 bytes。"""
        self._rendered = None
        try:
            self._guard_sources()
            candidate_hash = self._candidate_digest(candidate)
            _require(self._validated == candidate_hash, "candidate_not_validated")
            _require(type(account_ids) is list and len(account_ids) == 8
                     and all(type(value) is str for value in account_ids), "owner_accounts_required")
            matches = [_OWNER_ACCOUNT.fullmatch(identifier) for identifier in account_ids]
            _require(all(matches) and len({match[1] for match in matches}) == 1, "owner_accounts_mixed")
            owner = matches[0][1]
            changed_ids = {row["id"] for row in candidate["accounts"] if row not in self._before["accounts"]}
            _require(all(identifier.startswith("pnl-" + owner + "-") for identifier in changed_ids),
                     "render_owner_differs_from_candidate")
            policy = self.manager.policy()
            parameters = policy.project_client_parameters(candidate, self.admin.read(self.manager.paths["base"]),
                                                           self.manager.settings.get("reality_public_key"))
            parameters = self.subscriptions.select_subscription_accounts(parameters, account_ids)
            documents = policy.load_rule_documents(candidate, self.manager.paths["rules"])
            cn = self.builder.p7.loads_unique(Path(self.runtime["cn_rules"]).read_text(encoding="utf-8"))
            blacklist = self.builder.p7.loads_unique(Path(self.runtime["blacklist_rules"]).read_text(encoding="utf-8"))
            output = self._stage("render") / "bundle"
            manifest = self.builder.build_bundle(parameters, cn, documents, output,
                blacklist_source=blacklist, fixture=False, core=self.runtime["android_core"])
            _require(manifest.get("public_fixture") is False and manifest.get("core_check", {}).get("result") == "PASS"
                     and manifest["core_check"].get("version") == EXPECTED_CORE_VERSION, "client_core_check_required")
            payloads, checked_manifest = self.subscriptions.validate_payloads(output)
            _require(checked_manifest == manifest, "client_manifest_changed")
            _require(type(payloads) is dict and set(payloads) == REQUIRED_ARTIFACTS
                     and all(type(raw) is bytes and 0 < len(raw) <= 16 * 1024 * 1024 for raw in payloads.values()),
                     "client_payloads_incomplete")
            self._guard_sources()
            self._rendered = candidate_hash
            return dict(payloads)
        except ProductionError:
            raise
        except Exception:
            raise ProductionError("client_render_failed") from None

    def publish(self, candidate, expected_hash):
        """一次调用真实 publish；不重入其锁，不擅自恢复全局旧快照。"""
        applied = False
        publishing = False
        self.last_backup = None
        try:
            _require(type(expected_hash) is str and _SHA256.fullmatch(expected_hash)
                     and expected_hash == self._before_hash, "expected_snapshot_hash_required")
            self._guard_sources()
            candidate_hash = self._candidate_digest(candidate)
            _require(candidate_hash == self._validated, "candidate_not_validated")
            if self._identity_scope(candidate):
                _require(self._rendered == candidate_hash, "client_render_required_before_activation")
            publishing = True
            backup = self.manager.publish(candidate, expected_hash=expected_hash)
            applied = True
            self.last_backup = Path(backup).name
            current_hash = self.admin.digest(self.manager.paths["authority"])
            if current_hash != candidate_hash:
                raise ProductionError("published_authority_changed", applied=True)
            self._before, self._before_hash = copy.deepcopy(candidate), current_hash
            self._validated = self._rendered = None
            return current_hash
        except ProductionError:
            raise
        except Exception:
            # 真实管理器可能已写入后在恢复过程中失败；异常返回不能证明零变更。
            raise ProductionError("production_publish_failed", applied=True if applied else None if publishing else False) from None
