"""资源用量纯状态转换；不导入Django、不读取文件、不联网、不修改原始累计。"""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
import re


SHANGHAI = timezone(timedelta(hours=8))
MAX_BYTES = 2**63-1
MAX_METERS = 16
HOME_TTL = 180
BWH_TTL = 7*3600
BOUNDARY_GRACE = 180
SAMPLE_KEYS = {'observed_at', 'expires_at', 'used_bytes', 'quota_bytes', 'upload_bytes', 'download_bytes',
               'estimate_start', 'collection_gaps', 'reset_at'}
STATE_KEYS = {'identity_fingerprint', 'plan_fingerprint', 'last_sample', 'cycle_index', 'baseline',
              'carry_upload_bytes', 'carry_download_bytes', 'has_interval', 'cycle_gap_codes',
              'accepted_historical_gap', 'provider_starts_at', 'last_alert_codes'}
GAP_CODES = {'collection_gap', 'counter_epoch_changed', 'cycle_skipped', 'late_baseline'}
MESSAGES = {
    'source_missing': ('info', '尚无可用统计来源。'),
    'source_stale': ('warning', '统计更新延迟，显示上次记录。'),
    'source_error': ('warning', '统计更新失败，保留上次有效记录。'),
    'invalid_sample': ('warning', '统计样本未通过校验。'),
    'future_sample': ('warning', '采样时间在未来，请检查来源时钟。'),
    'late_sample': ('warning', '迟到样本已隔离，保留较新的统计。'),
    'conflicting_sample': ('warning', '同一采样时刻出现不同计数，已保留原记录。'),
    'counter_regression': ('warning', '累计计数倒退，已保留原有效记录。'),
    'counter_epoch_changed': ('warning', '累计来源发生换代，本周期记录可能不完整。'),
    'collection_gap': ('warning', '采集器发现新的统计缺口。'),
    'coverage_gap': ('info', '本周期存在统计缺口，详情可查看覆盖范围。'),
    'cycle_waiting': ('info', '正在等待新周期的边界样本。'),
    'cycle_not_advanced': ('warning', '周期更新延迟，尚未取得新周期基线。'),
    'cycle_skipped': ('warning', '采样跨过多个周期，未将未知流量分摊到本期。'),
    'late_baseline': ('warning', '周期基线建立较晚，部分区间未覆盖。'),
    'clock_regression': ('warning', '处理时钟发生回退，暂停推进周期。'),
    'provider_cycle_waiting': ('info', '等待供应商返回新周期用量。'),
    'provider_cycle_not_advanced': ('warning', '供应商周期尚未更新，保留上一期记录。'),
    'provider_reset_reversed': ('warning', '供应商重置时间倒退，已保留原记录。'),
    'over_quota': ('warning', '记录的已用流量超过本期额度。'),
    'expiry_soon': ('info', '登记的到期日期临近。'),
    'expiry_passed': ('warning', '登记到期日期已过，请核对续费信息。'),
}


class ResourceUsageError(ValueError):
    """设置/持久状态错误只给固定码；调用者保留原文件与上一有效DTO。"""


def _need(condition, code):
    if not condition:
        raise ResourceUsageError(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def _bytes(value, nullable=False):
    if value is None and nullable:
        return None
    _need(type(value) is str and re.fullmatch(r'0|[1-9][0-9]{0,18}', value) is not None, 'invalid_bytes')
    number = int(value)
    _need(number <= MAX_BYTES, 'invalid_bytes')
    return number


def _time(value, nullable=False):
    if value is None and nullable:
        return None
    _need(type(value) is str and 20 <= len(value) <= 40, 'invalid_time')
    try:
        parsed = datetime.fromisoformat(value[:-1]+'+00:00' if value.endswith('Z') else value)
        _need(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'invalid_time')
        result = parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        raise ResourceUsageError('invalid_time') from None
    _need(946684800 <= result <= 4102444800, 'invalid_time')
    return result


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _date(value):
    if value is None:
        return None
    _need(type(value) is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is not None, 'invalid_plan')
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ResourceUsageError('invalid_plan') from None


def _plan(plan):
    _need(type(plan) is dict and set(plan) == {'id', 'label', 'source', 'scope', 'quota_bytes', 'cycle',
                                            'expires_on', 'accepted_initial_gap'}, 'invalid_plan')
    _need(type(plan['id']) is str and re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', plan['id']), 'invalid_plan')
    _need(type(plan['label']) is str and 0 < len(plan['label'].strip()) <= 80
          and not any(ord(c) < 32 for c in plan['label']), 'invalid_plan')
    _need(plan['source'] in ('residential', 'bwh') and plan['scope'] in ('external_node', 'server'), 'invalid_plan')
    _need(type(plan['accepted_initial_gap']) is bool, 'invalid_plan')
    _date(plan['expires_on'])
    _bytes(plan['quota_bytes'], nullable=True)
    cycle = plan['cycle']
    if plan['source'] == 'residential':
        _need(type(cycle) is dict and set(cycle) == {'kind', 'days', 'anchor'}
              and cycle['kind'] == 'fixed_days' and type(cycle['days']) is int and 1 <= cycle['days'] <= 366,
              'invalid_plan')
        _time(cycle['anchor'])
        _need(plan['quota_bytes'] is None or _bytes(plan['quota_bytes']) > 0, 'invalid_plan')
    else:
        _need(cycle == {'kind': 'provider'} and plan['quota_bytes'] is None
              and not plan['accepted_initial_gap'], 'invalid_plan')
    identity = {key: plan[key] for key in ('id', 'source', 'scope', 'cycle')}
    if plan['source'] == 'residential':
        identity['cycle'] = {**cycle, 'anchor': _iso(_time(cycle['anchor']))}
    return _digest(identity), _digest(plan)


def _new_state(identity, fingerprint):
    return {'identity_fingerprint': identity, 'plan_fingerprint': fingerprint, 'last_sample': None,
            'cycle_index': None, 'baseline': None, 'carry_upload_bytes': '0', 'carry_download_bytes': '0',
            'has_interval': False, 'cycle_gap_codes': [], 'accepted_historical_gap': False,
            'provider_starts_at': None, 'last_alert_codes': []}


def _validate_sample(sample):
    _need(type(sample) is dict and set(sample) == SAMPLE_KEYS, 'invalid_state')
    observed, expires = _time(sample['observed_at']), _time(sample['expires_at'])
    _need(expires > observed, 'invalid_state')
    for key in ('used_bytes', 'quota_bytes', 'upload_bytes', 'download_bytes'):
        _bytes(sample[key], nullable=key != 'used_bytes')
    for key in ('estimate_start', 'reset_at'):
        _time(sample[key], nullable=True)
    gaps = sample['collection_gaps']
    _need(gaps is None or type(gaps) is int and 0 <= gaps <= 2**31-1, 'invalid_state')
    if sample['estimate_start'] is not None:
        _need(sample['upload_bytes'] is not None and sample['download_bytes'] is not None
              and gaps is not None and sample['reset_at'] is None
              and _bytes(sample['upload_bytes'])+_bytes(sample['download_bytes']) == _bytes(sample['used_bytes'])
              and _time(sample['estimate_start']) <= observed and expires <= observed+HOME_TTL, 'invalid_state')
    else:
        _need(sample['upload_bytes'] is None and sample['download_bytes'] is None and gaps is None
              and sample['reset_at'] is not None and sample['quota_bytes'] is not None
              and _bytes(sample['quota_bytes']) > 0 and expires <= observed+BWH_TTL, 'invalid_state')


def _state(prior):
    if prior is None:
        return {'schema_version': 1, 'last_generated_at': None, 'meters': {}}
    _need(type(prior) is dict and set(prior) == {'schema_version', 'last_generated_at', 'meters'}
          and type(prior['schema_version']) is int and prior['schema_version'] == 1
          and type(prior['meters']) is dict and len(prior['meters']) <= MAX_METERS, 'invalid_state')
    _time(prior['last_generated_at'], nullable=True)
    for key, value in prior['meters'].items():
        _need(type(key) is str and re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', key)
              and type(value) is dict and set(value) == STATE_KEYS, 'invalid_state')
        for field in ('identity_fingerprint', 'plan_fingerprint'):
            _need(type(value[field]) is str and re.fullmatch(r'[0-9a-f]{64}', value[field]), 'invalid_state')
        if value['last_sample'] is not None:
            _validate_sample(value['last_sample'])
        index = value['cycle_index']
        _need(index is None or type(index) is int and 0 <= index <= 100000, 'invalid_state')
        baseline = value['baseline']
        if baseline is not None:
            _need(type(baseline) is dict and set(baseline) == {'upload_bytes', 'download_bytes', 'observed_at', 'estimate_start'}, 'invalid_state')
            _bytes(baseline['upload_bytes']); _bytes(baseline['download_bytes'])
            _time(baseline['observed_at']); _time(baseline['estimate_start'])
            sample = value['last_sample']
            _need(sample is not None and index is not None and sample['estimate_start'] == baseline['estimate_start']
                  and _time(baseline['observed_at']) <= _time(sample['observed_at'])
                  and _bytes(baseline['upload_bytes']) <= _bytes(sample['upload_bytes'])
                  and _bytes(baseline['download_bytes']) <= _bytes(sample['download_bytes']), 'invalid_state')
        else:
            _need(index is None and not value['has_interval'], 'invalid_state')
        if value['last_sample'] is not None and value['last_sample']['estimate_start'] is not None:
            _need(baseline is not None, 'invalid_state')
        for field in ('carry_upload_bytes', 'carry_download_bytes'):
            _bytes(value[field])
        _time(value['provider_starts_at'], nullable=True)
        _need(type(value['has_interval']) is bool and type(value['accepted_historical_gap']) is bool, 'invalid_state')
        for field, allowed in (('cycle_gap_codes', GAP_CODES), ('last_alert_codes', MESSAGES)):
            codes = value[field]
            _need(type(codes) is list and len(codes) <= len(allowed) and all(type(code) is str and code in allowed for code in codes)
                  and len(set(codes)) == len(codes), 'invalid_state')
    # JSON重建只复制已验证的固定结构，不复用/修改调用者对象。
    return json.loads(json.dumps(prior))


def _sample(source, home, now):
    if type(source) is not dict or source.get('quality') == 'missing':
        return None, 'missing', None
    kind = source.get('quality')
    if kind not in (('estimate', 'stale', 'error') if home else ('provider_reported', 'stale', 'error')):
        return None, 'error', 'invalid_sample'
    if source.get('updated_at') is None or source.get('used_bytes') is None:
        return None, 'error', 'source_error'
    try:
        observed = _time(source['updated_at'])
        if observed > now:
            return None, 'error', 'future_sample'
        expiry = _time(source['expires_at'])
        ttl = HOME_TTL if home else BWH_TTL
        _need(observed < expiry <= observed+ttl, 'invalid_sample')
        used = _bytes(source['used_bytes'])
        total = _bytes(source.get('total_bytes'), nullable=True)
        _need(total is None or total > 0, 'invalid_sample')
        value = {'observed_at': _iso(observed), 'expires_at': _iso(expiry), 'used_bytes': str(used),
                 'quota_bytes': str(total) if total is not None else None, 'upload_bytes': None,
                 'download_bytes': None, 'estimate_start': None, 'collection_gaps': None, 'reset_at': None}
        if home:
            _need(source.get('source') == 'estimate' and source.get('accounting_scope') == 'shared_residential_outbound', 'invalid_sample')
            upload, download = _bytes(source['upload_bytes']), _bytes(source['download_bytes'])
            start = _time(source['estimate_start'])
            gaps = source['collection_gaps']
            _need(upload+download == used and start <= observed and type(gaps) is int and 0 <= gaps <= 2**31-1, 'invalid_sample')
            value.update(upload_bytes=str(upload), download_bytes=str(download), estimate_start=_iso(start), collection_gaps=gaps)
        else:
            _need(source.get('source') == 'KiwiVM_getServiceInfo' and source.get('accounting_scope') == 'provider_vps_billing'
                  and total is not None, 'invalid_sample')
            value['reset_at'] = _iso(_time(source['reset_at']))
        quality = 'error' if kind == 'error' or source.get('last_query_ok') is False else 'stale' if expiry <= now or kind == 'stale' else 'current'
        return value, quality, None
    except (ResourceUsageError, KeyError, ValueError, TypeError, OverflowError):
        return None, 'error', 'invalid_sample'


def _cycle(plan, index):
    start = _time(plan['cycle']['anchor']) + index*plan['cycle']['days']*86400
    end = start + plan['cycle']['days']*86400
    return {'kind': 'fixed_days', 'starts_at': _iso(start), 'ends_at': _iso(end), 'next_reset_at': _iso(end)}


def _index(plan, stamp):
    return math.floor((stamp-_time(plan['cycle']['anchor']))/(plan['cycle']['days']*86400))


def _amounts(state):
    sample, baseline = state['last_sample'], state['baseline']
    if sample is None or baseline is None or not state['has_interval']:
        return None, None
    upload = _bytes(sample['upload_bytes'])-_bytes(baseline['upload_bytes'])+_bytes(state['carry_upload_bytes'])
    download = _bytes(sample['download_bytes'])-_bytes(baseline['download_bytes'])+_bytes(state['carry_download_bytes'])
    _need(0 <= upload <= MAX_BYTES and 0 <= download <= MAX_BYTES and upload+download <= MAX_BYTES, 'invalid_state')
    return upload, download


def _baseline(sample, zero=False):
    return {'upload_bytes': '0' if zero else sample['upload_bytes'], 'download_bytes': '0' if zero else sample['download_bytes'],
            'observed_at': sample['estimate_start'] if zero else sample['observed_at'], 'estimate_start': sample['estimate_start']}


def _home_accept(state, sample, plan, now, alerts):
    prior = state['last_sample']
    observed = _time(sample['observed_at'])
    index = _index(plan, observed)
    expected = _index(plan, now)
    if index < 0:
        alerts.append('invalid_sample'); return False
    old_index = state['cycle_index']
    changed_epoch = prior is not None and sample['estimate_start'] != prior['estimate_start']
    if prior is not None and not changed_epoch:
        if any(_bytes(sample[k]) < _bytes(prior[k]) for k in ('upload_bytes', 'download_bytes')) or sample['collection_gaps'] < prior['collection_gaps']:
            alerts.append('counter_regression'); return False
    if old_index is not None and index < old_index:
        alerts.append('late_sample'); return False
    if old_index is None:
        accepted = (index == 0 and plan['accepted_initial_gap'] and expected == 0
                    and _time(sample['estimate_start']) >= _time(plan['cycle']['anchor']))
        state.update(cycle_index=index, baseline=_baseline(sample, zero=accepted), has_interval=accepted,
                     accepted_historical_gap=accepted)
        if not accepted:
            state['cycle_gap_codes'] = ['late_baseline']; alerts.append('late_baseline')
    elif index > old_index:
        boundary = _time(_cycle(plan, index)['starts_at'])
        timely = (index == old_index+1 and index == expected and observed-boundary <= BOUNDARY_GRACE
                  and prior is not None and _time(prior['observed_at']) >= boundary-HOME_TTL and not changed_epoch)
        state.update(cycle_index=index, baseline=_baseline(sample), carry_upload_bytes='0', carry_download_bytes='0',
                     has_interval=timely, cycle_gap_codes=[])
        if not timely:
            code = 'cycle_skipped' if index > old_index+1 else 'late_baseline'
            state['cycle_gap_codes'].append(code); alerts.append(code)
    elif changed_epoch:
        upload, download = _amounts(state)
        state.update(baseline=_baseline(sample), carry_upload_bytes=str(upload or 0), carry_download_bytes=str(download or 0),
                     has_interval=upload is not None)
        state['cycle_gap_codes'] = sorted(set(state['cycle_gap_codes']) | {'counter_epoch_changed'})
        alerts.append('counter_epoch_changed')
    elif state['baseline'] is not None and observed > _time(state['baseline']['observed_at']):
        state['has_interval'] = True
    if prior is not None and not changed_epoch and sample['collection_gaps'] > prior['collection_gaps']:
        state['cycle_gap_codes'] = sorted(set(state['cycle_gap_codes']) | {'collection_gap'})
        alerts.append('collection_gap')
    elif prior is None and sample['collection_gaps'] > 0:
        # 接受早期漏记不等于接受之后采集器已明确发现的缺口。
        state['cycle_gap_codes'] = sorted(set(state['cycle_gap_codes']) | {'collection_gap'})
        alerts.append('collection_gap')
    state['last_sample'] = sample
    return True


def _provider_accept(state, sample, alerts):
    prior = state['last_sample']
    if prior is not None:
        old_reset, new_reset = _time(prior['reset_at']), _time(sample['reset_at'])
        if new_reset < old_reset:
            alerts.append('provider_reset_reversed'); return False
        if _bytes(sample['used_bytes']) < _bytes(prior['used_bytes']) and new_reset == old_reset:
            alerts.append('counter_regression'); return False
        if new_reset > old_reset:
            # P11只提供下一重置；中间可能跳过多期，不能将旧预测当本期已核起点。
            state['provider_starts_at'] = None
    state['last_sample'] = sample
    return True


def _meter(snapshot, plan, state, now, clock_back):
    home = plan['source'] == 'residential'
    sample, quality, problem = _sample(snapshot.get(plan['source']), home, now)
    alerts, replayed = [], False
    if quality != 'current':
        alerts.append({'missing': 'source_missing', 'stale': 'source_stale', 'error': 'source_error'}[quality])
    if problem:
        alerts.append(problem)
    prior = state['last_sample']
    if clock_back:
        sample = None; quality = 'error'; alerts.append('clock_regression')
    if sample is not None:
        if prior is not None:
            incoming, last = _time(sample['observed_at']), _time(prior['observed_at'])
            if incoming < last:
                sample = None; quality = 'error'; alerts.append('late_sample')
            elif incoming == last:
                if sample != prior:
                    sample = None; quality = 'error'; alerts.append('conflicting_sample')
                else:
                    replayed = True; sample = None
            elif quality == 'error':
                # 失败源不能用一次“更晚时间”推进周期；首次仍可保留结构有效旧记录。
                sample = None
        if sample is not None:
            accepted = _home_accept(state, sample, plan, now, alerts) if home else _provider_accept(state, sample, alerts)
            if not accepted:
                quality = 'error'
    saved = state['last_sample']
    if saved is not None and quality == 'current' and _time(saved['expires_at']) <= now:
        quality = 'stale'; alerts.append('source_stale')
    quota, used, remaining, upload, download = None, None, None, None, None
    expected = None
    if home:
        expected = _index(plan, now)
        index = state['cycle_index']
        active = index if index is not None else max(0, expected)
        cycle = _cycle(plan, active)
        quota = _bytes(plan['quota_bytes'], nullable=True)
        upload, download = _amounts(state)
        if upload is not None:
            used = upload+download
        if state['cycle_gap_codes']:
            if quality == 'current': quality = 'gap'
            if not any(code in alerts for code in GAP_CODES): alerts.append('coverage_gap')
        if index is not None and expected > index:
            boundary = _time(_cycle(plan, index)['ends_at'])
            alerts.append('cycle_not_advanced' if now-boundary > BOUNDARY_GRACE else 'cycle_waiting')
            if quality in ('current', 'gap'): quality = 'stale'
        if expected < 0:
            quality = 'missing'; alerts.append('invalid_sample')
        if used is not None and quota is not None and not state['cycle_gap_codes']:
            remaining = max(0, quota-used)
    else:
        cycle = {'kind': 'provider', 'starts_at': state['provider_starts_at'],
                 'ends_at': saved['reset_at'] if saved else None, 'next_reset_at': saved['reset_at'] if saved else None}
        if saved is not None:
            used, quota = _bytes(saved['used_bytes']), _bytes(saved['quota_bytes'])
            remaining = max(0, quota-used)
            reset = _time(saved['reset_at'])
            if now >= reset:
                alerts.append('provider_cycle_not_advanced' if now-reset > BWH_TTL else 'provider_cycle_waiting')
                if quality == 'current': quality = 'stale'
    if used is not None and quota is not None and used > quota:
        alerts.append('over_quota')
    expires = _date(plan['expires_on'])
    if expires is not None:
        days = (expires-datetime.fromtimestamp(now, SHANGHAI).date()).days
        if days < 0: alerts.append('expiry_passed')
        elif days <= 7: alerts.append('expiry_soon')
    alerts = list(dict.fromkeys(alerts))
    state['last_alert_codes'] = alerts
    baseline = state['baseline']
    details = {'accounting_basis': 'shared_outbound_estimate' if home else 'provider_reported_no_extra_multiplier',
               'plan_fingerprint': state['plan_fingerprint'], 'accepted_historical_gap': state['accepted_historical_gap'],
               'coverage_start': saved['estimate_start'] if saved and home else None,
               'baseline_at': baseline['observed_at'] if baseline else None,
               'collection_gaps': saved['collection_gaps'] if saved and home else None,
               'cycle_gap_codes': list(state['cycle_gap_codes']), 'sample_replayed': replayed,
               'expected_cycle_index': expected, 'active_cycle_index': state['cycle_index'],
               'lifetime_used_bytes': saved['used_bytes'] if saved and home else None,
               'hard_limit_enforced': False}
    return {'id': plan['id'], 'label': plan['label'], 'scope': plan['scope'], 'source_kind': 'estimate' if home else 'official',
            'quality': quality, 'quota_bytes': str(quota) if quota is not None else None,
            'used_bytes': str(used) if used is not None else None, 'remaining_bytes': str(remaining) if remaining is not None else None,
            'upload_bytes': str(upload) if upload is not None else None, 'download_bytes': str(download) if download is not None else None,
            'observed_at': saved['observed_at'] if saved else None, 'expires_at': saved['expires_at'] if saved else None,
            'cycle': cycle, 'expires_on': plan['expires_on'],
            'alerts': [{'code': code, 'severity': MESSAGES[code][0], 'message': MESSAGES[code][1]} for code in alerts],
            'details': details}


def project_resource_usage(snapshot, plans, prior_state=None, now=None):
    """纯函数返回新持久状态和schema2 DTO；调用者负责原子保存，GET不得调用写状态。"""
    _need(type(now) in (int, float) and 946684800 <= now <= 4102444800 and math.isfinite(now), 'invalid_clock')
    _need(type(snapshot) is dict and type(plans) is list and 0 < len(plans) <= MAX_METERS, 'invalid_input')
    state = _state(prior_state)
    old_time = _time(state['last_generated_at'], nullable=True)
    clock_back = old_time is not None and now < old_time
    identities = [_plan(plan) for plan in plans]
    _need(len({plan['id'] for plan in plans}) == len(plans), 'duplicate_meter')
    _need(len(set(state['meters']) | {plan['id'] for plan in plans}) <= MAX_METERS, 'too_many_meters')
    meters = []
    for plan, (identity, fingerprint) in zip(plans, identities):
        item = state['meters'].get(plan['id'], _new_state(identity, fingerprint))
        _need(item['identity_fingerprint'] == identity, 'plan_identity_changed')
        item['plan_fingerprint'] = fingerprint
        meters.append(_meter(snapshot, plan, item, now, clock_back))
        state['meters'][plan['id']] = item
    state['last_generated_at'] = _iso(max(now, old_time) if old_time is not None else now)
    return state, {'schema_version': 2, 'generated_at': _iso(now), 'meters': meters}
