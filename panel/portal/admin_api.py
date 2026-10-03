"""管理员工作区只读查询；不把已登记对象当作在线观测。"""
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.paginator import EmptyPage, Paginator
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .api import endpoint, error, page_options, success
from .api_helpers import iso, legacy_services, project_service, service_queryset, visible_legacy_services
from .legacy_binding import verified_legacy_bindings
from .models import Entitlement


def users_query():
    unresolved = legacy_services().filter(user_id=OuterRef('pk')).exclude(
        pk__in=verified_legacy_bindings().values('membership_id')).filter(
            Q(legacy_binding__isnull=False) | Q(user_id__in=Entitlement.objects.values('user_id')))
    return get_user_model().objects.annotate(
        has_service=Exists(service_queryset().filter(user_id=OuterRef('pk'))),
        has_legacy=Exists(visible_legacy_services().filter(user_id=OuterRef('pk'))),
        unresolved_legacy=Exists(unresolved),
    )


def user_projection(user):
    return {'id': str(user.pk), 'username': user.get_username(), 'is_active': user.is_active,
            'is_staff': user.is_staff, 'service_count': int(user.has_service) + int(user.has_legacy),
            'mapping_required': user.unresolved_legacy}


@endpoint(staff=True)
def users(request):
    options, failure = page_options(request)
    if failure is not None:
        return failure
    q = request.GET.get('q', '').strip()
    state = request.GET.get('status', 'all')
    if len(q) > 150 or state not in ('all', 'active', 'disabled'):
        return error('invalid_filter', '搜索或账号状态筛选无效。', 422)
    query = users_query()
    if q:
        query = query.filter(username__icontains=q)
    if state != 'all':
        query = query.filter(is_active=state == 'active')
    paginator = Paginator(query.order_by('username', 'pk'), options[1])
    try:
        page = paginator.page(options[0])
    except EmptyPage:
        return error('page_not_found', '此页不存在，请返回前一页。', 404)
    return success({'items': [user_projection(row) for row in page],
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
    services = []
    for query, kind in ((service_queryset(), 'entitlement'), (visible_legacy_services(), 'membership')):
        for record in query.filter(user=user).select_related('user'):
            data = project_service(record, kind, administrator=True)
            data['user'] = {'username': user.get_username()}
            services.append(data)
    return success({'user': user_projection(user), 'services': services,
                    'capabilities': {'create': False, 'quota': False, 'renew': False,
                                     'grants': False, 'enable': False}})


@endpoint(staff=True)
def overview(request):
    now = timezone.now()
    accounts = users_query()
    services, legacy = service_queryset(), visible_legacy_services()
    return success({
        'users': {'total': accounts.count(), 'active': accounts.filter(is_active=True).count(),
                  'disabled': accounts.filter(is_active=False).count()},
        'services': {'total': services.count() + legacy.count(),
                     'expired': services.filter(expires_at__lte=now).count() + legacy.filter(expires_at__lte=now).count(),
                     'mapping_required': accounts.filter(unresolved_legacy=True).count(),
                     'statistics_incomplete': None,
                     'recorded_metering_gaps': services.filter(metering_gap=True).count()},
        'generated_at': iso(now),
        'environment': {'kind': 'production' if settings.PANEL_LIVE else 'local_candidate',
                        'is_demo': bool(getattr(settings, 'CANDIDATE_DEMO_DATA', False))},
        'limitations': ['这是当前数据库中的账号与服务概览，不代表服务器在线状态。',
                        '已记录计量缺口不包含所有尚未发现的漏采；完整统计覆盖尚未汇总。',
                        '服务器流量、供应商账单和套餐扣费尚未在此汇总。'],
    })
