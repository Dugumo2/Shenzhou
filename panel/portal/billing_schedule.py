"""下一次重置计划：纯读预览、独立修订和按权威当前时间推进。"""
import hashlib
import json
import re
from functools import wraps
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, transaction
from django.utils import timezone

from .entitlements import SHANGHAI, next_reset
from .metering_quality import assess_metering
from .models import BillingCycle, BillingPlan, BillingPlanRevision, Entitlement, UsageLedger

PREVIEW_TTL = 600
PREVIEW_SALT = 'portal.billing-plan.preview.v1'


class BillingError(ValidationError):
    def __init__(self, message, code='BILLING_CONFLICT', status=409, fields=None):
        super().__init__(message, code=code)
        self.status, self.fields = status, fields or {}


def invalid(field, message):
    return BillingError(message, 'INVALID_INPUT', 422, {field: [message]})


def require_admin(actor):
    # 先核验当前权限；已保存请求的重放也不能绕过撤权。
    if not actor.is_authenticated:
        raise BillingError('请先登录。', 'UNAUTHENTICATED', 401)
    if not get_user_model().objects.filter(pk=actor.pk, is_active=True, is_staff=True).exists():
        raise PermissionDenied('仅管理员可以修改流量重置计划。')


def _record(actor, service_id, lock=False):
    require_admin(actor)
    query = Entitlement.objects.select_for_update() if lock else Entitlement.objects
    record = query.filter(public_id=service_id).first()
    if record is None:
        raise BillingError('服务不存在。', 'NOT_FOUND', 404)
    # 隐藏操作按钮不能代替写API门禁：失效旧源映射不能绕开统一服务入口改账期。
    from .p8_entitlement import claimed_entitlement_ids, mappings, valid_mapping
    if record.pk in claimed_entitlement_ids():
        mapping = mappings().filter(entitlement_id=record.pk).first()
        if mapping is None or not valid_mapping(mapping):
            raise BillingError('服务不存在。', 'NOT_FOUND', 404)
    return record


def parse_reset_at(value):
    try:
        if isinstance(value, str):
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', value):
                result = datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)
            else:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::00(?:\.0+)?)?(?:Z|[+-]\d{2}:\d{2})', value):
                    raise ValueError
                result = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if timezone.is_naive(result):
                    raise ValueError
        elif isinstance(value, datetime) and timezone.is_aware(value):
            result = value
        else:
            raise ValueError
        result = result.astimezone(SHANGHAI)
        if result.second or result.microsecond:
            raise ValueError
        # 预览必须能完整计算之后三个自然月。
        _following_resets(result, result.day, result.hour, result.minute)
        return result
    except (ValueError, TypeError, OverflowError):
        raise invalid('next_reset_at', '请输入完整的未来分钟时间；本地输入按上海时间解释，秒必须为零。')


def stamp(value):
    return value.astimezone(SHANGHAI).isoformat() if value else None


def _legacy_anchor(record):
    anchor = record.activated_at.astimezone(SHANGHAI)
    applied = record.applied_snapshot if record.applied_revision else {}
    day = applied.get('reset_day', record.reset_day) or anchor.day
    hour = applied.get('reset_hour', record.reset_hour)
    minute = applied.get('reset_minute', record.reset_minute)
    return day, hour if hour is not None else anchor.hour, minute if minute is not None else anchor.minute


def lookup_cycle(record, at):
    """历史查询绝不建期；重叠账期不得任意选第一条。"""
    from .entitlements import MeteringGap
    if timezone.is_naive(at):
        raise ValidationError('账期查询时间必须包含时区。')
    rows = list(record.cycles.filter(starts_at__lte=at, ends_at__gt=at).order_by('starts_at')[:2])
    if len(rows) > 1:
        raise MeteringGap('账期边界重叠，需要人工核验。')
    return rows[0] if rows else None


def _context(record, now, lock=False):
    plans = BillingPlan.objects.select_for_update() if lock else BillingPlan.objects
    plan = plans.filter(entitlement=record).first()
    cycles = record.cycles.select_for_update() if lock else record.cycles
    rows = list(cycles.filter(starts_at__lte=now, ends_at__gt=now).order_by('starts_at')[:2])
    cycle = rows[0] if len(rows) == 1 else None
    reason = ''
    if not record.activated_at:
        reason = '服务尚未实际开通，请在开通流程设置初始计划。'
    elif not cycle:
        reason = '当前账期未建立、已结束或边界重叠，请先核验并推进账期。'
    elif plan and (plan.cycle_id != cycle.pk or plan.next_reset_at != cycle.ends_at):
        reason = '计划与当前账期尚未同步，请先核验并推进账期。'
    elif record.cycles.exclude(pk=cycle.pk).filter(ends_at__gt=cycle.starts_at).exists():
        reason = '存在重叠或已确认的后续账期，不能移动当前边界。'
    elif UsageLedger.objects.filter(cycle=cycle, observed_at__gte=cycle.ends_at).exists():
        reason = '当前期末已有确认样本，不能重新打开已确认边界。'
    if plan:
        anchor = (plan.anchor_day, plan.hour, plan.minute)
        next_at = plan.next_reset_at
    elif cycle and record.activated_at:
        anchor = _legacy_anchor(record)
        next_at = cycle.ends_at
    else:
        anchor, next_at = None, None
    return plan, cycle, anchor, next_at, reason


def _snapshot(record, now, lock=False):
    plan, cycle, anchor, next_at, reason = _context(record, now, lock)
    metering = assess_metering(record, cycle, now)
    quality = metering['quality']
    known = metering['usable']
    quota = record.applied_snapshot.get('quota_bytes') if record.applied_revision else None
    used = cycle.used_bytes if cycle and known else None
    return {
        'service_id': str(record.public_id), 'time_zone': 'Asia/Shanghai',
        'billing_revision': plan.revision if plan else 0,
        'current_cycle': {'id': cycle.pk, 'starts_at': stamp(cycle.starts_at), 'ends_at': stamp(cycle.ends_at),
            'used_bytes': str(used) if used is not None else None,
            'raw_bytes': str(cycle.raw_bytes) if known and cycle.raw_bytes is not None else None,
            'weighted_remainder': str(cycle.weighted_remainder) if known else None,
            'recorded_used_bytes': str(cycle.used_bytes),
            'recorded_raw_bytes': str(cycle.raw_bytes) if cycle.raw_bytes is not None else None,
            'recorded_weighted_remainder': str(cycle.weighted_remainder)} if cycle else None,
        'plan': {'next_reset_at': stamp(next_at), 'anchor_day': anchor[0], 'hour': anchor[1], 'minute': anchor[2]} if anchor else None,
        'quota_bytes': str(quota) if quota is not None else None, 'used_bytes': str(used) if used is not None else None,
        'remaining_bytes': str(max(0, quota - used)) if quality == 'fresh' and used is not None and quota is not None else None,
        'usage_quality': quality, 'usage_updated_at': stamp(record.usage_updated_at),
        'expires_at': stamp(record.expires_at), 'can_modify': not reason, 'blocked_reason': reason,
    }


def billing_state(actor, service_id):
    return _snapshot(_record(actor, service_id), timezone.now())


def _following_resets(first, day, hour, minute):
    values = []
    for _ in range(3):
        first = next_reset(first, day, hour, minute)
        values.append(stamp(first))
    return values


def _check_change(record, selected, expected_revision, now, lock=False):
    if type(expected_revision) is not int or expected_revision < 0:
        raise invalid('expected_billing_revision', '计划修订必须为非负整数。')
    plan, cycle, anchor, old_at, reason = _context(record, now, lock)
    if reason:
        raise BillingError(reason)
    if (plan.revision if plan else 0) != expected_revision:
        raise BillingError('重置计划已被修改，请重新获取并预览。')
    if selected <= now:
        raise invalid('next_reset_at', '下一次流量重置时间必须在未来。')
    if selected <= cycle.starts_at:
        raise BillingError('新终点必须严格晚于当前账期起点。')
    latest = UsageLedger.objects.filter(cycle__entitlement=record).order_by('-observed_at').first()
    if latest and selected <= latest.observed_at:
        raise BillingError('新终点必须严格晚于全部已入账样本；不能重算历史。')
    # 不变输入保持31日原锚点；只有选择新日期才改月日锚点。
    anchor = anchor if selected == old_at else (selected.day, selected.hour, selected.minute)
    _following_resets(selected, *anchor)
    return plan, cycle, anchor, old_at


def _binding(actor, record, selected, revision, cycle, anchor):
    return {'actor': actor.pk, 'service': str(record.public_id), 'next': stamp(selected),
        'revision': revision, 'cycle': [cycle.pk, stamp(cycle.starts_at), stamp(cycle.ends_at)],
        'anchor': list(anchor)}


def preview_billing(actor, service_id, next_reset_at, expected_billing_revision):
    record = _record(actor, service_id)
    now = timezone.now()
    selected = parse_reset_at(next_reset_at)
    _, cycle, anchor, old_at = _check_change(record, selected, expected_billing_revision, now)
    token = signing.dumps(_binding(actor, record, selected, expected_billing_revision, cycle, anchor), salt=PREVIEW_SALT)
    result = _snapshot(record, now)
    result.update(old_next_reset_at=stamp(old_at), next_reset_at=stamp(selected),
        shift_seconds=int((selected - old_at).total_seconds()), next_resets=_following_resets(selected, *anchor),
        expires_before_reset=bool(record.expires_at and record.expires_at <= selected),
        preview_token=token, preview_expires_at=stamp(now + timedelta(seconds=PREVIEW_TTL)))
    return result


def atomic_change(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            with transaction.atomic():
                return view(*args, **kwargs)
        except OperationalError as exc:
            if 'locked' in str(exc).lower():
                raise BillingError('其他操作正在保存此计划，请刷新后重试。', 'BILLING_BUSY') from exc
            raise
    return wrapped


@atomic_change
def save_billing(actor, service_id, next_reset_at, expected_billing_revision, preview_token, idempotency_key):
    record = _record(actor, service_id, lock=True)
    selected = parse_reset_at(next_reset_at)
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128:
        raise invalid('idempotency_key', '请提供8至128字符的幂等键。')
    if not isinstance(preview_token, str) or not preview_token or len(preview_token) > 4096:
        raise invalid('preview_token', '请先预览影响，再确认保存。')
    if type(expected_billing_revision) is not int or expected_billing_revision < 0:
        raise invalid('expected_billing_revision', '计划修订必须为非负整数。')
    digest = hashlib.sha256(json.dumps([stamp(selected), expected_billing_revision, preview_token],
                                      ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    old = BillingPlanRevision.objects.filter(entitlement=record, actor=actor, idempotency_key=idempotency_key).first()
    if old:
        if old.request_digest != digest:
            raise BillingError('相同幂等键已用于不同内容。')
        return old.result
    now = timezone.now()
    plan, cycle, anchor, old_at = _check_change(record, selected, expected_billing_revision, now, lock=True)
    try:
        binding = signing.loads(preview_token, salt=PREVIEW_SALT, max_age=PREVIEW_TTL)
    except signing.BadSignature:
        raise BillingError('预览已过期或无效，请重新预览。', 'PREVIEW_INVALID')
    if binding != _binding(actor, record, selected, expected_billing_revision, cycle, anchor):
        raise BillingError('预览与当前服务、输入或账期不一致，请重新预览。', 'PREVIEW_INVALID')
    before = _snapshot(record, now)['plan']
    cycle.ends_at = selected
    cycle.save(update_fields=['ends_at'])
    if plan is None:
        plan = BillingPlan(entitlement=record, cycle=cycle)
    else:
        plan.revision += 1
    plan.next_reset_at, plan.anchor_day, plan.hour, plan.minute = selected, *anchor
    plan.updated_by = actor
    plan.save()
    result = {'message': '重置计划已保存', 'saved_at': stamp(now), 'billing': _snapshot(record, now)}
    BillingPlanRevision.objects.create(entitlement=record, actor=actor, revision=plan.revision,
        expected_revision=expected_billing_revision, idempotency_key=idempotency_key, request_digest=digest,
        before=before, after=result['billing']['plan'], result=result)
    return result


@transaction.atomic
def advance_billing_cycles(entitlement):
    """仅按服务端权威现在推进；保留旧期，重启补执行不会多赠额度。"""
    from .entitlements import MeteringGap
    record = Entitlement.objects.select_for_update().get(pk=entitlement.pk)
    now = timezone.now()
    if not record.activated_at or now < record.activated_at:
        return None
    plan = BillingPlan.objects.select_for_update().filter(entitlement=record).first()
    if plan:
        cycle = BillingCycle.objects.select_for_update().get(pk=plan.cycle_id)
        if cycle.ends_at != plan.next_reset_at:
            raise MeteringGap('账期与独立计划边界不一致，不能猜算。')
        anchor = (plan.anchor_day, plan.hour, plan.minute)
        if now < cycle.starts_at:
            return lookup_cycle(record, now)
    else:
        cycle = lookup_cycle(record, now)
        if cycle:
            return cycle
        cycle = record.cycles.filter(ends_at__lte=now).order_by('-ends_at').first()
        anchor = _legacy_anchor(record)
        if cycle is None:
            if record.cycles.exists():
                raise MeteringGap('未找到已确认的账期起点，不能猜算。')
            cycle = BillingCycle.objects.create(entitlement=record, starts_at=record.activated_at,
                ends_at=next_reset(record.activated_at, *anchor), raw_bytes=0)
    expired_boundary = False
    while cycle.ends_at <= now:
        start = cycle.ends_at
        if record.expires_at and start >= record.expires_at:
            expired_boundary = True
            break
        end = next_reset(start, *anchor)
        following = record.cycles.filter(starts_at=start).first()
        if following:
            if following.ends_at != end:
                raise MeteringGap('后续账期已有不同边界，不能覆盖。')
        else:
            if record.cycles.filter(starts_at__lt=end, ends_at__gt=start).exists():
                raise MeteringGap('推进会与已确认账期重叠，不能猜算。')
            following = BillingCycle.objects.create(entitlement=record, starts_at=start, ends_at=end, raw_bytes=0)
        cycle = following
    if plan and plan.cycle_id != cycle.pk:
        plan.cycle, plan.next_reset_at = cycle, cycle.ends_at
        plan.save(update_fields=['cycle', 'next_reset_at'])
    return None if expired_boundary else cycle
