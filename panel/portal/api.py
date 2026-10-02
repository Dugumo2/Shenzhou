"""同源 Session/CSRF 接口；本人归属、管理员权限与查询副作用分开核验。"""
import hashlib
import json
import uuid
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login as auth_login, logout as auth_logout
from django.core.exceptions import ValidationError
from django.core.paginator import EmptyPage, Paginator
from django.db.models import CharField, F, Q, Value
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.csrf import csrf_failure as html_csrf_failure
from django.views.decorators.debug import sensitive_variables

from .api_helpers import (CLIENTS, client_catalog, compatibility_for, iso,
                          project_service, service_queryset, visible_legacy_services)
from .legacy_binding import verified_legacy_bindings
from .client_rules import PROTECTED, policy_document, validate_rule
from .models import ClientDirectRule, Entitlement
from .services import audit, throttle


def success(data, status=200):
    return JsonResponse({'data': data, 'error': None}, status=status)


def error(code, message, status, fields=None):
    value = {'code': code, 'message': message}
    if fields:
        value['fields'] = fields
    return JsonResponse({'data': None, 'error': value}, status=status)


def csrf_failure(request, reason=''):
    """API 使用固定错误码，保留既有模板路径的框架错误页面。"""
    if request.path.startswith('/api/v1/'):
        return error('csrf_failed', '安全凭证已失效，请刷新页面后重试。', 403)
    return html_csrf_failure(request, reason=reason)


def endpoint(*, methods=('GET',), public=False, staff=False):
    def decorate(view):
        @wraps(view)
        def guarded(request, *args, **kwargs):
            if not public and (not request.user.is_authenticated or not request.user.is_active):
                return error('authentication_required', '请先登录。', 401)
            if staff and not request.user.is_staff:
                return error('permission_denied', '此操作仅限管理员。', 403)
            if request.method not in methods:
                response = error('method_not_allowed', '此接口不支持该请求方法。', 405)
                response['Allow'] = ', '.join(methods)
                return response
            return view(request, *args, **kwargs)
        return guarded
    return decorate


def session_data(request):
    authenticated = request.user.is_authenticated and request.user.is_active
    return {'authenticated': authenticated,
            'user': {'username': request.user.get_username(), 'is_staff': request.user.is_staff} if authenticated else None,
            'csrf_token': get_token(request), 'brand': '神舟云', 'time_zone': 'Asia/Shanghai',
            'environment': {'kind': 'production' if settings.PANEL_LIVE else 'local_candidate',
                            'is_demo': bool(getattr(settings, 'CANDIDATE_DEMO_DATA', False))},
            'password_policy': {'min_length': 12, 'validated_by_server': True,
                                'messages': ['密码至少 12 个字符。', '不能与账号相似、使用常见密码或纯数字。']}}


@endpoint(public=True)
def session(request):
    return success(session_data(request))


def json_body(request, keys):
    """拒绝额外字段，避免复用入口时夹带独立操作参数。"""
    if request.content_type != 'application/json':
        return None, error('unsupported_media_type', '请使用 JSON 请求。', 415)
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate_field')
                result[key] = value
            return result
        body = json.loads(request.body, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_number')))
    except (ValueError, UnicodeError):
        return None, error('invalid_json', '请求内容不是有效 JSON。', 422)
    if type(body) is not dict or set(body) != set(keys):
        return None, error('invalid_fields', '请求字段不完整或包含不支持的字段。', 422)
    return body, None


@endpoint(methods=('POST',), public=True)
@sensitive_variables()
def login(request):
    if request.user.is_authenticated:
        return error('already_authenticated', '请先退出当前账号。', 409)
    body, failure = json_body(request, ('username', 'password'))
    if failure is not None:
        return failure
    if (type(body['username']) is not str or not body['username'].strip() or len(body['username']) > 150
            or type(body['password']) is not str or not 1 <= len(body['password']) <= 1024):
        return error('invalid_fields', '请填写有效账号和密码。', 422)
    username = get_user_model().normalize_username(body['username'].strip())
    address = request.META.get('REMOTE_ADDR', 'unknown')
    if not (throttle('login-address', address, limit=30) and throttle('login-account', username.casefold(), limit=10)):
        response = error('rate_limited', '尝试过于频繁，请稍后再试。', 429)
        response['Retry-After'] = '900'
        return response
    user = authenticate(request, username=username, password=body['password'])
    if user is None or not user.is_active:
        return error('invalid_credentials', '账号或密码不正确。', 401)
    auth_login(request, user)
    audit(user, 'login')
    return success(session_data(request))


@endpoint(methods=('POST',))
def logout(request):
    body, failure = json_body(request, ())
    if failure is not None:
        return failure
    auth_logout(request)
    return success(session_data(request))


@endpoint()
def my_services(request):
    record = service_queryset().filter(user=request.user).first()
    legacy = visible_legacy_services().select_related('user').filter(user=request.user).first()
    items = [project_service(record, 'entitlement')] if record else []
    if legacy is not None:
        items.append(project_service(legacy, 'membership'))
    return success({'items': items, 'service_count': len(items),
                    'compatibility': compatibility_for(request.user.pk, record is not None)})


@endpoint()
def my_service_detail(request, public_id):
    try:
        value = uuid.UUID(public_id)
    except (ValueError, TypeError, AttributeError):
        return error('not_found', '服务不存在或不可访问。', 404)
    record = service_queryset().filter(user=request.user, public_id=value).first()
    if record is not None:
        return success(project_service(record, 'entitlement', detail=True))
    # 已核验合并的旧UUID指向同一目标投影，未核验或坏绑定不能通过详情绕过列表。
    binding = verified_legacy_bindings().filter(owner=request.user, membership__public_id=value).first()
    if binding is not None and binding.entitlement_id is not None:
        return success(project_service(binding.entitlement, 'entitlement', detail=True))
    legacy = visible_legacy_services().select_related('user').filter(user=request.user, public_id=value).first()
    if legacy is None:
        return error('not_found', '服务不存在或不可访问。', 404)
    return success(project_service(legacy, 'membership', detail=True))


@endpoint()
def clients(request):
    return success({'items': client_catalog()})


@endpoint()
def client_guide(request, client_id):
    client = next((value for value in CLIENTS if value['id'] == client_id), None)
    if client is None:
        return error('not_found', '软件指南不存在。', 404)
    return success({'client_id': client_id, 'title': client['name'] + ' 使用指南',
                    'verification': 'unsupported' if client_id == 'router' else 'not_tested',
                    'software_version': None, 'core_version': None,
                    'steps': [] if client_id == 'router' else [
                        {'title': '确认服务与软件版本', 'body': '先进入本人服务，确认管理员已开通，并核对所装软件及核心版本。'},
                        {'title': '取得匹配资源', 'body': '在服务详情选择此软件；资源未准备好时等待管理员处理，不使用示例链接。'},
                        {'title': '导入、更新与验证', 'body': '实际菜单与更新步骤须按核验后的版本补齐；下载后仍需在客户端应用并验证。'}],
                    'update_status': {'published': 'unknown', 'downloaded': 'unknown', 'applied': 'unknown'},
                    'limitations': ['此入口未完成实际软件版本、菜单、导入和更新核验。',
                                    '发布、下载和客户端应用是三个独立状态；下载成功不代表已生效。']})


def page_options(request):
    try:
        page = int(request.GET.get('page', '1'))
        size = int(request.GET.get('page_size', '25'))
    except ValueError:
        return None, error('invalid_pagination', '分页参数必须是整数。', 422)
    if page < 1 or not 1 <= size <= 100:
        return None, error('invalid_pagination', '页码须从 1 开始，每页数量须为 1～100。', 422)
    return (page, size), None


@endpoint(staff=True)
def admin_services(request):
    options, failure = page_options(request)
    if failure is not None:
        return failure
    q, state = request.GET.get('q', '').strip(), request.GET.get('state', 'all')
    states = {'all', 'pending', 'active', 'simulated', 'disabled', 'suspended', 'enforcement_pending',
              'suspension_pending', 'resuming', 'disabling', 'resetting', 'failed', 'blocked', 'expired', 'metering_gap'}
    if len(q) > 150 or state not in states:
        return error('invalid_filter', '搜索或状态筛选无效。', 422)
    now = timezone.now()
    entitlements = service_queryset()
    memberships = visible_legacy_services()
    if q:
        search = Q(user__username__icontains=q) | Q(public_id__icontains=q.replace('-', ''))
        old_targets = verified_legacy_bindings().filter(
            membership__public_id__icontains=q.replace('-', '')).values('entitlement_id')
        entitlements, memberships = entitlements.filter(search | Q(pk__in=old_targets)), memberships.filter(search)
    if state == 'expired':
        entitlements, memberships = entitlements.filter(expires_at__lte=now), memberships.filter(expires_at__lte=now)
    elif state == 'metering_gap':
        entitlements, memberships = entitlements.filter(metering_gap=True), memberships.none()
    elif state == 'disabled':
        entitlements = entitlements.filter(Q(enabled=False) | Q(state='disabled'))
        memberships = memberships.none()
    elif state != 'all':
        entitlements, memberships = entitlements.filter(state=state), memberships.filter(status=state)
    # 只分页公开标识索引，再读取当前页对象，不把全部用户和私有身份送进内存。
    def index(query, source):
        return query.order_by().annotate(sort_name=F('user__username'),
            source_type=Value(source, output_field=CharField())).values('pk', 'public_id', 'sort_name', 'source_type')
    rows = index(entitlements, 'entitlement').union(index(memberships, 'membership')).order_by(
        'sort_name', 'public_id', 'source_type')
    paginator = Paginator(rows, options[1])
    try:
        page = paginator.page(options[0])
    except EmptyPage:
        return error('page_not_found', '此页不存在，请返回前一页。', 404)
    current = list(page.object_list)
    records = {('entitlement', row.pk): row for row in service_queryset().filter(
        pk__in=[value['pk'] for value in current if value['source_type'] == 'entitlement'])}
    records.update({('membership', row.pk): row for row in visible_legacy_services().select_related('user').filter(
        pk__in=[value['pk'] for value in current if value['source_type'] == 'membership'])})
    items = []
    for row in current:
        record = records.get((row['source_type'], row['pk']))
        if record is None:
            # 索引分页后绑定被撤销时关闭该行，不能从旧索引恢复已失效服务。
            continue
        value = project_service(record, row['source_type'], now=now, administrator=True)
        value['user'] = {'username': record.user.get_username()}
        value['compatibility'] = compatibility_for(record.user_id, row['source_type'] == 'entitlement')
        items.append(value)
    return success({'items': items, 'pagination': {'page': page.number, 'page_size': options[1],
                    'total': paginator.count, 'pages': paginator.num_pages,
                    'has_next': page.has_next(), 'has_previous': page.has_previous()}, 'filters': {'q': q, 'state': state}})


@endpoint(staff=True)
def admin_rules(request):
    rules = list(ClientDirectRule.objects.order_by('pk'))
    valid, digest = True, None
    try:
        _, digest = policy_document(rules)
    except (ValidationError, TypeError, ValueError):
        # 不把校验异常和私有路径发到浏览器。
        valid = False
    protected_hash = hashlib.sha256('\n'.join(sorted(PROTECTED)).encode()).hexdigest()
    items = []
    for rule in rules:
        try:
            validate_rule(rule.kind, rule.value, rule.scope_domain, rule.outbound)
            valid_row = True
        except (ValidationError, TypeError, ValueError):
            valid_row = False
        items.append({'id': rule.pk, 'action': 'client_direct' if rule.outbound == 'direct' else 'proxy',
                      'kind': rule.kind, 'value': rule.value if valid_row else None,
                      'scope_domain': rule.scope_domain if valid_row else None, 'validation': 'valid' if valid_row else 'invalid',
                      'enabled': rule.enabled, 'revision': rule.revision, 'source_id': 'custom'})
    return success({'read_only': True, 'scope': 'local_candidate',
                    'sources': [
                        {'id': 'custom', 'name': '数据库自建规则候选', 'kind': 'database', 'state': 'candidate' if valid else 'invalid',
                         'rule_count': len(rules), 'sha256': digest, 'updated_at': iso(max((r.updated_at for r in rules), default=None)),
                         'version': 2, 'generator': 'client_policy', 'order_semantics': '按动作、类型、域名生成候选清单'},
                        {'id': 'protected', 'name': '内置受保护域名校验集', 'kind': 'built_in', 'state': 'validation_only',
                         'rule_count': len(PROTECTED), 'sha256': protected_hash, 'updated_at': None, 'version': None,
                         'generator': 'client_rule_validator', 'order_semantics': '用于拒绝敏感直连重叠，不代表线上匹配顺序'},
                        {'id': 'base', 'name': '基础规则来源', 'kind': 'external_reference', 'state': 'not_connected',
                         'rule_count': None, 'sha256': None, 'updated_at': None, 'version': None,
                         'generator': 'client_bundle', 'order_semantics': '尚未核对当前来源与实际发布版本'}],
                    'items': items,
                    'publish_state': 'unknown', 'client_apply_state': 'unknown',
                    'message': '这是当前数据库候选及已知校验来源索引；不代表生产规则已恢复、发布或客户端已应用。'})
