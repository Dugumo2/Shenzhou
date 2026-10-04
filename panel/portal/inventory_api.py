"""管理员资源资料与受信观测投影；资料保存不触发运行控制或现场采集。"""
import uuid
import json

from django.core.paginator import EmptyPage, Paginator
from django.db.models import Count, Exists, IntegerField, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce

from .api import endpoint, error, page_options, success
from .api_helpers import iso
from .models import CheckResult, CoreInstance, Egress, Ingress, Line, Server, ServerObservation
from .inventory_service import InventoryError, edit_metadata, metadata_edit_enabled
from .observation_ingest import check_projection, freshness, observation_projection


ENDPOINT_LIMIT = 100
SERVER_FIELDS = ('id', 'public_id', 'name', 'notes', 'revision', 'enabled', 'adapter', 'last_seen_at')
INGRESS_FIELDS = ('id', 'name', 'protocol', 'enabled', 'server_id')
EGRESS_FIELDS = ('id', 'name', 'kind', 'fail_closed', 'server_id')
LIMITATIONS = [
    '仅查询当前数据库中的登记资源；登记启用不等于在线或已经应用。',
    '仅展示受信采集来源的独立回执；刷新列表不会采集或检测，过期回执不代表当前健康。',
    '现有入口与出口关联不代表完整上下游拓扑，不能证明 TCP、UDP、DNS 或失败关闭已验收。',
]


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _filters(request, *, with_server=False):
    options, failure = page_options(request)
    if failure is not None:
        return None, failure
    q = request.GET.get('q', '').strip()
    status = request.GET.get('status', 'all')
    if len(q) > 150 or status not in ('all', 'enabled', 'disabled'):
        return None, error('invalid_filter', '搜索或登记状态筛选无效。', 422)
    server_id = None
    if with_server and request.GET.get('server_id'):
        server_id = _uuid(request.GET['server_id'])
        if server_id is None:
            return None, error('invalid_filter', '关联服务器筛选无效。', 422)
    return {'options': options, 'q': q, 'status': status, 'server_id': server_id}, None


def _listing(query, options, project):
    paginator = Paginator(query.order_by('name', 'pk'), options[1])
    try:
        page = paginator.page(options[0])
    except EmptyPage:
        return error('page_not_found', '此页不存在，请返回前一页。', 404)
    return success({'items': [project(row) for row in page], 'read_only': not metadata_edit_enabled(),
                    'pagination': {'page': page.number, 'page_size': options[1], 'total': paginator.count,
                                   'pages': paginator.num_pages, 'has_next': page.has_next(),
                                   'has_previous': page.has_previous()}, 'limitations': LIMITATIONS})


def _count_for_server(model):
    # 分别聚合两类端点，避免入口与出口交叉连接产生膨胀。
    return Coalesce(Subquery(model.objects.filter(server_id=OuterRef('pk')).order_by()
                             .values('server_id').annotate(total=Count('pk')).values('total'),
                             output_field=IntegerField()), 0)


def _servers_query():
    return Server.objects.only(*SERVER_FIELDS).annotate(
        ingress_count=_count_for_server(Ingress), egress_count=_count_for_server(Egress)).prefetch_related(
            Prefetch('observations', queryset=ServerObservation.objects.order_by('-observed_at', '-pk')[:1], to_attr='latest_observations'))


def _server_ref(server):
    return {'id': str(server.public_id), 'name': server.name, 'enabled': server.enabled}


def _server_projection(server):
    row = next(iter(server.latest_observations), None)
    observation = observation_projection(row, server)
    monitoring = {'state': 'not_connected', 'online': None, 'observed_at': None}
    if observation:
        monitoring = {'state': observation['freshness'], 'online': observation['metrics'].get('online')
                      if observation['freshness'] == 'fresh' else None,
                      'observed_at': observation['observed_at'], 'expires_at': observation['expires_at'], 'source': observation['source']}
    return {**_server_ref(server), 'adapter': server.adapter, 'last_seen_at': iso(server.last_seen_at),
            'notes': server.notes, 'revision': server.revision,
            'ingress_count': server.ingress_count, 'egress_count': server.egress_count,
            'monitoring': monitoring}


def _ingresses_query():
    return Ingress.objects.select_related('server').only(
        *INGRESS_FIELDS, *('server__' + field for field in SERVER_FIELDS)).order_by('pk')


def _egresses_query():
    return Egress.objects.select_related('server').only(
        *EGRESS_FIELDS, *('server__' + field for field in SERVER_FIELDS)).order_by('pk')


def _ingress_projection(ingress):
    return {'id': str(ingress.pk), 'name': ingress.name, 'protocol': ingress.protocol,
            'protocol_label': ingress.get_protocol_display(), 'enabled': ingress.enabled,
            'server': _server_ref(ingress.server)}


def _egress_projection(egress):
    return {'id': str(egress.pk), 'name': egress.name, 'kind': egress.kind,
            'kind_label': egress.get_kind_display(), 'fail_closed': egress.fail_closed,
            'server': _server_ref(egress.server)}


def _endpoint_page(query, project, total):
    return {'items': [project(row) for row in query[:ENDPOINT_LIMIT]],
            'total': total, 'truncated': total > ENDPOINT_LIMIT}


@endpoint(staff=True)
def servers(request):
    filters, failure = _filters(request)
    if failure is not None:
        return failure
    query = _servers_query()
    if filters['q']:
        query = query.filter(Q(name__icontains=filters['q']) | Q(adapter__icontains=filters['q']))
    if filters['status'] != 'all':
        query = query.filter(enabled=filters['status'] == 'enabled')
    return _listing(query, filters['options'], _server_projection)


def _patch(request, kind, identifier):
    if request.content_type != 'application/json':
        return error('unsupported_media_type', '请使用 JSON 请求。', 415)
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        if len(request.body) > 8192:
            raise ValueError()
        body = json.loads(request.body, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        return error('invalid_json', '请求内容无效或超过大小限制。', 422)
    try:
        return success(edit_metadata(request.user, kind, identifier, body))
    except InventoryError as exc:
        return error(exc.code, exc.message, exc.status)


def _checks(query):
    rows = list(query.order_by('-observed_at', '-pk')[:101])
    return {'items': [check_projection(row) for row in rows[:100]], 'truncated': len(rows) > 100}


@endpoint(methods=('GET', 'PATCH'), staff=True)
def server_detail(request, public_id):
    identifier = _uuid(public_id)
    if identifier is None:
        return error('not_found', '服务器不存在或不可访问。', 404)
    if request.method == 'PATCH':
        return _patch(request, 'server', identifier)
    server = _servers_query().filter(public_id=identifier).first() if identifier is not None else None
    if server is None:
        return error('not_found', '服务器不存在或不可访问。', 404)
    observation = observation_projection(next(iter(server.latest_observations), None), server)
    cores = list(CoreInstance.objects.filter(server=server).prefetch_related('ingresses').order_by('pk')[:101])
    core_items = [{'id': str(core.public_id), 'name': core.name, 'instance_key': core.instance_key,
                   'core_type': core.core_type, 'registered_version': core.registered_version,
                   'configuration_owner': core.configuration_owner, 'configuration_version': core.configuration_version,
                   'revision': core.revision, 'notes': core.notes,
                   'ingress_ids': [str(entry.pk) for entry in core.ingresses.all() if entry.server_id == server.pk],
                   'association_valid': all(entry.server_id == server.pk for entry in core.ingresses.all())} for core in cores[:100]]
    return success({'server': _server_projection(server), 'read_only': not metadata_edit_enabled(),
                    'metrics': {'cpu_percent': None, 'memory_percent': None, 'disk_percent': None,
                                'upload_bps': None, 'download_bps': None, 'total_transfer_bytes': None},
                    'observation': observation,
                    'cores': {'items': core_items, 'truncated': len(cores) > 100},
                    'checks': _checks(CheckResult.objects.filter(Q(server=server) | Q(core__server=server)
                                        | Q(ingress__server=server) | Q(egress__server=server))),
                    'core': {'actual_version': None, 'state': 'not_connected'},
                    'ingresses': _endpoint_page(_ingresses_query().filter(server=server),
                                               _ingress_projection, server.ingress_count),
                    'egresses': _endpoint_page(_egresses_query().filter(server=server),
                                             _egress_projection, server.egress_count),
                    'capabilities': {'edit': metadata_edit_enabled(), 'probe': False, 'manage_cores': False},
                    'limitations': LIMITATIONS})


def _lines_query():
    through = Line.additional_ingresses.through
    additions = through.objects.filter(line_id=OuterRef('pk'))
    additional_count = additions.exclude(ingress_id=OuterRef('ingress_id')).order_by().values('line_id') \
        .annotate(total=Count('pk')).values('total')
    fields = ['id', 'public_id', 'name', 'notes', 'revision', 'enabled', 'ingress_id', 'egress_id']
    fields += ['ingress__' + field for field in INGRESS_FIELDS]
    fields += ['egress__' + field for field in EGRESS_FIELDS]
    fields += [prefix + field for prefix in ('ingress__server__', 'egress__server__') for field in SERVER_FIELDS]
    return Line.objects.select_related('ingress__server', 'egress__server').only(*fields).annotate(
        additional_count=Coalesce(Subquery(additional_count, output_field=IntegerField()), 0),
        has_disabled_additional=Exists(additions.filter(Q(ingress__enabled=False) | Q(ingress__server__enabled=False))),
    ).prefetch_related(Prefetch('additional_ingresses', queryset=_ingresses_query()[:ENDPOINT_LIMIT],
                               to_attr='registered_additional_ingresses'),
                       Prefetch('checks', queryset=CheckResult.objects.filter(check_kind='whole_path').order_by('-observed_at', '-pk')[:1],
                                to_attr='latest_checks'))


def _line_projection(line):
    # 旧模型允许主入口同时出现在附加入口集合中；展示与计数均按同一入口去重。
    unique = {line.ingress.pk: line.ingress}
    for ingress in line.registered_additional_ingresses:
        unique.setdefault(ingress.pk, ingress)
    ingresses = list(unique.values())[:ENDPOINT_LIMIT]
    total = 1 + line.additional_count
    endpoints_enabled = (line.ingress.enabled and line.ingress.server.enabled
                         and line.egress.server.enabled and not line.has_disabled_additional)
    latest = next(iter(line.latest_checks), None)
    verification = {'state': 'not_tested', 'observed_at': None}
    if latest:
        verification = {'state': freshness(latest, 'line', line), 'result': latest.result,
                        'observed_at': iso(latest.observed_at), 'expires_at': iso(latest.expires_at), 'source': latest.source}
    return {'id': str(line.public_id), 'name': line.name, 'notes': line.notes, 'revision': line.revision, 'enabled': line.enabled,
            'ingresses': [_ingress_projection(row) for row in ingresses], 'ingress_count': total,
            'ingresses_truncated': total > len(ingresses), 'egress': _egress_projection(line.egress),
            'endpoint_registration': 'enabled' if endpoints_enabled else 'disabled',
            'verification': verification}


@endpoint(staff=True)
def lines(request):
    filters, failure = _filters(request, with_server=True)
    if failure is not None:
        return failure
    query = _lines_query()
    if filters['q']:
        q = filters['q']
        query = query.filter(Q(name__icontains=q) | Q(ingress__name__icontains=q)
                             | Q(ingress__protocol__icontains=q) | Q(ingress__server__name__icontains=q)
                             | Q(additional_ingresses__name__icontains=q) | Q(additional_ingresses__protocol__icontains=q)
                             | Q(additional_ingresses__server__name__icontains=q)
                             | Q(egress__name__icontains=q) | Q(egress__kind__icontains=q)
                             | Q(egress__server__name__icontains=q)).distinct()
    if filters['status'] != 'all':
        query = query.filter(enabled=filters['status'] == 'enabled')
    if filters['server_id']:
        server_id = filters['server_id']
        query = query.filter(Q(ingress__server__public_id=server_id) | Q(egress__server__public_id=server_id)
                             | Q(additional_ingresses__server__public_id=server_id)).distinct()
    return _listing(query, filters['options'], _line_projection)


@endpoint(methods=('GET', 'PATCH'), staff=True)
def line_detail(request, public_id):
    identifier = _uuid(public_id)
    if identifier is None:
        return error('not_found', '线路不存在或不可访问。', 404)
    if request.method == 'PATCH':
        return _patch(request, 'line', identifier)
    line = _lines_query().filter(public_id=identifier).first() if identifier is not None else None
    if line is None:
        return error('not_found', '线路不存在或不可访问。', 404)
    ingress_ids = list(line.additional_ingresses.values_list('pk', flat=True)) + [line.ingress_id]
    return success({'line': _line_projection(line), 'read_only': not metadata_edit_enabled(),
                    'checks': _checks(CheckResult.objects.filter(Q(line=line) | Q(ingress_id__in=ingress_ids) | Q(egress_id=line.egress_id))),
                    'capabilities': {'edit': metadata_edit_enabled(), 'probe': False, 'publish': False},
                    'limitations': LIMITATIONS + ['失败关闭仅为出口登记设置，尚无整条线路的实际验证证据。']})
