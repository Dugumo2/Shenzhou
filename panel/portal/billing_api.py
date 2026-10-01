"""独立账期JSON接口；使用现有Session与CSRF，不连接服务器执行器。"""
import json
from functools import wraps

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse

from .billing_schedule import BillingError, billing_state, invalid, preview_billing, require_admin, save_billing


def api_response(methods):
    def decorate(view):
        @wraps(view)
        def handler(request, *args, **kwargs):
            try:
                require_admin(request.user)
                if request.method not in methods:
                    response = JsonResponse({'data': None, 'error': {'code': 'METHOD_NOT_ALLOWED', 'message': '不支持此操作。', 'fields': {}}}, status=405)
                    response['Allow'] = ', '.join(methods)
                    return response
                return JsonResponse({'data': view(request, *args, **kwargs), 'error': None})
            except PermissionDenied:
                return JsonResponse({'data': None, 'error': {'code': 'FORBIDDEN', 'message': '仅管理员可以访问重置计划。', 'fields': {}}}, status=403)
            except BillingError as exc:
                return JsonResponse({'data': None, 'error': {'code': exc.code, 'message': exc.messages[0], 'fields': exc.fields}}, status=exc.status)
            except ValidationError as exc:
                return JsonResponse({'data': None, 'error': {'code': 'INVALID_INPUT', 'message': exc.messages[0], 'fields': getattr(exc, 'message_dict', {'__all__': exc.messages})}}, status=422)
        return handler
    return decorate


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('重复JSON字段')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('JSON不能包含非有限数字')


def payload(request, fields):
    if request.content_type != 'application/json':
        raise invalid('__all__', '请使用JSON提交。')
    try:
        data = json.loads(request.body, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeDecodeError):
        raise invalid('__all__', 'JSON格式无效。')
    if not isinstance(data, dict) or set(data) != set(fields):
        raise invalid('__all__', '字段必须完整且仅包含本操作允许的内容。')
    return data


@api_response(['GET', 'PATCH'])
def billing(request, public_id):
    if request.method == 'GET':
        return billing_state(request.user, public_id)
    data = payload(request, ['next_reset_at', 'expected_billing_revision', 'preview_token', 'idempotency_key'])
    return save_billing(request.user, public_id, **data)


@api_response(['POST'])
def preview(request, public_id):
    data = payload(request, ['next_reset_at', 'expected_billing_revision'])
    return preview_billing(request.user, public_id, **data)
