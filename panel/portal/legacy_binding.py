"""本地管理员核验旧会员映射；查询仅投影已核验且未失效的关系。"""
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q
from django.db.models.functions import Length
from django.utils import timezone

from .models import Entitlement, LegacyServiceBinding, Membership, SubscriptionGrant


def legacy_service_memberships():
    """注册生成的零额度、无授权待开通记录不构成服务。"""
    return Membership.objects.annotate(has_grants=Exists(
        SubscriptionGrant.objects.filter(membership_id=OuterRef('pk')))).filter(
            Q(quota_bytes__gt=0) | Q(has_grants=True) | ~Q(status='pending') | Q(expires_at__isnull=False))


def verified_legacy_bindings():
    """核验字段和当前归属都从数据库重新检查；坏绑定不允许降级为独立服务。"""
    return LegacyServiceBinding.objects.select_related('membership__user', 'entitlement__user').annotate(
        evidence_length=Length('evidence_sha256')).filter(
            state='verified', verified_at__isnull=False, verified_by__is_active=True,
            verified_by__is_staff=True, evidence_length=64, evidence_sha256__regex=r'^[a-f0-9]{64}$',
            revision__gte=1, membership_revision=F('membership__revision'),
            owner_id=F('membership__user_id'), membership_id__in=legacy_service_memberships().values('pk'),
        ).filter(Q(entitlement__isnull=True) | Q(entitlement__user_id=F('owner_id')))


def _local_administrator(actor):
    if settings.PANEL_LIVE:
        raise PermissionDenied('此映射操作仅允许在本地隔离环境执行。')
    if not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        raise PermissionDenied('此映射操作仅限已登录管理员。')


@transaction.atomic
def verify_legacy_binding(actor, membership_id, *, evidence_sha256, entitlement_id=None, expected_revision=0):
    """显式确认独立旧服务或合并目标；不创建权益、账期、身份或发布任务。"""
    _local_administrator(actor)
    try:
        member = Membership.objects.select_for_update().select_related('user').get(pk=membership_id)
    except Membership.DoesNotExist:
        raise ValidationError('旧会员不存在。') from None
    target = None
    if entitlement_id is not None:
        try:
            target = Entitlement.objects.select_for_update().get(pk=entitlement_id)
        except Entitlement.DoesNotExist:
            raise ValidationError('目标权益不存在。') from None
    binding = LegacyServiceBinding.objects.select_for_update().filter(membership=member).first()
    revision = binding.revision if binding else 0
    if type(expected_revision) is not int or expected_revision != revision:
        raise ValidationError('绑定已被修改，请重新读取后再核验。')
    if binding is None:
        binding = LegacyServiceBinding(membership=member)
    binding.owner_id = member.user_id
    binding.entitlement = target
    binding.evidence_sha256 = evidence_sha256
    binding.membership_revision = member.revision
    binding.revision = revision + 1
    binding.state = 'verified'
    binding.verified_by = actor
    binding.verified_at = timezone.now()
    binding.full_clean()
    binding.save()
    return binding


@transaction.atomic
def revoke_legacy_binding(actor, membership_id, *, expected_revision):
    """撤销映射只关闭旧服务投影，不撤销核心身份或更改目标权益。"""
    _local_administrator(actor)
    try:
        member = Membership.objects.select_for_update().get(pk=membership_id)
        binding = LegacyServiceBinding.objects.select_for_update().get(membership=member)
    except (Membership.DoesNotExist, LegacyServiceBinding.DoesNotExist):
        raise ValidationError('绑定不存在。') from None
    if type(expected_revision) is not int or binding.revision != expected_revision:
        raise ValidationError('绑定已被修改，请重新读取后再撤销。')
    binding.state = 'revoked'
    binding.revision += 1
    binding.full_clean()
    binding.save(update_fields=['state', 'revision', 'updated_at'])
    return binding
