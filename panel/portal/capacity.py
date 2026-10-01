"""管理员资源账本与站内提醒；不购买流量、不发送外部通知。"""
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .billing import current_line_rate, decimal_multiplier
from .entitlements import _require_admin, add_months
from .models import (AuditEvent, CapacityAdjustment, CapacityAlert, CapacityPool,
                     CapacitySample, Entitlement, Line, LineCapacity)

SOURCES = ('api', 'manual', 'agent')
POOL_FIELDS = {'name', 'owner', 'server', 'provider', 'capacity_mode', 'planned_bytes', 'period_start',
    'period_end', 'original_unit', 'accounting_basis', 'source_priority', 'stale_after_seconds',
    'safety_bytes', 'warning_percent', 'critical_percent', 'urgent_percent', 'overage_policy', 'enabled'}


def _bytes(value, label, allow_none=False):
    if value is None and allow_none:
        return None
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValidationError(label + '须为非负整数字节数')
    return value


@transaction.atomic
def save_pool(actor, pool_id=None, **fields):
    _require_admin(actor)
    if set(fields) - POOL_FIELDS:
        raise ValidationError('包含不支持的容量字段')
    now = timezone.now()
    pool = CapacityPool.objects.select_for_update().get(pk=pool_id) if pool_id else CapacityPool(
        period_start=now, period_end=add_months(now, 1), source_priority=list(SOURCES))
    for key, value in fields.items():
        setattr(pool, key, value)
    if not pool.name.strip() or len(pool.name) > 100:
        raise ValidationError('资源名称应为1至100字符')
    if (timezone.is_naive(pool.period_start) or timezone.is_naive(pool.period_end)
            or pool.period_end <= pool.period_start):
        raise ValidationError('资源账期必须有时区且结束晚于开始')
    if (not isinstance(pool.source_priority, list) or len(pool.source_priority) != 3
            or set(pool.source_priority) != set(SOURCES)):
        raise ValidationError('来源顺序须包含API、人工、探针各一次')
    if (any(type(v) is not int for v in (pool.warning_percent, pool.critical_percent, pool.urgent_percent))
            or not 0 < pool.warning_percent < pool.critical_percent < pool.urgent_percent <= 100):
        raise ValidationError('提醒阈值应递增且处于1至100之间')
    if type(pool.stale_after_seconds) is not int or pool.stale_after_seconds < 60:
        raise ValidationError('数据陈旧时间至少60秒')
    _bytes(pool.planned_bytes, '规划容量', True)
    _bytes(pool.safety_bytes, '安全余量')
    pool.full_clean()
    pool.save()
    AuditEvent.objects.create(actor=actor, action='capacity_pool_saved', subject=str(pool.pk), result='SAVED')
    return pool


@transaction.atomic
def link_line(actor, line_id, pool_id, consumption_factor=1, topology_verified=False, note=''):
    _require_admin(actor)
    line = Line.objects.get(pk=line_id)
    pool = CapacityPool.objects.get(pk=pool_id)
    if type(topology_verified) is not bool or len(str(note)) > 240:
        raise ValidationError('资源拓扑字段无效')
    relation, _ = LineCapacity.objects.update_or_create(line=line, pool=pool, defaults={
        'consumption_factor': decimal_multiplier(consumption_factor), 'topology_verified': topology_verified,
        'note': str(note).strip()})
    AuditEvent.objects.create(actor=actor, action='capacity_line_linked', subject=str(relation.pk), result='SAVED')
    return relation


@transaction.atomic
def record_sample(actor, pool_id, source, source_key, used_bytes, observed_at,
                  reported_capacity_bytes=None, accounting_basis='', reason='',
                  period_start=None, period_end=None):
    """受认证采集任务或管理员调用；公开网页不能指定任意外部API地址。"""
    _require_admin(actor)
    pool = CapacityPool.objects.select_for_update().get(pk=pool_id)
    if source not in SOURCES or not isinstance(source_key, str) or not 1 <= len(source_key) <= 120:
        raise ValidationError('采样来源或幂等标识无效')
    if (timezone.is_naive(observed_at) or observed_at > timezone.now() + timedelta(seconds=30)
            or len(reason) > 240 or (source == 'manual' and not reason.strip())):
        raise ValidationError('请提供有效采样时间；人工校准必须写明原因')
    basis = accounting_basis or pool.accounting_basis
    if not basis or len(basis) > 120:
        raise ValidationError('需明确计量口径，例如供应商双向账单或整机接口观测')
    start, end = period_start or pool.period_start, period_end or pool.period_end
    if (timezone.is_naive(start) or timezone.is_naive(end) or end <= start
            or not start <= observed_at <= end):
        raise ValidationError('采样时间不在所声明的资源账期内')
    values = {'period_start': start, 'period_end': end, 'observed_at': observed_at,
        'used_bytes': _bytes(used_bytes, '已用量'),
        'reported_capacity_bytes': _bytes(reported_capacity_bytes, '供应商报告容量', True),
        'quality': {'api': 'provider_reported', 'agent': 'observed', 'manual': 'manual_calibration'}[source],
        'accounting_basis': basis, 'reason': reason.strip()}
    old = CapacitySample.objects.filter(pool=pool, source=source, source_key=source_key).first()
    if old:
        if any(getattr(old, key) != value for key, value in values.items()):
            raise ValidationError('同一采样标识不能覆盖不同数值')
        return old
    result = CapacitySample.objects.create(pool=pool, source=source, source_key=source_key, actor=actor, **values)
    AuditEvent.objects.create(actor=actor, action='capacity_sample', subject=str(pool.pk), result='RECORDED')
    return result


@transaction.atomic
def add_capacity(actor, pool_id, added_bytes, reason, request_key):
    """仅记录已购买/规划的追加容量，不调用供应商购买接口。"""
    _require_admin(actor)
    _bytes(added_bytes, '追加容量')
    if not added_bytes or not str(reason).strip() or len(reason) > 240 or not 1 <= len(request_key) <= 120:
        raise ValidationError('追加容量需为正数，并填写原因和请求标识')
    pool = CapacityPool.objects.select_for_update().get(pk=pool_id)
    old = pool.adjustments.filter(request_key=request_key).first()
    if old:
        if (old.added_bytes, old.reason) != (added_bytes, reason.strip()):
            raise ValidationError('同一追加标识不能更改额度')
        return old
    return CapacityAdjustment.objects.create(pool=pool, added_bytes=added_bytes, reason=reason.strip(),
        request_key=request_key, actor=actor, period_start=pool.period_start, period_end=pool.period_end)


def _commitment(pool, now):
    links = list(pool.line_links.select_related('line'))
    if not links or any(not link.topology_verified for link in links):
        return None, '资源拓扑尚未核验'
    ratios = {}
    for link in links:
        rate = current_line_rate(link.line_id, now)
        if rate is None:
            return None, '所关联线路倍率未知'
        scheduled = list(link.line.rate_versions.filter(effective_at__gt=now,
            effective_at__lt=pool.period_end).values_list('multiplier', flat=True))
        # 未来已排程的降倍率会增加物理流量承诺，不能只按此刻倍率低估。
        ratios[link.line_id] = link.consumption_factor / min([rate.multiplier] + scheduled)
    commitment = Decimal(0)
    records = Entitlement.objects.filter(enabled=True, user__is_active=True,
        expires_at__gt=now, lines__pk__in=ratios).distinct()
    for record in records:
        if record.applied_revision != record.revision or record.state not in {'active', 'simulated'}:
            return None, '存在尚未核验的服务分配'
        cycle = record.cycles.filter(starts_at__lte=now, ends_at__gt=now).first()
        if (cycle is None or record.metering_gap or record.usage_updated_at is None
                or not now - timedelta(minutes=3) <= record.usage_updated_at <= now + timedelta(seconds=30)):
            return None, '服务剩余额度计量不足'
        if cycle.ends_at < min(pool.period_end, record.expires_at):
            return None, '用户与资源账期不同步，需考虑后续重置，暂不计算精确承诺值'
        allowed = [ratios[line_id] for line_id in record.applied_snapshot.get('line_ids', []) if line_id in ratios]
        if allowed:
            remaining = max(0, record.applied_snapshot.get('quota_bytes', 0) - cycle.used_bytes)
            # 同一服务能选多个共享线路时，取最坏可行路径一次，不逐线路重复相加。
            commitment += Decimal(remaining) * max(allowed)
    return int(commitment.to_integral_value(rounding=ROUND_CEILING)), '按已核验路径估计，资源协议开销可能不同'


def pool_summary(pool, now=None):
    now = now or timezone.now()
    samples = pool.samples.filter(period_start=pool.period_start, period_end=pool.period_end)
    latest = {source: samples.filter(source=source).order_by('-observed_at', '-pk').first() for source in SOURCES}
    priority = pool.source_priority or list(SOURCES)
    selected = next((latest[source] for source in priority if latest.get(source)), None)
    added = pool.adjustments.filter(period_start=pool.period_start, period_end=pool.period_end).aggregate(
        value=Sum('added_bytes'))['value'] or 0
    planned = pool.planned_bytes + added if pool.planned_bytes is not None else None
    supplier = latest['api'].reported_capacity_bytes if latest['api'] else None
    supplier_comparable = latest['api'] is not None and latest['api'].accounting_basis == pool.accounting_basis
    capacity = None if pool.capacity_mode != 'limited' else (supplier if supplier is not None and supplier_comparable else planned)
    used = selected.used_bytes if selected else None
    basis_matches = selected is not None and selected.accounting_basis == pool.accounting_basis
    period_active = pool.period_start <= now < pool.period_end
    supplier_stale = bool(supplier is not None and supplier_comparable and latest['api'] and
        latest['api'].observed_at < now - timedelta(seconds=pool.stale_after_seconds))
    stale = (not period_active or selected is None or
        selected.observed_at < now - timedelta(seconds=pool.stale_after_seconds) or
        pool.capacity_mode == 'limited' and supplier_stale)
    remaining = max(0, capacity - used) if capacity is not None and used is not None and basis_matches else None
    ratio = Decimal(used) * 100 / capacity if capacity and used is not None and basis_matches else None
    if capacity == 0 and used is not None and basis_matches:
        ratio = Decimal(100)
    forecast, forecast_reason, window = None, '至少需要一小时跨度的同来源样本', None
    if selected and not stale and basis_matches:
        previous = samples.filter(source=selected.source, accounting_basis=selected.accounting_basis,
            observed_at__lte=selected.observed_at - timedelta(hours=1),
            observed_at__gte=selected.observed_at - timedelta(days=7)).order_by('-observed_at').first()
        if previous and selected.used_bytes >= previous.used_bytes:
            window = int((selected.observed_at - previous.observed_at).total_seconds())
            rate = Decimal(selected.used_bytes - previous.used_bytes) / window
            forecast = int(Decimal(selected.used_bytes) + rate * Decimal(max(0, (pool.period_end - selected.observed_at).total_seconds())))
            forecast_reason = '按同来源近期平均消耗估计，不保证供应商账单相同'
    elif stale:
        forecast_reason = '采样不足或陈旧，暂停趋势预测'
    elif not basis_matches:
        forecast_reason = '采样与资源计量口径不一致，不能比较容量或预测耗尽'
    differences = []
    if selected:
        for source, sample in latest.items():
            if (sample and source != selected.source and sample.accounting_basis == selected.accounting_basis
                    and abs((sample.observed_at - selected.observed_at).total_seconds()) <= 300
                    and abs(sample.used_bytes - selected.used_bytes) > max(1, selected.used_bytes // 10)):
                differences.append(source)
    commitment, commitment_reason = _commitment(pool, now)
    return {'pool': pool, 'capacity_bytes': capacity, 'capacity_mode': pool.capacity_mode,
        'planned_bytes': planned, 'added_bytes': added, 'supplier_capacity_bytes': supplier,
        'used_bytes': used, 'remaining_bytes': remaining, 'usage_percent': ratio,
        'source': selected.source if selected else None, 'quality': selected.quality if selected else 'unknown',
        'basis_matches': basis_matches, 'selected_accounting_basis': selected.accounting_basis if selected else None,
        'period_active': period_active, 'supplier_capacity_stale': supplier_stale,
        'observed_at': selected.observed_at if selected else None, 'stale': stale, 'sources': latest,
        'source_differences': differences, 'forecast_bytes': forecast, 'forecast_reason': forecast_reason,
        'forecast_window_seconds': window, 'commitment_bytes': commitment, 'commitment_reason': commitment_reason,
        'planning_available_bytes': max(0, remaining - pool.safety_bytes) if remaining is not None and not stale else None}


@transaction.atomic
def refresh_alerts(actor, pool_id=None):
    _require_admin(actor)
    pools = CapacityPool.objects.filter(enabled=True)
    if pool_id is not None:
        pools = pools.filter(pk=pool_id)
    for pool in pools:
        summary = pool_summary(pool)
        alerts = {}
        percent = summary['usage_percent']
        if percent is not None and percent >= pool.warning_percent:
            severity = 'urgent' if percent >= pool.urgent_percent else 'critical' if percent >= pool.critical_percent else 'warning'
            alerts['capacity_threshold'] = (severity, f'资源已使用约 {percent:.1f}%')
        if summary['stale']:
            alerts['source_stale'] = ('warning', '资源采样缺失或超过允许更新时间')
        if not summary['period_active']:
            alerts['period_inactive'] = ('warning', '资源账期尚未开始或已结束，请确认当前账期')
        if pool.capacity_mode == 'unknown' or pool.capacity_mode == 'limited' and summary['capacity_bytes'] is None:
            alerts['capacity_unknown'] = ('warning', '资源总容量未知，不能判断剩余容量')
        if summary['source_differences']:
            alerts['source_difference'] = ('warning', '相近时刻且相同口径的来源用量相差超过10%')
        if summary['source'] and not summary['basis_matches']:
            alerts['basis_mismatch'] = ('warning', '采样与资源口径不一致，剩余容量和耗尽预测暂停计算')
        if summary['forecast_bytes'] is not None and summary['capacity_bytes'] is not None and summary['forecast_bytes'] > summary['capacity_bytes']:
            alerts['forecast_exhaustion'] = ('critical', '按近期趋势估计，资源可能在本账期结束前耗尽')
        if summary['commitment_bytes'] is None:
            alerts['commitment_unknown'] = ('info', summary['commitment_reason'])
        elif summary['planning_available_bytes'] is not None and summary['commitment_bytes'] > summary['planning_available_bytes']:
            alerts['commitment_high'] = ('warning', '服务剩余额度对应的最坏路径消耗高于规划可用资源')
        pool.alerts.exclude(code__in=alerts).update(active=False)
        for code, (severity, message) in alerts.items():
            CapacityAlert.objects.update_or_create(pool=pool, code=code,
                defaults={'severity': severity, 'message': message, 'active': True})
    return CapacityAlert.objects.filter(active=True, pool__enabled=True)


def capacity_overview(actor):
    _require_admin(actor)
    return {'summaries': [pool_summary(pool) for pool in CapacityPool.objects.filter(enabled=True).order_by('pk')],
            'active_alerts': CapacityAlert.objects.filter(active=True, pool__enabled=True).select_related('pool')}
