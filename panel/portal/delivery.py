"""独立设备订阅生命周期；下载令牌不承载实际节点凭据。"""
from datetime import timedelta

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .entitlements import validated_lines
from .jobs import enqueue, fingerprint, get_existing_job
from .models import DeviceSubscription, Entitlement, NodeIdentity, ServiceDeliveryReset, DeploymentJob

CLIENTS = dict(DeviceSubscription.CLIENTS)
TOKEN_SALT = 'xingzhou-device-subscription-v1'


def subscriptions_for(actor):
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied('请先登录')
    return DeviceSubscription.objects.filter(entitlement__user=actor).select_related('entitlement').prefetch_related('lines')


def _owned(actor, public_id):
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied('请先登录')
    try:
        return DeviceSubscription.objects.select_for_update().select_related('entitlement').get(
            public_id=public_id, entitlement__user=actor)
    except DeviceSubscription.DoesNotExist:
        raise PermissionDenied('订阅不存在或不属于当前账号')


def _allowed(record, line_ids):
    if not record.enabled or record.state in {'disabled', 'enforcement_pending', 'suspended', 'resuming'}:
        raise ValidationError('套餐未启用或已被停用')
    if record.expires_at is not None and record.expires_at <= timezone.now():
        raise ValidationError('套餐已到期')
    lines = validated_lines(line_ids)
    if not {x.pk for x in lines}.issubset(set(record.lines.values_list('pk', flat=True))):
        raise PermissionDenied('订阅不能选择未获授权的线路')
    return lines


def _make_identities(sub, lines):
    NodeIdentity.objects.bulk_create([NodeIdentity(subscription=sub, line=line, ingress=ingress,
        generation=sub.generation) for line in lines for ingress in line.all_ingresses()])


def expected_identity_pairs(sub):
    return {(line.pk, ingress.pk) for line in sub.lines.all() for ingress in line.all_ingresses()}


@transaction.atomic
def create_subscription(actor, name, client, line_ids, idempotency_key=None, delivery_scope=''):
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied('请先登录')
    if client not in CLIENTS:
        raise ValidationError('暂不支持此客户端')
    name = name.strip()
    if not name or len(name) > 80:
        raise ValidationError('订阅名称应为 1 至 80 字符')
    record = Entitlement.objects.select_for_update().filter(user=actor).first()
    if record is None:
        raise ValidationError('请先由管理员分配套餐')
    values = [name, client, sorted(int(x) for x in line_ids)]
    if delivery_scope:
        values.append(delivery_scope)
    digest = fingerprint(values)
    old = get_existing_job(actor, 'create', idempotency_key, digest)
    if old:
        return old.subscription, old
    lines = _allowed(record, line_ids)
    sub = DeviceSubscription.objects.create(entitlement=record, name=name, client=client, delivery_scope=delivery_scope)
    sub.lines.set(lines)
    _make_identities(sub, lines)
    return sub, enqueue(actor, record, 'create', idempotency_key, digest, subscription=sub)


@transaction.atomic
def reset_subscription(actor, public_id, idempotency_key=None):
    sub = _owned(actor, public_id)
    record = Entitlement.objects.select_for_update().get(pk=sub.entitlement_id)
    digest = fingerprint([str(sub.public_id), 'reset'])
    old = get_existing_job(actor, 'reset', idempotency_key, digest)
    if old:
        return old.subscription, old
    if sub.state in {'resetting', 'disabling'}:
        raise ValidationError('已有重置或停用任务，请先查看任务结果')
    lines = _allowed(record, list(sub.lines.values_list('pk', flat=True)))
    sub.identities.filter(revoked_at__isnull=True).update(revoked_at=timezone.now(), state='revocation_pending')
    sub.generation += 1
    sub.token_version += 1
    sub.state = 'resetting'
    sub.save(update_fields=['generation', 'token_version', 'state'])
    _make_identities(sub, lines)
    return sub, enqueue(actor, record, 'reset', idempotency_key, digest, subscription=sub)


@transaction.atomic
def disable_subscription(actor, public_id, idempotency_key=None):
    sub = _owned(actor, public_id)
    record = Entitlement.objects.select_for_update().get(pk=sub.entitlement_id)
    digest = fingerprint([str(sub.public_id), 'disable'])
    old = get_existing_job(actor, 'disable', idempotency_key, digest)
    if old:
        return old.subscription, old
    sub.identities.filter(revoked_at__isnull=True).update(revoked_at=timezone.now(), state='revocation_pending')
    sub.generation += 1
    sub.token_version += 1
    sub.state = 'disabling'
    sub.save(update_fields=['generation', 'token_version', 'state'])
    return sub, enqueue(actor, record, 'disable', idempotency_key, digest, subscription=sub)


@transaction.atomic
def refresh_subscription(actor, public_id, idempotency_key=None):
    sub = _owned(actor, public_id)
    record = Entitlement.objects.select_for_update().get(pk=sub.entitlement_id)
    digest = fingerprint([str(sub.public_id), 'refresh'])
    old = get_existing_job(actor, 'refresh', idempotency_key, digest)
    if old:
        return old.subscription, old
    if sub.state not in {'active', 'simulated'}:
        raise ValidationError('订阅尚未开通，不能更新内容')
    _allowed(record, list(sub.lines.values_list('pk', flat=True)))
    # 不改变身份代际与令牌；失败时保留此前可用订阅产物。
    return sub, enqueue(actor, record, 'refresh', idempotency_key, digest, subscription=sub)


def _downloadable(sub):
    record, now = sub.entitlement, timezone.now()
    if (not record.user.is_active or not record.enabled or record.metering_gap or record.state != 'active'
            or record.applied_revision != record.revision or sub.state != 'active'
            or sub.applied_generation != sub.generation
            or record.expires_at is None or record.expires_at <= now):
        return False
    if (record.usage_updated_at is None or record.usage_updated_at < now - timedelta(minutes=3)
            or record.usage_updated_at > now + timedelta(seconds=30)):
        return False
    cycle = record.cycles.filter(starts_at__lte=now, ends_at__gt=now).first()
    if not cycle or cycle.used_bytes >= record.quota_bytes:
        return False
    required = set(sub.lines.values_list('pk', flat=True))
    permitted = set(record.lines.filter(enabled=True, ingress__enabled=True,
                    ingress__server__enabled=True, egress__server__enabled=True).values_list('pk', flat=True))
    actual = set(sub.identities.filter(generation=sub.generation, state='active', ingress__enabled=True,
                 ingress__server__enabled=True, revoked_at__isnull=True).values_list('line_id', 'ingress_id'))
    return bool(required) and required.issubset(permitted) and expected_identity_pairs(sub) == actual


def token_for_subscription(actor, public_id):
    sub = subscriptions_for(actor).get(public_id=public_id)
    if not _downloadable(sub):
        raise ValidationError('服务器应用或计量尚未核验，不能提供下载链接')
    return signing.Signer(salt=TOKEN_SALT).sign_object({'id': str(sub.public_id), 'v': sub.token_version}, compress=False)


def resolve_subscription(token):
    """只解析当前有效的用户范围；返回实体后仍须从受限产物库取对应配置。"""
    try:
        data = signing.Signer(salt=TOKEN_SALT).unsign_object(token)
        if not isinstance(data, dict) or set(data) != {'id', 'v'}:
            raise ValueError
        sub = DeviceSubscription.objects.select_related('entitlement__user').get(public_id=data['id'], token_version=data['v'])
        if not _downloadable(sub):
            raise ValueError
        return sub
    except (signing.BadSignature, DeviceSubscription.DoesNotExist, ValueError, TypeError, ValidationError):
        raise PermissionDenied('订阅链接已失效或尚未完成开通')


@transaction.atomic
def obtain_delivery(actor, service_id, client, scope='all', idempotency_key=None):
    """服务已由管理员分配；取某格式不会新建权益或重复创建设备卡片。"""
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied('请先登录')
    record = Entitlement.objects.select_for_update().filter(user=actor, public_id=service_id).first()
    if record is None:
        raise PermissionDenied('服务不存在或不属于当前账号')
    if (record.state not in {'active', 'simulated'} or not record.enabled
            or record.applied_revision != record.revision or record.metering_gap):
        raise ValidationError('服务尚未开通，请等待管理员完成应用')
    if client not in CLIENTS:
        raise ValidationError('此客户端尚未支持')
    lines = list(record.lines.order_by('pk'))
    if scope != 'all':
        if not isinstance(scope, str) or len(scope) > 40 or not scope.startswith('line:') or not scope[5:].isdigit():
            raise ValidationError('线路范围无效')
        line_pk = int(scope[5:])
        scope = f'line:{line_pk}'
        lines = [line for line in lines if line.pk == line_pk]
    permitted = _allowed(record, [line.pk for line in lines])
    wanted = {line.pk for line in permitted}
    # 用逻辑范围识别交付，不把“当前恰好只有一条线路”等同于单线路订阅。
    sub = record.subscriptions.filter(client=client, delivery_scope=scope).exclude(state__in=['disabled', 'disabling']).first()
    if sub:
        pairs = {(line.pk, ingress.pk) for line in permitted for ingress in line.all_ingresses()}
        existing = set(sub.identities.filter(generation=sub.generation, revoked_at__isnull=True).values_list('line_id', 'ingress_id'))
        if set(sub.lines.values_list('pk', flat=True)) == wanted and pairs == existing:
            return sub, sub.jobs.order_by('-pk').first(), False
        if sub.state == 'resetting':
            raise ValidationError('订阅正在重置，请完成后更新线路范围')
        digest = fingerprint([str(sub.public_id), 'scope-refresh', sorted(pairs), record.revision])
        old = get_existing_job(actor, 'refresh', idempotency_key, digest)
        if old:
            return sub, old, False
        sub.lines.set(permitted)
        for identity in sub.identities.filter(generation=sub.generation, revoked_at__isnull=True):
            if (identity.line_id, identity.ingress_id) not in pairs:
                identity.revoked_at, identity.state = timezone.now(), 'revocation_pending'
                identity.save(update_fields=['revoked_at', 'state'])
        NodeIdentity.objects.bulk_create([NodeIdentity(subscription=sub, line_id=line_id, ingress_id=ingress_id,
            generation=sub.generation) for line_id, ingress_id in pairs - existing])
        sub.state = 'pending'
        sub.save(update_fields=['state'])
        # 链接版本不变；成功应用新的授权范围后沿用原地址。
        job = enqueue(actor, record, 'refresh', idempotency_key, digest, subscription=sub)
        return sub, job, True
    name = (record.service_name or ' + '.join(line.name for line in permitted))[:80]
    sub, job = create_subscription(actor, name, client, sorted(wanted), idempotency_key, delivery_scope=scope)
    return sub, job, True


@transaction.atomic
def reset_service_deliveries(actor, service_id, idempotency_key):
    """只重置此服务全部已交付身份，保留额度、到期和历史用量。"""
    if not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied('请先登录')
    record = Entitlement.objects.select_for_update().filter(user=actor, public_id=service_id).first()
    if record is None:
        raise PermissionDenied('服务不存在或不属于当前账号')
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 64:
        raise ValidationError('操作标识无效')
    previous = ServiceDeliveryReset.objects.filter(entitlement=record, key=idempotency_key).first()
    if previous:
        jobs = list(DeploymentJob.objects.filter(pk__in=previous.job_ids).order_by('pk'))
        if len(jobs) != len(previous.job_ids):
            raise ValidationError('重置记录不完整，请联系管理员核对')
        return jobs
    subscriptions = list(record.subscriptions.exclude(state__in=['disabled', 'disabling']).order_by('pk'))
    if not subscriptions:
        raise ValidationError('此服务尚未生成订阅链接')
    jobs = []
    for sub in subscriptions:
        _, job = reset_subscription(actor, sub.public_id, idempotency_key + '-' + str(sub.pk))
        jobs.append(job)
    ServiceDeliveryReset.objects.create(entitlement=record, actor=actor, key=idempotency_key,
        subscription_ids=[sub.pk for sub in subscriptions], job_ids=[job.pk for job in jobs])
    return jobs
