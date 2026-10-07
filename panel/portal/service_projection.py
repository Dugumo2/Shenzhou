"""统一三类服务的只读索引、归属、去重和操作目标；永不读取来源秘密。"""
from uuid import UUID

from django.conf import settings
from django.db.models import CharField, F, Q, Value
from django.utils import timezone

from . import p8_compat
from .api_helpers import (compatibility_for, project_service, service_business_status,
                          service_facts, service_queryset, visible_legacy_services)
from .legacy_binding import verified_legacy_bindings
from .models import DeviceSubscription, Entitlement, Membership, P8SourceBinding
from .p8_entitlement import claimed_entitlement_ids, current_entitlement


def p8_records():
    if getattr(settings, 'P8_COMPAT_ENABLED', False) is not True:
        return P8SourceBinding.objects.none()
    return P8SourceBinding.objects.select_related('owner', 'legacy_membership', 'verified_by')


def claimed_memberships():
    return p8_records().filter(legacy_membership__user_id=F('owner_id'))


def service_queries(owner=None):
    """相同显式来源只计一次；相撞UUID不形成可访问P8服务。"""
    claimed_ids = claimed_entitlement_ids()
    p8 = p8_records().exclude(public_id__in=Entitlement.objects.exclude(pk__in=claimed_ids).values('public_id')).exclude(
        public_id__in=Membership.objects.values('public_id')).exclude(
        public_id__in=DeviceSubscription.objects.values('public_id'))
    members = visible_legacy_services().exclude(pk__in=p8.filter(
        legacy_membership__user_id=F('owner_id')).values('legacy_membership_id'))
    queries = {'entitlement': service_queryset().exclude(pk__in=claimed_ids),
               'membership': members.select_related('user'), 'p8': p8}
    if owner is not None:
        queries = {kind: query.filter(**{'owner_id' if kind == 'p8' else 'user_id': owner.pk})
                   for kind, query in queries.items()}
    return queries


def unresolved(item):
    for key in ('quota_bytes', 'used_bytes', 'raw_bytes', 'remaining_bytes', 'next_reset_at', 'expires_at'):
        item[key] = None
    item.update(state='mapping_required', business_state='mapping_required', enabled=False,
                status_label='资料待核对', quota_state='unknown')
    item['usage'] = {'quality': 'unknown', 'updated_at': None, 'message': '来源关联需要重新核对，当前额度与用量暂不可确认。'}
    item['delivery'] = {'state': 'blocked', 'message': '请联系管理员核对服务资料。', 'download_url': None}
    item['application'] = {'state': 'verification_required', 'desired_revision': None, 'applied_revision': None}
    item['compatibility'] = {'state': 'mapping_required', 'message': '来源关联需要重新核对。'}
    item['actions'] = {key: False for key in ('billing', 'quota', 'renew', 'grants', 'enable', 'reset')}
    for client in item.get('clients', []):
        client['delivery'] = dict(item['delivery'])
    return item


def valid_p8(record):
    return p8_compat.eligible_bindings().filter(pk=record.pk).exists() and p8_compat._source_config(record) is not None


def project(record, kind, *, administrator=False, detail=False, now=None, viewer=None, usage_source=None):
    now = now or timezone.now()
    if kind == 'p8':
        item = p8_compat._project(record, detail=detail)
        if not valid_p8(record):
            unresolved(item)
        owner = record.owner
    else:
        item = project_service(record, kind, now=now, detail=detail, administrator=administrator)
        owner = record.user
        if kind == 'membership' and claimed_memberships().filter(legacy_membership_id=record.pk).exists():
            unresolved(item)
    # 权限只决定是否显示可提交入口，写API仍独立核验最新状态。
    billing = False
    billing_target = record if kind == 'entitlement' else current_entitlement(record) if kind == 'p8' else None
    if administrator and billing_target is not None and item['state'] != 'mapping_required':
        from .billing_schedule import _context
        billing = not _context(billing_target, now)[-1]
    item['actions']['billing'] = billing
    item['capabilities'] = dict(item['actions'], p8_delivery=(kind == 'p8' and not administrator
        and item['state'] != 'mapping_required'))
    item['operation_targets'] = {
        'billing': str(record.public_id) if billing else None,
        'p8_delivery': str(record.public_id) if item['capabilities']['p8_delivery'] else None,
    }
    if administrator:
        item['user'] = {'username': owner.get_username()}
        item.setdefault('compatibility', owner_compatibility(owner))
    # 套餐投影永不混入供应商账单；管理员资源使用独立权限端点读取。
    return item


def service_index(owner=None, *, q='', state='all', now=None):
    """数据库分页前统一来源索引；只投影派生状态筛选需要的候选。"""
    now = now or timezone.now()
    queries = service_queries(owner)
    indices = []
    for kind, query in queries.items():
        relation = 'owner' if kind == 'p8' else 'user'
        if q:
            search = Q(**{relation + '__username__icontains': q}) | Q(public_id__icontains=q.replace('-', ''))
            if kind == 'p8':
                search |= Q(legacy_membership__user_id=F('owner_id'), legacy_membership__public_id__icontains=q.replace('-', ''))
            elif kind == 'entitlement':
                search |= Q(pk__in=verified_legacy_bindings().filter(
                    membership__public_id__icontains=q.replace('-', '')).values('entitlement_id'))
            query = query.filter(search)
        if state == 'expired':
            # P8仅采用已核套餐期限；失效来源不借会员登记期限参与筛选。
            if kind == 'p8':
                matches = []
                for record in query.iterator(chunk_size=100):
                    target = current_entitlement(record) if valid_p8(record) else None
                    if target is not None and target.expires_at is not None and target.expires_at <= now:
                        matches.append(record.pk)
                query = query.filter(pk__in=matches)
            elif kind == 'membership':
                query = query.filter(expires_at__lte=now).exclude(pk__in=claimed_memberships().values('legacy_membership_id'))
            else:
                query = query.filter(expires_at__lte=now)
        elif state != 'all':
            matches = []
            if kind == 'entitlement' and state == 'mapping_required':
                query = query.none()
            elif kind == 'entitlement' and state == 'active':
                query = query.filter(state='active', enabled=True, user__is_active=True,
                    applied_revision=F('revision'), applied_revision__gt=0, expires_at__gt=now)
            for record in query.iterator(chunk_size=100):
                if kind == 'p8':
                    target = current_entitlement(record) if valid_p8(record) else None
                    if target is not None:
                        facts = service_facts(target, 'entitlement', now)
                        business = service_business_status(target, 'entitlement', facts, now)[0]
                        match = facts['quality'] == 'gap' if state == 'metering_gap' else business == state
                    else:
                        match = state == ('verification_required' if valid_p8(record) else 'mapping_required')
                elif kind == 'membership' and claimed_memberships().filter(legacy_membership_id=record.pk).exists():
                    match = state == 'mapping_required'
                else:
                    # 派生状态只核必要事实，分页前不构造服务详情、资源或操作能力。
                    facts = service_facts(record, kind, now)
                    business = service_business_status(record, kind, facts, now)[0]
                    match = facts['quality'] == 'gap' if state == 'metering_gap' else business == state
                    if kind == 'membership' and state not in ('active', 'metering_gap', 'mapping_required'):
                        match = match or facts['state'] == state
                if match:
                    matches.append(record.pk)
            query = query.filter(pk__in=matches)
        indices.append(query.order_by().annotate(
            sort_name=F(relation + '__username'), service_owner=F(relation + '_id'),
            source_type=Value(kind, output_field=CharField())).values(
                'pk', 'public_id', 'sort_name', 'service_owner', 'source_type'))
    return indices[0].union(*indices[1:]).order_by('sort_name', 'public_id', 'source_type')


def project_index(rows, *, administrator=False, detail=False, viewer=None):
    rows = list(rows)
    records = {}
    for kind, query in service_queries().items():
        records.update({(kind, record.pk): record for record in query.filter(
            pk__in=[row['pk'] for row in rows if row['source_type'] == kind])})
    result = []
    for row in rows:
        record = records.get((row['source_type'], row['pk']))
        actual_owner = (record.owner_id if row['source_type'] == 'p8' else record.user_id) if record else None
        if record is None or actual_owner != row['service_owner'] or record.public_id != row['public_id']:
            continue
        result.append(project(record, row['source_type'], administrator=administrator, detail=detail))
    return result


def owner_compatibility(owner):
    value = compatibility_for(owner.pk, Entitlement.objects.filter(user=owner).exists())
    if any(not valid_p8(record) for record in p8_records().filter(owner=owner)):
        return {'state': 'mapping_required', 'message': '部分服务资料需要管理员重新核对，暂不提供资源。'}
    # 已显式核验的P8会员别名属于同一套餐，不再要求另一份LegacyServiceBinding。
    from .legacy_binding import legacy_service_memberships
    members = legacy_service_memberships().filter(user=owner).exclude(
        pk__in=verified_legacy_bindings().values('membership_id'))
    mapped_members = [record.legacy_membership_id for record in p8_records().filter(owner=owner)
                      if current_entitlement(record) is not None]
    if value['state'] == 'mapping_required' and mapped_members and not members.exclude(pk__in=mapped_members).exists():
        return {'state': 'clear', 'message': None}
    return value


def resolve(owner, public_id):
    """只查指定本人；管理员元数据页面不能将此函数变成秘密读取授权。"""
    try:
        value = UUID(str(public_id))
    except (ValueError, TypeError, AttributeError):
        return None
    queries = service_queries(owner)
    for kind, query in queries.items():
        record = query.filter(public_id=value).first()
        if record is not None:
            return kind, record
    p8 = queries['p8'].filter(legacy_membership__user_id=owner.pk, legacy_membership__public_id=value).first()
    if p8 is not None:
        return 'p8', p8
    binding = verified_legacy_bindings().filter(owner=owner, membership__public_id=value,
                                                entitlement__isnull=False).first()
    if binding is not None:
        return 'entitlement', binding.entitlement
    return None
