"""管理员本地规则候选接口；数据库保存与生产发布严格分开。"""
import hashlib
import ipaddress
import json
import re
import uuid

from django.conf import settings
from django.core.exceptions import RequestDataTooBig, ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import F
from django.utils import timezone

from . import api
from .client_rules import normal_domain, preview_rule, validate_rule
from .forms import ClientDirectRuleForm
from .models import ClientDirectRule, OperationJob
from .services import audit


EDITABLE = frozenset(('action', 'kind', 'value', 'scope_domain', 'enabled'))
ACTIONS = {'client_direct': 'direct', 'proxy': 'proxy'}
# 只保存已完成的本地回执，不使用执行器认识的动作，也不产生 queued 记录。
RECEIPT_ACTION = 'local_rule_candidate_receipt'
ORDER = '启用代理候选优先；其后保护校验，再本机直连候选；同动作按数据库ID'
LIMITATIONS = ['仅支持本机直连与当前代理；服务器直出和阻断尚未接入。',
               '导入、排序、基础源覆盖、方案绑定与生产发布尚未接入。',
               '当前写入仅在本地 SQLite IMMEDIATE 事务模式开放。']
MESSAGE = '这是本地数据库候选；保存和命中解释均不代表生产发布或客户端已应用。'


class RuleError(Exception):
    def __init__(self, code, message, status=422, fields=None, conflicts=None):
        self.code, self.message, self.status = code, message, status
        self.fields, self.conflicts = fields or {}, conflicts or []


def _failure(exc):
    response = api.error(exc.code, exc.message, exc.status, exc.fields)
    body = json.loads(response.content)
    body['error']['field_errors'] = exc.fields
    body['error']['conflicts'] = exc.conflicts
    response.content = json.dumps(body, ensure_ascii=False)
    return response


def _context():
    return {'scope': 'local_candidate', 'publish_state': 'unknown',
            'client_apply_state': 'unknown', 'message': MESSAGE}


def _revision(rules):
    # 与生成器 hash 分开：禁用、版本和坏候选也必须使编辑快照失效。
    rows = [[r.pk, r.outbound, r.kind, r.value, r.scope_domain, r.enabled, r.revision]
            for r in rules]
    raw = json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _valid(rule):
    try:
        _domain(rule.scope_domain if rule.kind == 'regex' else rule.value)
        validate_rule(rule.kind, rule.value, rule.scope_domain, rule.outbound)
        return True
    except (ValidationError, TypeError, ValueError):
        return False


def _domain(value):
    domain = normal_domain(value)
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        return domain
    raise ValidationError('这里只接受域名，不接受 IP 地址。')


def _item(rule):
    valid = _valid(rule)
    value, scope = validate_rule(rule.kind, rule.value, rule.scope_domain, rule.outbound) if valid else (None, None)
    return {'id': rule.pk, 'action': 'client_direct' if rule.outbound == 'direct' else 'proxy',
            'kind': rule.kind, 'value': value, 'scope_domain': scope,
            'enabled': rule.enabled, 'revision': rule.revision,
            'validation': 'valid' if valid else 'invalid', 'source_id': 'custom'}


def _payload(request, required, optional=()):
    if request.content_type != 'application/json':
        raise RuleError('unsupported_media_type', '请使用 JSON 请求。', 415)
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('重复字段')
                result[key] = value
            return result

        raw = request.body
        if len(raw) > 8192:
            raise RuleError('invalid_fields', '请求内容过大。', fields={'__all__': ['请求内容过大。']})
        data = json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('无效数字')))
    except (ValueError, UnicodeError, RequestDataTooBig):
        raise RuleError('invalid_json', '请求内容不是有效 JSON。') from None
    if (type(data) is not dict or not set(required) <= set(data)
            or not set(data) <= set(required) | set(optional)):
        raise RuleError('invalid_fields', '请求字段不完整或包含不支持的字段。',
                        fields={'__all__': ['仅能提交本操作列出的字段。']})
    errors = {}
    for key in EDITABLE & data.keys():
        value = data[key]
        if key == 'enabled':
            if type(value) is not bool:
                errors[key] = ['启用状态必须是布尔值。']
        elif type(value) is not str or len(value) > 253:
            errors[key] = ['请输入有效文本，最多 253 个字符。']
    if 'action' in data and (type(data['action']) is not str or data['action'] not in ACTIONS):
        errors['action'] = ['仅支持本机直连与当前代理。']
    if 'kind' in data and data['kind'] not in ('exact', 'suffix', 'regex'):
        errors['kind'] = ['仅支持精确域名、域名后缀和限定正则。']
    if 'revision' in data and (type(data['revision']) is not int or not 1 <= data['revision'] <= 2_147_483_647):
        errors['revision'] = ['请提交当前规则的有效版本。']
    if 'confirm' in data and data['confirm'] is not True:
        errors['confirm'] = ['请确认删除此候选。']
    if 'idempotency_key' in data:
        try:
            key = data['idempotency_key']
            if type(key) is not str or len(key) != 36:
                raise ValueError()
            data['idempotency_key'] = str(uuid.UUID(key))
        except (ValueError, AttributeError):
            errors['idempotency_key'] = ['请使用有效 UUID 幂等键。']
    if errors:
        raise RuleError('invalid_fields', '请修正规则字段。', fields=errors)
    return data


def _write_gate():
    if settings.PANEL_LIVE:
        raise RuleError('candidate_only', '生产环境仅开放候选查询；此次保存未执行。', 403)
    # 当前唯一验收的存储模式在 BEGIN 时获得写锁；不将测试外推到其他数据库。
    if not _storage_valid():
        raise RuleError('unsupported_storage', '候选写入需要已核验的 SQLite IMMEDIATE 事务模式。', 409)


def _storage_valid():
    return (connection.vendor == 'sqlite'
            and connection.settings_dict.get('OPTIONS', {}).get('transaction_mode') == 'IMMEDIATE')


def _conflicts(candidate, rules):
    result = []
    candidate_value, candidate_scope = validate_rule(candidate.kind, candidate.value,
                                                    candidate.scope_domain, candidate.outbound)
    for other in rules:
        if candidate.pk == other.pk or not _valid(other):
            continue
        value = candidate_value
        existing, existing_scope = validate_rule(other.kind, other.value, other.scope_domain, other.outbound)
        duplicate = candidate.kind == other.kind and value == existing
        if duplicate:
            relation = 'duplicate'
        elif candidate.kind == 'regex' or other.kind == 'regex':
            left = candidate_scope if candidate.kind == 'regex' else value
            right = existing_scope if other.kind == 'regex' else existing
            if not (left == right or left.endswith('.' + right) or right.endswith('.' + left)):
                continue
            relation = 'regex_scope_overlap'
        elif ((other.kind == 'suffix' and (value == existing or value.endswith('.' + existing)))
              or (candidate.kind == 'suffix' and (existing == value or existing.endswith('.' + value)))):
            # 与旧编辑表单保持一致；精确父域不能覆盖其子域，禁用项也参与重复校验。
            relation = 'child_covered_by_parent' if value.endswith('.' + existing) else 'parent_covers_child'
        else:
            continue
        same = candidate.outbound == other.outbound
        blocking = duplicate or (same and relation != 'regex_scope_overlap')
        message = ('已有相同匹配条目，请编辑现有规则。' if duplicate else
                   '同一走向已有父域或子域覆盖；含禁用候选，需先编辑现有条目。' if blocking else
                   '正则作用域可能重叠；仅实际匹配域名时才能确认命中。' if relation == 'regex_scope_overlap' else
                   '相反走向的父子域候选重叠；启用代理候选优先。')
        result.append({'rule_id': other.pk, 'relation': relation,
                       'severity': 'blocking' if blocking else 'warning',
                       'action': 'client_direct' if other.outbound == 'direct' else 'proxy',
                       'enabled': other.enabled, 'message': message})
    return result


def _form(data, instance, rules):
    fields = {'action': _item(instance)['action'], 'kind': instance.kind, 'value': instance.value,
              'scope_domain': instance.scope_domain, 'enabled': instance.enabled} if instance.pk else {}
    fields.update({k: v for k, v in data.items() if k in EDITABLE})
    if fields.get('kind') != 'regex' and fields.get('scope_domain'):
        raise RuleError('invalid_fields', '域名限定仅用于正则。', fields={'scope_domain': ['非正则规则请留空。']})
    values = {k: v for k, v in fields.items() if k != 'action'}
    values['outbound'] = ACTIONS[fields['action']]
    candidate = ClientDirectRule(pk=instance.pk, **values)
    try:
        _domain(candidate.scope_domain if candidate.kind == 'regex' else candidate.value)
    except ValidationError as exc:
        field = 'scope_domain' if candidate.kind == 'regex' else 'value'
        raise RuleError('invalid_rule', '规则未通过安全校验。', fields={field: exc.messages}) from None
    try:
        candidate.value, candidate.scope_domain = validate_rule(candidate.kind, candidate.value,
                                                                candidate.scope_domain, candidate.outbound)
    except ValidationError as exc:
        field = 'scope_domain' if candidate.kind == 'regex' and '作用域' in exc.messages[0] else 'value'
        raise RuleError('invalid_rule', '规则未通过安全校验。', fields={field: exc.messages}) from None
    conflicts = _conflicts(candidate, rules)
    blocking = [conflict for conflict in conflicts if conflict['severity'] == 'blocking']
    if blocking:
        duplicate = any(conflict['relation'] == 'duplicate' for conflict in blocking)
        message = '已有相同条目，请编辑现有规则。' if duplicate else '同一走向已有父域或子域覆盖，请编辑现有规则。'
        raise RuleError('duplicate_rule' if duplicate else 'invalid_rule', message,
                        409 if duplicate else 422, {'value': [message]}, conflicts)
    form = ClientDirectRuleForm(values, instance=instance)
    if not form.is_valid():
        errors = {('action' if key == 'outbound' else key): [str(error) for error in value]
                  for key, value in form.errors.items()}
        duplicate = any(c['relation'] == 'duplicate' for c in conflicts)
        raise RuleError('duplicate_rule' if duplicate else 'invalid_rule',
                        '已有相同条目，请编辑现有规则。' if duplicate else '规则未通过校验，请检查覆盖与字段。',
                        409 if duplicate else 422, errors, conflicts)
    return form, conflicts


def _mutate(request, operation, data, rule_id=None):
    fingerprint = hashlib.sha256(json.dumps({'operation': operation, 'rule_id': rule_id, 'data': data},
                                          sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    try:
        with transaction.atomic():
            receipt = OperationJob.objects.filter(actor=request.user, action=RECEIPT_ACTION,
                payload__idempotency_key=data['idempotency_key']).first()
            if receipt:
                if receipt.payload.get('fingerprint') != fingerprint:
                    raise RuleError('idempotency_conflict', '此幂等键已用于其他规则操作。', 409)
                saved = dict(receipt.payload['response'])
                saved['replayed'] = True
                saved['message'] = '这是此前本地候选操作的保存结果；当前候选请刷新核对，不代表生产发布或客户端已应用。'
                return api.success(saved, status=receipt.payload['status'])
            rows = list(ClientDirectRule.objects.order_by('pk'))
            instance = next((r for r in rows if r.pk == rule_id), None) if rule_id else ClientDirectRule()
            if instance is None:
                raise RuleError('not_found', '规则不存在或已删除。', 404)
            if operation != 'create' and instance.revision != data['revision']:
                raise RuleError('revision_conflict', '规则已被其他操作修改；请刷新并核对后重试。', 409,
                                {'revision': ['版本已变化，旧编辑未保存。']})
            conflicts = []
            if operation == 'delete':
                deleted_id = instance.pk
                count, _ = ClientDirectRule.objects.filter(pk=instance.pk, revision=data['revision']).delete()
                if not count:
                    raise RuleError('revision_conflict', '规则版本已变化。', 409)
                item = None
            else:
                form, conflicts = _form(data, instance, rows)
                if operation == 'create':
                    rule = form.save(commit=False)
                    rule.creator = request.user
                    rule.save()
                else:
                    fields = {key: form.cleaned_data[key] for key in ('outbound', 'kind', 'value', 'scope_domain', 'enabled')}
                    updated = ClientDirectRule.objects.filter(pk=instance.pk, revision=data['revision']).update(
                        **fields, revision=F('revision') + 1, updated_at=timezone.now())
                    if not updated:
                        raise RuleError('revision_conflict', '规则版本已变化。', 409)
                    rule = ClientDirectRule.objects.get(pk=instance.pk)
                item = _item(rule)
            result = {'item': item, 'replayed': False, 'conflicts': conflicts,
                      'candidate_revision': _revision(list(ClientDirectRule.objects.order_by('pk'))), **_context()}
            if operation == 'delete':
                result['deleted_id'] = deleted_id
            status = 201 if operation == 'create' else 200
            OperationJob.objects.create(actor=request.user, action=RECEIPT_ACTION, state='done',
                result_code='candidate_saved', finished_at=timezone.now(),
                payload={'idempotency_key': data['idempotency_key'], 'fingerprint': fingerprint,
                         'response': result, 'status': status})
            audit(request.user, 'local_rule_' + operation, rule_id or item['id'], 'CANDIDATE_ONLY')
            return api.success(result, status=status)
    except IntegrityError:
        raise RuleError('duplicate_rule', '相同匹配条目已存在，请刷新后编辑现有规则。', 409,
                        {'value': ['相同匹配条目已存在。']}) from None
    except OperationalError:
        raise RuleError('database_busy', '候选库忙碌；保留当前输入及幂等键，稍后重试。', 409) from None


@api.endpoint(methods=('GET', 'POST'), staff=True)
def rules(request):
    if request.method == 'GET':
        # 保留已有来源/生成器字段，不读取活动策略文件或发布器状态文件。
        with transaction.atomic():
            response = api.admin_rules(request)
            body = json.loads(response.content)
            rows = list(ClientDirectRule.objects.order_by('pk'))
            body['data'].update({'read_only': bool(settings.PANEL_LIVE) or not _storage_valid(),
                'items': [_item(row) for row in rows], 'candidate_revision': _revision(rows),
                'actions': list(ACTIONS), 'limitations': LIMITATIONS, 'message': MESSAGE})
            if any(not _valid(row) for row in rows):
                custom = next(source for source in body['data']['sources'] if source['id'] == 'custom')
                custom.update(state='invalid', sha256=None)
        response.content = json.dumps(body, ensure_ascii=False)
        return response
    try:
        _write_gate()
        data = _payload(request, EDITABLE | {'idempotency_key'})
        return _mutate(request, 'create', data)
    except RuleError as exc:
        return _failure(exc)


@api.endpoint(methods=('PATCH', 'DELETE'), staff=True)
def rule_detail(request, rule_id):
    try:
        _write_gate()
        if type(rule_id) is not int or rule_id < 1:
            raise RuleError('not_found', '规则不存在。', 404)
        if request.method == 'DELETE':
            data = _payload(request, ('revision', 'idempotency_key', 'confirm'))
            return _mutate(request, 'delete', data, rule_id)
        data = _payload(request, ('revision', 'idempotency_key'), EDITABLE)
        if not EDITABLE & data.keys():
            raise RuleError('invalid_fields', '请选择至少一个候选字段进行修改。')
        return _mutate(request, 'update', data, rule_id)
    except RuleError as exc:
        return _failure(exc)


@api.endpoint(methods=('POST',), staff=True)
def preview(request):
    try:
        data = _payload(request, ('domain',))
        try:
            domain = _domain(data['domain'])
        except ValidationError as exc:
            raise RuleError('invalid_domain', '请输入有效的完整域名。', fields={'domain': exc.messages}) from None
        rows = list(ClientDirectRule.objects.order_by('pk'))
        invalid_ids = [r.pk for r in rows if not _valid(r)]
        valid_rows = [r for r in rows if r.pk not in invalid_ids]
        value = preview_rule(valid_rows, domain)
        ids = {r.pk: r for r in valid_rows}
        for related in value['related']:
            rule = ids[related['id']]
            normalized, scope = validate_rule(rule.kind, rule.value, rule.scope_domain, rule.outbound)
            related['action'] = 'client_direct' if rule.outbound == 'direct' else 'proxy'
            related.update({'scope_domain': scope, 'source_id': 'custom', 'order': rule.pk,
                'matched': domain == normalized if rule.kind == 'exact' else
                    domain == normalized or domain.endswith('.' + normalized) if rule.kind == 'suffix' else
                    domain.endswith('.' + scope) and re.fullmatch(normalized, domain) is not None})
        matched = ids.get(value['matched_id'])
        value['conflicts'] = _conflicts(matched, valid_rows) if matched else []
        value['source_id'] = 'custom' if matched else 'protected' if value['result'] == 'protected_proxy' else None
        value['final_action'] = ('proxy' if value['result'] in ('proxy_candidate', 'protected_proxy') else
                                 'client_direct' if value['result'] == 'local_direct_candidate' else None)
        # 存在坏候选时不能声称完整解释已知；尤其不能因跳过坏代理规则而宣告直连。
        if invalid_ids:
            value.update(result='invalid_candidate', final_action=None, matched_id=None, source_id=None)
        value.update({'invalid_rule_ids': invalid_ids, 'candidate_revision': _revision(rows),
                      'order_semantics': ORDER, **_context()})
        return api.success(value)
    except RuleError as exc:
        return _failure(exc)
