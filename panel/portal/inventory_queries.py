"""资源查询白名单与完整关系预取；HTTP和固定检查共享，不读取部署租约。"""
from django.db.models import Count, Exists, IntegerField, OuterRef, Prefetch, Q, Subquery, prefetch_related_objects
from django.db.models.functions import Coalesce
from .models import CheckResult, CoreInstance, Egress, Ingress, Line, Server, ServerObservation

ENDPOINT_LIMIT = 100


SERVER_FIELDS = ('id', 'public_id', 'name', 'notes', 'revision', 'enabled', 'adapter', 'last_seen_at', 'purchase_meter_id')


INGRESS_FIELDS = ('id', 'name', 'protocol', 'enabled', 'server_id', 'revision', 'endpoint_host', 'endpoint_port', 'evidence_source', 'evidence_observed_at', 'config_ref')


EGRESS_FIELDS = ('id', 'name', 'kind', 'fail_closed', 'server_id', 'revision', 'endpoint_host', 'endpoint_port', 'evidence_source', 'evidence_observed_at', 'ownership', 'external_kind', 'protocol', 'purchase_meter_id', 'config_ref', 'core_instance_id', 'next_egress_id')


CORE_FIELDS = ('id', 'public_id', 'server_id', 'instance_key', 'name', 'core_type', 'registered_version',
               'configuration_owner', 'configuration_version', 'revision', 'notes', 'service_unit',
               'owner_state', 'evidence_source', 'evidence_observed_at')


def _count_for_server(model):
    # 分别聚合两类端点，避免入口与出口交叉连接产生膨胀。
    return Coalesce(Subquery(model.objects.filter(server_id=OuterRef('pk')).order_by()
                             .values('server_id').annotate(total=Count('pk')).values('total'),
                             output_field=IntegerField()), 0)


def _servers_query():
    return Server.objects.only(*SERVER_FIELDS).annotate(
        ingress_count=_count_for_server(Ingress), egress_count=_count_for_server(Egress), core_count=_count_for_server(CoreInstance)).prefetch_related(
            Prefetch('observations', queryset=ServerObservation.objects.order_by('-observed_at', '-pk')[:2], to_attr='latest_observations'))


def _cores_query():
    return CoreInstance.objects.select_related('server').only(
        *CORE_FIELDS, *('server__' + field for field in SERVER_FIELDS)).prefetch_related(
            Prefetch('ingresses', queryset=Ingress.objects.only('id', 'server_id').order_by('pk')))


def _ingresses_query():
    return Ingress.objects.select_related('server').only(
        *INGRESS_FIELDS, *('server__' + field for field in SERVER_FIELDS)).prefetch_related(
            Prefetch('core_instances', queryset=_cores_query())).order_by('pk')


def _egresses_query():
    return Egress.objects.select_related('server', 'core_instance__server').only(
        *EGRESS_FIELDS, *('server__' + field for field in SERVER_FIELDS),
        *('core_instance__' + field for field in CORE_FIELDS),
        *('core_instance__server__' + field for field in SERVER_FIELDS)).prefetch_related(
            Prefetch('core_instance__ingresses', queryset=Ingress.objects.only('id', 'server_id').order_by('pk'))).order_by('pk')


def _hydrate_egress_chains(roots):
    """分页内批量接回已登记后续段；最多16层，不向API输出任何配置引用。"""
    rows = {row.pk: row for row in roots}
    pending = {row.next_egress_id for row in roots if row.next_egress_id} - rows.keys()
    for _ in range(16):
        if not pending:
            break
        found = list(_egresses_query().filter(pk__in=pending))
        rows.update({row.pk: row for row in found})
        pending = {row.next_egress_id for row in found if row.next_egress_id} - rows.keys()
    # 同一出口可能属于多条线路，各ORM实例都须接到批量加载的后续段。
    for row in list(roots) + list(rows.values()):
        if row.next_egress_id in rows:
            row.next_egress = rows[row.next_egress_id]
    return rows


def _lines_query():
    through = Line.additional_ingresses.through
    additions = through.objects.filter(line_id=OuterRef('pk'))
    additional_count = additions.exclude(ingress_id=OuterRef('ingress_id')).order_by().values('line_id') \
        .annotate(total=Count('pk')).values('total')
    fields = ['id', 'public_id', 'name', 'notes', 'revision', 'enabled', 'ingress_id', 'egress_id']
    fields += ['ingress__' + field for field in INGRESS_FIELDS]
    fields += ['egress__' + field for field in EGRESS_FIELDS]
    fields += [prefix + field for prefix in ('ingress__server__', 'egress__server__') for field in SERVER_FIELDS]
    fields += ['egress__core_instance__' + field for field in CORE_FIELDS]
    fields += ['egress__core_instance__server__' + field for field in SERVER_FIELDS]
    return Line.objects.select_related('ingress__server', 'egress__server', 'egress__core_instance__server').only(*fields).annotate(
        additional_count=Coalesce(Subquery(additional_count, output_field=IntegerField()), 0),
        has_disabled_additional=Exists(additions.filter(Q(ingress__enabled=False) | Q(ingress__server__enabled=False))),
    ).prefetch_related(Prefetch('egress__core_instance__ingresses', queryset=Ingress.objects.only('id', 'server_id').order_by('pk')),
                       Prefetch('ingress__core_instances', queryset=_cores_query()), Prefetch('additional_ingresses', queryset=_ingresses_query()[:ENDPOINT_LIMIT],
                               to_attr='registered_additional_ingresses'),
                       Prefetch('checks', queryset=CheckResult.objects.filter(check_kind='whole_path').order_by('-observed_at', '-pk')[:1],
                                to_attr='latest_checks'))


def _prepare_line_graphs(rows, *, complete=False):
    """回执需要完整目标版本，批量读取完整入口而非逐线路懒加载。"""
    _hydrate_egress_chains([row.egress for row in rows])
    needing = [row for row in rows if complete or row.latest_checks]
    if not needing:
        return
    prefetch_related_objects(needing, Prefetch('additional_ingresses', queryset=_ingresses_query(),
                                             to_attr='fingerprint_additional_ingresses'))
    for row in needing:
        row.registered_additional_ingresses = row.fingerprint_additional_ingresses
        row.registered_additional_ingresses_complete = True


def load_check_results(query):
    """只读回执及验证目标所需字段，批量复用完整关系。"""
    rows = list(query.prefetch_related(
        Prefetch('server', queryset=Server.objects.only(*SERVER_FIELDS)),
        Prefetch('core', queryset=_cores_query()), Prefetch('ingress', queryset=_ingresses_query()),
        Prefetch('egress', queryset=_egresses_query()), Prefetch('line', queryset=_lines_query())))
    _prepare_line_graphs([row.line for row in rows if row.line_id], complete=True)
    _hydrate_egress_chains([row.egress for row in rows if row.egress_id])
    return rows
