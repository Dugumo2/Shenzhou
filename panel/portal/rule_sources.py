"""严格文件规则来源的解析、差异和本地候选版本保存。"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F
from django.utils import timezone

from .client_rules import normal_domain, validate_rule
from .models import ClientDirectRule, OperationJob, RuleSource, RuleSourceVersion
from .services import audit


MAX_BYTES = 256 * 1024
MAX_RULES = 500
MAX_CONFLICTS = 100
PREVIEW_SECONDS = 600
TOKEN_SALT = 'portal.local-rule-source-preview.v1'
RECEIPT_ACTION = 'local_rule_source_receipt'
INPUT_KEYS = frozenset(('name', 'source_id', 'expected_revision', 'document'))
MESSAGE = '仅保存本地来源候选版本，尚未绑定组合方案、生产发布或客户端应用。'


class SourceError(Exception):
    def __init__(self, code, message, status=422, fields=None, conflicts=None):
        self.code, self.message, self.status = code, message, status
        self.fields, self.conflicts = fields or {}, conflicts or []


def strict_json(raw):
    """上传文本和请求信封都拒绝重复键、非标准数字及过深结构。"""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('重复字段')
            result[key] = value
        return result

    try:
        if type(raw) is bytes:
            raw = raw.decode('utf-8')
        value = json.loads(raw, object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('无效数字')))
        canonical(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise SourceError('invalid_json', '内容不是有效的严格 UTF-8 JSON；请检查重复字段和格式。') from None


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _domain(value):
    domain = normal_domain(value)
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValidationError('这里只接受域名，不接受 IP 地址。')


def normalize_document(document):
    try:
        size = len(document.encode('utf-8')) if type(document) is str else len(canonical(document))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise SourceError('invalid_document', '文件必须是有效的 UTF-8 JSON 对象。') from None
    if size > MAX_BYTES:
        raise SourceError('document_too_large', '规则文件最多 256 KiB。')
    if type(document) is str:
        document = strict_json(document)
    if (type(document) is not dict or set(document) != {'schema_version', 'rules'}
            or type(document.get('schema_version')) is not int or document['schema_version'] != 1
            or type(document.get('rules')) is not list):
        raise SourceError('invalid_document', '仅支持 schema_version=1 且只有 rules 数组的规则文件；不兼容字段不会被丢弃。')
    if len(document['rules']) > MAX_RULES:
        raise SourceError('rule_limit', '每个文件最多 500 条规则。')
    result, seen = [], {}
    for index, row in enumerate(document['rules']):
        field = f'document.rules.{index}'
        if (type(row) is not dict or not {'action', 'kind', 'value'} <= set(row)
                or not set(row) <= {'action', 'kind', 'value', 'scope_domain', 'enabled'}):
            raise SourceError('invalid_document', '规则包含缺失或不兼容字段。', fields={field: ['请使用已支持的规则字段。']})
        action, kind, value = row['action'], row['kind'], row['value']
        scope, enabled = row.get('scope_domain', ''), row.get('enabled', True)
        if (type(action) is not str or action not in ('proxy', 'client_direct')
                or type(kind) is not str or kind not in ('exact', 'suffix', 'regex')
                or type(value) is not str or len(value) > 253
                or type(scope) is not str or len(scope) > 253 or type(enabled) is not bool
                or (kind != 'regex' and scope != '')):
            raise SourceError('invalid_rule', '规则字段类型或动作不受支持；域名限定仅用于正则。',
                              fields={field: ['仅支持 proxy/client_direct、exact/suffix/regex 与布尔启用状态。']})
        try:
            _domain(scope if kind == 'regex' else value)
            value, scope = validate_rule(kind, value, scope, 'direct' if action == 'client_direct' else 'proxy')
        except ValidationError as exc:
            raise SourceError('invalid_rule', '规则未通过域名或安全保护校验。',
                              fields={field + '.value': exc.messages}) from None
        item = {'action': action, 'kind': kind, 'value': value, 'scope_domain': scope, 'enabled': enabled}
        key = _match_key(item)
        if key in seen:
            raise SourceError('duplicate_rule', '同一文件存在规范化后的相同匹配条目；请明确保留一条。',
                              conflicts=[{'origin': 'file', 'source_id': None, 'source_name': None,
                                  'candidate_rule_index': index, 'rule_index': seen[key],
                                  'relation': 'duplicate', 'severity': 'blocking',
                                  'message': '相同匹配条目重复，即使动作相反或禁用也需明确处理。'}])
        seen[key] = index
        result.append(item)
    normalized = {'schema_version': 1, 'rules': result}
    if len(canonical(normalized)) > MAX_BYTES:
        raise SourceError('document_too_large', '规范化后的规则文件最多 256 KiB。')
    return normalized


def _match_key(item):
    return item['kind'], item['value'], item['scope_domain']


def normalize_input(data):
    if type(data) is not dict or not INPUT_KEYS <= set(data):
        raise SourceError('invalid_fields', '来源输入字段不完整。')
    name, source_id, revision = data['name'], data['source_id'], data['expected_revision']
    try:
        if type(name) is str:
            name.encode('utf-8')
    except UnicodeError:
        raise SourceError('invalid_fields', '来源名称必须是有效的 UTF-8 文本。') from None
    if (type(name) is not str or not name.strip() or len(name.strip()) > 100
            or any(ord(char) < 32 for char in name)):
        raise SourceError('invalid_fields', '来源名称须为 1～100 个有效字符。', fields={'name': ['请填写有效来源名称。']})
    if type(revision) is not int or not 0 <= revision < 2_147_483_647:
        raise SourceError('invalid_fields', '请提交有效的来源修订号。')
    if source_id is not None:
        try:
            if type(source_id) is not str or len(source_id) != 36:
                raise ValueError()
            source_id = str(uuid.UUID(source_id))
        except (ValueError, AttributeError):
            raise SourceError('invalid_fields', '来源标识必须是 UUID 或新建时的 null。') from None
    if (source_id is None and revision != 0) or (source_id is not None and revision < 1):
        raise SourceError('invalid_fields', '新来源修订号须为 0，更新须提供当前修订号。')
    return {'name': name.strip(), 'source_id': source_id, 'expected_revision': revision,
            'document': normalize_document(data['document'])}


def storage_valid():
    return (connection.vendor == 'sqlite'
            and connection.settings_dict.get('OPTIONS', {}).get('transaction_mode') == 'IMMEDIATE')


def write_gate():
    if settings.PANEL_LIVE:
        raise SourceError('candidate_only', '生产环境未开放来源候选保存。', 403)
    if not storage_valid():
        raise SourceError('unsupported_storage', '候选保存仅支持已核验的本地 SQLite IMMEDIATE。', 409)


def _ensure_actor(actor):
    current = get_user_model().objects.filter(pk=actor.pk).values('is_active', 'is_staff').first()
    if not current or not current['is_active']:
        raise SourceError('authentication_required', '请先登录。', 401)
    if not current['is_staff']:
        raise SourceError('permission_denied', '此操作仅限管理员。', 403)


def load_state(data):
    custom = list(ClientDirectRule.objects.order_by('pk'))
    sources = list(RuleSource.objects.order_by('pk'))
    versions = {version.source_id: version for version in RuleSourceVersion.objects.filter(
        source__in=sources, revision=F('source__revision'))}
    target = next((source for source in sources if str(source.public_id) == data['source_id']), None)
    if data['source_id'] is not None and target is None:
        raise SourceError('not_found', '来源不存在。', 404)
    if target and target.revision != data['expected_revision']:
        raise SourceError('revision_conflict', '来源已更新，请重新预览当前差异。', 409)
    for source in sources:
        version = versions.get(source.pk)
        if version is None:
            raise SourceError('invalid_existing_source', '来源当前版本缺失，请保留旧资料并核查。', 409)
        verified_document(version)
    snapshot = digest({
        'custom': [[row.pk, row.revision, row.outbound, row.kind, row.value, row.scope_domain, row.enabled]
                   for row in custom],
        'sources': [[source.pk, str(source.public_id), source.name, source.revision, source.state,
                     None if source.pk not in versions else [versions[source.pk].sha256,
                         versions[source.pk].count, versions[source.pk].document]] for source in sources],
    })
    return target, custom, sources, versions, snapshot


def verified_document(version):
    """正文、规范化摘要及条目数必须一致，数据库资料损坏时不生成可信回执。"""
    try:
        normalized = normalize_document(version.document)
        valid = (canonical(normalized) == canonical(version.document)
                 and digest(normalized) == version.sha256
                 and type(version.count) is int and version.count == len(normalized['rules']))
    except (SourceError, TypeError, ValueError, UnicodeError, RecursionError):
        valid = False
    if not valid:
        raise SourceError('invalid_existing_source', '来源正文、摘要或条目数不一致，请核查；此次操作未改写。', 409)
    return normalized


def _relation(left, right):
    if _match_key(left) == _match_key(right):
        return 'duplicate'
    a = left['scope_domain'] if left['kind'] == 'regex' else left['value']
    b = right['scope_domain'] if right['kind'] == 'regex' else right['value']
    if left['kind'] == 'regex' or right['kind'] == 'regex':
        if a == b or a.endswith('.' + b) or b.endswith('.' + a):
            return 'regex_scope_overlap'
    elif right['kind'] == 'suffix' and (a == b or a.endswith('.' + b)):
        return 'child_covered_by_parent'
    elif left['kind'] == 'suffix' and (a == b or b.endswith('.' + a)):
        return 'parent_covers_child'
    return None


def analyze_conflicts(items, target, custom, sources, versions):
    conflicts = []

    def append(value):
        conflicts.append(value)
        return len(conflicts) > MAX_CONFLICTS

    references = []
    for row in custom:
        try:
            item = normalize_document({'schema_version': 1, 'rules': [{
                'action': 'client_direct' if row.outbound == 'direct' else row.outbound,
                'kind': row.kind, 'value': row.value, 'scope_domain': row.scope_domain, 'enabled': row.enabled}]})['rules'][0]
        except SourceError:
            if append({'origin': 'custom', 'source_id': 'custom', 'source_name': '自建规则',
                       'rule_id': row.pk, 'rule_index': None, 'candidate_rule_index': None,
                       'relation': 'invalid_existing_rule', 'severity': 'warning',
                       'message': '现有自建候选未通过校验；此次导入不会改写它。'}):
                return conflicts[:MAX_CONFLICTS], True
            continue
        references.append((item, {'origin': 'custom', 'source_id': 'custom', 'source_name': '自建规则',
                                 'rule_id': row.pk, 'rule_index': None}))
    for source in sources:
        if target and source.pk == target.pk:
            continue
        version = versions.get(source.pk)
        try:
            rows = normalize_document(version.document)['rules'] if version else None
            if rows is None:
                raise SourceError('invalid_existing_source', '来源版本缺失。')
        except SourceError:
            if append({'origin': 'source', 'source_id': str(source.public_id), 'source_name': source.name,
                       'rule_index': None, 'candidate_rule_index': None, 'relation': 'invalid_existing_source',
                       'severity': 'warning', 'message': '其它来源当前版本无效；此次导入不会改写它。'}):
                return conflicts[:MAX_CONFLICTS], True
            continue
        for index, row in enumerate(rows):
            references.append((row, {'origin': 'source', 'source_id': str(source.public_id),
                                    'source_name': source.name, 'revision': source.revision, 'rule_index': index}))
    for index, item in enumerate(items):
        within = [(row, {'origin': 'file', 'source_id': None if target is None else str(target.public_id),
                         'source_name': None if target is None else target.name, 'rule_index': other_index})
                  for other_index, row in enumerate(items[:index])]
        for other, location in within + references:
            relation = _relation(item, other)
            if not relation:
                continue
            same = item['action'] == other['action']
            message = ('正则作用域可能重叠，需具体域名和组合顺序才可确认。' if relation == 'regex_scope_overlap' else
                       '同向候选存在重复或父子覆盖；来源尚未组合，此处不判定最终命中。' if same else
                       '相反动作候选重叠；需显式覆盖和组合顺序后才能确定最终动作。')
            if append({**location, 'candidate_rule_index': index, 'relation': relation,
                       'severity': 'warning', 'action': other['action'], 'enabled': other['enabled'],
                       'candidate_value': item['value'], 'other_value': other['value'],
                       'candidate_action': item['action'], 'other_action': other['action'],
                       'same_action': same, 'message': message}):
                return conflicts[:MAX_CONFLICTS], True
    return conflicts, False


def difference(previous, current):
    before = {_match_key(row): row for row in previous}
    after = {_match_key(row): row for row in current}
    added = [row for row in current if _match_key(row) not in before]
    removed = [row for row in previous if _match_key(row) not in after]
    modified = [{'before': before[_match_key(row)], 'after': row} for row in current
                if _match_key(row) in before and before[_match_key(row)] != row]
    return {'added': added, 'removed': removed, 'modified': modified,
            'counts': {'added': len(added), 'removed': len(removed), 'modified': len(modified)},
            'order_changed': [_match_key(row) for row in previous if _match_key(row) in after]
                != [_match_key(row) for row in current if _match_key(row) in before]}


def preview_source(actor, data):
    data = normalize_input(data)
    try:
        with transaction.atomic():
            _ensure_actor(actor)
            target, custom, sources, versions, snapshot = load_state(data)
            previous = []
            if target:
                try:
                    previous = normalize_document(versions[target.pk].document)['rules']
                except SourceError:
                    raise SourceError('invalid_existing_source', '现有来源正文无效，不能用导入静默替换。', 409) from None
            conflicts, truncated = analyze_conflicts(data['document']['rules'], target, custom, sources, versions)
            writable = not settings.PANEL_LIVE and storage_valid()
            token = signing.dumps({'v': 1, 'actor': actor.pk, 'input': digest(data), 'snapshot': snapshot},
                                  salt=TOKEN_SALT, compress=True) if writable else None
            return {'source_id': data['source_id'], 'name': data['name'],
                    'expected_revision': data['expected_revision'], 'sha256': digest(data['document']),
                    'count': len(data['document']['rules']), 'items': data['document']['rules'],
                    'diff': difference(previous, data['document']['rules']), 'conflicts': conflicts,
                    'conflicts_truncated': truncated, 'conflict_limit': MAX_CONFLICTS,
                    'can_commit': writable, 'preview_token': token, 'expires_in': PREVIEW_SECONDS,
                    'state': 'candidate_unbound', 'message': MESSAGE}
    except OperationalError:
        raise SourceError('database_busy', '候选库忙碌，请稍后重新预览。', 409) from None


def version_item(version):
    return {'revision': version.revision, 'sha256': version.sha256, 'count': version.count,
            'created_at': version.created_at.isoformat()}


def source_item(source, version=None):
    return {'source_id': str(source.public_id), 'name': source.name, 'revision': source.revision,
            'state': source.state, 'format': 'json_schema_v1', 'origin': 'file',
            'sha256': None if version is None else version.sha256,
            'count': None if version is None else version.count,
            'created_at': source.created_at.isoformat(), 'updated_at': source.updated_at.isoformat()}


def commit_source(actor, raw):
    write_gate()
    data = normalize_input(raw)
    token, key = raw.get('preview_token'), raw.get('idempotency_key')
    try:
        if type(key) is not str or len(key) != 36:
            raise ValueError()
        key = str(uuid.UUID(key))
    except (ValueError, AttributeError):
        raise SourceError('invalid_fields', '保存需要有效的 UUID 幂等键。') from None
    if type(token) is not str or not token or len(token) > 4096:
        raise SourceError('invalid_preview', '请先取得当前文件的差异预览。', 409)
    if not token.isascii():
        raise SourceError('invalid_fields', '预览凭证必须是有效的签名文本。')
    fingerprint = digest({'input': data, 'preview_token': token})
    try:
        with transaction.atomic():
            _ensure_actor(actor)
            receipt = OperationJob.objects.filter(actor=actor, action=RECEIPT_ACTION,
                                                    payload__idempotency_key=key).first()
            if receipt:
                if receipt.payload.get('fingerprint') != fingerprint:
                    raise SourceError('idempotency_conflict', '此幂等键已用于其它来源输入。', 409)
                return {**receipt.payload['response'], 'replayed': True}, receipt.payload['status']
            try:
                signed = signing.loads(token, salt=TOKEN_SALT, max_age=PREVIEW_SECONDS)
            except signing.SignatureExpired:
                raise SourceError('preview_expired', '预览已超过 10 分钟，请重新预览。', 409) from None
            except (signing.BadSignature, ValueError, TypeError):
                raise SourceError('invalid_preview', '预览凭证无效，请重新预览。', 409) from None
            if (type(signed) is not dict or signed.get('v') != 1 or signed.get('actor') != actor.pk
                    or signed.get('input') != digest(data)):
                raise SourceError('invalid_preview', '预览与管理员或当前输入不一致，请重新预览。', 409)
            source, custom, sources, versions, snapshot = load_state(data)
            if signed.get('snapshot') != snapshot:
                raise SourceError('stale_preview', '来源或自建规则已变化，请重新审核差异。', 409)
            conflicts, truncated = analyze_conflicts(data['document']['rules'], source, custom, sources, versions)
            created = source is None
            if created:
                source = RuleSource.objects.create(name=data['name'], creator=actor)
            else:
                updated = RuleSource.objects.filter(pk=source.pk, revision=data['expected_revision']).update(
                    name=data['name'], revision=F('revision') + 1, updated_at=timezone.now())
                if not updated:
                    raise SourceError('revision_conflict', '来源修订已变化，请重新预览。', 409)
                source.refresh_from_db()
            version = RuleSourceVersion.objects.create(source=source, revision=source.revision,
                document=data['document'], sha256=digest(data['document']),
                count=len(data['document']['rules']), creator=actor)
            result = {'item': source_item(source, version), 'version': version_item(version),
                      'replayed': False, 'conflicts': conflicts, 'conflicts_truncated': truncated,
                      'message': MESSAGE}
            status = 201 if created else 200
            OperationJob.objects.create(actor=actor, action=RECEIPT_ACTION, state='done',
                result_code='candidate_saved', finished_at=timezone.now(), payload={
                    'idempotency_key': key, 'fingerprint': fingerprint, 'response': result, 'status': status})
            audit(actor, 'local_rule_source_save', str(source.public_id), 'CANDIDATE_ONLY')
            return result, status
    except IntegrityError:
        raise SourceError('revision_conflict', '来源修订写入冲突，请刷新后重新预览。', 409) from None
    except OperationalError:
        raise SourceError('database_busy', '候选库忙碌；保留当前输入与幂等键，稍后重试。', 409) from None
