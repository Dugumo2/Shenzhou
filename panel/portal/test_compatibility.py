"""N3 合成合同演练；不读数据库、私有文件、真实身份或网络。"""

import base64
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import unittest

from .compatibility import (
    DOWNLOAD_AUTHORITY, SYNTHETIC_FORMAT, SYNTHETIC_SCOPE, BindingSnapshot,
    CompatibilityResolver, CompatibilitySnapshot, DecisionCode, DeviceSubscriptionFacts,
    DownloadGrant, DownloadReference, LegacyAlias, MembershipFacts, MeteringState,
    OwnerSnapshot, PublishedAssetFacts, ReleaseManifest, ResourceSnapshot,
    ServiceSnapshot, SourceKey, SourceSnapshot, SourceType, manifest_sha256,
    safe_resource_path,
)


NOW = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
ROOT = 'profiles/fixture.json'
RULES = 'rules/fixture.json'


def _body(references=(), fixture='profile'):
    return json.dumps({'scope': SYNTHETIC_SCOPE, 'fixture': fixture,
                       'references': list(references)}, sort_keys=True).encode()


def _resource(path, content=None, release_version=23):
    content = _body() if content is None else content
    return ResourceSnapshot(path, release_version, hashlib.sha256(content).hexdigest(), content)


def _link(reference, path=RULES):
    return {**asdict(reference), 'resource': path}


def _fixture(source_type=SourceType.P8_ASSET):
    """所有标识固定为 fixture，永不代表审核账号或真实服务。"""
    key = SourceKey('fixture-instance', source_type, 'fixture-source')
    reference = DownloadReference('grant', 'fixture-grant', 3, 7, 5)
    alias = LegacyAlias('fixture-alias', 'fixture-grant', 3, 7, 5)
    manifest = ReleaseManifest('fixture-release', 23, (
        _resource(ROOT, _body((_link(reference),))),
        _resource(RULES, _body(fixture='rules')),
    ))
    digest = manifest_sha256(manifest)
    if source_type is SourceType.P8_ASSET:
        facts = PublishedAssetFacts('published_verified', 11, 'b' * 64, digest)
        metering = MeteringState.UNKNOWN
        state = 'published_verified'
    elif source_type is SourceType.MEMBERSHIP:
        facts = MembershipFacts('applied', 'measured', NOW, NOW + timedelta(days=1), 1000, 100)
        metering = MeteringState.MEASURED
        state = 'active_verified'
    else:
        facts = DeviceSubscriptionFacts(
            True, 'active', 9, 9, 'active', 13, 13, 17, False, NOW, NOW + timedelta(days=1),
            1000, 100, NOW - timedelta(days=1), NOW + timedelta(days=1),
            frozenset({'fixture-line'}), frozenset({'fixture-line'}),
            frozenset({('fixture-line', 'fixture-ingress')}),
            frozenset({('fixture-line', 'fixture-ingress')}),
        )
        metering = MeteringState.MEASURED
        state = 'active_verified'
    source = SourceSnapshot(key, 'fixture-owner', 'fixture-service', 11, state, 'verified',
                            (SYNTHETIC_FORMAT,), metering, 'fixture-release', 23, facts)
    binding = BindingSnapshot(key, 'fixture-owner', 'fixture-service', 5, 11, 'a' * 64,
                              NOW - timedelta(hours=1), 'fixture-verifier', 1)
    grant = DownloadGrant('fixture-grant', key, 'fixture-owner', 2, 'fixture-service', 4,
                          7, 3, 5, 'a' * 64, 11,
                          13 if source_type is SourceType.DEVICE_SUBSCRIPTION else None,
                          17 if source_type is SourceType.DEVICE_SUBSCRIPTION else None,
                          'fixture-release', 23, (ROOT, RULES), digest, manifest)
    service = ServiceSnapshot('fixture-service', 'fixture-owner', 4, 3, 'available',
                              'existing_compatibility', (key,))
    snapshot = CompatibilitySnapshot(1, (OwnerSnapshot('fixture-owner', 2, True),
                                        OwnerSnapshot('fixture-verifier', 1, True, staff=True)),
                                     (service,), (source,), (binding,), (grant,), (alias,))
    return snapshot, reference


class _Repository:
    """单进程原子合成快照；没有写生产存储的能力。"""

    def __init__(self, state):
        self.state = state
        self.reads = 0
        self.changed_after_read = False

    def snapshot(self):
        self.reads += 1
        return self.state

    def still_current(self, revision):
        return not self.changed_after_read and self.state.revision == revision


class _SyntheticCredentialCodec:
    """仅测试声明认证边界，固定假作用域/假密钥，禁止部署使用。"""

    _fake_key = b'isolated-fixture-only-do-not-deploy'

    @classmethod
    def issue(cls, reference):
        if reference.scope != SYNTHETIC_SCOPE:
            raise ValueError('夹具作用域不匹配')
        payload = json.dumps(asdict(reference), sort_keys=True).encode()
        signature = hmac.digest(cls._fake_key, payload, 'sha256')
        return base64.urlsafe_b64encode(payload).decode() + '.' + signature.hex()

    @classmethod
    def decode(cls, token):
        payload_text, signature = token.split('.')
        payload = base64.urlsafe_b64decode(payload_text)
        if not hmac.compare_digest(signature, hmac.digest(cls._fake_key, payload, 'sha256').hex()):
            raise ValueError('夹具声明认证失败')
        value = json.loads(payload)
        if value.get('scope') != SYNTHETIC_SCOPE:
            raise ValueError('夹具作用域不匹配')
        return DownloadReference(**value)


class CompatibilityContractTests(unittest.TestCase):
    def setUp(self):
        state, self.reference = _fixture()
        self.repository = _Repository(state)
        self.resolver = CompatibilityResolver(self.repository)

    def check(self, code=DecisionCode.COMPATIBLE_SYNTHETIC, reference=None, path=ROOT, now=NOW):
        result = self.resolver.resolve(reference or self.reference, path, now)
        self.assertEqual(result.code, code)
        self.assertTrue(result.synthetic_only)
        self.assertEqual(result.compatible, code is DecisionCode.COMPATIBLE_SYNTHETIC)
        if not result.compatible:
            self.assertIsNone(result.resource)
        return result

    def update(self, group, index=0, **fields):
        state = self.repository.state
        rows = list(getattr(state, group))
        rows[index] = replace(rows[index], **fields)
        self.repository.state = replace(state, **{group: tuple(rows)}, revision=state.revision + 1)

    def update_facts(self, **fields):
        facts = replace(self.repository.state.sources[0].facts, **fields)
        self.update('sources', facts=facts)

    def use(self, source_type):
        self.repository.state, self.reference = _fixture(source_type)

    def replace_manifest(self, manifest, grant_index=0):
        digest = manifest_sha256(manifest)
        self.update('grants', grant_index, manifest=manifest, manifest_sha256=digest)
        source = self.repository.state.sources[grant_index]
        if isinstance(source.facts, PublishedAssetFacts):
            self.update('sources', grant_index, facts=replace(source.facts, manifest_sha256=digest))

    def replace_root_links(self, *references):
        grant = self.repository.state.grants[0]
        manifest = replace(grant.manifest, resources=(
            _resource(ROOT, _body(references)), grant.manifest.resources[1]))
        self.replace_manifest(manifest)

    def test_three_sources_share_resolver_and_preserve_unknown(self):
        for kind in SourceType:
            with self.subTest(kind=kind):
                self.use(kind)
                result = self.check()
                self.assertEqual(result.resource.source_type, kind)
                self.assertEqual(result.resource.metering,
                                 MeteringState.UNKNOWN if kind is SourceType.P8_ASSET else MeteringState.MEASURED)
                self.assertFalse(hasattr(result, 'download_url'))
                self.assertFalse(hasattr(result.resource, 'content'))

    def test_new_alias_and_embedded_alias_use_same_grant(self):
        alias_reference = replace(self.reference, kind='alias', handle='fixture-alias')
        self.replace_root_links(_link(alias_reference))
        new = self.check()
        old = self.check(reference=alias_reference)
        self.assertEqual(new, old)
        self.assertEqual(self.repository.reads, 2)

    def test_every_resolution_checks_current_owner(self):
        self.check()
        self.update('owners', active=False)
        for reference in (self.reference, replace(self.reference, kind='alias', handle='fixture-alias')):
            self.check(DecisionCode.OWNER_INACTIVE, reference=reference)

    def test_owner_permission_revision_drift_is_rejected(self):
        self.update('owners', revision=3)
        self.check(DecisionCode.REVISION_MISMATCH)

    def test_source_ownership_drift_is_rejected(self):
        self.update('sources', owner_id='fixture-other-owner')
        self.check(DecisionCode.OWNERSHIP_MISMATCH)

    def test_source_service_drift_is_rejected(self):
        self.update('sources', service_id='fixture-other-service')
        self.check(DecisionCode.OWNERSHIP_MISMATCH)

    def test_target_owner_drift_is_rejected(self):
        self.update('services', owner_id='fixture-other-owner')
        self.check(DecisionCode.OWNERSHIP_MISMATCH)

    def test_missing_owner_does_not_create_service_or_grant(self):
        original = self.repository.state
        self.repository.state = replace(original, owners=(), services=(), grants=())
        self.check(DecisionCode.GRANT_NOT_FOUND)
        self.assertEqual(self.repository.state.owners, ())
        self.assertEqual(self.repository.state.services, ())
        self.assertEqual(self.repository.state.grants, ())

    def test_verification_state_and_verifier_drift_are_rejected(self):
        original = self.repository.state
        for fields in ({'state': 'revoked'}, {'state': 'unverified'}, {'verified_at': None},
                       {'verified_at': NOW + timedelta(seconds=1)},
                       {'verified_by_id': 'fixture-nonexistent-verifier'},
                       {'verified_by_revision': 2}, {'evidence_sha256': 'not-a-digest'}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update('bindings', **fields)
                self.check(DecisionCode.BINDING_INVALID)

    def test_current_verifier_inactive_demotion_and_revision_drift_invalidate_binding(self):
        original = self.repository.state
        for fields in ({'active': False}, {'staff': False}, {'revision': 2}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update('owners', 1, **fields)
                self.check(DecisionCode.BINDING_INVALID)

    def test_verification_revision_and_evidence_must_match_grant(self):
        original = self.repository.state
        for fields in ({'revision': 6}, {'source_revision': 12}, {'evidence_sha256': 'c' * 64}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update('bindings', **fields)
                self.check(DecisionCode.REVISION_MISMATCH)

    def test_source_revision_change_requires_reverification(self):
        self.update('sources', revision=12)
        self.check(DecisionCode.REVISION_MISMATCH)

    def test_service_disabled_missing_mapping_and_revision_drift(self):
        original = self.repository.state
        for fields, code in (({'state': 'disabled'}, DecisionCode.SERVICE_UNAVAILABLE),
                             ({'accepted_sources': ()}, DecisionCode.SERVICE_UNAVAILABLE),
                             ({'revision': 5}, DecisionCode.REVISION_MISMATCH)):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update('services', **fields)
                self.check(code)

    def test_same_source_to_multiple_owners_is_repository_conflict(self):
        state = self.repository.state
        self.repository.state = replace(state, bindings=state.bindings + (
            replace(state.bindings[0], owner_id='fixture-other-owner'),))
        self.check(DecisionCode.SOURCE_CONFLICT)

    def test_duplicate_source_snapshot_is_repository_conflict(self):
        state = self.repository.state
        self.repository.state = replace(state, sources=state.sources + (state.sources[0],))
        self.check(DecisionCode.SOURCE_CONFLICT)

    def test_same_source_id_different_instance_is_distinct(self):
        state = self.repository.state
        other_key = replace(state.sources[0].source, instance='fixture-other-instance')
        self.repository.state = replace(state, sources=state.sources + (
            replace(state.sources[0], source=other_key, owner_id='fixture-other-owner'),),
            bindings=state.bindings + (replace(state.bindings[0], source=other_key,
                                             owner_id='fixture-other-owner'),))
        self.check()

    def test_same_source_id_different_type_is_distinct(self):
        state = self.repository.state
        other_key = replace(state.sources[0].source, type=SourceType.MEMBERSHIP)
        self.repository.state = replace(state, sources=state.sources + (
            replace(state.sources[0], source=other_key),),
            bindings=state.bindings + (replace(state.bindings[0], source=other_key),))
        self.check()

    def test_multiple_sources_same_service_require_explicit_mapping(self):
        state = self.repository.state
        key = replace(state.sources[0].source, source_id='fixture-second-source')
        other_grant = replace(state.grants[0], grant_id='fixture-second-grant', source=key)
        other_manifest = replace(other_grant.manifest, resources=(
            _resource(ROOT), _resource(RULES, _body(fixture='rules'))))
        other_grant = replace(other_grant, manifest=other_manifest,
                              manifest_sha256=manifest_sha256(other_manifest))
        other_source = replace(state.sources[0], source=key,
                               facts=replace(state.sources[0].facts, manifest_sha256=other_grant.manifest_sha256))
        self.repository.state = replace(state, sources=state.sources + (other_source,),
            bindings=state.bindings + (replace(state.bindings[0], source=key),),
            grants=state.grants + (other_grant,))
        other_reference = replace(self.reference, handle='fixture-second-grant')
        self.check(DecisionCode.SERVICE_UNAVAILABLE, reference=other_reference)
        self.update('services', accepted_sources=(state.sources[0].source, key))
        self.check(reference=other_reference)
        self.check()

    def test_source_unknown_disabled_expired_fail_closed(self):
        original = self.repository.state
        for state in ('unknown', 'disabled', 'expired', 'active_verified'):
            with self.subTest(state=state):
                self.repository.state = original
                self.update('sources', state=state)
                self.check(DecisionCode.SOURCE_UNAVAILABLE)

    def test_capability_and_format_require_source_evidence(self):
        original = self.repository.state
        for fields in ({'download_capability': 'unknown'}, {'download_capability': True},
                       {'download_capability': 'unsupported'}, {'formats': ()}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update('sources', **fields)
                self.check(DecisionCode.CAPABILITY_UNVERIFIED)

    def test_p8_cannot_be_promoted_to_new_commercial_active(self):
        self.update('services', kind='managed_subscription')
        self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)

    def test_p8_requires_specific_published_and_parser_evidence(self):
        original = self.repository.state
        for fields in ({'publication_state': 'unknown'}, {'publication_state': True},
                       {'published_source_revision': 10}, {'parser_evidence_sha256': 'bad'},
                       {'manifest_sha256': 'd' * 64}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update_facts(**fields)
                self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)

    def test_bare_caller_boolean_cannot_replace_source_facts(self):
        self.update('sources', facts=True)
        self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)

    def test_adapter_boolean_is_not_a_typed_eligibility_decision(self):
        class BooleanAdapter:
            def eligibility(self, source, grant, now):
                return True
        self.resolver._adapters[SourceType.P8_ASSET] = BooleanAdapter()
        self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)

    def test_p8_cannot_claim_measured_personal_usage_without_measurement_evidence(self):
        self.update('sources', metering=MeteringState.MEASURED)
        self.check(DecisionCode.METERING_UNAVAILABLE)

    def test_membership_unknown_metering_and_unapplied_are_denied(self):
        self.use(SourceType.MEMBERSHIP)
        original = self.repository.state
        for fields in ({'provisioning_state': 'unknown'}, {'provisioning_state': 'pending'},
                       {'usage_state': 'unknown'}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update_facts(**fields)
                self.check(DecisionCode.METERING_UNAVAILABLE)
        self.repository.state = original
        self.update('sources', metering=MeteringState.UNKNOWN)
        self.check(DecisionCode.METERING_UNAVAILABLE)

    def test_native_gate_not_applied_cannot_be_promoted(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        original = self.repository.state
        for fields in ({'record_enabled': False}, {'record_state': 'pending'},
                       {'applied_revision': 8}, {'subscription_state': 'pending'},
                       {'applied_identity_generation': 12}, {'identity_generation': 12},
                       {'token_version': 16},
                       {'required_lines': frozenset()}, {'permitted_lines': frozenset()},
                       {'active_identity_pairs': frozenset()},
                       {'active_identity_pairs': frozenset({('fixture-line', 'fixture-other-ingress')})}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update_facts(**fields)
                self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)

    def test_native_gate_metering_gap_unknown_denied(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        self.update_facts(metering_gap=True)
        self.check(DecisionCode.METERING_UNAVAILABLE)
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        self.update('sources', metering=MeteringState.UNKNOWN)
        self.check(DecisionCode.METERING_UNAVAILABLE)

    def test_metered_sources_keep_exact_freshness_window(self):
        for kind in (SourceType.MEMBERSHIP, SourceType.DEVICE_SUBSCRIPTION):
            for seconds, code in ((-180, DecisionCode.COMPATIBLE_SYNTHETIC),
                                  (-181, DecisionCode.METERING_UNAVAILABLE),
                                  (30, DecisionCode.COMPATIBLE_SYNTHETIC),
                                  (31, DecisionCode.METERING_UNAVAILABLE)):
                with self.subTest(kind=kind, seconds=seconds):
                    self.use(kind)
                    self.update_facts(usage_updated_at=NOW + timedelta(seconds=seconds))
                    self.check(code)

    def test_metered_sources_missing_naive_and_future_times_fail(self):
        for kind in (SourceType.MEMBERSHIP, SourceType.DEVICE_SUBSCRIPTION):
            for timestamp in (None, NOW.replace(tzinfo=None)):
                with self.subTest(kind=kind, timestamp=timestamp):
                    self.use(kind)
                    self.update_facts(usage_updated_at=timestamp)
                    self.check(DecisionCode.METERING_UNAVAILABLE)

    def test_metered_sources_quota_and_expiry_are_enforced(self):
        for kind in (SourceType.MEMBERSHIP, SourceType.DEVICE_SUBSCRIPTION):
            for fields, code in (({'expires_at': NOW}, DecisionCode.EXPIRED),
                                 ({'expires_at': None}, DecisionCode.EXPIRED),
                                 ({'quota_bytes': None}, DecisionCode.QUOTA_UNAVAILABLE),
                                 ({'used_bytes': None}, DecisionCode.QUOTA_UNAVAILABLE),
                                 ({'used_bytes': -1}, DecisionCode.QUOTA_UNAVAILABLE),
                                 ({'used_bytes': 1000}, DecisionCode.QUOTA_UNAVAILABLE),
                                 ({'quota_bytes': True}, DecisionCode.QUOTA_UNAVAILABLE)):
                with self.subTest(kind=kind, fields=fields):
                    self.use(kind)
                    self.update_facts(**fields)
                    self.check(code)

    def test_native_gate_requires_current_cycle(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        original = self.repository.state
        for fields in ({'cycle_starts_at': None}, {'cycle_ends_at': NOW},
                       {'cycle_starts_at': NOW + timedelta(seconds=1)}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update_facts(**fields)
                self.check(DecisionCode.QUOTA_UNAVAILABLE)

    def test_native_gate_rejects_malformed_or_mutable_identity_facts(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        original = self.repository.state
        for fields in ({'required_lines': {'fixture-line'}},
                       {'permitted_lines': frozenset({True})},
                       {'active_identity_pairs': frozenset({('fixture-line',)})},
                       {'active_identity_pairs': frozenset({('fixture-line', '../outside')})}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update_facts(**fields)
                self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)

    def test_snapshot_collections_must_be_immutable(self):
        state = self.repository.state
        self.repository.state = replace(state, bindings=list(state.bindings))
        self.check(DecisionCode.INVALID_CONTRACT)

    def test_download_identity_credential_and_release_versions_are_independent(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        grant = self.repository.state.grants[0]
        facts = self.repository.state.sources[0].facts
        self.assertEqual((grant.download_generation, facts.identity_generation,
                          facts.token_version, grant.manifest.release_version), (3, 13, 17, 23))
        result = self.check()
        self.assertEqual(result.resource.download_generation, 3)
        self.assertEqual(result.resource.release_version, 23)

    def test_each_independent_version_drift_is_rejected(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        original = self.repository.state
        for group, fields, code in (
                ('services', {'download_generation': 4}, DecisionCode.GENERATION_MISMATCH),
                ('sources', {'release_version': 24}, DecisionCode.MANIFEST_INVALID),
                ('grants', {'source_identity_generation': 14}, DecisionCode.ELIGIBILITY_UNVERIFIED),
                ('grants', {'source_credential_version': 18}, DecisionCode.ELIGIBILITY_UNVERIFIED),
                ('grants', {'release_version': 24}, DecisionCode.MANIFEST_INVALID)):
            with self.subTest(group=group, fields=fields):
                self.repository.state = original
                self.update(group, **fields)
                self.check(code)
        for fields in ({'identity_generation': 14, 'applied_identity_generation': 14},
                       {'token_version': 18}):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update_facts(**fields)
                self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)

    def test_release_can_advance_with_same_download_reference_and_node_identity(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        grant = self.repository.state.grants[0]
        manifest = replace(grant.manifest, release_id='fixture-next-release', release_version=24,
            resources=tuple(replace(item, release_version=24) for item in grant.manifest.resources))
        self.update('sources', release_id=manifest.release_id, release_version=24)
        self.update('grants', release_id=manifest.release_id, release_version=24)
        self.replace_manifest(manifest)
        self.check()
        facts = self.repository.state.sources[0].facts
        self.assertEqual((facts.identity_generation, facts.token_version), (13, 17))

    def test_download_reset_invalidates_old_aliases_without_rotating_node_identity(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        old = self.reference
        new = replace(old, download_generation=4, grant_revision=8)
        self.update('grants', download_generation=4, revision=8)
        self.update('services', download_generation=4)
        self.update('aliases', state='revoked')
        self.replace_root_links(_link(new))
        self.check(reference=new)
        self.check(DecisionCode.GENERATION_MISMATCH, reference=old)
        self.check(DecisionCode.ALIAS_INVALID,
                   reference=replace(old, kind='alias', handle='fixture-alias'))
        self.assertEqual(self.repository.state.sources[0].facts.identity_generation, 13)

    def test_native_integer_fields_reject_bool_even_when_equal_to_one(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        self.update_facts(record_revision=1, applied_revision=1, identity_generation=1,
                          applied_identity_generation=1, token_version=1)
        self.update('grants', source_identity_generation=1, source_credential_version=1)
        self.check()
        original = self.repository.state
        for name in ('record_revision', 'applied_revision', 'identity_generation',
                     'applied_identity_generation', 'token_version'):
            with self.subTest(name=name):
                self.repository.state = original
                self.update_facts(**{name: True})
                self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)
        for name in ('source_identity_generation', 'source_credential_version'):
            with self.subTest(name=name):
                self.repository.state = original
                self.update('grants', **{name: True})
                self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)

    def test_p8_published_revision_rejects_bool_even_when_source_revision_is_one(self):
        self.update('sources', revision=1)
        self.update('bindings', source_revision=1)
        self.update('grants', source_revision=1)
        self.update_facts(published_source_revision=1)
        self.check()
        self.update_facts(published_source_revision=True)
        self.check(DecisionCode.ELIGIBILITY_UNVERIFIED)

    def test_release_integer_fields_reject_bool_even_when_equal_to_one(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        manifest = self.repository.state.grants[0].manifest
        manifest = replace(manifest, release_version=1,
                           resources=tuple(replace(item, release_version=1) for item in manifest.resources))
        self.update('sources', release_version=1)
        self.update('grants', release_version=1)
        self.replace_manifest(manifest)
        self.check()
        original = self.repository.state
        self.update('sources', release_version=True)
        self.check(DecisionCode.MANIFEST_INVALID)
        self.repository.state = original
        self.update('grants', release_version=True)
        self.check(DecisionCode.INVALID_CONTRACT)
        self.repository.state = original
        self.replace_manifest(replace(manifest, release_version=True))
        self.check(DecisionCode.MANIFEST_INVALID)
        self.repository.state = original
        self.replace_manifest(replace(manifest, resources=(
            replace(manifest.resources[0], release_version=True), manifest.resources[1])))
        self.check(DecisionCode.MANIFEST_INVALID)

    def test_service_download_generation_rejects_bool_when_grant_generation_is_one(self):
        self.update('grants', download_generation=1)
        self.update('services', download_generation=1)
        reference = replace(self.reference, download_generation=1)
        self.replace_root_links(_link(reference))
        self.check(reference=reference)
        self.update('services', download_generation=True)
        self.check(DecisionCode.GENERATION_MISMATCH, reference=reference)

    def test_native_facts_do_not_substitute_membership_gate(self):
        self.use(SourceType.DEVICE_SUBSCRIPTION)
        native_facts = self.repository.state.sources[0].facts
        self.use(SourceType.MEMBERSHIP)
        self.update('sources', facts=native_facts)
        self.check(DecisionCode.METERING_UNAVAILABLE)

    def test_old_signed_reference_replay_fails_after_generation_reset(self):
        token = _SyntheticCredentialCodec.issue(self.reference)
        self.check(reference=_SyntheticCredentialCodec.decode(token))
        self.update('grants', download_generation=4, revision=8)
        self.check(DecisionCode.GENERATION_MISMATCH, reference=_SyntheticCredentialCodec.decode(token))

    def test_signed_fake_reference_tampering_is_rejected(self):
        token = _SyntheticCredentialCodec.issue(self.reference)
        payload, signature = token.split('.')
        claims = json.loads(base64.urlsafe_b64decode(payload))
        claims['download_generation'] = 99
        forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode() + '.' + signature
        with self.assertRaises(ValueError):
            _SyntheticCredentialCodec.decode(forged)
        with self.assertRaises(ValueError):
            _SyntheticCredentialCodec.issue(replace(self.reference, scope='production'))

    def test_grant_revision_replay_fails_even_same_generation(self):
        self.update('grants', revision=8)
        self.check(DecisionCode.REVISION_MISMATCH)

    def test_revoke_grant_blocks_all_old_aliases(self):
        state = self.repository.state
        self.repository.state = replace(state, aliases=state.aliases + (
            replace(state.aliases[0], alias_id='fixture-second-alias'),))
        self.update('grants', state='revoked')
        for handle in ('fixture-alias', 'fixture-second-alias'):
            with self.subTest(handle=handle):
                self.check(DecisionCode.GRANT_REVOKED,
                           reference=replace(self.reference, kind='alias', handle=handle))

    def test_alias_state_generation_revision_and_scope_are_rechecked(self):
        alias_reference = replace(self.reference, kind='alias', handle='fixture-alias')
        original = self.repository.state
        for fields, code in (({'state': 'revoked'}, DecisionCode.ALIAS_INVALID),
                             ({'download_generation': 2}, DecisionCode.GENERATION_MISMATCH),
                             ({'grant_revision': 6}, DecisionCode.REVISION_MISMATCH),
                             ({'binding_revision': 4}, DecisionCode.REVISION_MISMATCH),
                             ({'scope': 'production'}, DecisionCode.WRONG_SCOPE)):
            with self.subTest(fields=fields):
                self.repository.state = original
                self.update('aliases', **fields)
                self.check(code, reference=alias_reference)

    def test_scope_must_match_every_resolution_object(self):
        original = self.repository.state
        for group in ('owners', 'services', 'sources', 'bindings', 'grants'):
            with self.subTest(group=group):
                self.repository.state = original
                self.update(group, scope='production')
                self.check(DecisionCode.WRONG_SCOPE)
        self.repository.state = replace(original, scope='production')
        self.check(DecisionCode.WRONG_SCOPE)
        self.repository.state = original
        self.check(DecisionCode.WRONG_SCOPE, reference=replace(self.reference, scope='production'))

    def test_download_reference_does_not_have_admin_authority(self):
        self.check(DecisionCode.REFERENCE_INVALID,
                   reference=replace(self.reference, authority='admin'))

    def test_strict_integer_and_source_key_validation(self):
        self.check(DecisionCode.REFERENCE_INVALID, reference=replace(self.reference, download_generation=True))
        self.update('grants', revision=True)
        self.check(DecisionCode.INVALID_CONTRACT)
        self.repository.state, self.reference = _fixture()
        self.update('grants', source=replace(self.repository.state.grants[0].source, instance='../bad'))
        self.check(DecisionCode.INVALID_CONTRACT)

    def test_resource_whitelist_is_checked_before_manifest(self):
        self.check(DecisionCode.RESOURCE_NOT_ALLOWED, path='profiles/other.json')
        self.update('grants', allowed_resources=(ROOT,))
        self.check(DecisionCode.RESOURCE_NOT_ALLOWED, path=RULES)

    def test_paths_reject_encoded_absolute_and_platform_ambiguities(self):
        for path in ('../secret', 'profiles/../secret', '/profiles/fixture.json',
                     'profiles//fixture.json', 'profiles\\fixture.json', 'C:/secret',
                     '%2e%2e/secret', 'profiles/%2fsecret', '%252e%252e/secret',
                     'https://fixture.invalid/file', '//fixture.invalid/file',
                     'profiles/fixture.json?x=1', 'profiles/fixture.json#x',
                     'profiles/fixture.json\x00', 'profiles/CON.json', 'profiles/aux',
                     'profiles/name.', 'profiles/名字.json', 'profiles/./fixture.json'):
            with self.subTest(path=path):
                self.assertFalse(safe_resource_path(path))
                self.check(DecisionCode.PATH_INVALID, path=path)

    def test_manifest_missing_resource_duplicate_and_scope_denied(self):
        original = self.repository.state
        manifest = original.grants[0].manifest
        for changed, code in ((replace(manifest, resources=manifest.resources[:1]), DecisionCode.MANIFEST_INVALID),
                              (replace(manifest, resources=manifest.resources + (manifest.resources[0],)), DecisionCode.MANIFEST_INVALID),
                              (replace(manifest, scope='production'), DecisionCode.WRONG_SCOPE),
                              (replace(manifest, release_version=22), DecisionCode.MANIFEST_INVALID),
                              (replace(manifest, release_id='other-release'), DecisionCode.MANIFEST_INVALID)):
            with self.subTest(code=code, release_version=changed.release_version):
                self.repository.state = original
                self.replace_manifest(changed)
                self.check(code)

    def test_partial_or_mixed_generation_resource_set_is_denied(self):
        manifest = self.repository.state.grants[0].manifest
        self.replace_manifest(replace(manifest, resources=(manifest.resources[0],
            replace(manifest.resources[1], release_version=22))))
        self.check(DecisionCode.MANIFEST_INVALID)

    def test_manifest_digest_tamper_is_denied(self):
        self.update('grants', manifest_sha256='c' * 64)
        self.update_facts(manifest_sha256='c' * 64)
        self.check(DecisionCode.MANIFEST_INVALID)

    def test_content_tamper_is_denied_even_when_manifest_is_unchanged(self):
        grant = self.repository.state.grants[0]
        changed = replace(grant.manifest.resources[1], content=b'changed-fixture')
        self.update('grants', manifest=replace(grant.manifest, resources=(grant.manifest.resources[0], changed)))
        self.check(DecisionCode.CONTENT_MISMATCH)

    def test_embedded_old_alias_is_denied_from_new_root(self):
        alias = replace(self.reference, kind='alias', handle='fixture-alias')
        self.replace_root_links(_link(alias))
        self.update('aliases', state='revoked')
        self.check(DecisionCode.ALIAS_INVALID)
        self.check(DecisionCode.ALIAS_INVALID, path=RULES)

    def test_embedded_generation_whitelist_path_and_authority_are_checked(self):
        original = self.repository.state
        for reference, path, code in (
                (replace(self.reference, download_generation=2), RULES, DecisionCode.GENERATION_MISMATCH),
                (self.reference, 'rules/other.json', DecisionCode.RESOURCE_NOT_ALLOWED),
                (self.reference, 'rules/%2e%2e/other.json', DecisionCode.PATH_INVALID),
                (replace(self.reference, authority='admin'), RULES, DecisionCode.REFERENCE_INVALID),
                (replace(self.reference, scope='production'), RULES, DecisionCode.WRONG_SCOPE)):
            with self.subTest(code=code):
                self.repository.state = original
                self.replace_root_links(_link(reference, path))
                self.check(code)

    def test_embedded_cannot_switch_to_another_grant(self):
        state = self.repository.state
        other = replace(state.grants[0], grant_id='fixture-second-grant')
        self.repository.state = replace(state, grants=state.grants + (other,))
        self.replace_root_links(_link(replace(self.reference, handle='fixture-second-grant')))
        current = self.repository.state.grants[0]
        self.update('grants', 1, manifest=current.manifest, manifest_sha256=current.manifest_sha256)
        self.check(DecisionCode.EMBEDDED_REFERENCE_INVALID)

    def test_all_resource_references_are_checked_including_indirect_links(self):
        grant = self.repository.state.grants[0]
        manifest = replace(grant.manifest, resources=(grant.manifest.resources[0],
            _resource(RULES, _body((_link(replace(self.reference, download_generation=2), ROOT),), fixture='rules'))))
        self.replace_manifest(manifest)
        self.check(DecisionCode.GENERATION_MISMATCH)

    def test_fixture_parser_rejects_unknown_fields_duplicate_keys_and_wrong_format(self):
        original = self.repository.state
        contents = (b'{"scope":"x","scope":"y","fixture":"rules","references":[]}',
                    json.dumps({'scope': SYNTHETIC_SCOPE, 'fixture': 'rules',
                                'references': [], 'unchecked_url': 'fixture'}).encode(),
                    _body(({'resource': RULES},)))
        for content in contents:
            with self.subTest(content=content):
                self.repository.state = original
                grant = original.grants[0]
                self.replace_manifest(replace(grant.manifest, resources=(grant.manifest.resources[0],
                                                                        _resource(RULES, content))))
                self.check(DecisionCode.EMBEDDED_REFERENCE_INVALID)
        self.repository.state = original
        grant = original.grants[0]
        self.replace_manifest(replace(grant.manifest, resources=(grant.manifest.resources[0],
            replace(grant.manifest.resources[1], format='real-client-json'))))
        self.check(DecisionCode.MANIFEST_INVALID)

    def test_snapshot_drift_denies_after_checks(self):
        self.repository.changed_after_read = True
        self.check(DecisionCode.SNAPSHOT_CHANGED)

    def test_repository_failure_never_exposes_exception_or_falls_back(self):
        class FailedRepository:
            def snapshot(self):
                raise RuntimeError('fake-sensitive-marker')
        result = CompatibilityResolver(FailedRepository()).resolve(self.reference, ROOT, NOW)
        self.assertEqual(result.code, DecisionCode.SNAPSHOT_UNAVAILABLE)
        self.assertNotIn('fake-sensitive-marker', repr(result))

    def test_invalid_now_is_closed(self):
        self.check(DecisionCode.INVALID_CONTRACT, now=NOW.replace(tzinfo=None))

    def test_resolution_does_not_mutate_or_provision_anything(self):
        snapshot = self.repository.state
        self.check()
        self.check(reference=replace(self.reference, kind='alias', handle='fixture-alias'))
        self.assertIs(self.repository.state, snapshot)
        self.assertEqual(snapshot.services[0].kind, 'existing_compatibility')
        self.assertEqual(len(snapshot.grants), 1)


if __name__ == '__main__':
    unittest.main()
