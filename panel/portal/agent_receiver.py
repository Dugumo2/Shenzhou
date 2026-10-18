"""默认关闭的轻 Agent 观测接收器；持久提交后才返回精确 ACK。"""
import hashlib
import hmac
import json
import re
import secrets
import uuid

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .agent_receiver_models import AgentRegistration
from .models import ServerObservation
from .observation_ingest import (METRICS, ObservationError, _metrics, _time,
                                 ingest_observation)


MAX_REQUEST_BYTES = 65536
MAX_EVENTS = 100


class AgentProtocolError(ValueError):
    """只保留固定错误码。"""


def _require(valid, code='invalid_request'):
    if not valid:
        raise AgentProtocolError(code)


def _uuid(value):
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _digest(token):
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def register_agent(server):
    """由批准后的本地管理流程调用；仅返回一次秘密，调用方不得记录。"""
    token = secrets.token_urlsafe(32)
    row = AgentRegistration.objects.create(server=server, token_digest=_digest(token))
    return row, token


def revoke_agent(machine_id):
    """与接收锁定同一身份行，撤销后拒绝新请求和已接收事件的重放。"""
    with transaction.atomic():
        row = AgentRegistration.objects.select_for_update().get(machine_id=machine_id)
        row.revoked_at = timezone.now()
        row.save(update_fields=['revoked_at'])


def _accept_observation(identity, event):
    _require(event['kind'] == 'observation', 'unsupported_event_kind')
    payload = event['payload']
    _require(type(payload) is dict and set(payload) == {
        'observed_at', 'expires_at', 'target_fingerprint', 'metrics'})
    _require(type(payload['target_fingerprint']) is str and
             re.fullmatch('[0-9a-f]{64}', payload['target_fingerprint']))
    source = f'agent.{identity.machine_id}.{identity.identity_epoch}'
    policy = {'targets': [f'server:{identity.server.public_id}'], 'check_kinds': [],
              'metrics': list(METRICS), 'max_ttl_seconds': 86400}
    # 已提交但 ACK 丢失时，即使当前目标版本已变，原样重放仍可确认。
    previous = ServerObservation.objects.filter(source=source, event_id=event['event_id']).first()
    if previous is not None:
        _require(previous.server_id == identity.server_id
                 and previous.target_fingerprint == payload['target_fingerprint']
                 and previous.observed_at == _time(payload['observed_at'])
                 and previous.expires_at == _time(payload['expires_at'])
                 and previous.metrics == _metrics(payload['metrics'], policy), 'event_conflict')
        return
    ingest_observation({**payload, 'source': source, 'event_id': event['event_id'],
                        'target_kind': 'server', 'target_id': str(identity.server.public_id)},
                       trusted_sources={source: policy})


def receive_events(body, token):
    """不可嵌套于外层事务：返回代表整个批次已经实际提交。"""
    _require(type(body) is dict and set(body) == {
        'schema_version', 'machine_id', 'identity_epoch', 'events'})
    _require(type(body['schema_version']) is int and body['schema_version'] == 1)
    _require(_uuid(body['machine_id']))
    _require(type(body['identity_epoch']) is int and 1 <= body['identity_epoch'] <= 2147483647)
    events = body['events']
    _require(type(events) is list and 1 <= len(events) <= MAX_EVENTS)
    _require(type(token) is str and re.fullmatch('[A-Za-z0-9_-]{43}', token), 'authentication_failed')
    ids = []
    # durable=True 拒绝外层普通事务，防止先确认后被调用方回滚。
    with transaction.atomic(durable=True):
        identity = AgentRegistration.objects.select_for_update().select_related('server').filter(
            machine_id=body['machine_id']).first()
        _require(identity is not None and identity.revoked_at is None
                 and identity.identity_epoch == body['identity_epoch']
                 and identity.server.enabled
                 and hmac.compare_digest(identity.token_digest, _digest(token)), 'authentication_failed')
        for event in events:
            _require(type(event) is dict and set(event) == {
                'schema_version', 'machine_id', 'identity_epoch', 'event_id', 'kind', 'payload'})
            _require(type(event['schema_version']) is int and event['schema_version'] == 1
                     and type(event['identity_epoch']) is int
                     and event['identity_epoch'] == body['identity_epoch']
                     and event['machine_id'] == body['machine_id']
                     and _uuid(event['event_id']) and event['event_id'] not in ids)
            _accept_observation(identity, event)
            ids.append(event['event_id'])
    return {'schema_version': 1, 'machine_id': body['machine_id'],
            'identity_epoch': body['identity_epoch'], 'durable': True, 'event_ids': ids}


def _response(value, status):
    response = JsonResponse(value, status=status)
    response['Cache-Control'] = 'no-store'
    return response


@transaction.non_atomic_requests
@csrf_exempt
def agent_events(request):
    """独立机器认证，不接受网页登录态，也没有任何命令获取或执行能力。"""
    if not getattr(settings, 'AGENT_RECEIVER_ENABLED', False):
        return _response({'error': 'agent_receiver_disabled'}, 404)
    if request.method != 'POST':
        return _response({'error': 'method_not_allowed'}, 405)
    if not request.is_secure():
        return _response({'error': 'tls_required'}, 400)
    if request.content_type != 'application/json':
        return _response({'error': 'invalid_content_type'}, 400)
    try:
        declared = request.META.get('CONTENT_LENGTH', '')
        _require(declared.isdigit() and 0 < int(declared) <= MAX_REQUEST_BYTES)
        raw = request.body
        _require(0 < len(raw) <= MAX_REQUEST_BYTES)
        body = json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(AgentProtocolError('invalid_request')))
        authorization = request.headers.get('Authorization', '')
        _require(authorization.startswith('Bearer '), 'authentication_failed')
        ack = receive_events(body, authorization[7:])
    except RequestDataTooBig:
        return _response({'error': 'request_too_large'}, 413)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        code = str(exc) if isinstance(exc, (AgentProtocolError, ObservationError)) else 'invalid_request'
        status = 401 if code == 'authentication_failed' else 409 if code == 'event_conflict' else 400
        return _response({'error': code}, status)
    return _response(ack, 200)
