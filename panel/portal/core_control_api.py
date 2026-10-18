"""管理员核心控制入口；仅受理有限候选与任务，不在请求内执行核心动作。"""
import json

from django.core.exceptions import RequestDataTooBig

from .api import endpoint, error, success
from . import core_control


MAX_BODY_BYTES = 4096


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError()
        value[key] = item
    return value


def _invalid_constant(_value):
    raise ValueError()


def _body(request):
    if request.content_type != 'application/json':
        return None, error('unsupported_media_type', '请使用 JSON 请求。', 415)
    try:
        raw = request.body
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        # JSON 解析器的递归上限因解释器而异；这里独立限制结构深度。
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 8:
                raise ValueError()
            children = item.values() if type(item) is dict else item if type(item) is list else ()
            pending.extend((child, depth + 1) for child in children)
    except (ValueError, UnicodeError, RecursionError, RequestDataTooBig):
        return None, error('invalid_json', '核心控制请求内容无效或过大。', 422)
    if type(value) is not dict:
        return None, error('invalid_fields', '请提供完整的核心控制请求对象。', 422)
    return value, None


def _respond(operation, *args, created_status=200, **kwargs):
    try:
        value = operation(*args, **kwargs)
        response = success(value, status=200 if value.get('replayed') else created_status)
    except core_control.CoreControlError as exc:
        response = error(exc.code, exc.message, exc.status)
    response['Cache-Control'] = 'no-store'
    return response


@endpoint(staff=True)
def core_control_detail(request, core_id):
    return _respond(core_control.control_detail, request.user, core_id)


@endpoint(methods=('POST',), staff=True)
def core_candidate_create(request, core_id):
    body, failure = _body(request)
    if failure is not None:
        return failure
    return _respond(core_control.create_candidate, request.user, core_id, body, created_status=201)


def _action(request, core_id, candidate_id, action):
    body, failure = _body(request)
    if failure is not None:
        return failure
    return _respond(core_control.enqueue_action, request.user, core_id, candidate_id, action, body,
                    created_status=202)


@endpoint(methods=('POST',), staff=True)
def core_candidate_check(request, core_id, candidate_id):
    return _action(request, core_id, candidate_id, 'check')


@endpoint(methods=('POST',), staff=True)
def core_candidate_apply(request, core_id, candidate_id):
    return _action(request, core_id, candidate_id, 'apply')


@endpoint(methods=('POST',), staff=True)
def core_candidate_rollback(request, core_id, candidate_id):
    return _action(request, core_id, candidate_id, 'rollback')


@endpoint(staff=True)
def core_job_detail(request, core_id, job_id):
    return _respond(core_control.get_job, request.user, job_id, core_id=core_id)
