"""事务任务箱与带租约的执行协议；默认没有生产适配器，绝不伪造已应用。"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from .models import AuditEvent, DeploymentJob, DeviceSubscription, Entitlement, NodeIdentity, Server

# 当前尚无通过生产能力验收的适配器。只有审核代码变更可以加入具体类，
# 网页字段、环境变量或回执中的 scope 都不能自行开启生产应用能力。
APPROVED_PRODUCTION_ADAPTERS = frozenset()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _key(actor, kind, key):
    if not isinstance(key, str) or not 8 <= len(key) <= 128:
        raise ValidationError('必须提供 8 至 128 字符的幂等请求标识')
    return fingerprint([actor.pk, kind, key])


def get_existing_job(actor, kind, key, digest):
    job = DeploymentJob.objects.filter(idempotency_key=_key(actor, kind, key)).first()
    if job and job.request_fingerprint != digest:
        raise ValidationError('同一请求标识不能用于不同内容')
    return job


def enqueue(actor, entitlement, kind, key, digest, subscription=None, payload=None):
    values = dict(payload or {})
    values['entitlement_revision'] = entitlement.revision
    job = DeploymentJob.objects.create(actor=actor, entitlement=entitlement, subscription=subscription,
        kind=kind, revision=subscription.generation if subscription else entitlement.revision,
        idempotency_key=_key(actor, kind, key), request_fingerprint=digest, payload=values)
    AuditEvent.objects.create(actor=actor, action='phase1_' + kind, subject=str(job.public_id), result='QUEUED')
    return job


def enqueue_enforcement(record, reason, period_id=0):
    """调用者持有权益事务锁；停用动作有持久幂等键。"""
    key = f'enforce-{reason}-{record.pk}-{record.revision}-{record.enforcement_epoch}-{period_id}'
    digest = fingerprint([record.pk, record.revision, record.enforcement_epoch, reason, period_id])
    old = get_existing_job(record.user, 'enforce', key, digest)
    if old:
        return old
    record.state, record.suspension_reason = 'enforcement_pending', reason
    record.save(update_fields=['state', 'suspension_reason'])
    identities = NodeIdentity.objects.filter(subscription__entitlement=record, revoked_at__isnull=True)
    ids = list(identities.values_list('pk', flat=True))
    identities.update(state='suspension_pending')
    DeviceSubscription.objects.filter(entitlement=record).exclude(state='disabled').update(state='blocked')
    return enqueue(record.user, record, 'enforce', key, digest, payload={'reason': reason, 'suspend_ids': ids})


@transaction.atomic
def queue_due_enforcements(now=None):
    now = now or timezone.now()
    count = 0
    records = Entitlement.objects.select_for_update().filter(
        Q(expires_at__lte=now) | Q(enabled=False) | Q(user__is_active=False) |
        Q(state='active', usage_updated_at__lt=now - timedelta(minutes=3)) |
        Q(state='active', usage_updated_at__isnull=True, activated_at__lt=now - timedelta(minutes=3))
    ).exclude(state='disabled')
    for record in records:
        before = DeploymentJob.objects.filter(entitlement=record, kind='enforce').count()
        if not record.enabled or not record.user.is_active:
            reason = 'account_disabled'
        else:
            reason = 'expired' if record.expires_at and record.expires_at <= now else 'metering_stale'
        enqueue_enforcement(record, reason)
        count += int(DeploymentJob.objects.filter(entitlement=record, kind='enforce').count() > before)
    return count


@transaction.atomic
def queue_resumptions(now=None):
    """账期重置或计量恢复后恢复同一身份；永久撤销记录永不参与恢复。"""
    from .entitlements import current_cycle
    now = now or timezone.now()
    count = 0
    records = Entitlement.objects.select_for_update().filter(state='suspended', enabled=True,
        user__is_active=True, expires_at__gt=now, usage_updated_at__gte=now - timedelta(minutes=3),
        usage_updated_at__lte=now + timedelta(seconds=30), metering_gap=False)
    for record in records:
        if record.applied_revision != record.revision:
            continue
        cycle = current_cycle(record, now)
        if cycle is None or cycle.used_bytes >= record.applied_snapshot.get('quota_bytes', 0):
            continue
        # 加入暂停任务主键，保证同账期多次计量中断恢复仍分别幂等。
        previous = record.jobs.filter(kind='enforce', state__in=['applied', 'simulated']).order_by('-pk').first()
        if previous is None:
            continue
        key = f'resume-{record.pk}-{record.revision}-{cycle.pk}-{previous.pk}'
        digest = fingerprint([record.pk, record.revision, cycle.pk, previous.pk])
        if get_existing_job(record.user, 'resume', key, digest):
            continue
        record.state = 'resuming'
        record.save(update_fields=['state'])
        enqueue(record.user, record, 'resume', key, digest)
        count += 1
    return count


@transaction.atomic
def retry_job(actor, public_id):
    if not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        raise PermissionDenied('仅管理员可以重试发布任务')
    job = DeploymentJob.objects.select_for_update().get(public_id=public_id)
    if job.state not in {'failed', 'blocked'}:
        raise ValidationError('任务当前不可重试')
    job.state, job.result_code, job.lease_id, job.lease_until = 'queued', '', None, None
    job.save(update_fields=['state', 'result_code', 'lease_id', 'lease_until'])
    return job


def _superseded(job):
    if job.payload['entitlement_revision'] != job.entitlement.revision:
        return True
    if job.subscription_id and job.revision != job.subscription.generation:
        return True
    if job.kind == 'resume' and job.entitlement.state != 'resuming':
        return True
    if job.kind == 'enforce' and job.entitlement.state not in {'enforcement_pending', 'suspended'}:
        return True
    return False


def _affected_servers(record):
    ids = set()
    for line in record.lines.select_related('ingress', 'egress'):
        ids.add(line.egress.server_id)
        ids.update(ingress.server_id for ingress in line.all_ingresses())
    ids.update(NodeIdentity.objects.filter(subscription__entitlement=record).values_list('ingress__server_id', flat=True))
    return sorted(ids)


def _release_servers(job):
    Server.objects.filter(deployment_lease_id=job.lease_id).update(deployment_lease_id=None, deployment_lease_until=None)


@transaction.atomic
def claim_job(public_id, lease_seconds=90):
    job = DeploymentJob.objects.select_for_update().select_related('entitlement', 'subscription').get(public_id=public_id)
    now = timezone.now()
    if job.state not in {'queued', 'running'} or (job.state == 'running' and job.lease_until and job.lease_until > now):
        return None
    # 同一权益的服务端配置发布必须串行，避免重置和续期在核心上互相覆盖。
    job.entitlement = Entitlement.objects.select_for_update().get(pk=job.entitlement_id)
    if DeploymentJob.objects.filter(entitlement_id=job.entitlement_id, state='running',
            lease_until__gt=now).exclude(pk=job.pk).exists():
        return None
    if _superseded(job):
        job.state, job.result_code, job.finished_at = 'superseded', 'NEWER_REVISION_EXISTS', now
        job.save(update_fields=['state', 'result_code', 'finished_at'])
        return None
    servers = list(Server.objects.select_for_update().filter(pk__in=_affected_servers(job.entitlement)).order_by('pk'))
    if any(server.deployment_lease_until and server.deployment_lease_until > now for server in servers):
        return None
    job.lease_id, job.lease_until = uuid.uuid4(), now + timedelta(seconds=lease_seconds)
    fences = {}
    for server in servers:
        server.deployment_lease_id, server.deployment_lease_until = job.lease_id, job.lease_until
        server.deployment_fence += 1
        server.save(update_fields=['deployment_lease_id', 'deployment_lease_until', 'deployment_fence'])
        fences[str(server.pk)] = server.deployment_fence
    job.payload['_server_fences'] = fences
    job.state, job.attempts = 'running', job.attempts + 1
    job.save(update_fields=['lease_id', 'lease_until', 'state', 'attempts', 'payload'])
    return job


def build_plan(job):
    """固定结构供受限适配器消费；没有任意命令、文件路径或秘密。"""
    record = job.entitlement
    from .entitlements import add_months
    activated_at = record.activated_at or timezone.now()
    planned_expiry = record.requested_expires_at or add_months(activated_at, record.service_months)
    if job.kind in {'assign', 'resume', 'create', 'reset', 'refresh'} and record.enabled:
        # 管理员再次保存套餐不能绕过已经成立的停用条件。
        if not record.user.is_active:
            raise AdapterUnavailable('ACCOUNT_DISABLED')
        if record.metering_gap:
            raise AdapterUnavailable('METERING_GAP_UNRESOLVED')
        if planned_expiry <= timezone.now():
            raise AdapterUnavailable('ENTITLEMENT_EXPIRED')
        now = timezone.now()
        cycle = record.cycles.filter(starts_at__lte=now, ends_at__gt=now).first()
        quota = record.quota_bytes if job.kind == 'assign' else record.applied_snapshot.get('quota_bytes', 0)
        if cycle is not None and cycle.used_bytes >= quota:
            raise AdapterUnavailable('QUOTA_EXHAUSTED')
    active_ids = []
    if job.subscription_id and job.kind in {'create', 'reset', 'refresh'}:
        active_ids = list(job.subscription.identities.filter(generation=job.revision,
                      revoked_at__isnull=True).values_list('pk', flat=True))
    elif job.kind in {'assign', 'resume'} and record.enabled:
        active_ids = list(NodeIdentity.objects.filter(subscription__entitlement=record,
            generation=F('subscription__generation'), line__in=record.lines.all(), revoked_at__isnull=True,
            state__in=['active', 'simulated', 'suspended', 'suspension_pending'])
            .exclude(subscription__state__in=['disabled', 'disabling']).values_list('pk', flat=True))
    revoked_ids = list(NodeIdentity.objects.filter(subscription__entitlement=record,
                       revoked_at__isnull=False).values_list('pk', flat=True))
    lines = list(record.lines.select_related('ingress').all())
    affected_ids = set(active_ids) | set(revoked_ids) | set(job.payload.get('suspend_ids', []))
    identities = list(NodeIdentity.objects.filter(pk__in=affected_ids).select_related('ingress'))
    protocols = {ingress.protocol for line in lines for ingress in line.all_ingresses()}
    protocols.update(identity.ingress.protocol for identity in identities)
    plan = {'job': str(job.public_id), 'lease': str(job.lease_id), 'kind': job.kind,
            'entitlement': record.pk, 'revision': record.revision, 'device_generation': job.revision,
            'server_fences': job.payload.get('_server_fences', {}),
            'quota_bytes': record.quota_bytes, 'enabled': record.enabled,
            'activated_at': activated_at.isoformat(), 'expires_at': planned_expiry.isoformat(),
            'line_ids': sorted(x.pk for x in lines), 'protocols': sorted(protocols),
            'identity_ingresses': {str(identity.pk): {'ingress': identity.ingress_id,
                'server': identity.ingress.server_id, 'protocol': identity.ingress.protocol,
                'line': identity.line_id} for identity in identities},
            'activate_ids': sorted(active_ids), 'revoke_ids': sorted(revoked_ids),
            'suspend_ids': sorted(job.payload.get('suspend_ids', []))
                if job.kind == 'enforce' or (job.kind == 'assign' and not record.enabled) else []}
    plan['digest'] = fingerprint(plan)
    return plan


class AdapterUnavailable(Exception):
    pass


def run_job(public_id, adapter=None):
    """adapter.apply(plan) 返回核验回执；没有适配器时明确 BLOCKED。"""
    job = claim_job(public_id)
    if job is None:
        return DeploymentJob.objects.get(public_id=public_id)
    try:
        plan = build_plan(job)
        if adapter is None:
            raise AdapterUnavailable('PRODUCTION_ADAPTER_NOT_CONFIGURED')
        scope = getattr(adapter, 'evidence_scope', None)
        if scope not in {'isolated', 'production'}:
            raise AdapterUnavailable('ADAPTER_SCOPE_INVALID')
        if scope == 'production' and type(adapter) not in APPROVED_PRODUCTION_ADAPTERS:
            raise AdapterUnavailable('PRODUCTION_ADAPTER_NOT_APPROVED')
        if scope == 'production' and getattr(adapter, 'enforces_server_fences', False) is not True:
            raise AdapterUnavailable('ADAPTER_FENCING_NOT_VERIFIED')
        if scope == 'production' and not set(plan['protocols']).issubset(getattr(adapter, 'verified_protocols', set())):
            raise AdapterUnavailable('PROTOCOL_CAPABILITY_NOT_VERIFIED')
        if job.subscription_id and job.kind in {'create', 'reset', 'refresh'}:
            if job.entitlement.state not in {'active', 'simulated'} or not job.entitlement.enabled:
                raise AdapterUnavailable('ENTITLEMENT_NOT_APPLIED')
            if scope == 'production' and job.entitlement.state != 'active':
                raise AdapterUnavailable('ENTITLEMENT_NOT_APPLIED')
        receipt = adapter.apply(plan)
        if (not isinstance(receipt, dict) or receipt.get('digest') != plan['digest']
                or receipt.get('scope') != scope or receipt.get('verified') is not True
                or receipt.get('revision') != plan['revision']
                or sorted(receipt.get('active_ids', [])) != plan['activate_ids']
                or sorted(receipt.get('revoked_ids', [])) != plan['revoke_ids']
                or sorted(receipt.get('suspended_ids', [])) != plan['suspend_ids']
                or ((plan['revoke_ids'] or plan['suspend_ids']) and receipt.get('connections_terminated') is not True)):
            raise ValidationError('RECEIPT_VERIFICATION_FAILED')
        # 严格白名单保存核验字段，外部回执中即使夹带密钥也不写数据库或日志。
        safe_receipt = {k: receipt.get(k) for k in ('digest', 'scope', 'verified', 'revision',
                         'active_ids', 'revoked_ids', 'suspended_ids', 'connections_terminated')}
        if receipt.get('impact') in {'all_fixture_connections_restarted', 'affected_identity_connections_terminated',
                                      'all_core_connections_restarted', 'no_connection_interruption'}:
            safe_receipt['impact'] = receipt['impact']
        if isinstance(receipt.get('core_sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', receipt['core_sha256']):
            safe_receipt['core_sha256'] = receipt['core_sha256']
        # 仅保存真假值，不保存来自外部适配器的任意说明正文。
        for field in ('metering_exact', 'production_ready'):
            if type(receipt.get(field)) is bool:
                safe_receipt[field] = receipt[field]
        return finish_job(job.pk, job.lease_id, plan, safe_receipt)
    except Exception as exc:
        code = str(exc) if isinstance(exc, AdapterUnavailable) else ('RECEIPT_VERIFICATION_FAILED' if isinstance(exc, ValidationError) else 'ADAPTER_EXECUTION_FAILED')
        with transaction.atomic():
            current = DeploymentJob.objects.select_for_update().get(pk=job.pk)
            if current.lease_id == job.lease_id and current.state == 'running':
                current.state = 'blocked' if isinstance(exc, AdapterUnavailable) else 'failed'
                current.result_code, current.finished_at = code[:80], timezone.now()
                current.save(update_fields=['state', 'result_code', 'finished_at'])
                _release_servers(current)
            return current


@transaction.atomic
def finish_job(pk, lease_id, plan, receipt):
    from .entitlements import add_months, current_cycle
    from .delivery import expected_identity_pairs
    job = DeploymentJob.objects.select_for_update().select_related('entitlement', 'subscription').get(pk=pk)
    now = timezone.now()
    if job.lease_id != lease_id or job.state != 'running' or not job.lease_until or job.lease_until < now:
        raise ValidationError('过期执行器回执不能覆盖新任务')
    for server_id, fence in plan['server_fences'].items():
        if not Server.objects.filter(pk=server_id, deployment_lease_id=lease_id,
                deployment_lease_until__gte=now, deployment_fence=fence).exists():
            raise ValidationError('服务器发布租约已改变，拒绝旧回执')
    record = Entitlement.objects.select_for_update().get(pk=job.entitlement_id)
    job.entitlement = record
    if _superseded(job):
        job.state, job.result_code, job.finished_at = 'superseded', 'NEWER_REVISION_EXISTS', now
        job.save(update_fields=['state', 'result_code', 'finished_at'])
        _release_servers(job)
        return job
    if job.subscription_id and job.kind in {'create', 'reset', 'refresh'}:
        current = set(job.subscription.identities.filter(generation=job.revision,
            revoked_at__isnull=True).values_list('pk', flat=True))
        planned_pairs = {(details['line'], details['ingress'])
            for key, details in plan['identity_ingresses'].items() if int(key) in plan['activate_ids']}
        if current != set(plan['activate_ids']) or planned_pairs != expected_identity_pairs(job.subscription):
            # 内容更新沿用令牌和身份代际；仍须拒绝线路/协议集合变化前的迟到回执。
            job.state, job.result_code, job.finished_at = 'superseded', 'NEWER_REVISION_EXISTS', now
            job.save(update_fields=['state', 'result_code', 'finished_at'])
            _release_servers(job)
            return job
    # 即使执行期间发生撤销，也不能按旧快照激活已吊销的身份。
    if NodeIdentity.objects.filter(pk__in=plan['activate_ids'], revoked_at__isnull=False).exists():
        raise ValidationError('已撤销身份不能被重新启用')
    simulated = receipt['scope'] == 'isolated'
    NodeIdentity.objects.filter(pk__in=plan['revoke_ids']).update(state='revoked')
    NodeIdentity.objects.filter(pk__in=plan['suspend_ids'], revoked_at__isnull=True).update(state='suspended')
    NodeIdentity.objects.filter(pk__in=plan['activate_ids'], revoked_at__isnull=True).update(state='simulated' if simulated else 'active')
    if job.kind == 'assign':
        if record.enabled:
            record.activated_at = datetime.fromisoformat(plan['activated_at'])
            record.expires_at = datetime.fromisoformat(plan['expires_at'])
            record.state = 'simulated' if simulated else 'active'
            record.suspension_reason = ''
            record.enforcement_epoch += 1
            record.applied_revision = record.revision
            record.applied_snapshot = {'quota_bytes': record.quota_bytes,
                'line_ids': list(record.lines.values_list('pk', flat=True)), 'reset_day': record.reset_day,
                'reset_hour': record.reset_hour, 'reset_minute': record.reset_minute,
                'service_months': record.service_months, 'enabled': record.enabled,
                'expires_at': record.expires_at.isoformat(), 'evidence_scope': receipt['scope']}
            record.save()
            current_cycle(record, now)
            for sub in record.subscriptions.exclude(state='disabled'):
                required = expected_identity_pairs(sub)
                existing = set(sub.identities.filter(generation=sub.generation, revoked_at__isnull=True,
                         state='simulated' if simulated else 'active').values_list('line_id', 'ingress_id'))
                if required == existing and sub.applied_generation == sub.generation:
                    sub.state = 'simulated' if simulated else 'active'
                    sub.save(update_fields=['state'])
        else:
            record.state = 'disabled'
            record.applied_revision = record.revision
            record.save(update_fields=['state', 'applied_revision'])
            record.subscriptions.exclude(state__in=['disabled', 'disabling']).update(state='blocked')
    elif job.kind in {'create', 'reset', 'refresh'}:
        NodeIdentity.objects.filter(pk__in=plan['activate_ids'], revoked_at__isnull=True).update(state='simulated' if simulated else 'active')
        sub = job.subscription
        sub.state, sub.applied_generation = ('simulated' if simulated else 'active'), sub.generation
        sub.save(update_fields=['state', 'applied_generation'])
    elif job.kind == 'disable':
        job.subscription.state = 'disabled'
        job.subscription.save(update_fields=['state'])
    elif job.kind == 'enforce':
        record.state = 'suspended'
        record.save(update_fields=['state'])
    elif job.kind == 'resume':
        record.state, record.suspension_reason = ('simulated' if simulated else 'active'), ''
        record.enforcement_epoch += 1
        record.save(update_fields=['state', 'suspension_reason', 'enforcement_epoch'])
        for sub in record.subscriptions.filter(state='blocked'):
            required = expected_identity_pairs(sub)
            actual = set(sub.identities.filter(generation=sub.generation, revoked_at__isnull=True,
                state='simulated' if simulated else 'active').values_list('line_id', 'ingress_id'))
            if required and required == actual and sub.generation == sub.applied_generation:
                sub.state = 'simulated' if simulated else 'active'
                sub.save(update_fields=['state'])
    job.state, job.result_code, job.receipt, job.finished_at = ('simulated' if simulated else 'applied'), 'VERIFIED', receipt, now
    job.save(update_fields=['state', 'result_code', 'receipt', 'finished_at'])
    _release_servers(job)
    AuditEvent.objects.create(actor=job.actor, action='phase1_receipt', subject=str(job.public_id),
                              result='SIMULATED' if simulated else 'APPLIED')
    return job
