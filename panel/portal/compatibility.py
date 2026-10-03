"""N3 隔离兼容合同：只校验合成快照，不提供生产鉴权、URL、存储或下载。

本模块的通过结果仅表示合同演练相容。真实来源、凭据认证、原格式解析、
原子发布及实际读取仍须由 N2/N4 依据现场事实接入；不能据此放宽既有门禁。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import hashlib
import json
import re
from typing import Protocol


SYNTHETIC_SCOPE = 'isolated-compatibility-fixture-v1'
SYNTHETIC_FORMAT = 'compatibility-fixture-json-v1'
DOWNLOAD_AUTHORITY = 'synthetic-resource-only'


class SourceType(StrEnum):
    P8_ASSET = 'p8_published_asset'
    MEMBERSHIP = 'membership'
    DEVICE_SUBSCRIPTION = 'device_subscription'


class MeteringState(StrEnum):
    UNKNOWN = 'unknown'
    MEASURED = 'measured'


class DecisionCode(StrEnum):
    COMPATIBLE_SYNTHETIC = 'compatible_synthetic'
    INVALID_CONTRACT = 'invalid_contract'
    WRONG_SCOPE = 'wrong_scope'
    SNAPSHOT_UNAVAILABLE = 'snapshot_unavailable'
    SNAPSHOT_CHANGED = 'snapshot_changed'
    REFERENCE_INVALID = 'reference_invalid'
    GRANT_NOT_FOUND = 'grant_not_found'
    GRANT_REVOKED = 'grant_revoked'
    ALIAS_INVALID = 'alias_invalid'
    GENERATION_MISMATCH = 'generation_mismatch'
    REVISION_MISMATCH = 'revision_mismatch'
    SOURCE_CONFLICT = 'source_conflict'
    BINDING_INVALID = 'binding_invalid'
    OWNER_INACTIVE = 'owner_inactive'
    OWNERSHIP_MISMATCH = 'ownership_mismatch'
    SERVICE_UNAVAILABLE = 'service_unavailable'
    SOURCE_UNAVAILABLE = 'source_unavailable'
    CAPABILITY_UNVERIFIED = 'capability_unverified'
    ELIGIBILITY_UNVERIFIED = 'eligibility_unverified'
    METERING_UNAVAILABLE = 'metering_unavailable'
    QUOTA_UNAVAILABLE = 'quota_unavailable'
    EXPIRED = 'expired'
    PATH_INVALID = 'path_invalid'
    RESOURCE_NOT_ALLOWED = 'resource_not_allowed'
    MANIFEST_INVALID = 'manifest_invalid'
    CONTENT_MISMATCH = 'content_mismatch'
    EMBEDDED_REFERENCE_INVALID = 'embedded_reference_invalid'


@dataclass(frozen=True)
class SourceKey:
    instance: str
    type: SourceType
    source_id: str


@dataclass(frozen=True)
class OwnerSnapshot:
    """当前账号投影，资源归属人和核验管理员均从本次快照重新查询。"""

    owner_id: str
    revision: int
    active: bool
    scope: str = SYNTHETIC_SCOPE
    staff: bool = False


@dataclass(frozen=True)
class ServiceSnapshot:
    service_id: str
    owner_id: str
    revision: int
    download_generation: int
    state: str
    kind: str
    accepted_sources: tuple[SourceKey, ...]
    scope: str = SYNTHETIC_SCOPE


@dataclass(frozen=True)
class BindingSnapshot:
    source: SourceKey
    owner_id: str
    service_id: str
    revision: int
    source_revision: int
    evidence_sha256: str
    verified_at: datetime | None
    verified_by_id: str
    verified_by_revision: int
    state: str = 'verified'
    scope: str = SYNTHETIC_SCOPE


@dataclass(frozen=True)
class PublishedAssetFacts:
    """独立 P8 发布资产证据；个人计量未知不成为商业套餐已激活证明。"""

    publication_state: str
    published_source_revision: int
    parser_evidence_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class MembershipFacts:
    """合成事实对应 Membership.eligible，未知计量不允许兼容放行。"""

    provisioning_state: str
    usage_state: str
    usage_updated_at: datetime | None
    expires_at: datetime | None
    quota_bytes: int | None
    used_bytes: int | None


@dataclass(frozen=True)
class DeviceSubscriptionFacts:
    """合成事实对应 delivery._downloadable，不是生产门禁的替代实现。"""

    record_enabled: bool
    record_state: str
    record_revision: int
    applied_revision: int
    subscription_state: str
    identity_generation: int
    applied_identity_generation: int
    token_version: int
    metering_gap: bool
    usage_updated_at: datetime | None
    expires_at: datetime | None
    quota_bytes: int | None
    used_bytes: int | None
    cycle_starts_at: datetime | None
    cycle_ends_at: datetime | None
    required_lines: frozenset[str]
    permitted_lines: frozenset[str]
    expected_identity_pairs: frozenset[tuple[str, str]]
    active_identity_pairs: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class SourceSnapshot:
    source: SourceKey
    owner_id: str
    service_id: str
    revision: int
    state: str
    download_capability: str
    formats: tuple[str, ...]
    metering: MeteringState
    release_id: str
    release_version: int
    facts: PublishedAssetFacts | MembershipFacts | DeviceSubscriptionFacts
    scope: str = SYNTHETIC_SCOPE


@dataclass(frozen=True)
class ResourceSnapshot:
    path: str
    release_version: int
    content_sha256: str
    content: bytes = field(repr=False)
    format: str = SYNTHETIC_FORMAT


@dataclass(frozen=True)
class ReleaseManifest:
    release_id: str
    release_version: int
    resources: tuple[ResourceSnapshot, ...]
    scope: str = SYNTHETIC_SCOPE


@dataclass(frozen=True)
class DownloadGrant:
    """下载代际、源身份/凭据快照及发布版本分别绑定，不能相互替代。"""

    grant_id: str = field(repr=False)
    source: SourceKey
    owner_id: str
    owner_revision: int
    service_id: str
    service_revision: int
    revision: int
    download_generation: int
    binding_revision: int
    binding_evidence_sha256: str
    source_revision: int
    source_identity_generation: int | None
    source_credential_version: int | None
    release_id: str
    release_version: int
    allowed_resources: tuple[str, ...]
    manifest_sha256: str
    manifest: ReleaseManifest
    state: str = 'current'
    scope: str = SYNTHETIC_SCOPE


@dataclass(frozen=True)
class LegacyAlias:
    alias_id: str = field(repr=False)
    grant_id: str = field(repr=False)
    download_generation: int
    grant_revision: int
    binding_revision: int
    state: str = 'current'
    scope: str = SYNTHETIC_SCOPE


@dataclass(frozen=True)
class DownloadReference:
    """已由隔离夹具解码的声明；自身不证明生产凭据认证通过。"""

    kind: str
    handle: str = field(repr=False)
    download_generation: int
    grant_revision: int
    binding_revision: int
    authority: str = DOWNLOAD_AUTHORITY
    scope: str = SYNTHETIC_SCOPE


@dataclass(frozen=True)
class CompatibilitySnapshot:
    revision: int
    owners: tuple[OwnerSnapshot, ...]
    services: tuple[ServiceSnapshot, ...]
    sources: tuple[SourceSnapshot, ...]
    bindings: tuple[BindingSnapshot, ...]
    grants: tuple[DownloadGrant, ...]
    aliases: tuple[LegacyAlias, ...]
    scope: str = SYNTHETIC_SCOPE


class CompatibilityRepository(Protocol):
    """实现者须原子读取，修订涵盖归属、状态、授权、别名及发布变化。"""

    def snapshot(self) -> CompatibilitySnapshot: ...

    def still_current(self, revision: int) -> bool: ...


@dataclass(frozen=True)
class ResourceDecision:
    path: str
    download_generation: int
    release_id: str
    release_version: int
    content_sha256: str
    manifest_sha256: str
    source_type: SourceType
    metering: MeteringState
    service_kind: str


@dataclass(frozen=True)
class CompatibilityDecision:
    code: DecisionCode
    resource: ResourceDecision | None = None
    synthetic_only: bool = field(default=True, init=False)

    @property
    def compatible(self) -> bool:
        return self.code is DecisionCode.COMPATIBLE_SYNTHETIC


class SourceAdapter(Protocol):
    def eligibility(self, source: SourceSnapshot, grant: DownloadGrant,
                    now: datetime) -> DecisionCode: ...


def _sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _positive(value: object) -> bool:
    return type(value) is int and value > 0


def _identifier(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', value) is not None


def _source_key(value: object) -> bool:
    return (isinstance(value, SourceKey) and type(value.type) is SourceType
            and _identifier(value.instance) and _identifier(value.source_id))


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _usage_gate(updated: datetime | None, expires: datetime | None,
                quota: int | None, used: int | None, now: datetime) -> DecisionCode:
    if not _aware(expires) or expires <= now:
        return DecisionCode.EXPIRED
    if not _aware(updated) or not now - timedelta(minutes=3) <= updated <= now + timedelta(seconds=30):
        return DecisionCode.METERING_UNAVAILABLE
    if type(quota) is not int or type(used) is not int or used < 0 or quota <= used:
        return DecisionCode.QUOTA_UNAVAILABLE
    return DecisionCode.COMPATIBLE_SYNTHETIC


class P8PublishedAssetAdapter:
    def eligibility(self, source, grant, now):
        facts = source.facts
        if (not isinstance(facts, PublishedAssetFacts)
                or facts.publication_state != 'published_verified'
                or not _positive(facts.published_source_revision)
                or facts.published_source_revision != source.revision
                or not _sha256(facts.parser_evidence_sha256)
                or facts.manifest_sha256 != grant.manifest_sha256):
            return DecisionCode.ELIGIBILITY_UNVERIFIED
        if source.metering is not MeteringState.UNKNOWN:
            # 本片的独立发布资产没有个人计量证明，不能凭枚举填出可靠计量。
            return DecisionCode.METERING_UNAVAILABLE
        return DecisionCode.COMPATIBLE_SYNTHETIC


class MembershipAdapter:
    def eligibility(self, source, grant, now):
        facts = source.facts
        if (not isinstance(facts, MembershipFacts) or facts.provisioning_state != 'applied'
                or facts.usage_state != 'measured' or source.metering is not MeteringState.MEASURED):
            return DecisionCode.METERING_UNAVAILABLE
        return _usage_gate(facts.usage_updated_at, facts.expires_at, facts.quota_bytes, facts.used_bytes, now)


class DeviceSubscriptionAdapter:
    def eligibility(self, source, grant, now):
        facts = source.facts
        if (not isinstance(facts, DeviceSubscriptionFacts) or facts.record_enabled is not True
                or facts.record_state != 'active' or facts.subscription_state != 'active'
                or not all(_positive(value) for value in (facts.record_revision, facts.applied_revision,
                        facts.identity_generation, facts.applied_identity_generation, facts.token_version,
                        grant.source_identity_generation, grant.source_credential_version))
                or facts.record_revision != facts.applied_revision
                or facts.identity_generation != grant.source_identity_generation
                or facts.applied_identity_generation != facts.identity_generation
                or facts.token_version != grant.source_credential_version):
            return DecisionCode.ELIGIBILITY_UNVERIFIED
        if facts.metering_gap is not False or source.metering is not MeteringState.MEASURED:
            return DecisionCode.METERING_UNAVAILABLE
        usage = _usage_gate(facts.usage_updated_at, facts.expires_at, facts.quota_bytes, facts.used_bytes, now)
        if usage is not DecisionCode.COMPATIBLE_SYNTHETIC:
            return usage
        if (not _aware(facts.cycle_starts_at) or not _aware(facts.cycle_ends_at)
                or not facts.cycle_starts_at <= now < facts.cycle_ends_at):
            return DecisionCode.QUOTA_UNAVAILABLE
        if (type(facts.required_lines) is not frozenset or type(facts.permitted_lines) is not frozenset
                or type(facts.expected_identity_pairs) is not frozenset
                or type(facts.active_identity_pairs) is not frozenset
                or not all(_identifier(item) for item in facts.required_lines | facts.permitted_lines)
                or not all(type(pair) is tuple and len(pair) == 2
                           and all(_identifier(item) for item in pair)
                           for pair in facts.expected_identity_pairs | facts.active_identity_pairs)
                or not facts.required_lines or not facts.required_lines.issubset(facts.permitted_lines)
                or not facts.expected_identity_pairs
                or {pair[0] for pair in facts.expected_identity_pairs} != facts.required_lines
                or facts.expected_identity_pairs != facts.active_identity_pairs):
            return DecisionCode.ELIGIBILITY_UNVERIFIED
        return DecisionCode.COMPATIBLE_SYNTHETIC


def safe_resource_path(path: object) -> bool:
    """仅接受规范相对路径；不解码、不归一化用户路径，编码歧义直接拒绝。"""
    if not isinstance(path, str) or not 1 <= len(path) <= 240:
        return False
    reserved = {'con', 'prn', 'aux', 'nul'} | {f'{prefix}{i}' for prefix in ('com', 'lpt') for i in range(1, 10)}
    for part in path.split('/'):
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', part)
                or part.endswith('.') or part.split('.')[0].lower() in reserved):
            return False
    return True


def manifest_sha256(manifest: ReleaseManifest) -> str:
    """摘要绑定整套资源；内容及内嵌引用由每项内容摘要间接绑定。"""
    value = {'scope': manifest.scope, 'release_id': manifest.release_id,
             'release_version': manifest.release_version,
             'resources': [{'path': item.path, 'release_version': item.release_version,
                            'format': item.format, 'sha256': item.content_sha256}
                           for item in sorted(manifest.resources, key=lambda item: item.path)]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class _Rejected(Exception):
    def __init__(self, code):
        self.code = code


def _require(condition, code):
    if not condition:
        raise _Rejected(code)


def _one(items, predicate, missing, duplicate=DecisionCode.INVALID_CONTRACT):
    found = tuple(item for item in items if predicate(item))
    _require(len(found) == 1, duplicate if len(found) > 1 else missing)
    return found[0]


class CompatibilityResolver:
    """三入口共享当前快照校验；通过不返回资源正文，也不授予生产权限。"""

    def __init__(self, repository: CompatibilityRepository):
        self.repository = repository
        self._adapters: dict[SourceType, SourceAdapter] = {
            SourceType.P8_ASSET: P8PublishedAssetAdapter(),
            SourceType.MEMBERSHIP: MembershipAdapter(),
            SourceType.DEVICE_SUBSCRIPTION: DeviceSubscriptionAdapter(),
        }

    def resolve(self, reference: DownloadReference, resource_path: str,
                now: datetime) -> CompatibilityDecision:
        try:
            _require(_aware(now), DecisionCode.INVALID_CONTRACT)
            snapshot = self.repository.snapshot()
            _require(isinstance(snapshot, CompatibilitySnapshot) and _positive(snapshot.revision),
                     DecisionCode.INVALID_CONTRACT)
            _require(all(type(rows) is tuple for rows in (snapshot.owners, snapshot.services,
                        snapshot.sources, snapshot.bindings, snapshot.grants, snapshot.aliases)),
                     DecisionCode.INVALID_CONTRACT)
            _require(snapshot.scope == SYNTHETIC_SCOPE, DecisionCode.WRONG_SCOPE)
            result, grant = self._resolve(snapshot, reference, resource_path, now)
            self._check_manifest(snapshot, grant, now)
            _require(self.repository.still_current(snapshot.revision) is True, DecisionCode.SNAPSHOT_CHANGED)
            return CompatibilityDecision(DecisionCode.COMPATIBLE_SYNTHETIC, result)
        except _Rejected as rejected:
            return CompatibilityDecision(rejected.code)
        except (AttributeError, TypeError, ValueError, KeyError, IndexError):
            return CompatibilityDecision(DecisionCode.INVALID_CONTRACT)
        except Exception:
            # 仓储/适配器失败不泄露异常中的路径、来源标识或凭据，也不降级放行。
            return CompatibilityDecision(DecisionCode.SNAPSHOT_UNAVAILABLE)

    def _resolve(self, snapshot, reference, path, now):
        _require(isinstance(reference, DownloadReference), DecisionCode.REFERENCE_INVALID)
        _require(reference.scope == SYNTHETIC_SCOPE, DecisionCode.WRONG_SCOPE)
        _require(reference.authority == DOWNLOAD_AUTHORITY and reference.kind in ('grant', 'alias')
                 and _identifier(reference.handle)
                 and all(_positive(value) for value in
                         (reference.download_generation, reference.grant_revision, reference.binding_revision)),
                 DecisionCode.REFERENCE_INVALID)
        _require(safe_resource_path(path), DecisionCode.PATH_INVALID)
        alias = None
        if reference.kind == 'alias':
            alias = _one(snapshot.aliases, lambda item: item.alias_id == reference.handle,
                         DecisionCode.ALIAS_INVALID)
            _require(alias.scope == SYNTHETIC_SCOPE, DecisionCode.WRONG_SCOPE)
            _require(alias.state == 'current', DecisionCode.ALIAS_INVALID)
        grant_id = alias.grant_id if alias else reference.handle
        grant = _one(snapshot.grants, lambda item: item.grant_id == grant_id, DecisionCode.GRANT_NOT_FOUND)
        _require(grant.scope == SYNTHETIC_SCOPE, DecisionCode.WRONG_SCOPE)
        _require(_source_key(grant.source)
                 and all(_identifier(value) for value in (grant.grant_id, grant.owner_id, grant.service_id))
                 and all(_positive(value) for value in (grant.revision, grant.download_generation,
                         grant.binding_revision, grant.source_revision, grant.owner_revision,
                         grant.service_revision, grant.release_version))
                 and _identifier(grant.release_id), DecisionCode.INVALID_CONTRACT)
        _require(grant.state == 'current', DecisionCode.GRANT_REVOKED)
        _require(reference.download_generation == grant.download_generation,
                 DecisionCode.GENERATION_MISMATCH)
        _require(reference.grant_revision == grant.revision and reference.binding_revision == grant.binding_revision,
                 DecisionCode.REVISION_MISMATCH)
        if alias:
            _require(all(_positive(value) for value in (alias.download_generation, alias.grant_revision,
                                                       alias.binding_revision)), DecisionCode.ALIAS_INVALID)
            _require(alias.download_generation == grant.download_generation, DecisionCode.GENERATION_MISMATCH)
            _require(alias.grant_revision == grant.revision and alias.binding_revision == grant.binding_revision,
                     DecisionCode.REVISION_MISMATCH)
        binding = _one(snapshot.bindings, lambda item: item.source == grant.source,
                       DecisionCode.BINDING_INVALID, DecisionCode.SOURCE_CONFLICT)
        source = _one(snapshot.sources, lambda item: item.source == grant.source,
                      DecisionCode.SOURCE_UNAVAILABLE, DecisionCode.SOURCE_CONFLICT)
        owner = _one(snapshot.owners, lambda item: item.owner_id == grant.owner_id, DecisionCode.OWNER_INACTIVE)
        verifier = _one(snapshot.owners, lambda item: item.owner_id == binding.verified_by_id,
                        DecisionCode.BINDING_INVALID)
        service = _one(snapshot.services, lambda item: item.service_id == grant.service_id,
                       DecisionCode.SERVICE_UNAVAILABLE)
        _require(all(item.scope == SYNTHETIC_SCOPE for item in (binding, source, owner, service, verifier)),
                 DecisionCode.WRONG_SCOPE)
        _require(owner.active is True, DecisionCode.OWNER_INACTIVE)
        _require(all(item.owner_id == grant.owner_id for item in (binding, source, service))
                 and binding.service_id == source.service_id == service.service_id,
                 DecisionCode.OWNERSHIP_MISMATCH)
        _require(binding.state == 'verified' and _positive(binding.revision)
                 and _positive(binding.source_revision)
                 and _identifier(binding.verified_by_id) and _positive(binding.verified_by_revision)
                 and _aware(binding.verified_at) and binding.verified_at <= now
                 and verifier.active is True and verifier.staff is True
                 and _positive(verifier.revision) and verifier.revision == binding.verified_by_revision
                 and _sha256(binding.evidence_sha256), DecisionCode.BINDING_INVALID)
        _require(binding.revision == grant.binding_revision
                 and binding.evidence_sha256 == grant.binding_evidence_sha256
                 and binding.source_revision == source.revision == grant.source_revision
                 and owner.revision == grant.owner_revision and service.revision == grant.service_revision,
                 DecisionCode.REVISION_MISMATCH)
        _require(_positive(source.revision) and _positive(owner.revision) and _positive(service.revision),
                 DecisionCode.INVALID_CONTRACT)
        _require(service.state == 'available' and grant.source in service.accepted_sources
                 and type(service.accepted_sources) is tuple
                 and len(service.accepted_sources) == len(set(service.accepted_sources))
                 and all(_source_key(key) for key in service.accepted_sources),
                 DecisionCode.SERVICE_UNAVAILABLE)
        _require(_positive(service.download_generation)
                 and service.download_generation == grant.download_generation, DecisionCode.GENERATION_MISMATCH)
        expected_state = 'published_verified' if source.source.type is SourceType.P8_ASSET else 'active_verified'
        _require(source.state == expected_state, DecisionCode.SOURCE_UNAVAILABLE)
        _require(source.download_capability == 'verified' and type(source.formats) is tuple
                 and SYNTHETIC_FORMAT in source.formats,
                 DecisionCode.CAPABILITY_UNVERIFIED)
        _require(type(source.source.type) is SourceType, DecisionCode.INVALID_CONTRACT)
        if source.source.type is SourceType.P8_ASSET:
            _require(service.kind == 'existing_compatibility', DecisionCode.ELIGIBILITY_UNVERIFIED)
        _require(type(source.metering) is MeteringState, DecisionCode.INVALID_CONTRACT)
        eligibility = self._adapters[source.source.type].eligibility(source, grant, now)
        _require(type(eligibility) is DecisionCode and eligibility is DecisionCode.COMPATIBLE_SYNTHETIC,
                 eligibility if type(eligibility) is DecisionCode else DecisionCode.ELIGIBILITY_UNVERIFIED)
        _require(path in grant.allowed_resources, DecisionCode.RESOURCE_NOT_ALLOWED)
        resource = _one(grant.manifest.resources, lambda item: item.path == path,
                        DecisionCode.MANIFEST_INVALID, DecisionCode.MANIFEST_INVALID)
        _require(_positive(source.release_version) and source.release_id == grant.release_id
                 and source.release_version == grant.release_version, DecisionCode.MANIFEST_INVALID)
        return ResourceDecision(path, grant.download_generation, grant.release_id, grant.release_version,
                                resource.content_sha256, grant.manifest_sha256,
                                source.source.type, source.metering, service.kind), grant

    def _check_manifest(self, snapshot, grant, now):
        manifest = grant.manifest
        _require(manifest.scope == SYNTHETIC_SCOPE, DecisionCode.WRONG_SCOPE)
        _require(type(manifest.resources) is tuple and type(grant.allowed_resources) is tuple,
                 DecisionCode.INVALID_CONTRACT)
        _require(_positive(manifest.release_version) and _identifier(manifest.release_id)
                 and manifest.release_id == grant.release_id and manifest.release_version == grant.release_version
                 and _sha256(grant.manifest_sha256)
                 and manifest_sha256(manifest) == grant.manifest_sha256, DecisionCode.MANIFEST_INVALID)
        paths = tuple(item.path for item in manifest.resources)
        _require(paths and len(paths) == len(set(paths))
                 and len(grant.allowed_resources) == len(set(grant.allowed_resources))
                 and set(paths) == set(grant.allowed_resources), DecisionCode.MANIFEST_INVALID)
        for resource in manifest.resources:
            _require(safe_resource_path(resource.path), DecisionCode.PATH_INVALID)
            _require(_positive(resource.release_version) and resource.release_version == grant.release_version
                     and resource.format == SYNTHETIC_FORMAT,
                     DecisionCode.MANIFEST_INVALID)
            _require(isinstance(resource.content, bytes) and _sha256(resource.content_sha256)
                     and hashlib.sha256(resource.content).hexdigest() == resource.content_sha256,
                     DecisionCode.CONTENT_MISMATCH)
            # 夹具格式封闭；禁止把真实格式正文的“部分已检查引用”伪称全部引用。
            body = json.loads(resource.content, object_pairs_hook=self._unique_json)
            _require(isinstance(body, dict) and set(body) == {'scope', 'fixture', 'references'}
                     and body['scope'] == SYNTHETIC_SCOPE
                     and body['fixture'] in ('profile', 'rules', 'routes')
                     and isinstance(body['references'], list), DecisionCode.EMBEDDED_REFERENCE_INVALID)
            for link in body['references']:
                _require(isinstance(link, dict)
                         and set(link) == {'kind', 'handle', 'download_generation', 'grant_revision',
                                           'binding_revision', 'scope', 'authority', 'resource'},
                         DecisionCode.EMBEDDED_REFERENCE_INVALID)
                reference = DownloadReference(**{key: value for key, value in link.items() if key != 'resource'})
                _, linked_grant = self._resolve(snapshot, reference, link['resource'], now)
                _require(linked_grant.grant_id == grant.grant_id, DecisionCode.EMBEDDED_REFERENCE_INVALID)

    @staticmethod
    def _unique_json(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, DecisionCode.EMBEDDED_REFERENCE_INVALID)
            result[key] = value
        return result
