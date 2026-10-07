"""管理员工作区只读查询；不把已登记对象当作在线观测。"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from .api import endpoint, error, page_options, success
from .api_helpers import iso, legacy_services, project_service, service_queryset, visible_legacy_services
from .legacy_binding import verified_legacy_bindings
from .models import Entitlement
from .service_projection import owner_compatibility, project_index, service_index


def users_query():
    unresolved = legacy_services().filter(user_id=OuterRef('pk')).exclude(
        pk__in=verified_legacy_bindings().values('membership_id')).filter(
            Q(legacy_binding__isnull=False) | Q(user_id__in=Entitlement.objects.values('user_id')))
    return get_user_model().objects.annotate(
        has_service=Exists(service_queryset().filter(user_id=OuterRef('pk'))),
        has_legacy=Exists(visible_legacy_services().filter(user_id=OuterRef('pk'))),
        unresolved_legacy=Exists(unresolved),
    )


def service_counts():
    """只汇总规范索引，不构造全站服务详情或读取任何来源文件。"""
    counts = {}
    from .service_projection import service_queries
    for kind, query in service_queries().items():
        key = 'owner_id' if kind == 'p8' else 'user_id'
        for row in query.order_by().values(key).annotate(total=Count('pk')).iterator(chunk_size=250):
            counts[row[key]] = counts.get(row[key], 0) + row['total']
    return counts


def user_projection(user, counts=None):
    count = counts.get(user.pk, 0) if counts is not None else service_index(user).count()
    return {'id': str(user.pk), 'username': user.get_username(), 'is_active': user.is_active,
            'is_staff': user.is_staff, 'service_count': count,
            'mapping_required': owner_compatibility(user)['state'] == 'mapping_required'}


@endpoint(staff=True)
def resource_usage(request):
    """仅管理员资源区读取已采集采购统计，不把它作为套餐额度返回。"""
    from .service_projection import p8_records, valid_p8
    from .service_usage_source import ServiceUsageSource
    source = ServiceUsageSource(request.user)
    items = []
    for record in p8_records().filter(owner=request.user):
        if valid_p8(record):
            value = source.read(record, 'p8', {'state': 'verified'})
            if value is not None:
                items.append({'service_id': str(record.public_id), 'provider_usage': value})
    return success({'items': items})


@endpoint(staff=True)
def users(request):
    options, failure = page_options(request)
    if failure is not None:
        return failure
    q = request.GET.get('q', '').strip()
    state = request.GET.get('status', 'all')
    service = request.GET.get('service', 'all')
    if len(q) > 150 or state not in ('all', 'active', 'disabled') or service not in ('all', 'with', 'none'):
        return error('invalid_filter', '搜索或账号状态筛选无效。', 422)
    query = users_query()
    if q:
        query = query.filter(username__icontains=q)
    if state != 'all':
        query = query.filter(is_active=state == 'active')
    counts = service_counts()
    if service == 'with':
        query = query.filter(pk__in=counts)
    elif service == 'none':
        query = query.exclude(pk__in=counts)
    paginator = Paginator(query.order_by('username', 'pk'), options[1])
    try:
        page = paginator.page(options[0])
    except EmptyPage:
        return error('page_not_found', '此页不存在，请返回前一页。', 404)
    return success({'items': [user_projection(row, counts) for row in page],
                    'pagination': {'page': page.number, 'page_size': options[1], 'total': paginator.count,
                                   'pages': paginator.num_pages, 'has_next': page.has_next(),
                                   'has_previous': page.has_previous()}})


@endpoint(staff=True)
def user_detail(request, user_id):
    if not user_id.isascii() or not user_id.isdecimal() or len(user_id) > 18:
        return error('not_found', '用户不存在或不可访问。', 404)
    user = users_query().filter(pk=int(user_id)).first()
    if user is None:
        return error('not_found', '用户不存在或不可访问。', 404)
    services = project_index(service_index(user), administrator=True, viewer=request.user)
    return success({'user': user_projection(user), 'services': services,
                    'capabilities': {'create': False, 'quota': False, 'renew': False,
                                     'grants': False, 'enable': False}})


@endpoint(staff=True)
def overview(request):
    now = timezone.now()
    accounts = users_query()
    services = service_queryset()
    count = service_index().count()
    expired = service_index(state='expired').count()
    from .service_projection import p8_records, valid_p8
    unresolved_ids = set(accounts.filter(unresolved_legacy=True).values_list('pk', flat=True))
    unresolved_ids.update(record.owner_id for record in p8_records().iterator(chunk_size=100) if not valid_p8(record))
    unresolved_count = len(unresolved_ids)
    return success({
        'users': {'total': accounts.count(), 'active': accounts.filter(is_active=True).count(),
                  'disabled': accounts.filter(is_active=False).count()},
        'services': {'total': count,
                     'expired': expired,
                     'mapping_required': unresolved_count,
                     'statistics_incomplete': None,
                     'recorded_metering_gaps': services.filter(metering_gap=True).count()},
        'generated_at': iso(now),
        'environment': {'kind': 'production' if settings.PANEL_LIVE else 'local_candidate',
                        'is_demo': bool(getattr(settings, 'CANDIDATE_DEMO_DATA', False))},
        'limitations': ['这是当前数据库中的账号与服务概览，不代表服务器在线状态。',
                        '已记录计量缺口不包含所有尚未发现的漏采；完整统计覆盖尚未汇总。',
                        '服务器流量、供应商账单和套餐扣费尚未在此汇总。'],
    })
