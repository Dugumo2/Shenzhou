"""采购计划有限接口；仅消费权威服务，不连接供应商或改写个人账本。"""
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import RequestDataTooBig

from .api import endpoint, error, success
from .resource_plan_service import (
    ResourcePlanError,
    confirm_resource_plan,
    preview_resource_plan,
    resource_plan_state,
)


MAX_BODY_BYTES = 8192
PREVIEW_FIELDS = frozenset(('expected_revision', 'source_fingerprint', 'operation', 'changes'))
CONFIRM_FIELDS = PREVIEW_FIELDS | {'preview_token', 'idempotency_key'}


def _current_staff(request):
    """缓存的会话用户不能绕过管理员撤权，读取也须核验当前身份。"""
    return get_user_model().objects.filter(
        pk=request.user.pk, is_active=True, is_staff=True,
    ).first()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_field')
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError('non_finite_number')


def _payload(request, fields):
    if request.content_type != 'application/json':
        return None, error('unsupported_media_type', '请使用 JSON 请求。', 415)
    try:
        body = request.body
        if len(body) > MAX_BODY_BYTES:
            raise ValueError('body_too_large')
        value = json.loads(body, object_pairs_hook=_unique_pairs, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError, RequestDataTooBig):
        return None, error('invalid_json', '请求内容无效或超过大小限制。', 422)
    if type(value) is not dict or set(value) != fields:
        return None, error('invalid_fields', '请求字段不完整或包含不支持的字段。', 422)
    return value, None


def _dispatch(request, binding_key, operation, fields=None):
    actor = _current_staff(request)
    if actor is None:
        return error('permission_denied', '此操作仅限当前有效管理员。', 403)
    if fields is not None:
        payload, failure = _payload(request, fields)
        if failure is not None:
            return failure
    try:
        # 所有归属、来源、版本、预览和幂等判定均由同一权威服务完成。
        result = operation(actor, binding_key) if fields is None else operation(actor, binding_key, payload)
        response = success(result)
    except ResourcePlanError as exc:
        response = error(exc.code, exc.message, exc.status)
    response['Cache-Control'] = 'no-store'
    return response


@endpoint(staff=True)
def resource_plan(request, binding_key):
    return _dispatch(request, binding_key, resource_plan_state)


@endpoint(methods=('POST',), staff=True)
def preview(request, binding_key):
    return _dispatch(request, binding_key, preview_resource_plan, PREVIEW_FIELDS)


@endpoint(methods=('POST',), staff=True)
def confirm(request, binding_key):
    return _dispatch(request, binding_key, confirm_resource_plan, CONFIRM_FIELDS)
