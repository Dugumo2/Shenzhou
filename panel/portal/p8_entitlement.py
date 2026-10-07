"""P8 与既有套餐的本地显式核验；不读取链接或修改任何套餐事实。"""
import hashlib
import json
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (DeviceSubscription, Entitlement, LegacyServiceBinding, Membership,
                     P8EntitlementBinding, P8SourceBinding)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def mapping_snapshot(source, target):
    """只冻结归属和原来源资格；套餐正常调额/计费不使关联失效。"""
    member = source.legacy_membership
    return {'p8_id': source.pk, 'public_id': str(source.public_id), 'owner_id': source.owner_id,
            'source_revision': source.revision, 'source_verification_sha256': source.verification_sha256,
            'membership_id': source.legacy_membership_id,
            'membership_revision': member.revision if member else None,
            'membership_public_id': str(member.public_id) if member else None,
            'membership_owner_id': member.user_id if member else None,
            'entitlement_id': target.pk, 'entitlement_public_id': str(target.public_id),
            'entitlement_owner_id': target.user_id}


def _snapshot_valid(value):
    """先核对封闭结构，再将历史快照中的主键用于查询；布尔值不是整数ID。"""
    from .p8_compat import DIGEST
    fields = {'p8_id', 'public_id', 'owner_id', 'source_revision', 'source_verification_sha256',
              'membership_id', 'membership_revision', 'membership_public_id', 'membership_owner_id',
              'entitlement_id', 'entitlement_public_id', 'entitlement_owner_id'}
    if type(value) is not dict or value.keys() != fields:
        return False

    def positive_integer(item, maximum=2**63 - 1):
        return type(item) is int and 0 < item <= maximum

    def canonical_uuid(item):
        if type(item) is not str or len(item) != 36:
            return False
        try:
            return str(UUID(item)) == item
        except ValueError:
            return False

    if (not all(positive_integer(value[key]) for key in (
            'p8_id', 'owner_id', 'entitlement_id', 'entitlement_owner_id'))
            or not positive_integer(value['source_revision'], 2**31 - 1)
            or not all(canonical_uuid(value[key]) for key in ('public_id', 'entitlement_public_id'))
            or type(value['source_verification_sha256']) is not str
            or not DIGEST.fullmatch(value['source_verification_sha256'])
            or value['owner_id'] != value['entitlement_owner_id']
            or value['public_id'] != value['entitlement_public_id']):
        return False
    member_fields = ('membership_id', 'membership_revision', 'membership_public_id', 'membership_owner_id')
    if value['membership_id'] is None:
        return all(value[key] is None for key in member_fields)
    return (positive_integer(value['membership_id'])
            and positive_integer(value['membership_revision'], 2**31 - 1)
            and positive_integer(value['membership_owner_id'])
            and canonical_uuid(value['membership_public_id'])
            and value['membership_owner_id'] == value['owner_id'])


def mapping_verification_sha256(binding):
    return _digest({'p8_id': binding.p8_binding_id, 'entitlement_id': binding.entitlement_id,
                    'owner_id': binding.owner_id, 'revision': binding.revision,
                    'snapshot_sha256': binding.snapshot_sha256, 'evidence_sha256': binding.evidence_sha256,
                    'verified_by_id': binding.verified_by_id,
                    'verified_at': binding.verified_at.isoformat() if binding.verified_at else None})


def _source_and_target_valid(source, target):
    from .p8_compat import _source_config
    now = timezone.now()
    if (getattr(settings, 'P8_COMPAT_ENABLED', False) is not True or not source.enabled
            or source.state != 'verified' or source.revision < 1 or source.verified_at is None
            or source.verified_at > now or source.verified_by_id is None
            or not get_user_model().objects.filter(pk=source.verified_by_id, is_active=True, is_staff=True).exists()
            or not get_user_model().objects.filter(pk=source.owner_id, is_active=True).exists()
            or target.user_id != source.owner_id or target.public_id != source.public_id
            or _source_config(source) is None):
        return False
    member = source.legacy_membership
    if member and (member.user_id != source.owner_id
                   or LegacyServiceBinding.objects.filter(membership_id=member.pk).exists()):
        return False
    return not (Membership.objects.filter(public_id=source.public_id).exists()
                or DeviceSubscription.objects.filter(public_id=source.public_id).exists())


def valid_mapping(binding):
    from .p8_compat import DIGEST
    if (not _snapshot_valid(binding.snapshot)
            or binding.state != 'verified' or binding.revision < 1 or binding.verified_at is None
            or binding.verified_at > timezone.now() or binding.verified_by_id is None
            or not get_user_model().objects.filter(pk=binding.verified_by_id, is_active=True, is_staff=True).exists()
            or not DIGEST.fullmatch(binding.evidence_sha256 or '')
            or binding.owner_id != binding.p8_binding.owner_id
            or binding.snapshot_sha256 != _digest(binding.snapshot)
            or binding.verification_sha256 != mapping_verification_sha256(binding)):
        return False
    source, target = binding.p8_binding, binding.entitlement
    return (_source_and_target_valid(source, target)
            and binding.snapshot == mapping_snapshot(source, target))


def mappings():
    return P8EntitlementBinding.objects.select_related(
        'p8_binding__legacy_membership', 'entitlement__user', 'verified_by')


def current_entitlement(source):
    """每次读取当前归属，不能信任跨请求缓存的关联对象。"""
    binding = mappings().filter(p8_binding_id=source.pk).first()
    return binding.entitlement if binding is not None and valid_mapping(binding) else None


def claimed_entitlement_ids():
    """坏映射不恢复成第二份服务；跨用户错误引用不能隐藏他人套餐。"""
    result = []
    for binding in mappings():
        source, target = binding.p8_binding, binding.entitlement
        current_same = (binding.owner_id == target.user_id == source.owner_id
                        and source.public_id == target.public_id)
        if current_same:
            result.append(target.pk)
        old = binding.snapshot
        if not _snapshot_valid(old):
            continue
        # 被篡改的当前FK不能使原套餐退回独立服务；核对原回执证明的目标。
        original_receipt = _digest({'p8_id': old.get('p8_id'),
            'entitlement_id': old.get('entitlement_id'), 'owner_id': old.get('owner_id'),
            'revision': binding.revision, 'snapshot_sha256': binding.snapshot_sha256,
            'evidence_sha256': binding.evidence_sha256, 'verified_by_id': binding.verified_by_id,
            'verified_at': binding.verified_at.isoformat() if binding.verified_at else None})
        historical_same = (binding.snapshot_sha256 == _digest(old)
            and binding.verification_sha256 == original_receipt
            and old.get('owner_id') == old.get('entitlement_owner_id')
            and old.get('p8_id') == source.pk
            and old.get('public_id') == old.get('entitlement_public_id'))
        if historical_same and Entitlement.objects.filter(pk=old.get('entitlement_id')).exists():
            result.append(old['entitlement_id'])
    return result


def _administrator(actor, *, write=False):
    if write and getattr(settings, 'PANEL_LIVE', True) is not False:
        raise PermissionDenied('此映射操作仅允许在本地隔离环境执行。')
    if not getattr(actor, 'is_authenticated', False):
        raise PermissionDenied('此映射操作仅限已登录管理员。')
    query = get_user_model().objects
    if write:
        query = query.select_for_update()
    current = query.filter(pk=actor.pk, is_active=True, is_staff=True).first()
    if current is None:
        raise PermissionDenied('此映射操作仅限已登录管理员。')
    return current


def _context(p8_public_id, entitlement_id, expected_revision, *, write=False,
             replay_actor=None, replay_evidence=None):
    source_query, target_query, binding_query = P8SourceBinding.objects, Entitlement.objects, mappings()
    if write:
        source_query, target_query, binding_query = (source_query.select_for_update(),
            target_query.select_for_update(), binding_query.select_for_update())
    try:
        source = source_query.select_related('legacy_membership').get(public_id=p8_public_id)
        target = target_query.get(pk=entitlement_id)
    except (P8SourceBinding.DoesNotExist, Entitlement.DoesNotExist, ValueError, TypeError, ValidationError):
        raise ValidationError('原服务或目标套餐不存在。') from None
    if not _source_and_target_valid(source, target):
        raise ValidationError('来源资格、服务标识或套餐归属冲突，请重新核对。')
    binding = binding_query.filter(p8_binding=source).first()
    revision = binding.revision if binding else 0
    # 仅首次核验的原请求允许旧版本重放；撤销、漂移或换操作者均不能借重放恢复。
    same_creation = (write and type(expected_revision) is int and expected_revision == 0
        and binding is not None and binding.revision == 1 and binding.entitlement_id == target.pk
        and replay_actor is not None and binding.verified_by_id == replay_actor.pk
        and binding.evidence_sha256 == replay_evidence and valid_mapping(binding))
    if type(expected_revision) is not int or (expected_revision != revision and not same_creation):
        raise ValidationError('绑定已被修改，请重新读取后再核验。')
    if ((binding is not None and (binding.entitlement_id != target.pk or not valid_mapping(binding)))
            or P8EntitlementBinding.objects.filter(entitlement=target).exclude(p8_binding=source).exists()):
        raise ValidationError('已有绑定冲突或失效，请单独核对，不自动重绑。')
    return source, target, binding


def preview_p8_entitlement(actor, p8_public_id, entitlement_id, expected_revision=0):
    """只读白名单预览；允许生产核对元数据，不触碰秘密或来源文件。"""
    _administrator(actor)
    source, target, binding = _context(p8_public_id, entitlement_id, expected_revision)
    return {'p8_public_id': str(source.public_id), 'entitlement_id': target.pk,
            'owner_id': source.owner_id, 'revision': binding.revision if binding else 0,
            'state': binding.state if binding else 'unverified',
            'snapshot_sha256': _digest(mapping_snapshot(source, target)), 'changes': []}


@transaction.atomic
def verify_p8_entitlement(actor, p8_public_id, entitlement_id, *, expected_revision=0, evidence_sha256):
    """仅原子新增映射；相同确认幂等，不新建权益、账期或变更原交付。"""
    from .p8_compat import DIGEST
    actor = _administrator(actor, write=True)
    if type(evidence_sha256) is not str or not DIGEST.fullmatch(evidence_sha256):
        raise ValidationError('必须提供64位小写SHA256摘要。')
    source, target, binding = _context(p8_public_id, entitlement_id, expected_revision, write=True,
                                       replay_actor=actor, replay_evidence=evidence_sha256)
    if binding is not None:
        if binding.evidence_sha256 != evidence_sha256:
            raise ValidationError('核验依据冲突，不能覆盖原确认。')
        return binding
    snapshot = mapping_snapshot(source, target)
    binding = P8EntitlementBinding(p8_binding=source, entitlement=target, owner_id=source.owner_id,
        state='verified', revision=1, snapshot=snapshot, snapshot_sha256=_digest(snapshot),
        evidence_sha256=evidence_sha256, verified_by=actor, verified_at=timezone.now())
    binding.verification_sha256 = mapping_verification_sha256(binding)
    binding.full_clean()
    try:
        with transaction.atomic():
            binding.save()
    except IntegrityError:
        raise ValidationError('绑定已被修改，请重新读取后再核验。') from None
    return binding


@transaction.atomic
def revoke_p8_entitlement(actor, p8_public_id, *, expected_revision):
    """撤销只关闭本映射，不轮换旧链接或改动核心和套餐。"""
    _administrator(actor, write=True)
    binding = mappings().select_for_update().filter(p8_binding__public_id=p8_public_id).first()
    if binding is None or type(expected_revision) is not int or binding.revision != expected_revision:
        raise ValidationError('绑定不存在或已被修改。')
    if not valid_mapping(binding):
        raise ValidationError('绑定已经失效，请单独核对，不覆盖原核验回执。')
    binding.state = 'revoked'
    binding.revision += 1
    binding.verification_sha256 = mapping_verification_sha256(binding)
    binding.save(update_fields=['state', 'revision', 'verification_sha256', 'updated_at'])
    return binding
