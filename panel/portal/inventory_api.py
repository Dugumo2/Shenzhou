"""管理员资源资料与受信观测投影；资料保存不触发运行控制或现场采集。"""
import uuid
import json

from django.core.paginator import EmptyPage, Paginator
from django.core.exceptions import RequestDataTooBig
from django.db.models import Q

from .api import endpoint, error, page_options, success
from .api_helpers import iso
from .models import CheckResult, CoreInstance, Egress, Ingress, Line, Server, ServerObservation
from .inventory_service import (InventoryError, METADATA_MODELS, edit_metadata, metadata_edit_enabled,
                                metadata_identifier, metadata_projection)
from .observation_ingest import ObservationError, check_projection, freshness, observation_projection, target_fingerprint
from .inventory_purchase import InventoryPurchases
from .resource_plan_service import resource_plan_write_enabled
from .inventory_queries import (ENDPOINT_LIMIT, _servers_query, _cores_query, _ingresses_query,
    _egresses_query, _hydrate_egress_chains, _lines_query, _prepare_line_graphs, load_check_results)
from .node_resources import project_ingress, project_egress
from .line_projection import egress_chain_resources, line_path_projection
from .resource_checks import actions_for_targets, enqueue_check, get_job


def _purchased_node(row, purchases):
    purchase = purchases.for_resource(row.purchase_meter_id, 'external_node')
    node = project_egress(row, purchase['purchase_meter'])
    node['purchase_binding_key'] = purchase['purchase_binding_key']
    node['purchase_plan_editable'] = purchase['purchase_meter_state'] == 'ready' and resource_plan_write_enabled()
    return node


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


def _listing(query, options, project, prepare=None):
    paginator = Paginator(query.order_by('name', 'pk'), options[1])
    try:
        page = paginator.page(options[0])
    except EmptyPage:
        return error('page_not_found', '此页不存在，请返回前一页。', 404)
    rows = list(page)
    if prepare is not None:
        prepare(rows)
    return success({'items': [project(row) for row in rows], 'read_only': not metadata_edit_enabled(),
                    'pagination': {'page': page.number, 'page_size': options[1], 'total': paginator.count,
                                   'pages': paginator.num_pages, 'has_next': page.has_next(),
                                   'has_previous': page.has_previous()}, 'limitations': LIMITATIONS})


def _server_ref(server):
    return {'id': str(server.public_id), 'name': server.name, 'enabled': server.enabled}


def _server_projection(server):
    row = next(iter(server.latest_observations), None)
    previous = server.latest_observations[1] if len(server.latest_observations) > 1 else None
    observation = observation_projection(row, server, previous=previous)
    monitoring = {'state': 'not_connected', 'online': None, 'observed_at': None}
    if observation:
        monitoring = {'state': observation['freshness'], 'online': observation['metrics'].get('online')
                      if observation['freshness'] == 'fresh' else None,
                      'observed_at': observation['observed_at'], 'expires_at': observation['expires_at'], 'source': observation['source']}
    return {**_server_ref(server), 'adapter': server.adapter, 'last_seen_at': iso(server.last_seen_at),
            'notes': server.notes, 'revision': server.revision,
            'ingress_count': server.ingress_count, 'egress_count': server.egress_count,
            'core_count': server.core_count, 'node_count': server.ingress_count,
            'observation': observation, 'purchase_meter_id': server.purchase_meter_id,
            'monitoring': monitoring}


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
    except (ValueError, UnicodeError, RecursionError, RequestDataTooBig):
        return error('invalid_json', '请求内容无效或超过大小限制。', 422)
    try:
        return success(edit_metadata(request.user, kind, identifier, body))
    except InventoryError as exc:
        return error(exc.code, exc.message, exc.status)


def _checks(query):
    rows = load_check_results(query.order_by('-observed_at', '-pk')[:101])
    return {'items': [check_projection(row) for row in rows[:100]], 'truncated': len(rows) > 100}


def _metadata_detail(request, kind, identifier):
    """最小资料接口不查询配置、身份、观测，也不触发运行控制。"""
    try:
        lookup = metadata_identifier(kind, identifier)
    except InventoryError as exc:
        return error(exc.code, exc.message, exc.status)
    if request.method == 'PATCH':
        return _patch(request, kind, identifier)
    item = METADATA_MODELS[kind].objects.filter(**lookup).first()
    if item is None:
        return error('not_found', '资源不存在或不可访问。', 404)
    return success({'item': metadata_projection(kind, item), 'read_only': not metadata_edit_enabled(),
                    'capabilities': {'edit': metadata_edit_enabled()}})


@endpoint(methods=('GET', 'PATCH'), staff=True)
def core_detail(request, public_id):
    return _metadata_detail(request, 'core', public_id)


@endpoint(methods=('GET', 'PATCH'), staff=True)
def ingress_detail(request, resource_id):
    return _metadata_detail(request, 'ingress', resource_id)


@endpoint(methods=('GET', 'PATCH'), staff=True)
def egress_detail(request, resource_id):
    return _metadata_detail(request, 'egress', resource_id)


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
    observation = observation_projection(next(iter(server.latest_observations), None), server,
        previous=server.latest_observations[1] if len(server.latest_observations) > 1 else None)
    purchases = InventoryPurchases(request.user)
    ingress_rows = list(_ingresses_query().filter(server=server)[:ENDPOINT_LIMIT])
    egress_rows = list(_egresses_query().filter(server=server)[:ENDPOINT_LIMIT])
    _hydrate_egress_chains(egress_rows)
    nodes = [project_ingress(row) for row in ingress_rows]
    nodes.extend(_purchased_node(row, purchases) for row in egress_rows)
    cores = list(_cores_query().filter(server=server).order_by('pk')[:101])
    core_items = [{'id': str(core.public_id), 'name': core.name, 'instance_key': core.instance_key,
                   'core_type': core.core_type, 'registered_version': core.registered_version,
                   'configuration_owner': core.configuration_owner, 'configuration_version': core.configuration_version,
                   'revision': core.revision, 'notes': core.notes,
                   'service_unit': core.service_unit, 'owner_state': core.owner_state,
                   'evidence_source': core.evidence_source, 'evidence_observed_at': iso(core.evidence_observed_at),
                   'ingress_ids': [str(entry.pk) for entry in core.ingresses.all() if entry.server_id == server.pk],
                   'association_valid': all(entry.server_id == server.pk for entry in core.ingresses.all())} for core in cores[:100]]
    actions = actions_for_targets([('server', server)] + [('core', core) for core in cores[:100]]
                                  + [('ingress', row) for row in ingress_rows] + [('egress', row) for row in egress_rows])
    return success({'server': _server_projection(server), 'read_only': not metadata_edit_enabled(),
                    **purchases.for_resource(server.purchase_meter_id, 'server'),
                    'purchase_plan_editable': resource_plan_write_enabled(),
                    'nodes': {'items': nodes, 'total': server.ingress_count + server.egress_count,
                              'truncated': server.ingress_count + server.egress_count > len(nodes)},
                    'check_actions': actions,
                    'metrics': {'cpu_percent': None, 'memory_percent': None, 'disk_percent': None,
                                'upload_bps': None, 'download_bps': None, 'total_transfer_bytes': None},
                    'observation': observation,
                    'cores': {'items': core_items, 'truncated': len(cores) > 100},
                    'checks': _checks(CheckResult.objects.filter(Q(server=server) | Q(core__server=server)
                                        | Q(ingress__server=server) | Q(egress__server=server))),
                    'core': {'actual_version': None, 'state': 'not_connected'},
                    'ingresses': _endpoint_page(ingress_rows, _ingress_projection, server.ingress_count),
                    'egresses': _endpoint_page(egress_rows, _egress_projection, server.egress_count),
                    'capabilities': {'edit': metadata_edit_enabled(), 'probe': bool(actions), 'manage_cores': False},
                    'limitations': LIMITATIONS})


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
    path = line_path_projection(line, ingress_nodes=[project_ingress(row) for row in ingresses],
                               egress_node=project_egress(line.egress), total_ingresses=total)
    return {'id': str(line.public_id), 'name': line.name, 'notes': line.notes, 'revision': line.revision, 'enabled': line.enabled,
            'path': path,
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
    return _listing(query, filters['options'], _line_projection, prepare=_prepare_line_graphs)


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
    # 详情目标版本必须覆盖全部入口；显式安全字段预取，不读取部署租约或凭据。
    complete_ingresses = list(_ingresses_query().filter(additional_lines=line))
    purchases = InventoryPurchases(request.user)
    _prepare_line_graphs([line], complete=True)
    unique = {line.ingress.pk: line.ingress}
    unique.update({row.pk: row for row in line.registered_additional_ingresses})
    ingress_rows = list(unique.values())[:ENDPOINT_LIMIT]
    nodes = [project_ingress(row) for row in ingress_rows]
    chain, chain_issues = egress_chain_resources(line.egress)
    nodes.extend(_purchased_node(row, purchases) for row in chain)
    projected = _line_projection(line)
    line.registered_additional_ingresses = complete_ingresses
    line.registered_additional_ingresses_complete = True
    actions = actions_for_targets([('line', line)] + [('ingress', row) for row in ingress_rows] + [('egress', row) for row in chain])
    try:
        projected['target_version'] = target_fingerprint('line', line)
    except ObservationError:
        projected['target_version'] = None
        actions = []
    return success({'line': projected, 'read_only': not metadata_edit_enabled(),
                    'nodes': {'items': nodes, 'total': 1 + line.additional_count + len(chain),
                              'truncated': bool(chain_issues) or 1 + line.additional_count > len(ingress_rows)},
                    'check_actions': actions,
                    'checks': _checks(CheckResult.objects.filter(Q(line=line) | Q(ingress_id__in=ingress_ids) | Q(egress_id__in=[row.pk for row in chain]))),
                    'capabilities': {'edit': metadata_edit_enabled(), 'probe': bool(actions), 'publish': False},
                    'limitations': LIMITATIONS + ['失败关闭仅为出口登记设置，尚无整条线路的实际验证证据。']})


@endpoint(methods=('POST',), staff=True)
def resource_check_create(request):
    if request.content_type != 'application/json':
        return error('unsupported_media_type', '请使用 JSON 请求。', 415)
    try:
        def unique(pairs):
            data = {}
            for key, value in pairs:
                if key in data:
                    raise ValueError()
                data[key] = value
            return data
        if len(request.body) > 4096:
            raise ValueError()
        body = json.loads(request.body, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError, RequestDataTooBig):
        return error('invalid_json', '检查请求内容无效或过大。', 422)
    try:
        value = enqueue_check(request.user, body)
        return success(value, status=200 if value.get('replayed') else 202)
    except InventoryError as exc:
        return error(exc.code, exc.message, exc.status)


@endpoint(staff=True)
def resource_check_detail(request, public_id):
    try:
        return success(get_job(request.user, public_id))
    except InventoryError as exc:
        return error(exc.code, exc.message, exc.status)
