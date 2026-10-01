"""倍率只追加版本，固定百万分母保留余数，不重新猜算历史扣费。"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import AuditEvent, Line, LineRateVersion, UsageLedger

RATE_SCALE = 1_000_000


def decimal_multiplier(value):
    try:
        multiplier = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError('倍率必须为正数，最多六位小数')
    if (not multiplier.is_finite() or multiplier <= 0 or multiplier >= 1_000_000
            or multiplier * RATE_SCALE != (multiplier * RATE_SCALE).to_integral_value()):
        raise ValidationError('倍率必须大于零、小于一百万，最多六位小数')
    return multiplier


def current_line_rate(line, at=None):
    return LineRateVersion.objects.filter(line=line, effective_at__lte=at or timezone.now()).order_by('-effective_at', '-pk').first()


@transaction.atomic
def set_line_rate(actor, line_id, multiplier, effective_at, reason):
    from .entitlements import _require_admin
    _require_admin(actor)
    multiplier = decimal_multiplier(multiplier)
    if timezone.is_naive(effective_at) or not str(reason).strip() or len(str(reason)) > 240:
        raise ValidationError('请填写生效时间与修改原因')
    line = Line.objects.select_for_update().get(pk=line_id)
    latest = UsageLedger.objects.filter(stream__identity__line=line).order_by('-observed_at').first()
    if latest and effective_at <= latest.observed_at:
        raise ValidationError('倍率不能回溯修改已计量区间；请选择未来边界')
    old = line.rate_versions.filter(effective_at=effective_at).first()
    if old:
        if old.multiplier == multiplier and old.reason == reason:
            return old
        raise ValidationError('该生效时刻已有倍率版本，不能覆盖历史')
    result = LineRateVersion.objects.create(line=line, multiplier=multiplier, effective_at=effective_at,
                                            actor=actor, reason=str(reason).strip())
    AuditEvent.objects.create(actor=actor, action='line_rate_set', subject=str(line.public_id), result='SAVED')
    return result


def price_usage(identity, cycle, latest, observed_at, raw_bytes, activated_at):
    """调用者在权益事务内；跨边界非零差分必须先补边界累计样本。"""
    from .entitlements import MeteringGap
    Line.objects.select_for_update().get(pk=identity.line_id)
    start = latest.observed_at if latest else activated_at
    # 零流量检查点可建立当前倍率基线，不产生追溯扣款。
    if not raw_bytes:
        version = current_line_rate(identity.line_id, observed_at)
    else:
        version = current_line_rate(identity.line_id, start)
        if LineRateVersion.objects.filter(line_id=identity.line_id, effective_at__gt=start,
                                          effective_at__lt=observed_at).exists():
            raise MeteringGap('倍率变化缺少边界累计样本，不能猜算扣费')
    if version is None:
        raise MeteringGap('线路倍率未配置，不能假设扣费倍率')
    units = int(version.multiplier * RATE_SCALE)
    charged, remainder = divmod(raw_bytes * units + cycle.weighted_remainder, RATE_SCALE)
    if charged + cycle.used_bytes > 2**63 - 1:
        raise ValidationError('扣费累计超出安全存储范围')
    return version, charged, remainder
