"""固定来源与自建快照的组合、解释及 P8 本地编译；不接生产写入器。"""
import re
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .client_rules import normal_domain, overlaps_protected
from .models import ClientDirectRule, OperationJob, RuleSourceVersion
from .rule_policy_models import RulePolicy, RulePolicyVersion, RulePolicyBinding, RulePolicyCandidate
from .rule_sources import SourceError, canonical, digest, normalize_document, verified_document, write_gate
from .services import audit


RECEIPT_ACTION = 'local_rule_policy_receipt'
MAX_COMPONENTS = 100
MAX_ENTRIES = 2000
PUBLISHER = 'p8_local_candidate'
MESSAGE = '组合版本和编译材料已保存在本地；生产资源与客户端应用尚未改变。'


def require(value, code, message, status=422):
    if not value:
        raise SourceError(code, message, status)


def identifier(value):
    try:
        require(type(value) is str and len(value) == 36, 'invalid_fields', '请使用有效 UUID。')
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise SourceError('invalid_fields', '请使用有效 UUID。') from None


def integer(value, minimum=0):
    require(type(value) is int and minimum <= value <= 2_147_483_647,
            'invalid_fields', '修订号与栅栏必须是有效整数。')
    return value


def actor_check(actor):
    current = get_user_model().objects.filter(pk=actor.pk).values('is_active', 'is_staff').first()
    require(current and current['is_active'], 'authentication_required', '请先登录。', 401)
    require(current['is_staff'], 'permission_denied', '此操作仅限管理员。', 403)


def _rule(row):
    return normalize_document({'schema_version': 1, 'rules': [row]})['rules'][0]


def build_document(data):
    """来源只引用固定版本；自建规则在保存时冻结，后续修改不偷偷漂移。"""
    require(set(data) == {'components', 'overrides'}, 'invalid_fields', '组合只接受来源顺序与显式覆盖。')
    components, overrides = data['components'], data['overrides']
    require(type(components) is list and len(components) <= MAX_COMPONENTS,
            'invalid_fields', '每个方案最多100个来源或自建条目。')
    require(type(overrides) is dict and len(overrides) <= MAX_ENTRIES,
            'invalid_fields', '覆盖必须是条目标识到替代条目标识的映射。')
    entries, references, seen = [], [], set()
    for index, component in enumerate(components):
        require(type(component) is dict and type(component.get('enabled')) is bool,
                'invalid_fields', '每个组合项必须明确启停。')
        kind = component.get('type')
        if kind == 'source':
            require(set(component) == {'type', 'source_id', 'revision', 'enabled'}, 'invalid_fields', '来源引用字段无效。')
            source_id = identifier(component['source_id'])
            revision = integer(component['revision'], 1)
            version = RuleSourceVersion.objects.select_related('source').filter(source__public_id=source_id, revision=revision).first()
            require(version is not None, 'source_not_found', '指定来源版本不存在。', 404)
            rows = verified_document(version)['rules']
            prefix = f'source/{source_id}/{revision}'
            references.append({'type': kind, 'source_id': source_id, 'revision': revision,
                               'sha256': version.sha256, 'name': version.source.name, 'enabled': component['enabled']})
            expanded = [(f'{prefix}/{number}', row, version.source.name) for number, row in enumerate(rows)]
        elif kind == 'custom':
            require(set(component) == {'type', 'rule_id', 'revision', 'enabled'}, 'invalid_fields', '自建引用字段无效。')
            row = ClientDirectRule.objects.filter(pk=integer(component['rule_id'], 1)).first()
            require(row is not None, 'rule_not_found', '自建规则不存在。', 404)
            require(row.revision == integer(component['revision'], 1), 'revision_conflict', '自建规则已更新，请刷新后重选。', 409)
            normalized = _rule({'action': 'client_direct' if row.outbound == 'direct' else 'proxy',
                                'kind': row.kind, 'value': row.value, 'scope_domain': row.scope_domain, 'enabled': row.enabled})
            prefix = f'custom/{row.pk}/{row.revision}'
            references.append({'type': kind, 'rule_id': row.pk, 'revision': row.revision,
                               'sha256': digest(normalized), 'name': '自建规则', 'enabled': component['enabled']})
            expanded = [(prefix, normalized, '自建规则')]
        else:
            raise SourceError('invalid_fields', '仅能引用已有来源版本或自建规则。')
        require(prefix not in seen, 'duplicate_component', '同一版本不能在方案内重复引用。')
        seen.add(prefix)
        for key, row, label in expanded:
            entries.append({'key': key, 'component': index, 'source_name': label, 'origin': kind,
                            'rule': row, 'enabled': component['enabled'] and row['enabled']})
        require(len(entries) <= MAX_ENTRIES, 'rule_limit', '组合最多2000条规则。')
    by_key = {entry['key']: entry for entry in entries}
    for target, replacement in overrides.items():
        require(type(target) is str and target in by_key and by_key[target]['origin'] == 'source',
                'invalid_override', '只能显式覆盖已选来源条目。')
        require(replacement is None or (type(replacement) is str and replacement in by_key
                and by_key[replacement]['origin'] == 'custom' and by_key[replacement]['enabled']),
                'invalid_override', '替代项必须是启用的自建条目；null表示显式禁用该来源项。')
        if replacement is not None:
            require(match_key(by_key[target]['rule']) == match_key(by_key[replacement]['rule']),
                    'invalid_override', '替代规则必须具有相同匹配范围；父子域请调整顺序并单独审查。')
        by_key[target]['enabled'] = False
        by_key[target]['overridden_by'] = replacement
    active_keys = {}
    for entry in entries:
        if not entry['enabled']:
            continue
        key = match_key(entry['rule'])
        require(key not in active_keys, 'explicit_override_required',
                '多个启用条目使用相同匹配；请显式覆盖来源或禁用一个组合项。', 409)
        active_keys[key] = entry['key']
    return {'schema_version': 1, 'components': components, 'references': references,
            'overrides': overrides, 'entries': entries, 'scope': 'client_routing'}


def match_key(row):
    return row['kind'], row['value'], row['scope_domain']


def version_document(version):
    doc = version.document
    require(type(doc) is dict and digest(doc) == version.sha256 and doc.get('schema_version') == 1
            and doc.get('scope') == 'client_routing' and type(doc.get('entries')) is list,
            'invalid_policy_version', '组合版本正文或摘要不一致，请核查。', 409)
    for entry in doc['entries']:
        require(type(entry) is dict and type(entry.get('enabled')) is bool and _rule(entry['rule']) == entry['rule'],
                'invalid_policy_version', '组合版本含有无效条目。', 409)
    return doc


def policy_item(policy):
    return {'policy_id': str(policy.public_id), 'name': policy.name, 'revision': policy.revision,
            'updated_at': policy.updated_at.isoformat(), 'state': 'candidate_local'}


def binding_item(binding):
    return {'binding_id': str(binding.public_id), 'service_id': str(binding.service_public_id),
            'service_source': binding.service_source, 'revision': binding.revision, 'fence': binding.fence,
            'publisher': binding.publisher, 'current_candidate_id': str(binding.current_candidate.public_id) if binding.current_candidate_id else None}


def candidate_item(candidate):
    require(digest(candidate.artifact) == candidate.sha256 and candidate.manifest.get('artifact_sha256') == candidate.sha256,
            'invalid_candidate', '候选材料摘要不一致。', 409)
    return {'candidate_id': str(candidate.public_id), 'fence': candidate.fence, 'manifest': candidate.manifest,
            'artifact': candidate.artifact, 'sha256': candidate.sha256, 'state': 'candidate_local',
            'production_published': False, 'client_apply_state': 'unknown'}


def get_policy(policy_id):
    policy = RulePolicy.objects.filter(public_id=identifier(str(policy_id))).first()
    require(policy is not None, 'not_found', '规则方案不存在。', 404)
    return policy


def get_version(policy, revision):
    version = policy.versions.filter(revision=integer(revision, 1)).first()
    require(version is not None, 'not_found', '方案版本不存在。', 404)
    version_document(version)
    return version


def _mutation(actor, operation, data, callback, check=None):
    write_gate()
    key = identifier(data['idempotency_key'])
    fingerprint = digest({'operation': operation, 'data': data})
    with transaction.atomic():
        actor_check(actor)
        if check is not None:
            check()
        receipt = OperationJob.objects.filter(actor=actor, action=RECEIPT_ACTION, payload__idempotency_key=key).first()
        if receipt:
            require(receipt.payload['fingerprint'] == fingerprint, 'idempotency_conflict', '该幂等键已用于其他操作。', 409)
            return {**receipt.payload['response'], 'replayed': True}
        result = callback()
        result.update(message=MESSAGE, replayed=False)
        OperationJob.objects.create(actor=actor, action=RECEIPT_ACTION, state='succeeded', result_code='LOCAL_CANDIDATE_ONLY',
            finished_at=timezone.now(), payload={'idempotency_key': key, 'fingerprint': fingerprint, 'response': result})
        audit(actor, 'rule_policy_' + operation[:20], result='local_candidate')
        return result


def save_policy(actor, data, policy_id=None):
    def apply():
        name = data['name'].strip() if type(data['name']) is str else None
        require(name is not None and len(name) <= 100 and not any(ord(c) < 32 for c in name), 'invalid_fields', '方案名称无效。')
        name = name or '默认规则方案'
        revision = integer(data['expected_revision'])
        if policy_id:
            policy = get_policy(policy_id)
            require(policy.revision == revision, 'revision_conflict', '方案已更新，请刷新并保留当前输入。', 409)
            policy.revision += 1
            policy.name = name
            policy.save(update_fields=['revision', 'name', 'updated_at'])
        else:
            require(revision == 0, 'revision_conflict', '新方案修订号须为0。', 409)
            policy = RulePolicy.objects.create(name=name, creator=actor)
        document = build_document({'components': data['components'], 'overrides': data['overrides']})
        version = RulePolicyVersion.objects.create(policy=policy, revision=policy.revision, document=document,
                                                   sha256=digest(document), creator=actor)
        return {'item': policy_item(policy), 'version': {'revision': version.revision, 'sha256': version.sha256, 'document': document}}
    return _mutation(actor, 'save:' + str(policy_id or 'new'), data, apply)


def resolve_service(service_id, source):
    from . import service_projection
    from .models import Entitlement, Membership, P8SourceBinding
    require(source in ('entitlement', 'membership', 'p8'), 'invalid_fields', '服务来源不受支持。')
    public_id = identifier(service_id)
    model = {'entitlement': Entitlement, 'membership': Membership, 'p8': P8SourceBinding}[source]
    record = model.objects.filter(public_id=public_id).first()
    require(record is not None, 'service_not_found', '服务不存在。', 404)
    owner = record.owner if source == 'p8' else record.user
    resolved = service_projection.resolve(owner, public_id)
    require(resolved is not None and resolved[0] == source and str(resolved[1].public_id) == public_id,
            'service_mapping_required', '请使用已核对的规范服务编号和来源。', 409)
    item = service_projection.project(record, source)
    require(owner.is_active and item.get('state') not in ('mapping_required', 'unknown')
            and item.get('business_state') not in ('mapping_required', 'unknown'),
            'service_mapping_required', '服务来源或归属尚未核对，不能绑定方案。', 409)
    return item


def bind_policy(actor, policy_id, data):
    # 幂等重试也重新核对当前服务归属，不借旧回执绕过失效绑定。
    def apply():
        policy = get_policy(policy_id)
        binding = RulePolicyBinding.objects.filter(service_public_id=data['service_id'], service_source=data['service_source']).first()
        expected = integer(data['expected_binding_revision'])
        if binding:
            require(binding.policy_id == policy.pk, 'publisher_conflict', '该服务已有规则方案；请在原方案内创建新版本。', 409)
            require(binding.revision == expected, 'revision_conflict', '服务方案绑定已变化。', 409)
        else:
            require(expected == 0, 'revision_conflict', '新绑定修订号须为0。', 409)
            binding = RulePolicyBinding.objects.create(policy=policy, service_public_id=data['service_id'],
                service_source=data['service_source'], creator=actor)
        return {'binding': binding_item(binding)}
    return _mutation(actor, 'bind:' + str(policy_id), data, apply,
                     lambda: resolve_service(data['service_id'], data['service_source']))


def explain(document, domain):
    try:
        domain = normal_domain(domain)
    except ValidationError:
        raise SourceError('invalid_domain', '请输入完整有效域名。') from None
    matches = []
    for index, entry in enumerate(document['entries']):
        row = entry['rule']
        matched = (domain == row['value'] if row['kind'] == 'exact' else
                   domain == row['value'] or domain.endswith('.' + row['value']) if row['kind'] == 'suffix' else
                   domain.endswith('.' + row['scope_domain']) and re.fullmatch(row['value'], domain) is not None)
        if matched:
            matches.append({'key': entry['key'], 'order': index + 1, 'source_name': entry['source_name'],
                            'action': row['action'], 'enabled': entry['enabled'], 'overridden': 'overridden_by' in entry})
    selected = next((entry for entry in matches if entry['enabled']), None)
    protected = overlaps_protected(domain)
    return {'domain': domain, 'matches': matches, 'protected': protected,
            'selected_key': None if protected else selected['key'] if selected else None,
            'final_action': 'proxy' if protected or selected is None else selected['action'],
            'reason': '强制保护域优先' if protected else '按组合顺序首次匹配' if selected else '默认走当前代理',
            'scope': 'policy_candidate', 'production_published': False}


def compile_artifact(document):
    active = [entry for entry in document['entries'] if entry['enabled']]
    require(len(active) <= 200, 'p8_rule_limit', '现有P8生成器最多接收200条组合规则，不能静默截断。', 422)
    # 现生成器按动作分组，代理优先；有相反顺序时拒绝假装已保留任意顺序。
    seen_direct = False
    for entry in active:
        if entry['rule']['action'] == 'client_direct':
            seen_direct = True
        elif seen_direct:
            raise SourceError('p8_order_unsupported', '现有P8生成器要求代理项在直连项之前，请调整组合顺序；未改变旧候选。', 422)
    return {'schema_version': 2, 'rules': [
        {'action': 'direct' if row['rule']['action'] == 'client_direct' else 'proxy',
         'kind': row['rule']['kind'], 'value': row['rule']['value'], 'scope': row['rule']['scope_domain']}
        for row in active]}


def compile_candidate(actor, policy_id, data):
    actor_check(actor)
    binding = RulePolicyBinding.objects.filter(public_id=identifier(data['binding_id']), policy__public_id=policy_id).first()
    require(binding is not None, 'not_found', '方案绑定不存在。', 404)
    def check():
        binding.refresh_from_db()
        resolve_service(str(binding.service_public_id), binding.service_source)
        require(binding.service_source == 'p8', 'adapter_unavailable', '本批仅接已有P8生成器；此来源尚无编译适配器。', 409)
    def apply():
        binding.refresh_from_db()
        policy = get_policy(policy_id)
        require(policy.revision == integer(data['revision'], 1), 'revision_conflict', '方案已更新，请选择当前版本重新编译。', 409)
        require(binding.publisher == PUBLISHER and binding.fence == integer(data['expected_fence']),
                'publisher_conflict', '发布者或版本栅栏已变化，请刷新。', 409)
        expected = None if data['expected_candidate_id'] is None else identifier(data['expected_candidate_id'])
        current = str(binding.current_candidate.public_id) if binding.current_candidate_id else None
        require(current == expected, 'candidate_conflict', '已有更新候选，不能覆盖或回退它。', 409)
        version = get_version(policy, policy.revision)
        artifact = compile_artifact(version_document(version))
        fence = binding.fence + 1
        manifest = {'schema_version': 1, 'kind': 'p8_client_policy_candidate', 'publisher': PUBLISHER,
            'service_id': str(binding.service_public_id), 'service_source': binding.service_source,
            'policy_id': str(policy.public_id), 'policy_revision': version.revision, 'policy_sha256': version.sha256,
            'artifact_sha256': digest(artifact), 'artifact_encoding': 'utf8_sorted_compact_json_no_newline',
            'fence': fence, 'expected_candidate_id': current,
            'affected_resources': ['windows-rules', 'windows-routing', 'android', 'v2rayng-routes', 'v2rayng-geosite', 'v2rayng-geoip'],
            'preserved_resources': ['windows', 'v2rayng'], 'production_published': False,
            'production_requirements': ['fixed_generator_and_base_rules_sha256', 'same_source_version_for_all_resources',
                'exclusive_publisher_lock', 'expected_current_release_and_fence', 'protected_domains_recheck',
                'whole_bundle_check_then_atomic_publish', 'rollback_only_if_current_release_is_ours'],
            'dns_scope': 'client_artifacts_only', 'core_or_server_dns_mutation': False}
        candidate = RulePolicyCandidate.objects.create(binding=binding, version=version, fence=fence,
            manifest=manifest, artifact=artifact, sha256=digest(artifact), creator=actor)
        binding.fence = fence
        binding.current_candidate = candidate
        binding.save(update_fields=['fence', 'current_candidate', 'updated_at'])
        return {'candidate': candidate_item(candidate), 'binding': binding_item(binding)}
    return _mutation(actor, 'compile:' + str(policy_id), data, apply, check)
