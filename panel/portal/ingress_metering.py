"""内部累计采样账：只接受已归属入口，不启动采集器或调用核心。"""
import hashlib
import json
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.core.exceptions import PermissionDenied, ValidationError
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .billing import RATE_SCALE, decimal_multiplier
from .models import (AuditEvent, BillingCycle, CoreInstance, Entitlement, IngressUsageSample,
                     Line, NodeIdentity, ServiceRateVersion, UsageLedger, UsageStream)

MAX_BYTES = 2**63 - 1


def _aware(value):
    return isinstance(value, datetime) and timezone.is_aware(value)


def _grant(record, line_id, ingress_id):
    line = Line.objects.get(pk=line_id)
    if (not record.lines.filter(pk=line_id).exists()
            or (line.ingress_id != ingress_id and not line.additional_ingresses.filter(pk=ingress_id).exists())):
        raise PermissionDenied('线路入口不属于该服务授权')
    return line


@transaction.atomic
def set_service_rate(actor, entitlement_id, line_id, ingress_id, multiplier, effective_at, reason):
    """追加授权倍率；不回溯已收到的区间，也不改写历史流水。"""
    if not actor.is_authenticated:
        raise PermissionDenied('仅当前有效管理员可以设置授权倍率')
    actor = get_user_model().objects.select_for_update().filter(pk=actor.pk, is_active=True, is_staff=True).first()
    if actor is None:
        raise PermissionDenied('仅当前有效管理员可以设置授权倍率')
    multiplier = decimal_multiplier(multiplier)
    if not _aware(effective_at) or not isinstance(reason, str) or not reason.strip() or len(reason) > 240:
        raise ValidationError('请填写有效的生效时间与修改原因')
    reason = reason.strip()
    record = Entitlement.objects.select_for_update().get(pk=entitlement_id)
    _grant(record, line_id, ingress_id)
    scope = dict(entitlement=record, line_id=line_id, ingress_id=ingress_id)
    old = ServiceRateVersion.objects.filter(**scope, effective_at=effective_at).first()
    if old:
        if old.multiplier == multiplier and old.reason == reason and old.actor_id == actor.pk:
            return old
        raise ValidationError('该授权生效时刻已有倍率版本，不能覆盖')
    latest = IngressUsageSample.objects.filter(entitlement=record, line_id=line_id,
        ingress_id=ingress_id).order_by('-observed_at').first()
    legacy = UsageLedger.objects.filter(cycle__entitlement=record, stream__identity__line_id=line_id,
        stream__identity__ingress_id=ingress_id).order_by('-observed_at').first()
    if any(row and effective_at < row.observed_at for row in (latest, legacy)):
        raise ValidationError('倍率不能回溯修改已经采样的区间')
    result = ServiceRateVersion.objects.create(**scope, multiplier=multiplier,
        effective_at=effective_at, actor=actor, reason=reason)
    AuditEvent.objects.create(actor=actor, action='service_rate_set', subject=str(record.public_id), result='SAVED')
    return result


@transaction.atomic
def record_ingress_sample(*, identity_id, server_id, core_instance_id, epoch, sequence,
                          upload_bytes, download_bytes, observed_at, coverage_verified,
                          counter_kind, accounting_layer='ingress'):
    """coverage_verified 由受信调用方核实前锚到本次的完整性，不能从累计值自动推定。"""
    now = timezone.now()
    if accounting_layer != 'ingress':
        raise PermissionDenied('转发及资源层样本不得扣减套餐')
    if counter_kind != 'cumulative' or type(coverage_verified) is not bool:
        raise ValidationError('必须明确累计计数类型与覆盖证据')
    if (not isinstance(epoch, str) or not 1 <= len(epoch) <= 80 or not epoch.strip()
            or any(type(value) is not int or value < 0 or value > MAX_BYTES
                   for value in (sequence, upload_bytes, download_bytes))):
        raise ValidationError('无效累计样本')
    if not _aware(observed_at) or observed_at > now + timedelta(seconds=30):
        raise ValidationError('样本时间无效')
    initial = NodeIdentity.objects.select_related('subscription').get(pk=identity_id)
    record = Entitlement.objects.select_for_update().get(pk=initial.subscription.entitlement_id)
    # 同权益锁下重新读取归属；与旧路径共用锁，避免切换时重复扣账。
    identity = NodeIdentity.objects.select_related('subscription', 'ingress').get(pk=identity_id)
    if identity.subscription.entitlement_id != record.pk:
        raise PermissionDenied('身份所属服务发生变化')
    if identity.ingress.server_id != server_id:
        raise PermissionDenied('采集服务器与认证入口不匹配')
    core = CoreInstance.objects.get(pk=core_instance_id)
    if core.server_id != server_id or not core.ingresses.filter(pk=identity.ingress_id).exists():
        raise PermissionDenied('核心实例没有登记该认证入口')
    _grant(record, identity.line_id, identity.ingress_id)
    if (IngressUsageSample.objects.filter(identity=identity).exclude(entitlement=record).exists()
            or UsageLedger.objects.filter(stream__identity=identity).exclude(cycle__entitlement=record).exists()):
        raise PermissionDenied('身份历史所属服务冲突，必须先核实归属')
    anchors = dict(server_id=server_id, line_id=identity.line_id, ingress_id=identity.ingress_id,
                   subscription_id=identity.subscription_id, identity_generation=identity.generation)
    if IngressUsageSample.objects.filter(identity=identity).exclude(**anchors).exists():
        # 新 epoch 只证明计数器换代，不能授权复用已经改变归属的身份。
        raise PermissionDenied('身份计量归属发生变化，必须隔离并重新核实身份')
    if (identity.state not in ('active', 'simulated') or identity.revoked_at is not None
            or identity.subscription.state not in ('active', 'simulated')
            or not identity.subscription.lines.filter(pk=identity.line_id).exists()
            or not record.activated_at or observed_at < record.activated_at
            or not record.applied_revision):
        raise ValidationError('未核实生效的身份和服务不能计量')
    body = [identity.pk, record.pk, anchors, core.pk, epoch, sequence, upload_bytes, download_bytes,
            observed_at.astimezone(datetime_timezone.utc).isoformat(), coverage_verified,
            counter_kind, accounting_layer]
    fingerprint = hashlib.sha256(json.dumps(body, separators=(',', ':')).encode()).hexdigest()
    existing = IngressUsageSample.objects.filter(identity=identity, epoch=epoch, sequence=sequence).first()
    if existing:
        if existing.fingerprint != fingerprint:
            raise ValidationError('同一序列内容冲突，拒绝覆盖')
        return existing
    previous = IngressUsageSample.objects.filter(identity=identity).exclude(status='late').order_by('-pk').first()
    stream = UsageStream.objects.filter(identity=identity, epoch=epoch).first()
    previous_epoch = IngressUsageSample.objects.filter(identity=identity, epoch=epoch).first()
    if previous_epoch and previous_epoch.core_instance_id != core.pk:
        raise ValidationError('核心实例变化必须建立新的计数代际')
    sample = IngressUsageSample(identity=identity, entitlement=record, core_instance=core,
        **anchors,
        epoch=epoch, sequence=sequence, upload_bytes=upload_bytes, download_bytes=download_bytes,
        observed_at=observed_at, fingerprint=fingerprint, coverage_verified=coverage_verified,
        interval_start=previous.observed_at if previous else None)

    def save_status(status, reason='', *, advance=False, retire=False):
        sample.status, sample.reason_code = status, reason
        sample.save()
        if status == 'gap' or (status == 'baseline' and (
                previous is not None or not record.cycles.filter(starts_at=observed_at).exists())):
            record.metering_gap = True
            record.save(update_fields=['metering_gap'])
        if advance:
            stream.sequence, stream.upload_bytes, stream.download_bytes = sequence, upload_bytes, download_bytes
            if retire:
                stream.retired_at = observed_at
            stream.save(update_fields=['sequence', 'upload_bytes', 'download_bytes', 'retired_at'])
        return sample

    if (stream and stream.retired_at) or (previous and observed_at <= previous.observed_at):
        return save_status('late', 'retired_epoch' if stream and stream.retired_at else 'time_reversed')
    if previous and previous.epoch == epoch and sequence <= previous.sequence:
        return save_status('late', 'sequence_reversed')
    if previous is None or previous.epoch != epoch:
        legacy = UsageLedger.objects.filter(stream__identity=identity).order_by('-observed_at').first()
        if legacy and (observed_at < legacy.observed_at or (stream and sequence <= stream.sequence)):
            return save_status('late', 'before_legacy_watermark')
        UsageStream.objects.filter(identity=identity, retired_at__isnull=True).exclude(epoch=epoch).update(retired_at=observed_at)
        if stream is None:
            stream = UsageStream.objects.create(identity=identity, epoch=epoch)
        # 首样本永远只建立锚点；迁入前累计值及重启前未采到的尾量不追扣。
        return save_status('baseline', 'initial_baseline' if previous is None else 'epoch_changed', advance=True)
    if upload_bytes < previous.upload_bytes or download_bytes < previous.download_bytes:
        return save_status('gap', 'counter_reversed', advance=True, retire=True)
    if not coverage_verified:
        return save_status('gap', 'coverage_unverified', advance=True)
    raw = upload_bytes - previous.upload_bytes + download_bytes - previous.download_bytes
    # 以实际区间核对已确定账期；端点属于刚结束的旧期，迟到不改变发生归属。
    # 检查任何重叠账期，既不猜分跨期流量，也不调用自动推进/创建账期的方法。
    cycles = list(BillingCycle.objects.select_for_update().filter(entitlement=record,
        starts_at__lt=observed_at, ends_at__gt=previous.observed_at).order_by('pk')[:2])
    if (len(cycles) != 1 or cycles[0].starts_at > previous.observed_at
            or cycles[0].ends_at < observed_at):
        return save_status('gap', 'cycle_boundary', advance=True)
    cycle = cycles[0]
    if not record.expires_at or observed_at > record.expires_at:
        return save_status('gap', 'outside_service_period', advance=True)
    rates = ServiceRateVersion.objects.filter(entitlement=record, line_id=identity.line_id, ingress_id=identity.ingress_id)
    version = rates.filter(effective_at__lte=previous.observed_at).order_by('-effective_at', '-pk').first()
    if version is None:
        return save_status('gap', 'rate_missing', advance=True)
    if rates.filter(effective_at__gt=previous.observed_at, effective_at__lt=observed_at).exists():
        return save_status('gap', 'rate_boundary', advance=True)
    units = int(version.multiplier * RATE_SCALE)
    charged, remainder = divmod(raw * units + cycle.weighted_remainder, RATE_SCALE)
    if (cycle.used_bytes + charged > MAX_BYTES or
            (cycle.raw_bytes is not None and cycle.raw_bytes + raw > MAX_BYTES)):
        raise ValidationError('累计超出安全存储范围')
    UsageLedger.objects.create(cycle=cycle, stream=stream, sequence=sequence,
        upload_delta=upload_bytes - previous.upload_bytes, download_delta=download_bytes - previous.download_bytes,
        weighted_bytes=charged, grant_rate_version=version, quality='metered',
        observed_at=observed_at, interval_start=previous.observed_at, interval_end=observed_at)
    cycle.used_bytes += charged
    cycle.weighted_remainder = remainder
    if cycle.raw_bytes is not None:
        cycle.raw_bytes += raw
    cycle.save(update_fields=['used_bytes', 'raw_bytes', 'weighted_remainder'])
    record.usage_updated_at = max(record.usage_updated_at or observed_at, observed_at)
    record.save(update_fields=['usage_updated_at'])
    return save_status('accepted', advance=True)
