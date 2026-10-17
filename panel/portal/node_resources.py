"""管理员节点安全投影；只读登记关系，不读取凭据、订阅或执行现场动作。"""
import ipaddress
import re

from .api_helpers import iso


PROTOCOL_LABELS = {'reality': 'Reality', 'hy2': 'Hysteria 2', 'anytls': 'AnyTLS',
                   'ws': 'WebSocket', 'ss2022': 'Shadowsocks 2022', 'direct': '服务器出站',
                   'socks': 'SOCKS 中转'}
RESTRICTED_TEXT = re.compile(r'://|(?:password|passwd|token|secret|uuid|credential)\s*[:=]', re.I)


def _text(value, maximum=160):
    """公开文本不承接误填的连接 URI 或认证参数。"""
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    if RESTRICTED_TEXT.search(value) or any(ord(char) < 32 for char in value):
        return None
    return value


def _endpoint(resource):
    host = getattr(resource, 'endpoint_host', None)
    if isinstance(host, str):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (len(host) > 253 or not re.fullmatch(r'[A-Za-z0-9.-]+', host)
                    or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', part)
                           for part in host.rstrip('.').split('.'))):
                host = None
    else:
        host = None
    port = getattr(resource, 'endpoint_port', None)
    if type(port) is not int or not 1 <= port <= 65535:
        port = None
    return {'host': host, 'port': port}


def _base(resource, resource_type):
    server = resource.server
    return {'id': f'{resource_type}:{resource.pk}', 'resource_id': str(resource.pk),
            'resource_type': resource_type, 'name': _text(resource.name) or '名称待核实',
            'revision': getattr(resource, 'revision', 1), 'endpoint': _endpoint(resource),
            'execution_server': {'id': str(server.public_id), 'name': _text(server.name) or '服务器名称待核实',
                                 'enabled': server.enabled},
            'evidence': {'source': _text(getattr(resource, 'evidence_source', None), 120),
                         'observed_at': iso(getattr(resource, 'evidence_observed_at', None))},
            'capabilities': {'manage_core': False, 'install_agent': False, 'grant': False, 'sync_source': False},
            'limitations': ['登记启用与协议支持不等于已验证认证、UDP、DNS 或当前在线。',
                            '资源资料与用户授权、倍率及套餐账本分别维护。']}


def _core_ref(core):
    return {'id': str(core.public_id), 'name': _text(core.name) or '核心名称待核实',
            'core_type': core.core_type,
            'owner_state': core.owner_state if core.owner_state in ('unknown', 'observed_only', 'verified') else 'unknown'}


def project_ingress(ingress):
    """一个入口是一份节点资源；不查询或输出 NodeIdentity。"""
    result = _base(ingress, 'ingress')
    cores = list(ingress.core_instances.all())
    invalid = any(core.server_id != ingress.server_id for core in cores)
    state = 'conflict' if invalid or len(cores) > 1 else 'linked' if cores else 'unknown'
    # 冲突时仍展示已登记引用，但绝不选一个核心当作实际执行者。
    refs = [_core_ref(core) for core in cores]
    result.update(kind='owned', protocol=ingress.protocol,
                  protocol_label=PROTOCOL_LABELS.get(ingress.protocol, '协议待核实'), enabled=ingress.enabled,
                  core_refs={'state': state, 'items': refs}, external_kind=None,
                  purchase_meter_id=None, purchase_meter=None, next_resource=None)
    if state != 'linked':
        result['limitations'].append('核心归属存在冲突，需核对登记证据。' if state == 'conflict'
                                     else '尚未登记实际执行核心，不能按协议或服务器名称推断。')
    return result


def _purchase_meter(resource, meter):
    """调用方先做来源与查看者授权；此层再核显式关联和嵌套字段白名单。"""
    meter_id = getattr(resource, 'purchase_meter_id', None)
    if (not meter_id or not isinstance(meter, dict) or meter.get('id') != meter_id
            or meter.get('scope') != 'external_node'):
        return None
    result = {'id': meter_id, 'label': _text(meter.get('label')) or '外部资源采购', 'scope': 'external_node',
              'source_kind': meter.get('source_kind'), 'quality': meter.get('quality'), 'details': {}, 'alerts': []}
    if result['source_kind'] not in ('estimate', 'official') or result['quality'] not in ('current', 'stale', 'error', 'gap', 'missing'):
        return None
    for key in ('quota_bytes', 'used_bytes', 'remaining_bytes', 'upload_bytes', 'download_bytes'):
        value = meter.get(key)
        result[key] = value if isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]{0,29}', value) else None
    for key in ('observed_at', 'expires_at', 'expires_on'):
        result[key] = _text(meter.get(key), 40)
    cycle = meter.get('cycle')
    result['cycle'] = None
    if isinstance(cycle, dict) and cycle.get('kind') in ('fixed_days', 'provider', 'none', 'calendar_month', 'manual'):
        result['cycle'] = {'kind': cycle['kind'], **{key: _text(cycle.get(key), 40)
                            for key in ('starts_at', 'ends_at', 'next_reset_at')}}
    alerts = meter.get('alerts', [])
    for alert in (alerts[:32] if isinstance(alerts, list) else []):
        if (isinstance(alert, dict) and re.fullmatch(r'[a-z0-9_]{1,80}', str(alert.get('code', '')))
                and alert.get('severity') in ('info', 'warning', 'error') and _text(alert.get('message'), 300)):
            result['alerts'].append({key: alert[key] for key in ('code', 'severity', 'message')})
    return result


def project_egress(egress, purchase_meter=None):
    """出口执行位置与外部所有权分开；来源更新不执行导入、授权或订阅抓取。"""
    result = _base(egress, 'egress')
    ownership = getattr(egress, 'ownership', 'unknown')
    kind = {'self_owned': 'owned', 'external': 'external'}.get(ownership, 'unknown')
    external_kind = getattr(egress, 'external_kind', None) if kind == 'external' else None
    if external_kind not in ('node', 'subscription'):
        external_kind = None
    protocol = _text(getattr(egress, 'protocol', None), 24)
    meter_id = getattr(egress, 'purchase_meter_id', None)
    meter_id = meter_id if isinstance(meter_id, str) and re.fullmatch(r'[a-z0-9_-]{1,64}', meter_id) else None
    core = egress.core_instance if getattr(egress, 'core_instance_id', None) is not None else None
    core_state = 'unknown' if core is None else 'linked' if core.server_id == egress.server_id else 'conflict'
    next_egress = egress.next_egress if getattr(egress, 'next_egress_id', None) is not None else None
    # 只返回一跳引用，链路展开及循环检测由线路投影负责；不能递归携带其他资源的采购数据。
    next_resource = None if next_egress is None else {
        'id': f'egress:{next_egress.pk}', 'resource_id': str(next_egress.pk),
        'name': _text(next_egress.name) or '资源名称待核实'}
    result.update(kind=kind, protocol=protocol, protocol_label=PROTOCOL_LABELS.get(protocol, '协议待核实'),
                  enabled=None, external_kind=external_kind,
                  core_refs={'state': core_state, 'items': [_core_ref(core)] if core is not None else []},
                  core_instance_id=str(core.public_id) if core is not None else None,
                  next_egress_id=str(next_egress.pk) if next_egress is not None else None,
                  next_resource=next_resource,
                  purchase_meter_id=meter_id,
                  purchase_meter=_purchase_meter(egress, purchase_meter) if kind == 'external' and meter_id else None)
    if core_state == 'conflict':
        result['limitations'].append('登记核心与执行服务器不一致，请核对资源归属。')
    if next_egress is not None and next_egress.pk == egress.pk:
        result['limitations'].append('后续资源指向自身，不能作为有效线路。')
    if kind == 'external':
        result['limitations'].append('外部资源由供应商维护；执行服务器不代表拥有上游服务器。')
    if external_kind == 'subscription':
        result['limitations'].append('此对象是上游订阅来源；更新来源不会自动授权新节点，也不是用户输出订阅。')
    if kind == 'unknown':
        result['limitations'].append('资源所有权尚未核实，不提供服务器或核心控制。')
    return result
