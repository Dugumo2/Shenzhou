"""通用权益、自然月账期及入口身份计量；不连接生产核心。"""
import calendar
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (BillingCycle, DeviceSubscription, Entitlement, Line, NodeIdentity,
                     UsageLedger, UsageStream)

SHANGHAI = ZoneInfo('Asia/Shanghai')


def add_months(value, months, anchor_day=None):
    """自然月计算保留原始锚点，31 日在二月落到月末，三月仍可回到 31 日。"""
    if timezone.is_naive(value):
        raise ValidationError('时间必须包含时区')
    local = value.astimezone(SHANGHAI)
    year, month0 = divmod(local.year * 12 + local.month - 1 + months, 12)
    return local.replace(year=year, month=month0 + 1,
        day=min(anchor_day or local.day, calendar.monthrange(year, month0 + 1)[1]))


def next_reset(value, reset_day, reset_hour=0, reset_minute=0):
    local = value.astimezone(SHANGHAI)
    candidate = local.replace(day=min(reset_day, calendar.monthrange(local.year, local.month)[1]),
                              hour=reset_hour, minute=reset_minute, second=0, microsecond=0)
    if candidate <= local:
        candidate = add_months(candidate, 1, reset_day)
    return candidate


def current_cycle(entitlement, at=None):
    """调用者持有权益行锁；没有真实开通时间就不虚构账期。"""
    at = at or timezone.now()
    if not entitlement.activated_at or at < entitlement.activated_at:
        return None
    cycle = entitlement.cycles.filter(starts_at__lte=at, ends_at__gt=at).first()
    if cycle:
        return cycle
    start = entitlement.activated_at
    last = entitlement.cycles.filter(ends_at__lte=at).order_by('-ends_at').first()
    if last:
        start = last.ends_at
    anchor = entitlement.activated_at.astimezone(SHANGHAI)
    applied = entitlement.applied_snapshot if entitlement.applied_revision else {}
    configured_day = applied.get('reset_day', entitlement.reset_day)
    configured_hour = applied.get('reset_hour', entitlement.reset_hour)
    configured_minute = applied.get('reset_minute', entitlement.reset_minute)
    day = configured_day or anchor.day
    hour = configured_hour if configured_hour is not None else anchor.hour
    minute = configured_minute if configured_minute is not None else anchor.minute
    end = next_reset(start, day, hour, minute)
    while end <= at:
        start, end = end, next_reset(end, day, hour, minute)
    return BillingCycle.objects.get_or_create(entitlement=entitlement, starts_at=start,
                                              defaults={'ends_at': end, 'raw_bytes': 0})[0]


def _require_admin(actor):
    if not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        raise PermissionDenied('仅管理员可以分配套餐')


def validated_lines(line_ids):
    ids = list(dict.fromkeys(int(value) for value in line_ids))
    lines = list(Line.objects.select_related('ingress__server', 'egress__server').filter(pk__in=ids))
    if not ids or len(lines) != len(ids):
        raise ValidationError('请选择存在的线路')
    for line in lines:
        ingresses = line.all_ingresses()
        if (not line.enabled or not line.egress.server.enabled or
                any(not ingress.enabled or not ingress.server.enabled for ingress in ingresses)):
            raise ValidationError('所选线路不可用')
        if any(ingress.server_id != line.egress.server_id for ingress in ingresses):
            raise ValidationError('本阶段不支持跨服务器线路执行')
        if line.egress.kind == 'upstream' and not line.egress.fail_closed:
            raise ValidationError('上游线路必须失败关闭')
    return lines


@transaction.atomic
def assign_entitlement(actor, user, line_ids, quota_bytes, reset_day=None, service_months=1,
                       expires_at=None, enabled=True, idempotency_key=None, reset_hour=None,
                       reset_minute=None, service_name=None, renew=False):
    from .jobs import get_existing_job, enqueue, fingerprint
    _require_admin(actor)
    if type(quota_bytes) is not int or not 0 < quota_bytes <= 2**63 - 1:
        raise ValidationError('月额度必须是正整数字节数')
    if reset_day is not None and (type(reset_day) is not int or not 1 <= reset_day <= 31):
        raise ValidationError('重置日应为 1 至 31')
    if ((reset_hour is None) != (reset_minute is None) or
            reset_hour is not None and (type(reset_hour) is not int or not 0 <= reset_hour <= 23 or
            type(reset_minute) is not int or not 0 <= reset_minute <= 59)):
        raise ValidationError('重置时间需完整填写小时和分钟')
    if service_name is not None and (not isinstance(service_name, str) or len(service_name.strip()) > 100):
        raise ValidationError('服务名称最多100字符')
    if renew and expires_at is not None:
        raise ValidationError('续期时长与指定到期日期不能同时填写')
    if type(service_months) is not int or not 1 <= service_months <= 120:
        raise ValidationError('有效月数应为 1 至 120')
    if expires_at is not None and (timezone.is_naive(expires_at) or expires_at <= timezone.now()):
        raise ValidationError('到期日期必须包含时区且在未来')
    lines = validated_lines(line_ids)
    values = dict(user=user.pk, lines=sorted(x.pk for x in lines), quota=quota_bytes,
                  day=reset_day, months=service_months, expires=expires_at.isoformat() if expires_at else None,
                  enabled=bool(enabled), hour=reset_hour, minute=reset_minute, name=service_name, renew=bool(renew))
    digest = fingerprint(values)
    old = get_existing_job(actor, 'assign', idempotency_key, digest)
    if old:
        return old.entitlement, old
    record = Entitlement.objects.select_for_update().filter(user=user).first()
    if record is None:
        if renew:
            raise ValidationError('服务尚未开通，不能续期')
        record = Entitlement(user=user)
    else:
        # 已开始账期不重算边界，新的锚点在下个账期生效；额度不清零。
        record.revision += 1
    if renew:
        if not record.activated_at or not record.expires_at:
            raise ValidationError('服务尚未实际开通，不能续期')
        expires_at = add_months(max(timezone.now(), record.expires_at), service_months)
    elif expires_at is None and record.activated_at:
        # 普通编辑不延长或缩短已开通服务；显式续期才按月顺延。
        expires_at = record.expires_at
    record.quota_bytes, record.reset_day, record.service_months = quota_bytes, reset_day, service_months
    record.reset_hour, record.reset_minute = reset_hour, reset_minute
    if service_name is not None:
        record.service_name = service_name.strip()
    record.requested_expires_at, record.enabled, record.state = expires_at, bool(enabled), 'pending'
    record.save()
    record.lines.set(lines)
    # 权益缩减不能仅隐藏订阅列表，撤销记录在任务箱提交时就持久化。
    identities = NodeIdentity.objects.filter(subscription__entitlement=record, revoked_at__isnull=True)
    # 套餐开关仅暂挂；只有删除线路才永久撤销该线路的身份。
    identities = identities.exclude(line__in=lines)
    revoke_ids = list(identities.values_list('pk', flat=True))
    identities.update(revoked_at=timezone.now(), state='revocation_pending')
    suspend_ids = []
    if not enabled:
        suspended = NodeIdentity.objects.filter(subscription__entitlement=record,
            revoked_at__isnull=True).exclude(state='candidate')
        suspend_ids = list(suspended.values_list('pk', flat=True))
        suspended.update(state='suspension_pending')
    DeviceSubscription.objects.filter(entitlement=record).exclude(state='disabled').update(state='pending')
    job = enqueue(actor, record, 'assign', idempotency_key, digest,
                  payload={'lines': values['lines'], 'revoke_ids': revoke_ids, 'suspend_ids': suspend_ids})
    return record, job


def summary_for(user, actor=None):
    """只查询指定已认证用户的权益；调用端不得传来自 URL 的其他用户。"""
    if actor is not None:
        _require_admin(actor)
    if not user.is_authenticated or (not user.is_active and actor is None):
        raise PermissionDenied('请先登录')
    record = Entitlement.objects.filter(user=user).first()
    if record is None:
        return None
    now = timezone.now()
    cycle = record.cycles.filter(starts_at__lte=now, ends_at__gt=now).first()
    measured = (not record.metering_gap and record.usage_updated_at is not None and
                now - timedelta(minutes=3) <= record.usage_updated_at <= now + timedelta(seconds=30))
    used = cycle.used_bytes if cycle and record.usage_updated_at else None
    applied = dict(record.applied_snapshot)
    effective_quota = applied.get('quota_bytes')
    return {'entitlement': record, 'state': record.state,
            'lines': list(Line.objects.filter(pk__in=applied.get('line_ids', []))),
            'quota_bytes': effective_quota,
            'quota_gb': Decimal(effective_quota) / Decimal(1_000_000_000) if effective_quota is not None else None,
            'service_name': record.service_name or ' / '.join(record.lines.values_list('name', flat=True)) or '我的服务',
            'desired': {'quota_bytes': record.quota_bytes, 'lines': list(record.lines.all()),
                        'reset_day': record.reset_day, 'reset_hour': record.reset_hour, 'reset_minute': record.reset_minute,
                        'service_months': record.service_months,
                        'expires_at': record.requested_expires_at, 'enabled': record.enabled},
            'applied_snapshot': applied,
            'used_bytes': used, 'raw_bytes': cycle.raw_bytes if cycle and record.usage_updated_at else None,
            'remaining_bytes': max(0, effective_quota - used) if used is not None and effective_quota is not None else None,
            'usage_fresh': measured, 'usage_updated_at': record.usage_updated_at,
            'metering_gap': record.metering_gap, 'suspension_reason': record.suspension_reason,
            'expires_at': record.expires_at, 'resets_at': cycle.ends_at if cycle else None,
            'applied': record.state == 'active' and record.applied_revision == record.revision}


class MeteringGap(ValidationError):
    """缺少账期边界，必须由采集器补齐后才能继续计费。"""


def record_usage(identity_id, server_id, epoch, sequence, upload_bytes, download_bytes, observed_at):
    try:
        return _record_usage_atomic(identity_id, server_id, epoch, sequence, upload_bytes, download_bytes, observed_at)
    except MeteringGap:
        from .jobs import enqueue_enforcement
        with transaction.atomic():
            identity = NodeIdentity.objects.get(pk=identity_id)
            record = Entitlement.objects.select_for_update().get(pk=identity.subscription.entitlement_id)
            record.metering_gap = True
            record.save(update_fields=['metering_gap'])
            enqueue_enforcement(record, 'metering_gap')
        raise


@transaction.atomic
def _record_usage_atomic(identity_id, server_id, epoch, sequence, upload_bytes, download_bytes, observed_at):
    """内部采集入口。仅入口累计值计费，epoch 必须由持久采集器提供，不能随请求变更。"""
    from .jobs import enqueue_enforcement
    if (not isinstance(epoch, str) or not 1 <= len(epoch) <= 80 or
            any(type(v) is not int or v < 0 or v > 2**63 - 1 for v in (sequence, upload_bytes, download_bytes))):
        raise ValidationError('无效累计样本')
    if timezone.is_naive(observed_at) or observed_at > timezone.now() + timedelta(seconds=30):
        raise ValidationError('样本时间无效')
    identity = NodeIdentity.objects.select_related('subscription__entitlement', 'ingress').get(pk=identity_id)
    if identity.ingress.server_id != server_id:
        raise PermissionDenied('采集服务器与入口身份不匹配')
    if identity.state == 'candidate':
        raise ValidationError('未开通身份不能写入计量')
    record = Entitlement.objects.select_for_update().get(pk=identity.subscription.entitlement_id)
    cycle = current_cycle(record, observed_at)
    if cycle is None:
        raise ValidationError('开通前不能计费')
    stream = UsageStream.objects.filter(identity=identity, epoch=epoch).first()
    if stream and (stream.retired_at or sequence <= stream.sequence):
        return 0
    previous_epoch_sample = None
    if stream is None:
        latest = UsageLedger.objects.filter(stream__identity=identity).order_by('-observed_at').first()
        previous_epoch_sample = latest
        if latest and observed_at <= latest.observed_at:
            raise ValidationError('拒绝晚到的旧计数代际')
        if (latest and latest.cycle_id != cycle.pk and latest.observed_at < cycle.starts_at
                and upload_bytes + download_bytes):
            # 更换核心计数代际不能代替旧代际的账期末检查点。
            raise MeteringGap('跨账期重启仍需要旧代际边界样本')
        UsageStream.objects.filter(identity=identity, retired_at__isnull=True).update(retired_at=observed_at)
        stream = UsageStream.objects.create(identity=identity, epoch=epoch)
    latest = UsageLedger.objects.filter(stream=stream).order_by('-sequence').first()
    if latest and observed_at < latest.observed_at:
        raise ValidationError('样本时间不能倒退')
    if upload_bytes < stream.upload_bytes or download_bytes < stream.download_bytes:
        raise ValidationError('累计值回退必须提供新的计数代际')
    up, down = upload_bytes - stream.upload_bytes, download_bytes - stream.download_bytes
    if latest and latest.cycle_id != cycle.pk and observed_at == cycle.starts_at:
        # 边界瞬间累计值作为上一周期的末样本；下一样本再从此累计值算新周期。
        cycle = latest.cycle
    if latest and latest.cycle_id != cycle.pk and latest.observed_at < cycle.starts_at and up + down:
        # 两次采样跨月且没有边界累计值时，不能把上一账期的流量错误扣到新月。
        raise MeteringGap('跨账期需要边界累计样本，不能猜测流量归属')
    from .billing import price_usage
    version, charged, remainder = price_usage(identity, cycle, latest or previous_epoch_sample,
                                             observed_at, up + down, record.activated_at)
    UsageLedger.objects.create(cycle=cycle, stream=stream, sequence=sequence,
        upload_delta=up, download_delta=down, observed_at=observed_at, rate_version=version,
        weighted_bytes=charged, quality='metered')
    cycle.used_bytes += charged
    cycle.weighted_remainder = remainder
    if cycle.raw_bytes is not None:
        cycle.raw_bytes += up + down
    cycle.save(update_fields=['used_bytes', 'raw_bytes', 'weighted_remainder'])
    stream.sequence, stream.upload_bytes, stream.download_bytes = sequence, upload_bytes, download_bytes
    stream.save(update_fields=['sequence', 'upload_bytes', 'download_bytes'])
    record.usage_updated_at = max(record.usage_updated_at or observed_at, observed_at)
    record.save(update_fields=['usage_updated_at'])
    effective_quota = record.applied_snapshot.get('quota_bytes', record.quota_bytes)
    if cycle.ends_at > timezone.now() and cycle.used_bytes >= effective_quota:
        enqueue_enforcement(record, 'quota', cycle.pk)
    return up + down


@transaction.atomic
def acknowledge_metering_recovery(actor, entitlement_id):
    """补齐所有入口边界样本后人工确认；不能靠清掉提示绕过漏计量。"""
    _require_admin(actor)
    record = Entitlement.objects.select_for_update().get(pk=entitlement_id)
    cycle = current_cycle(record)
    if cycle is None:
        raise ValidationError('尚无开通账期')
    identities = NodeIdentity.objects.filter(subscription__entitlement=record, revoked_at__isnull=True).exclude(state='candidate')
    if not identities.exists():
        raise ValidationError('没有可核验的入口样本，不能清除计量缺口')
    for identity in identities:
        stream = identity.streams.filter(retired_at__isnull=True).first()
        if stream is None:
            raise ValidationError('仍有入口没有可靠计量样本')
        latest = UsageLedger.objects.filter(stream=stream).order_by('-sequence').first()
        if not latest or latest.observed_at < cycle.starts_at:
            raise ValidationError('仍有入口缺少账期边界或新周期样本')
    record.metering_gap = False
    record.save(update_fields=['metering_gap'])
