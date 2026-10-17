"""受信本地采集回执导入；无 HTTP/命令执行入口，不接受浏览器伪造采样。"""
import math
import re
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
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
           'network_rx_bytes', 'network_tx_bytes', 'network_interfaces', 'boot_id', 'sample_monotonic_seconds')
_AUTO_PREVIOUS = object()
MAX_EGRESS_CHAIN_DEPTH = 16
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


def _line_ingresses(target):
    """展示用截断列表不能决定完整目标版本；只有显式完整缓存可参与。"""
    if (hasattr(target, 'registered_additional_ingresses')
            and getattr(target, 'registered_additional_ingresses_complete', False)):
        extra = target.registered_additional_ingresses
    else:
        extra = target.additional_ingresses.all()
    unique = {row.pk: row for row in extra if row.pk != target.ingress_id}
    return [target.ingress] + [unique[key] for key in sorted(unique)]


def _egress_chain_identity(target):
    """完整出口链才可形成版本；有限深度与visited拒绝非法链。"""
    nodes, visited = [], set()
    current = target
    while current is not None:
        _require(current.pk not in visited, 'egress_chain_cycle')
        _require(len(nodes) < MAX_EGRESS_CHAIN_DEPTH, 'egress_chain_too_deep')
        visited.add(current.pk)
        core = current.core_instance if current.core_instance_id is not None else None
        nodes.append({**_identity('egress', current, include_egress_chain=False),
                      'ownership': current.ownership, 'external_kind': current.external_kind,
                      'execution_core': _identity('core', core) if core is not None else None})
        next_id = current.next_egress_id
        if next_id is None:
            break
        _require(next_id not in visited, 'egress_chain_cycle')
        _require(len(nodes) < MAX_EGRESS_CHAIN_DEPTH, 'egress_chain_too_deep')
        if 'next_egress' in current._state.fields_cache:
            current = current.next_egress
        else:
            # 未预取时每一段固定查询本段与执行核心，不展开无界递归JOIN。
            current = Egress.objects.select_related('server', 'core_instance__server').prefetch_related(
                'core_instance__ingresses').get(pk=next_id)
    return nodes


def _identity(kind, target, *, include_egress_chain=True):
    """别名/备注不参与；端点引用、核心配置版本或拓扑变化使旧回执过时。"""
    base = {'kind': kind, 'id': target_key(kind, target)}
    if kind == 'server':
        return {**base, 'adapter': target.adapter, 'enabled': target.enabled}
    if kind == 'core':
        return {**base, 'server': _identity('server', target.server), 'type': target.core_type,
                'version': target.registered_version, 'configuration_owner': target.configuration_owner,
                'service_unit': getattr(target, 'service_unit', ''),
                'owner_state': getattr(target, 'owner_state', 'unknown'),
                'configuration_version': target.configuration_version,
                'ingresses': sorted(row.pk for row in target.ingresses.all())}
    if kind in ('ingress', 'egress'):
        value = {**base, 'server': _identity('server', target.server),
                 'reference_digest': digest(target.config_ref)}
        # 新端点地址参与版本；别名、备注和资料修订号不改变检测目标。
        if hasattr(target, 'endpoint_host'):
            value.update(endpoint_host=target.endpoint_host, endpoint_port=target.endpoint_port)
        if kind == 'ingress':
            return {**value, 'protocol': target.protocol, 'enabled': target.enabled,
                    'cores': [_identity('core', core) for core in sorted(target.core_instances.all(), key=lambda row: row.pk)]}
        value.update(type=target.kind, fail_closed=target.fail_closed)
        if getattr(target, 'protocol', None) is not None:
            value['protocol'] = target.protocol
        if include_egress_chain and (getattr(target, 'core_instance_id', None) is not None
                                     or getattr(target, 'next_egress_id', None) is not None):
            value['egress_chain'] = _egress_chain_identity(target)
        return value
    return {**base, 'enabled': target.enabled,
            'ingresses': [_identity('ingress', row) for row in _line_ingresses(target)],
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
    # 老回执没有世代字段时保持原JSON形状，使升级后的严格重放仍兼容。
    result = {key: value.get(key) for key in METRICS
              if key not in ('boot_id', 'sample_monotonic_seconds') or key in value}
    for key, item in result.items():
        if item is None:
            continue
        if key == 'online':
            valid = type(item) is bool
        elif key == 'boot_id':
            valid = type(item) is str and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', item)
        elif key == 'sample_monotonic_seconds':
            valid = _number(item, 9007199254740991)
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
    try:
        fingerprint = target_fingerprint(kind, target)
    except ObservationError:
        # 环路/超深链不能沿用旧成功，也不能拿截断链计算“当前版本”。
        return 'target_changed'
    if row.target_fingerprint != fingerprint:
        return 'target_changed'
    if row.observed_at > (now or timezone.now()) or row.expires_at <= (now or timezone.now()):
        return 'stale'
    return 'fresh'


def _network_rate(row, previous, state):
    """只有同世代、同范围的相邻有效样本可计算；累计永不写入个人账。"""
    empty = {'network_rx_bytes_per_second': None, 'network_tx_bytes_per_second': None,
             'network_window_seconds': None}
    if state != 'fresh':
        return empty, 'stale'
    if previous is None:
        return empty, 'unavailable'
    current, before = row.metrics, previous.metrics
    if current.get('online') is not True or before.get('online') is not True:
        return empty, 'unavailable'
    if row.source != previous.source or row.target_fingerprint != previous.target_fingerprint:
        return empty, 'scope_changed'
    if not current.get('boot_id') or not before.get('boot_id'):
        return empty, 'unavailable'
    if current['boot_id'] != before['boot_id']:
        return empty, 'generation_changed'
    if current.get('network_interfaces') != before.get('network_interfaces'):
        return empty, 'scope_changed'
    keys = ('network_rx_bytes', 'network_tx_bytes', 'sample_monotonic_seconds')
    if not current.get('network_interfaces') or any(current.get(k) is None or before.get(k) is None for k in keys):
        return empty, 'unavailable'
    window = current['sample_monotonic_seconds'] - before['sample_monotonic_seconds']
    wall_window = (row.observed_at - previous.observed_at).total_seconds()
    # 最多两个正常周期，且前样本在本次采集时仍有效；不跨断档补速率。
    if (not 0 < window <= 120 or wall_window <= 0 or abs(window - wall_window) > 5
            or previous.expires_at <= row.observed_at
            or (row.received_at and row.received_at > row.expires_at)
            or (previous.received_at and previous.received_at > previous.expires_at)):
        return empty, 'gap'
    if any(current[k] < before[k] for k in keys[:2]):
        return empty, 'counter_reset'
    return {'network_rx_bytes_per_second': (current['network_rx_bytes'] - before['network_rx_bytes']) / window,
            'network_tx_bytes_per_second': (current['network_tx_bytes'] - before['network_tx_bytes']) / window,
            'network_window_seconds': window}, 'valid'


def observation_projection(row, server, previous=_AUTO_PREVIOUS, *, now=None):
    if row is None:
        return None
    # 列表调用方预取两条并显式传 previous（包括 None），避免逐卡额外查询。
    if previous is _AUTO_PREVIOUS:
        previous = ServerObservation.objects.filter(server=server).filter(
            Q(observed_at__lt=row.observed_at) | Q(observed_at=row.observed_at, pk__lt=row.pk)
        ).order_by('-observed_at', '-pk').first()
    state = freshness(row, 'server', server, now)
    rates, rate_state = _network_rate(row, previous, state)
    # 世代与单调时钟仅留内部验算；HTTP只返回已确认的显示字段。
    public_metrics = {key: row.metrics.get(key) for key in METRICS
                      if key not in ('boot_id', 'sample_monotonic_seconds')}
    return {'source': row.source, 'observed_at': row.observed_at.isoformat(), 'expires_at': row.expires_at.isoformat(),
            'freshness': state, 'metrics': {**public_metrics, **rates}, 'rate_state': rate_state,
            'scope': '机器观测；CPU为采样区间均值，磁盘为根文件系统，网卡累计不是月用量或个人套餐流量；速率仅取相邻有效样本。'}


def check_projection(row):
    kind = next(key for key in TARGETS if getattr(row, key + '_id') is not None)
    target = getattr(row, kind)
    return {'id': str(row.pk), 'target_kind': kind, 'target_id': target_key(kind, target).split(':', 1)[1],
            'target_version': row.target_fingerprint,
            'source': row.source, 'check_kind': row.check_kind, 'result': row.result,
            'latency_ms': row.latency_ms, 'error_stage': row.error_stage, 'error_code': row.error_code,
            'observed_at': row.observed_at.isoformat(), 'expires_at': row.expires_at.isoformat(),
            'freshness': freshness(row, kind, target)}
