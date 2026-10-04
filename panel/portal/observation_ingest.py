"""受信本地采集回执导入；无 HTTP/命令执行入口，不接受浏览器伪造采样。"""
import math
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .inventory_service import digest
from .models import CheckResult, CoreInstance, Egress, Ingress, Line, Server, ServerObservation


TARGETS = {'server': Server, 'line': Line, 'core': CoreInstance, 'ingress': Ingress, 'egress': Egress}
KINDS = {'server': {'service_status'}, 'core': {'core_version', 'service_status'},
         'ingress': {'tcp', 'udp', 'tls'}, 'egress': {'dns', 'egress', 'tcp', 'udp'},
         'line': {'whole_path'}}
METRICS = ('online', 'cpu_percent', 'cpu_window_seconds', 'memory_percent', 'disk_percent',
           'network_rx_bytes', 'network_tx_bytes', 'network_interfaces')
ERROR_STAGES = {'', 'connect', 'handshake', 'resolve', 'request', 'parse', 'validate', 'collect'}
ERROR_CODES = {'', 'connection_refused', 'timeout', 'unreachable', 'tls_failed', 'dns_failed',
               'unexpected_response', 'invalid_result', 'collection_failed', 'unsupported'}


class ObservationError(ValueError):
    """只返回固定错误码，不回显可能含秘密的输入。"""


def _require(value, code):
    if not value:
        raise ObservationError(code)


def _target(kind, identifier):
    _require(type(kind) is str and kind in TARGETS and type(identifier) is str, 'invalid_target')
    model = TARGETS[kind]
    try:
        query = {'pk': int(identifier)} if kind in ('ingress', 'egress') else {'public_id': identifier}
        value = model.objects.filter(**query).first()
    except (ValueError, TypeError, OverflowError, ValidationError):
        raise ObservationError('invalid_target') from None
    _require(value is not None, 'target_not_found')
    canonical = str(value.pk if kind in ('ingress', 'egress') else value.public_id)
    _require(canonical == identifier, 'invalid_target')
    return value


def target_key(kind, target):
    return kind + ':' + str(target.pk if kind in ('ingress', 'egress') else target.public_id)


def _identity(kind, target):
    """别名/备注不参与；端点引用、核心配置版本或拓扑变化使旧回执过时。"""
    base = {'kind': kind, 'id': target_key(kind, target)}
    if kind == 'server':
        return {**base, 'adapter': target.adapter, 'enabled': target.enabled}
    if kind == 'core':
        return {**base, 'server': _identity('server', target.server), 'type': target.core_type,
                'version': target.registered_version, 'configuration_owner': target.configuration_owner,
                'configuration_version': target.configuration_version,
                'ingresses': list(target.ingresses.order_by('pk').values_list('pk', flat=True))}
    if kind in ('ingress', 'egress'):
        value = {**base, 'server': _identity('server', target.server),
                 'reference_digest': digest(target.config_ref)}
        if kind == 'ingress':
            return {**value, 'protocol': target.protocol, 'enabled': target.enabled,
                    'cores': [_identity('core', core) for core in target.core_instances.order_by('pk')]}
        return {**value, 'type': target.kind, 'fail_closed': target.fail_closed}
    return {**base, 'enabled': target.enabled,
            'ingresses': [_identity('ingress', row) for row in target.all_ingresses()],
            'egress': _identity('egress', target.egress)}


def target_fingerprint(kind, target):
    return digest(_identity(kind, target))


def _time(value):
    _require(type(value) is str and len(value) <= 40, 'invalid_time')
    try:
        result = parse_datetime(value)
    except (ValueError, OverflowError):
        raise ObservationError('invalid_time') from None
    _require(result is not None and timezone.is_aware(result), 'invalid_time')
    return result


def _common(payload, trusted_sources, extra):
    common = {'source', 'event_id', 'target_kind', 'target_id', 'target_fingerprint', 'observed_at', 'expires_at'}
    _require(type(payload) is dict and payload.keys() == common | extra, 'invalid_fields')
    source, event = payload['source'], payload['event_id']
    _require(type(source) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', source), 'invalid_source')
    _require(type(event) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', event), 'invalid_event')
    policy = trusted_sources.get(source) if type(trusted_sources) is dict else None
    _require(type(policy) is dict and policy.keys() == {'targets', 'check_kinds', 'metrics', 'max_ttl_seconds'}, 'untrusted_source')
    _require(all(type(policy[key]) in (list, tuple, set, frozenset) and all(type(v) is str for v in policy[key])
                 for key in ('targets', 'check_kinds', 'metrics')), 'invalid_source_policy')
    _require(set(policy['metrics']) <= set(METRICS), 'invalid_source_policy')
    ttl = policy['max_ttl_seconds']
    _require(type(ttl) is int and 0 < ttl <= 86400, 'invalid_source_policy')
    kind = payload['target_kind']
    target = _target(kind, payload['target_id'])
    _require(target_key(kind, target) in policy['targets'], 'target_not_allowed')
    fingerprint = target_fingerprint(kind, target)
    _require(payload['target_fingerprint'] == fingerprint, 'target_version_changed')
    observed, expires = _time(payload['observed_at']), _time(payload['expires_at'])
    _require(observed <= timezone.now() and observed < expires <= observed + timedelta(seconds=ttl), 'invalid_validity')
    return policy, kind, target, dict(source=source, event_id=event, target_fingerprint=fingerprint,
                                     observed_at=observed, expires_at=expires)


def _number(value, high, integer=False):
    return (type(value) is int if integer else type(value) in (int, float)) and 0 <= value <= high and math.isfinite(value)


def _metrics(value, policy):
    _require(type(value) is dict and value.keys() <= set(METRICS) and value.keys() <= set(policy['metrics']), 'invalid_metrics')
    result = {key: value.get(key) for key in METRICS}
    for key, item in result.items():
        if item is None:
            continue
        if key == 'online':
            valid = type(item) is bool
        elif key == 'network_interfaces':
            valid = (type(item) is list and 0 < len(item) <= 32
                     and all(type(x) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', x) for x in item)
                     and len(set(item)) == len(item))
        elif key.endswith('_bytes'):
            valid = _number(item, 9007199254740991, True)
        else:
            valid = _number(item, 300 if key == 'cpu_window_seconds' else 100)
        _require(valid, 'invalid_metrics')
    if result['cpu_percent'] is not None:
        _require(result['cpu_window_seconds'] is not None and result['cpu_window_seconds'] > 0, 'cpu_interval_required')
    if result['network_rx_bytes'] is not None or result['network_tx_bytes'] is not None:
        _require(result['network_interfaces'] is not None, 'network_scope_required')
    return result


def _save(model, values):
    """同来源事件严格相同才可重放；事件内容不可被新回执覆盖。"""
    lookup = {key: values[key] for key in ('source', 'event_id')}
    try:
        with transaction.atomic():
            row, created = model.objects.get_or_create(**lookup, defaults={k: v for k, v in values.items() if k not in lookup})
            if not created:
                _require(all(getattr(row, k) == v for k, v in values.items()), 'event_conflict')
            return row, created
    except IntegrityError:
        raise ObservationError('event_conflict') from None


def ingest_observation(payload, *, trusted_sources):
    """由受审本地采集适配器调用；可信 registry 不能由 payload 提供。"""
    policy, kind, target, values = _common(payload, trusted_sources, {'metrics'})
    _require(kind == 'server', 'observation_target_invalid')
    return _save(ServerObservation, {**values, 'server': target, 'metrics': _metrics(payload['metrics'], policy)})


def ingest_check(payload, *, trusted_sources):
    policy, kind, target, values = _common(payload, trusted_sources,
                                         {'check_kind', 'result', 'latency_ms', 'error_stage', 'error_code'})
    check = payload['check_kind']
    _require(type(check) is str and check in KINDS[kind] and check in policy['check_kinds'], 'check_scope_invalid')
    result, latency = payload['result'], payload['latency_ms']
    _require(result in ('pass', 'fail', 'timeout', 'unknown'), 'invalid_result')
    _require(latency is None or _number(latency, 300000), 'invalid_latency')
    _require(type(payload['error_stage']) is str and payload['error_stage'] in ERROR_STAGES
             and type(payload['error_code']) is str and payload['error_code'] in ERROR_CODES, 'invalid_error')
    _require(result != 'pass' or not payload['error_stage'] and not payload['error_code'], 'invalid_error')
    if kind == 'core':
        _require(not target.ingresses.exclude(server_id=target.server_id).exists(), 'core_target_mismatch')
    return _save(CheckResult, {**values, kind: target, 'check_kind': check, 'result': result,
                              'latency_ms': latency, 'error_stage': payload['error_stage'], 'error_code': payload['error_code']})


def freshness(row, kind, target, now=None):
    if row is None:
        return 'unknown'
    if row.target_fingerprint != target_fingerprint(kind, target):
        return 'target_changed'
    if row.observed_at > (now or timezone.now()) or row.expires_at <= (now or timezone.now()):
        return 'stale'
    return 'fresh'


def observation_projection(row, server):
    if row is None:
        return None
    return {'source': row.source, 'observed_at': row.observed_at.isoformat(), 'expires_at': row.expires_at.isoformat(),
            'freshness': freshness(row, 'server', server), 'metrics': row.metrics,
            'scope': '一次性机器观测；网卡累计不是月用量或个人套餐流量。'}


def check_projection(row):
    kind = next(key for key in TARGETS if getattr(row, key + '_id') is not None)
    target = getattr(row, kind)
    return {'id': str(row.pk), 'target_kind': kind, 'target_id': target_key(kind, target).split(':', 1)[1],
            'source': row.source, 'check_kind': row.check_kind, 'result': row.result,
            'latency_ms': row.latency_ms, 'error_stage': row.error_stage, 'error_code': row.error_code,
            'observed_at': row.observed_at.isoformat(), 'expires_at': row.expires_at.isoformat(),
            'freshness': freshness(row, kind, target)}
