"""同源管理员规则组合接口；所有变更沿用本地候选门禁。"""
from functools import wraps

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import IntegrityError, OperationalError, transaction

from . import api, rule_policies as policies
from .models import ClientDirectRule, RuleSource, RuleSourceVersion
from .rule_policy_models import RulePolicy
from .rule_sources import SourceError, strict_json, storage_valid, verified_document


SAVE_FIELDS = {'name', 'components', 'overrides', 'expected_revision', 'idempotency_key'}


def guarded(view):
    @wraps(view)
    def call(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except SourceError as exc:
            return api.error(exc.code, exc.message, exc.status, exc.fields)
        except (OperationalError, IntegrityError):
            return api.error('database_conflict', '候选库忙碌或版本冲突，请保留输入并刷新。', 409)
    return call


def payload(request, fields):
    if request.content_type != 'application/json':
        raise SourceError('unsupported_media_type', '请使用 JSON 请求。', 415)
    try:
        raw = request.body
    except RequestDataTooBig:
        raise SourceError('invalid_fields', '请求过大。') from None
    policies.require(len(raw) <= 65536, 'invalid_fields', '组合请求最多64KiB。')
    data = strict_json(raw)
    policies.require(type(data) is dict and set(data) == fields, 'invalid_fields', '请求字段不完整或含不支持的字段。')
    return data


def context():
    return {'read_only': bool(settings.PANEL_LIVE) or not storage_valid(), 'state': 'candidate_local',
            'production_publish_available': False, 'https_import_available': False,
            'message': policies.MESSAGE,
            'limitations': ['HTTPS来源获取尚未开放；可在来源页导入已取得的文件。',
                'P8最多编译200条启用规则，代理项须排在直连项之前。',
                '本页只生成现P8生成器输入；生产发布者锁、真实基础来源与资源包检查需单独接通。']}


@api.endpoint(methods=('GET', 'POST'), staff=True)
@guarded
def collection(request):
    if request.method == 'POST':
        return api.success(policies.save_policy(request.user, payload(request, SAVE_FIELDS)), status=201)
    rows = list(RulePolicy.objects.order_by('-updated_at')[:101])
    return api.success({'items': [policies.policy_item(row) for row in rows[:100]], 'truncated': len(rows) > 100, **context()})


@api.endpoint(staff=True)
@guarded
def options(request):
    """来源选择只给固定版本和非秘密规则；不读取节点或真实订阅链接。"""
    versions = list(RuleSourceVersion.objects.select_related('source').order_by('-created_at', '-id')[:201])
    custom = list(ClientDirectRule.objects.order_by('id')[:501])
    source_items = []
    total_keys = 0
    source_limit = False
    for version in versions[:200]:
        document = verified_document(version)
        if total_keys + len(document['rules']) > 2000:
            source_limit = True
            break
        total_keys += len(document['rules'])
        source_items.append({'source_id': str(version.source.public_id), 'name': version.source.name,
            'revision': version.revision, 'sha256': version.sha256, 'count': version.count,
            'current_revision': version.source.revision,
            'keys': [{'key': f'source/{version.source.public_id}/{version.revision}/{i}',
                      'rule': rule} for i, rule in enumerate(document['rules'])]})
    custom_items = []
    for row in custom[:500]:
        try:
            rule = policies._rule({'action': 'client_direct' if row.outbound == 'direct' else 'proxy',
                'kind': row.kind, 'value': row.value, 'scope_domain': row.scope_domain, 'enabled': row.enabled})
            custom_items.append({'rule_id': row.pk, 'revision': row.revision, 'key': f'custom/{row.pk}/{row.revision}', 'rule': rule})
        except SourceError:
            # 坏旧行不回显为可选择的已验证条目。
            continue
    return api.success({'sources': source_items, 'custom': custom_items,
                        'sources_truncated': len(versions) > 200 or source_limit, 'custom_truncated': len(custom) > 500, **context()})


@api.endpoint(methods=('GET', 'PATCH'), staff=True)
@guarded
def detail(request, policy_id):
    if request.method == 'PATCH':
        return api.success(policies.save_policy(request.user, payload(request, SAVE_FIELDS), policy_id))
    with transaction.atomic():
        policy = policies.get_policy(policy_id)
        text = request.GET.get('revision', str(policy.revision))
        policies.require(text.isascii() and text.isdecimal() and len(text) <= 10, 'invalid_fields', '版本号无效。')
        version = policies.get_version(policy, int(text))
        return api.success({'item': policies.policy_item(policy),
            'version': {'revision': version.revision, 'sha256': version.sha256, 'document': policies.version_document(version)},
            'history': list(policy.versions.values('revision', 'sha256', 'created_at')[:100]),
            'bindings': [policies.binding_item(row) for row in policy.bindings.select_related('current_candidate')], **context()})


@api.endpoint(methods=('POST',), staff=True)
@guarded
def preview(request, policy_id):
    data = payload(request, {'domain', 'revision'})
    with transaction.atomic():
        version = policies.get_version(policies.get_policy(policy_id), data['revision'])
        return api.success({**policies.explain(policies.version_document(version), data['domain']),
                            'revision': version.revision, 'sha256': version.sha256})


@api.endpoint(methods=('POST',), staff=True)
@guarded
def bind(request, policy_id):
    return api.success(policies.bind_policy(request.user, policy_id, payload(request,
        {'service_id', 'service_source', 'expected_binding_revision', 'idempotency_key'})))


@api.endpoint(methods=('POST',), staff=True)
@guarded
def compile(request, policy_id):
    return api.success(policies.compile_candidate(request.user, policy_id, payload(request,
        {'revision', 'binding_id', 'expected_candidate_id', 'expected_fence', 'idempotency_key'})))


@api.endpoint(staff=True)
@guarded
def candidate(request, policy_id, candidate_id):
    policy = policies.get_policy(policy_id)
    row = policies.RulePolicyCandidate.objects.filter(public_id=candidate_id, version__policy=policy).first()
    policies.require(row is not None, 'not_found', '编译候选不存在。', 404)
    return api.success({'candidate': policies.candidate_item(row), **context()})
