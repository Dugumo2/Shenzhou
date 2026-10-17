"""既有线路的只读投影；配置关系和能力检测分别取证。"""

from .node_resources import project_egress, project_ingress


MAX_EGRESS_SEGMENTS = 16


def _unknown_capability():
    return {'state': 'unknown', 'source': '', 'observed_at': None,
            'expires_at': None, 'freshness': 'unknown'}


def egress_chain_resources(egress):
    """有界读取已存的下一段；异常保留明确警告，不能把截短链当终点。"""
    chain, seen = [], set()
    current = egress
    while current is not None:
        if current.pk in seen:
            return chain, ['出口关联出现环路，不能视作可用路径。']
        if len(chain) >= MAX_EGRESS_SEGMENTS:
            return chain, ['出口关联超过 16 段读取上限，路径不完整，不能视作可用路径。']
        seen.add(current.pk)
        chain.append(current)
        following = getattr(current, 'next_egress', None)
        if getattr(current, 'next_egress_id', None) and following is None:
            return chain, ['下一段出口关联缺失，路径不完整。']
        current = following
    return chain, []


def _has_evidence(node):
    evidence = node.get('evidence', {})
    return bool(evidence.get('source') and evidence.get('observed_at'))


def _execution_core(resource, node):
    """必须有明确执行核心与双方证据，不以同机或名称推断。"""
    core = getattr(resource, 'core_instance', None)
    if (core is None or core.server_id != resource.server_id or not _has_evidence(node)
            or not getattr(core, 'evidence_source', None) or not getattr(core, 'evidence_observed_at', None)):
        return None
    refs = node.get('core_refs', {})
    if refs.get('state') != 'linked' or len(refs.get('items', [])) != 1:
        return None
    value = refs['items'][0]
    return {key: value[key] for key in ('id', 'name', 'core_type')} if value['id'] == str(core.public_id) else None


def _point(node, kind='egress'):
    return {'id': node['resource_id'], 'name': node['name'], 'kind': kind}


def _core_point(core):
    return {'id': core['id'], 'name': core['name'], 'kind': 'core'}


def _segment(identifier, start, end, *, core=None, protocol=None, evidence=None, required=False, observed=False):
    return {'id': identifier, 'from': start, 'to': end, 'relation': 'observed' if observed else 'registered',
            'protocol': protocol, 'core': core,
            'tcp': _unknown_capability(), 'udp': _unknown_capability(), 'dns': _unknown_capability(),
            'authentication': 'unknown', 'failure_behavior': 'fail_closed_required' if required else 'unknown',
            'evidence_source': (evidence or {}).get('source', ''),
            'evidence_observed_at': (evidence or {}).get('observed_at')}


def _terminal_projection(resource, node):
    """出口摘要只复用已白名单化的节点，不暴露配置引用或认证内容。"""
    kind = getattr(resource, 'kind', 'unknown')
    return {'id': node['resource_id'], 'name': node['name'], 'kind': kind,
            'kind_label': {'direct': '服务器直出', 'upstream': '上游链路', 'block': '阻断'}.get(kind, '类型待核实'),
            'fail_closed': resource.fail_closed, 'server': node['execution_server'],
            'revision': node['revision'], 'ownership': getattr(resource, 'ownership', 'unknown'),
            'external_kind': node['external_kind'], 'protocol': node['protocol'],
            'endpoint_host': node['endpoint']['host'], 'endpoint_port': node['endpoint']['port'],
            'evidence_source': node['evidence']['source'] or '', 'evidence_observed_at': node['evidence']['observed_at'],
            'purchase_meter_id': node['purchase_meter_id'],
            'core_instance_id': str(resource.core_instance.public_id), 'next_egress_id': None}


def line_path_projection(line, *, ingress_nodes=None, egress_node=None, total_ingresses=None):
    """消费节点白名单投影，避免读取凭据或按 HOME/BWH 名称推导双核方向。

    调用方可传入同一响应已经投影的节点，避免重复查询。v1.3 明确记录
    出口执行核心与下一段，只有双方带证据的完整链才绘制配置关系。
    """
    if ingress_nodes is None:
        ingress_nodes = [project_ingress(row) for row in line.all_ingresses()]
    if egress_node is None:
        egress_node = project_egress(line.egress)
    seen = set()
    segments = []
    for node in ingress_nodes:
        if node.get('resource_type') != 'ingress' or node['resource_id'] in seen:
            continue
        seen.add(node['resource_id'])
        segments.append({
            'id': f"ingress:{node['resource_id']}:egress:{egress_node['resource_id']}",
            'from': {'id': node['resource_id'], 'name': node['name'], 'kind': 'ingress'},
            'to': {'id': egress_node['resource_id'], 'name': egress_node['name'], 'kind': 'egress'},
            'relation': 'registered', 'protocol': None, 'core': None,
            'tcp': _unknown_capability(), 'udp': _unknown_capability(), 'dns': _unknown_capability(),
            'authentication': 'unknown',
            'failure_behavior': 'fail_closed_required' if line.egress.fail_closed else 'unknown',
        })
    warnings = ['入口与出口为现有登记关联；中间跳段、执行核心方向和各段能力尚无完整证据。',
                '节点的 TCP 检查只证明该目标和该次检查，不代表认证、UDP、DNS 或整链通过。']
    if total_ingresses is not None and total_ingresses > len(segments):
        warnings.append(f'当前只显示 {len(segments)} 个入口分支，共登记 {total_ingresses} 个入口。')
    if not segments:
        warnings.append('尚无可展示的入口关联；不会把单独资源包装成多跳线路。')
    if any(node.get('core_refs', {}).get('state') == 'conflict' for node in ingress_nodes):
        warnings.append('入口核心归属存在冲突；请在节点资料中核对，不选择其中一个核心推测路径。')
    fallback = {'kind': 'registered_association', 'source': 'inventory_registration',
                'observed_at': None, 'segments': segments, 'warnings': warnings, 'terminal_egress': None}
    chain, issues = egress_chain_resources(line.egress)
    if issues:
        fallback['warnings'].extend(issues)
        return fallback
    nodes = [egress_node] + [project_egress(resource) for resource in chain[1:]]
    cores = [_execution_core(resource, node) for resource, node in zip(chain, nodes)]
    if not segments or not all(cores):
        if len(chain) > 1:
            fallback['warnings'].append('出口链部分核心归属或来源证据缺失，保留登记关联，不猜测缺失段。')
        return fallback
    if len({resource.server_id for resource in chain}) > 1:
        fallback['warnings'].append('跨服务器连接尚缺独立连接证据，不能按出站引用推断已验证跨机路径。')
        return fallback
    if (nodes[-1]['kind'] == 'owned' and nodes[-1]['protocol'] == 'socks'
            and nodes[-1]['endpoint']['host'] in ('127.0.0.1', '::1')):
        fallback['warnings'].append('回环中转缺少后续出口，不能把内部 SOCKS 端点当作最终出口。')
        return fallback
    path = []
    first_core = cores[0]
    for node in ingress_nodes:
        if node.get('resource_type') != 'ingress' or node['resource_id'] not in seen:
            continue
        seen.remove(node['resource_id'])
        refs = node.get('core_refs', {})
        linked = (_has_evidence(node) and refs.get('state') == 'linked'
                  and [value['id'] for value in refs.get('items', [])] == [first_core['id']])
        path.append(_segment('ingress:' + node['resource_id'], _point(node, 'ingress'), _core_point(first_core),
                             core=first_core if linked else None, protocol=node.get('protocol') if linked else None,
                             evidence=node.get('evidence') if linked else None, observed=linked))
    for index, (resource, node, core) in enumerate(zip(chain, nodes, cores)):
        if index == 0 or core['id'] != cores[index - 1]['id']:
            if index:
                previous = nodes[index - 1]
                path.append(_segment('handoff:' + previous['resource_id'], _point(previous), _core_point(core),
                                     core=cores[index - 1], protocol=previous['protocol'], evidence=previous['evidence'],
                                     required=chain[index - 1].fail_closed, observed=True))
            start = _core_point(core)
        else:
            start = _point(nodes[index - 1])
        path.append(_segment('egress:' + node['resource_id'], start, _point(node), core=core,
                             protocol=node['protocol'], evidence=node['evidence'], required=resource.fail_closed, observed=True))
    known_warnings = ['图中已观测表示配置、明确核心归属和既有下一段关系；不表示流量已通过。',
                      'TCP、UDP、DNS、认证与整链均需独立检查；端口监听和配置启用 UDP 不等于协议验证通过。',
                      '这里只展示该线路的既有出站路径；复杂规则条件仍由原规则控制，不代表所有流量无条件走此出口。']
    if total_ingresses is not None and total_ingresses > len({node['resource_id'] for node in ingress_nodes}):
        known_warnings.append(f'当前仅展示部分入口，共登记 {total_ingresses} 个入口。')
    if any(segment['relation'] != 'observed' for segment in path):
        known_warnings.append('部分入口与核心的关系缺少证据，以虚线保留登记关联。')
    sources = sorted({node['evidence']['source'] for node in nodes})
    observed = min(node['evidence']['observed_at'] for node in nodes)
    return {'kind': 'observed_path', 'source': ' / '.join(sources), 'observed_at': observed,
            'segments': path, 'warnings': known_warnings, 'terminal_egress': _terminal_projection(chain[-1], nodes[-1])}
