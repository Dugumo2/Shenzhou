"""生成原生订阅及单份 Android 配置；不启动核心、不设置系统网络、不记录秘密。"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote, urlsplit, urlunsplit

from . import p7_build_profiles as p7

SCHEMA_VERSION = 1
CORE_VERSION = '1.14.1'
REMOTE_DNS = '8.8.8.8'
REMOTE_DNS_URI = 'tcp://8.8.8.8:53'
PROJECT = Path(__file__).resolve().parents[2]
RULE_MODES = ('whitelist', 'blacklist', 'global')
RULE_NAMES = {'whitelist': 'P8-绕过大陆(Whitelist)', 'blacklist': 'P8-黑名单(Blacklist)',
              'global': 'P8-全局(Global)'}
MATCH_FIELDS = {'domain', 'domain_suffix', 'ip_cidr'}
RESERVED_TLDS = {'localhost', 'local', 'internal', 'invalid', 'test', 'example', 'onion', 'alt', 'home', 'lan'}


def require(condition, code):
    if not condition:
        raise ValueError(code)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_source(source, name):
    """仅接受已审域名/IP源；拒绝逻辑、ASN、关键词、下载指令及空源。"""
    require(isinstance(source, dict) and set(source) == {'version', 'rules'}, name + '_fields')
    require(source['version'] in (2, 3) and isinstance(source['rules'], list) and source['rules'], name + '_empty')
    result = []
    for rule in source['rules']:
        require(isinstance(rule, dict) and rule and not set(rule) - MATCH_FIELDS, name + '_rule_fields')
        # 一个源规则只接受一种匹配，避免多字段AND语义被转换成OR。
        require(len(rule) == 1, name + '_single_match_required')
        field, values = next(iter(rule.items()))
        require(isinstance(values, list) and values and all(isinstance(x, str) for x in values), name + '_values')
        cleaned = set()
        for value in values:
            if field == 'ip_cidr':
                net = ipaddress.ip_network(value, strict=True)
                require(not net.is_unspecified and net.prefixlen > 0, name + '_broad_ip')
            else:
                if field == 'domain_suffix' and '.' not in value:
                    require(value not in RESERVED_TLDS and bool(re.fullmatch(r'(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})', value)), name + '_tld')
                else:
                    p7._domain(value)
            cleaned.add(value)
        result.append({field: sorted(cleaned)})
    return {'version': 3, 'rules': result}


def merge_domain_sources(*sources):
    merged = {field: set() for field in MATCH_FIELDS}
    for source in sources:
        for rule in source['rules']:
            for field, values in rule.items():
                merged[field].update(values)
    return {'version': 3, 'rules': [{field: sorted(values)} for field, values in merged.items() if values]}


def media_source(parameters, documents):
    """复用旧规则原件校验；媒体仅标为当前代理，不把BWH固定到客户端。"""
    # 仍校验全部十媒体原件；服务器AKDNS支持不等于客户端必须代理国内B站。
    p7._streaming_rules(parameters['policy'], documents)
    rules = [rule for material in parameters['policy']['streaming_rule_sets']
             if material['service'] != 'bilibili'
             for rule in documents[material['service']]['rules']]
    merged = {'domain': set(), 'domain_suffix': set()}
    regexes = []
    for rule in rules:
        for field, values in rule.items():
            if field == 'domain_regex':
                regexes.extend(values)
            else:
                merged[field].update(values)
    result = {'version': 3, 'rules': [{field: sorted(values)} for field, values in merged.items() if values]}
    # P7仅允许一个已核媒体正则，规则转换单独处理，避免开放任意正则导入。
    if regexes:
        result['rules'].append({'domain_regex': sorted(set(regexes))})
    return result


def local_routing_sources(policy, proxy):
    """复核面板清单；代理例外优先，直连不能覆盖受保护集合。"""
    require(isinstance(policy, dict) and set(policy) == {'schema_version', 'rules'}
            and policy['schema_version'] in (1, 2) and isinstance(policy['rules'], list)
            and len(policy['rules']) <= 200, 'local_direct_schema')
    protected = set()
    for rule in proxy['rules']:
        for field, values in rule.items():
            if field in ('domain', 'domain_suffix'):
                protected.update(values)
            elif field == 'domain_regex':
                require(values == [r'^ewcdn[0-9]+\.nowe\.com$'], 'local_direct_proxy_regex')
                protected.add('nowe.com')
    protected.add('relay.example.invalid')

    def overlap(domain):
        # 阿里云 OSS 共用后缀内已有受保护的精确主机。客户端始终先匹配代理集，
        # 因此只允许这一条经用户审核的父域例外；同名或上级保护域仍拒绝直连。
        if domain == 'aliyuncs.com' and all(
                item != domain and not domain.endswith('.' + item) for item in protected):
            return False
        return any(domain == item or domain.endswith('.' + item) or item.endswith('.' + domain)
                   for item in protected)

    result = {action: {'domain': set(), 'domain_suffix': set(), 'domain_regex': set()}
              for action in ('direct', 'proxy')}
    seen = set()
    for entry in policy['rules']:
        fields = {'kind', 'value', 'scope'} if policy['schema_version'] == 1 else {'action', 'kind', 'value', 'scope'}
        require(isinstance(entry, dict) and set(entry) == fields, 'local_direct_entry')
        kind, value, scope = entry['kind'], entry['value'], entry['scope']
        action = entry.get('action', 'direct')
        require(action in result and (kind, value) not in seen, 'local_direct_action_or_duplicate')
        seen.add((kind, value))
        require(kind in ('exact', 'suffix', 'regex') and isinstance(value, str) and isinstance(scope, str),
                'local_direct_type')
        if kind in ('exact', 'suffix'):
            require(not scope and value == value.lower(), 'local_direct_scope')
            p7._domain(value)
            require('.' in value and (action == 'proxy' or not overlap(value)), 'local_direct_protected')
            result[action]['domain' if kind == 'exact' else 'domain_suffix'].add(value)
        else:
            p7._domain(scope)
            require((action == 'proxy' or not overlap(scope)) and len(value) <= 180 and value.startswith('^')
                    and value.endswith(r'\.' + re.escape(scope) + '$')
                    and re.fullmatch(r'[a-z0-9.\\\[\]\-+*?^$]+', value)
                    and sum(value.count(char) for char in '+*?') <= 1
                    and '\\' not in value.replace(r'\.', ''), 'local_direct_regex_scope')
            re.compile(value)
            result[action]['domain_regex'].add(value)
    return {action: {'version': 3, 'rules': [{field: sorted(values)} for field, values in groups.items() if values]}
            for action, groups in result.items()}


def build_policy_sources(parameters, cn_source, documents, blacklist_source=None, local_direct_policy=None):
    cn = validate_source(cn_source, 'cn')
    # 用户批准学校根域及全部子域名，覆盖教务、主页和 WebVPN。
    cn['rules'].append({'domain_suffix': ['cuit.edu.cn']})
    protected = {'version': 3, 'rules': [{'domain_suffix': sorted(set(
        parameters['policy']['protected_suffixes'] + ['googleapis.cn', 'services.googleapis.cn']))}]}
    media = media_source(parameters, documents)
    proxy = {'version': 3, 'rules': copy.deepcopy(protected['rules'] + media['rules'])}
    if blacklist_source is not None:
        proxy['rules'].extend(validate_source(blacklist_source, 'blacklist')['rules'])
    custom = local_routing_sources(local_direct_policy, proxy) if local_direct_policy is not None else None
    if custom is not None:
        proxy['rules'].extend(custom['proxy']['rules'])
    rejected = []
    for rule in parameters['policy']['custom_rules']:
        if rule['enabled'] and rule['lane'] == 'reject':
            rejected.append({'domain' if rule['match'] == 'exact' else 'domain_suffix': [rule['domain']]})
    cn_domain = {'version': 3, 'rules': [rule for rule in cn['rules'] if 'ip_cidr' not in rule]}
    cn_ip = {'version': 3, 'rules': [rule for rule in cn['rules'] if 'ip_cidr' in rule]}
    return {'cn': cn, 'cn-domain': cn_domain, 'cn-ip': cn_ip, 'proxy': proxy,
            'reject': {'version': 3, 'rules': rejected},
            'local-direct': custom['direct'] if custom is not None else {'version': 3, 'rules': []}}


def node_name(account):
    return p7._tag(account['lane'], account['protocol'])


def standard_links(parameters, fixture=False):
    ordered = sorted(parameters['accounts'], key=lambda a: (p7.LANES.index(a['lane']), p7.PROTOCOLS.index(a['protocol'])))
    copied = copy.deepcopy(parameters)
    copied['accounts'] = ordered
    links = p7.build_node_links(copied, fixture=fixture)
    require(len(links) == 8, 'eight_enabled_accounts_required')
    return [urlunsplit((*urlsplit(link)[:4], quote(node_name(account), safe='')))
            for link, account in zip(links, ordered)]


def policy_group(lane):
    prefix = lane.upper()
    return {'ConfigVersion': 4, 'ConfigType': 101, 'CoreType': 24,
            'Remarks': prefix + '-Auto',
            'ProtoExtraObj': {'GroupType': 'PolicyGroup', 'SubChildItems': 'self',
                              'Filter': '^' + prefix + r'-(Reality|HY2|WS|AnyTLS)$',
                              'MultipleLoad': 0}}


def inner_group_uri(group):
    encoded = base64.urlsafe_b64encode(json.dumps(group, separators=(',', ':'), ensure_ascii=False).encode()).decode().rstrip('=')
    return 'v2rayn://policygroup/' + encoded


def windows_profiles(parameters):
    """原生数据库候选，仅存受限输出；不把账户材料写入控制台。"""
    result = []
    for account in sorted(parameters['accounts'], key=lambda a: (p7.LANES.index(a['lane']), p7.PROTOCOLS.index(a['protocol']))):
        protocol = account['protocol']
        item = {'ConfigVersion': 4, 'CoreType': 24, 'ConfigType': {'reality': 5, 'ws': 5, 'hy2': 7, 'anytls': 11}[protocol],
                'Remarks': node_name(account), 'Address': parameters['server_ipv4'],
                'Port': {'reality': 443, 'hy2': 8443, 'ws': 8443, 'anytls': 9443}[protocol],
                'Password': account['credential'], 'Username': '', 'Network': 'raw',
                'StreamSecurity': 'tls', 'AllowInsecure': 'false', 'Sni': parameters['edge_host'],
                'Alpn': '', 'Fingerprint': '', 'PublicKey': '', 'ShortId': '',
                'ProtoExtraObj': {}, 'TransportExtraObj': {}}
        if protocol == 'reality':
            item.update(StreamSecurity='reality', Sni=parameters['reality_server'], Fingerprint='chrome',
                        PublicKey=parameters['reality_public_key'], ShortId=parameters['reality_short_id'])
            item['ProtoExtraObj'] = {'Flow': 'xtls-rprx-vision', 'VlessEncryption': 'none'}
        elif protocol == 'ws':
            item.update(Address=parameters['ws_host'], Network='ws', Sni=parameters['ws_host'])
            item['ProtoExtraObj'] = {'VlessEncryption': 'none'}
            item['TransportExtraObj'] = {'Path': parameters['ws_path'], 'Host': parameters['ws_host']}
        elif protocol == 'hy2':
            item['Alpn'] = 'h3'
        result.append(item)
    result.extend(policy_group(lane) for lane in p7.LANES)
    return result


def win_rule(identifier, remarks, outbound, **match):
    return {'Id': identifier, 'Remarks': remarks, 'Enabled': True, 'OutboundTag': outbound, **match}


def windows_routes(sources):
    common = [win_rule('p8-private', '已批准局域网直连', 'direct', Ip=['geoip:private'])]
    if sources['reject']['rules']:
        common.append(win_rule('p8-reject', '保留明确拒绝规则', 'block', Domain=['geosite:p8-reject']))
    proxy = win_rule('p8-proxy', 'AI、Google Play及媒体跟随当前代理', 'proxy', Domain=['geosite:p8-proxy'])
    local_direct = ([win_rule('p8-local-direct', '面板审核的本地直连', 'direct',
                              Domain=['geosite:p8-local-direct'])] if sources['local-direct']['rules'] else [])
    cn_rules = []
    if sources['cn-domain']['rules']:
        cn_rules.append(win_rule('p8-cn-domain', '经审大陆域名直连', 'direct', Domain=['geosite:p8-cn-domain']))
    if sources['cn-ip']['rules']:
        cn_rules.append(win_rule('p8-cn-ip', '经审大陆IP直连，不将IP源用于DNS域名分类', 'direct', Ip=['geoip:p8-cn-ip']))
    # 不引入无审核GFW或MetaCubeX默认规则；黑名单只消费传入的已核集合。
    return {'whitelist': common + [proxy] + local_direct + cn_rules + [win_rule('p8-final-proxy', '其他跟随当前代理', 'proxy', Port='0-65535')],
            'blacklist': common + [proxy] + local_direct + [win_rule('p8-final-direct', '黑名单模式其他直连', 'direct', Port='0-65535')],
            'global': common + [win_rule('p8-final-global', '全局跟随当前代理', 'proxy', Port='0-65535')]}


def windows_dns():
    return {'schema_version': 1, 'core_type': 24,
            'SimpleDNSItem': {'RemoteDNS': REMOTE_DNS_URI, 'DirectDNS': 'https://dns.alidns.com/dns-query',
                              'BootstrapDNS': '223.5.5.5', 'FakeIP': False,
                              'FakeIPRange': '198.18.0.0/15', 'GlobalFakeIp': False,
                              'Strategy4Freedom': 'AsIs', 'Strategy4Proxy': 'AsIs',
                              'Strategy4ProxyDial': 'AsIs', 'BlockBindingQuery': True,
                              'BlockAAAAQuery': False, 'UseSystemHosts': False, 'AddCommonHosts': False},
            'disable_custom_dns_for_core': True, 'default_node_remarks': 'HOME-Reality',
            'default_routing': RULE_NAMES['whitelist'], 'system_proxy_mutation': False,
            'tun_mutation': False}


def _ancestors(domain: str):
    labels = domain.split('.')
    return ('.'.join(labels[i:]) for i in range(len(labels)))


def sanitize_ng_cn_domains(cn_material: dict, proxy_material: dict) -> tuple[dict, dict]:
    """专供 NG：排除会使敏感 DNS 成为境内服务器候选的 CN 重叠。

    不能表达“后缀减去子域”时删除整个 CN 后缀，保守减少直连。
    仅接受已审核媒体正则；无法证明边界的正则中止生成。
    """
    exact, suffix = set(), set()
    regex_envelopes = []
    for rule in proxy_material['rules']:
        if len(rule) != 1:
            raise ValueError('禁止未审计复合代理匹配')
        field, values = next(iter(rule.items()))
        if field == 'domain': exact.update(values)
        elif field == 'domain_suffix': suffix.update(values)
        elif field == 'domain_regex':
            for pattern in values:
                if pattern != r'^ewcdn[0-9]+\.nowe\.com$':
                    raise ValueError('无法安全求交的代理域名正则')
                suffix.add('nowe.com')
                regex_envelopes.append({'regex': pattern, 'conservative_suffix': 'nowe.com'})
        elif field != 'ip_cidr':
            raise ValueError('未知代理匹配字段')
    protected_ancestors = {a for domain in exact | suffix for a in _ancestors(domain)}
    result, removed = [], []
    for rule in cn_material['rules']:
        if len(rule) != 1:
            raise ValueError('禁止未审计复合 CN 匹配')
        field, values = next(iter(rule.items()))
        if field not in ('domain', 'domain_suffix'):
            raise ValueError('CN 域名排重只接受精确域名或后缀')
        kept = []
        for domain in values:
            overlap = any(a in suffix for a in _ancestors(domain))
            overlap |= domain in exact if field == 'domain' else domain in protected_ancestors
            if overlap:
                removed.append({'field': field, 'value': domain})
            else:
                kept.append(domain)
        if kept:
            result.append({field: kept})
    material = copy.deepcopy(cn_material)
    material['rules'] = result
    return material, {'removed_count': len(removed), 'removed': removed,
                      'regex_envelopes': regex_envelopes,
                      'scope': 'NG only; remove direct coverage, never add direct coverage'}


def _varint(value: int) -> bytes:
    if type(value) is not int or value < 0 or value >= 1 << 64:
        raise ValueError('protobuf 无符号整数范围错误')
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _bytes_field(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _uint_field(number: int, value: int) -> bytes:
    # proto3 默认零值可以省略；显式编码同样符合规范。
    return _varint(number << 3) + _varint(value)


def _domain(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('域名值无效')
    result = value.encode('idna').decode('ascii').lower()
    if len(result) > 253 or any(not re.fullmatch(r'[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?', label)
                              for label in result.split('.')):
        raise ValueError('域名不能含通配符、URL 或空标签')
    return result


def encode_geo_assets(sources: dict) -> tuple[dict[str, bytes], dict]:
    """将 clean sources 编成确定性的两个 dat，返回每标签匹配数量。

    sources 要求 proxy/reject/cn-domain/cn-ip 四份规则材料；只读输入，不推断
    未提供的规则。各规则只允许一个字段，避免把原 AND 语义展开成 OR。
    """
    if not isinstance(sources, dict) or not {'proxy', 'reject', 'cn-domain', 'cn-ip'} <= sources.keys():
        raise ValueError('缺少四份已审核 Geo 来源')
    groups = {'p15-proxy': (sources['proxy'],),
              'p15-cn': (sources['cn-domain'], sources['cn-ip']),
              'p15-reject': (sources['reject'],)}
    geosite, geoip, counts = bytearray(), bytearray(), {}
    for code, materials in groups.items():
        domains, cidrs = set(), set()
        for material in materials:
            if not isinstance(material, dict) or not isinstance(material.get('rules'), list):
                raise ValueError('Geo 来源规则结构无效')
            for rule in material['rules']:
                if not isinstance(rule, dict) or len(rule) != 1:
                    raise ValueError('Geo 资产不接受复合匹配')
                field, values = next(iter(rule.items()))
                if field not in ('domain', 'domain_suffix', 'domain_regex', 'ip_cidr'):
                    raise ValueError('Geo 资产禁止关键词、ASN 或未知字段')
                if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
                    raise ValueError('Geo 匹配值必须为非空字符串数组')
                for value in values:
                    if field == 'ip_cidr':
                        network = ipaddress.ip_network(value, strict=False)
                        cidrs.add((network.version, network.network_address.packed, network.prefixlen))
                    elif field == 'domain_regex':
                        if len(value) > 4096 or '\x00' in value:
                            raise ValueError('正则长度或字符无效')
                        # 匹配语法最终由目标 Xray/RE2 的配置检查验证，不能用 Python re 冒充。
                        domains.add((1, value))
                    else:
                        domains.add((3 if field == 'domain' else 2, _domain(value)))
        counts[code] = {'domain_count': len(domains), 'cidr_count': len(cidrs)}
        encoded_code = _bytes_field(1, code.upper().encode('ascii'))
        if domains:
            entry = bytearray(encoded_code)
            for domain_type, value in sorted(domains):
                domain = _uint_field(1, domain_type) + _bytes_field(2, value.encode('utf-8'))
                entry.extend(_bytes_field(2, domain))
            geosite.extend(_bytes_field(1, bytes(entry)))
        if cidrs:
            entry = bytearray(encoded_code)
            for _, address, prefix in sorted(cidrs):
                cidr = _bytes_field(1, address) + _uint_field(2, prefix)
                entry.extend(_bytes_field(2, cidr))
            geoip.extend(_bytes_field(1, bytes(entry)))
    result = {}
    if geosite:
        result['megabox-geosite.dat'] = bytes(geosite)
    if geoip:
        result['megabox-geoip.dat'] = bytes(geoip)
    return result, counts


def v2rayng_clean_sources(sources):
    cleaned = copy.deepcopy(sources)
    cleaned['cn-domain'], audit = sanitize_ng_cn_domains(sources['cn-domain'], sources['proxy'])
    # 本地白名单在独立校验后并入 NG 的直连 Geo 集，仍排在代理集之后。
    cleaned['cn-domain']['rules'].extend(copy.deepcopy(sources['local-direct']['rules']))
    return cleaned, audit


def v2rayng_routes(sources, mode='whitelist'):
    """原生小路由表引用本项目资源文件，不依赖内置规则来源。"""
    require(mode in RULE_MODES, 'ng_mode')
    cleaned, _ = v2rayng_clean_sources(sources)
    _, counts = encode_geo_assets(cleaned)
    result = [{'remarks': '已批准局域网直连', 'ip': ['10.0.0.0/8', '172.16.0.0/12',
               '192.168.0.0/16', '127.0.0.0/8', '169.254.0.0/16', '::1/128', 'fc00::/7', 'fe80::/10'],
               'outboundTag': 'direct', 'enabled': True, 'locked': False}]
    def append(label, outbound, remarks):
        for field, count, filename in [('domain', 'domain_count', 'megabox-geosite.dat'),
                                        ('ip', 'cidr_count', 'megabox-geoip.dat')]:
            if counts[label][count]:
                result.append({'remarks': remarks, field: ['ext:' + filename + ':' + label],
                               'outboundTag': outbound, 'enabled': True, 'locked': False})
    append('p15-reject', 'block', '已批准拒绝')
    if mode != 'global':
        append('p15-proxy', 'proxy', 'AI媒体跟随当前代理')
        if mode == 'whitelist':
            append('p15-cn', 'direct', '大陆与学校直连')
    result.append({'remarks': '默认代理' if mode != 'blacklist' else '黑名单默认直连',
                   'port': '0-65535', 'outboundTag': 'direct' if mode == 'blacklist' else 'proxy',
                   'enabled': True, 'locked': False})
    return result


def android_config(parameters, sources):
    accounts = sorted(parameters['accounts'], key=lambda a: (p7.LANES.index(a['lane']), p7.PROTOCOLS.index(a['protocol'])))
    nodes = [p7._node(parameters, a, False) for a in accounts]
    names = [x['tag'] for x in nodes]
    outbounds = [{'type': 'selector', 'tag': 'PROXY', 'outbounds': names + ['HOME-Auto', 'BWH-Auto'],
                  'default': 'HOME-Reality', 'interrupt_exist_connections': True}]
    for lane in p7.LANES:
        prefix = lane.upper()
        outbounds.append({'type': 'urltest', 'tag': prefix + '-Auto',
                          'outbounds': [p7._tag(lane, protocol) for protocol in p7.PROTOCOLS],
                          'url': 'https://www.gstatic.com/generate_204', 'interval': '5m',
                          'tolerance': 50, 'interrupt_exist_connections': True})
    outbounds.extend(nodes)
    outbounds.append({'type': 'direct', 'tag': 'DIRECT', 'domain_resolver': 'direct-dns'})
    rule_sets = [{'type': 'inline', 'tag': name, 'rules': material['rules']}
                 for name, material in sources.items() if material['rules']]
    dns_rules = [{'domain': [parameters['ws_host'], parameters['edge_host']], 'server': 'bootstrap'}]
    route_rules = [{'inbound': ['tun-in'], 'port': 53, 'action': 'hijack-dns'},
                   {'action': 'sniff', 'sniffer': ['http', 'tls', 'quic'], 'timeout': '500ms'},
                   {'ip_cidr': p7.FAKE_RANGES, 'action': 'reject'},
                   {'ip_is_private': True, 'outbound': 'DIRECT'}]
    if sources['reject']['rules']:
        dns_rules.append({'rule_set': ['reject'], 'action': 'reject'})
        route_rules.append({'rule_set': ['reject'], 'action': 'reject'})
    # 模式名称均来自实际route和DNS，SFA 1.14.1可原生显示。
    dns_rules.extend([{'clash_mode': 'Global', 'server': 'remote'},
                      {'rule_set': ['proxy'], 'server': 'remote'}])
    if sources['local-direct']['rules']:
        dns_rules.append({'rule_set': ['local-direct'], 'server': 'direct-dns'})
    dns_rules.append({'clash_mode': 'Blacklist', 'server': 'direct-dns'})
    if sources['cn-domain']['rules']:
        dns_rules.append({'clash_mode': 'Rule', 'rule_set': ['cn-domain'], 'server': 'direct-dns'})
    route_rules.extend([{'clash_mode': 'Global', 'outbound': 'PROXY'},
                        {'rule_set': ['proxy'], 'outbound': 'PROXY'}])
    if sources['local-direct']['rules']:
        route_rules.append({'rule_set': ['local-direct'], 'outbound': 'DIRECT'})
    route_rules.extend([{'clash_mode': 'Blacklist', 'outbound': 'DIRECT'},
                        {'clash_mode': 'Rule', 'rule_set': ['cn'], 'outbound': 'DIRECT'}])
    return {'log': {'level': 'warn', 'timestamp': True},
            'inbounds': [{'type': 'tun', 'tag': 'tun-in', 'address': ['172.19.0.1/30', 'fdfe:dcba:9876::1/126'],
                          'mtu': 1400, 'auto_route': True, 'stack': 'mixed'}],
            'outbounds': outbounds,
            'dns': {'servers': [{'type': 'tcp', 'tag': 'remote', 'server': REMOTE_DNS, 'server_port': 53, 'detour': 'PROXY'},
                                {'type': 'https', 'tag': 'direct-dns', 'server': '223.5.5.5', 'server_port': 443,
                                 'path': '/dns-query', 'tls': {'enabled': True, 'server_name': 'dns.alidns.com'}},
                                {'type': 'https', 'tag': 'bootstrap', 'server': '223.5.5.5', 'server_port': 443,
                                 'path': '/dns-query', 'tls': {'enabled': True, 'server_name': 'dns.alidns.com'}}],
                    'rules': dns_rules, 'final': 'remote', 'strategy': 'prefer_ipv4', 'reverse_mapping': False,
                    'disable_cache': True},
            'route': {'rules': route_rules, 'rule_set': rule_sets, 'auto_detect_interface': True, 'default_domain_resolver': 'remote', 'final': 'PROXY'},
            'experimental': {'clash_api': {'default_mode': 'Rule'},
                             'cache_file': {'enabled': True, 'cache_id': 'megabox-p8-android-v1', 'store_fakeip': False}}}


def validate_bundle_content(parameters, sources, android, groups):
    require(all(a['enabled'] for a in parameters['accounts']), 'disabled_account_not_publishable')
    require(android['outbounds'][0]['default'] == 'HOME-Reality', 'default_reality_required')
    names = {node_name(a) for a in parameters['accounts']}
    require(len(names) == 8, 'unique_nodes_required')
    for lane, group in zip(p7.LANES, groups):
        matched = {name for name in names if re.search(group['ProtoExtraObj']['Filter'], name)}
        require(matched == {p7._tag(lane, protocol) for protocol in p7.PROTOCOLS}, 'group_identity_boundary')
    for outbound in android['outbounds']:
        if outbound['type'] == 'urltest':
            require(all(x.startswith(outbound['tag'].split('-')[0] + '-') for x in outbound['outbounds']), 'android_mixed_auto')
    require(android['dns']['servers'][0]['detour'] == 'PROXY', 'dns_must_follow_proxy')
    require(android['route']['final'] == 'PROXY', 'default_no_direct_fallback')


def build_bundle(parameters, cn_source, documents, output, *, blacklist_source=None,
                 local_direct_policy=None, fixture=False, core=None):
    p7.validate_parameters(parameters, fixture=fixture)
    require(all(a['enabled'] for a in parameters['accounts']), 'eight_enabled_accounts_required')
    sources = build_policy_sources(parameters, cn_source, documents, blacklist_source, local_direct_policy)
    groups = [policy_group(lane) for lane in p7.LANES]
    links = standard_links(parameters, fixture)
    android = android_config(parameters, sources)
    validate_bundle_content(parameters, sources, android, groups)
    files = {'nodes.txt': ('\n'.join(links) + '\n').encode(),
             'windows.txt': ('\n'.join(links + [inner_group_uri(x) for x in groups]) + '\n').encode(),
             'android.json': json_bytes(android), 'windows/dns-settings.json': json_bytes(windows_dns()),
             'windows/profiles.json': json_bytes(windows_profiles(parameters))}
    ng_links = [link for link in links if not link.startswith('anytls://')]
    require(len(ng_links) == 6, 'ng_six_nodes_required')
    files['v2rayng.txt'] = ('\n'.join(ng_links) + '\n').encode()
    files['v2rayng-routes.json'] = json_bytes(v2rayng_routes(sources))
    ng_sources, ng_dns_overlap = v2rayng_clean_sources(sources)
    ng_assets, ng_asset_counts = encode_geo_assets(ng_sources)
    files.update(ng_assets)
    for mode in RULE_MODES:
        files['v2rayng/routes/' + mode + '.json'] = json_bytes(v2rayng_routes(sources, mode))
    for mode, rules in windows_routes(sources).items():
        files['windows/routes/' + mode + '.json'] = json_bytes(rules)
    for name, material in sources.items():
        if material['rules']:
            files['windows/rulesets/' + name + '.json'] = json_bytes(material)
    mapping = [{'type': 'local', 'format': 'source', 'tag': ('geoip-p8-' if name == 'cn-ip' else 'geosite-p8-') + name,
                'path': '__P8_RULES_DIR__/' + name + '.json'} for name, material in sources.items() if material['rules']]
    # 内网集合内联映射，杜绝geoip:private触发未批准的规则源下载。
    private_source = {'version': 3, 'rules': [{'ip_cidr': ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                                                        '127.0.0.0/8', '169.254.0.0/16', '::1/128', 'fc00::/7', 'fe80::/10']}]}
    files['windows/rulesets/private.json'] = json_bytes(private_source)
    mapping.append({'type': 'local', 'format': 'source', 'tag': 'geoip-private', 'path': '__P8_RULES_DIR__/private.json'})
    files['windows/ruleset-map.json'] = json_bytes(mapping)
    public_sources = {name + '.json': material for name, material in sources.items() if material['rules']}
    public_sources['private.json'] = private_source
    public_hashes = {name: sha(json_bytes(material)) for name, material in sorted(public_sources.items())}
    files['windows-rules.json'] = json_bytes({'schema_version': 1,
                                             'generation': sha(json_bytes(public_hashes)),
                                             'files': public_sources, 'sha256': public_hashes})
    root = Path(output).resolve()
    require(not root.exists() and root.parent.is_dir(), 'new_output_directory_required')
    if fixture:
        require(PROJECT in root.parents, 'fixture_project_scope')
    elif os.name == 'nt':
        require(root.drive.upper() in ('D:', 'E:') and 'private' in [p.lower() for p in root.parts]
                and PROJECT not in root.parents, 'restricted_private_destination_required')
    else:
        require(Path('/root/relay-exports') in root.parents, 'server_export_scope')
    # Windows沿用受限父目录ACL；Python 3.13的0700会改写DACL并影响沙箱令牌访问。
    directory_mode = 0o777 if os.name == 'nt' else 0o700
    root.mkdir(mode=directory_mode)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(mode=directory_mode, parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(content)
        if os.name != 'nt':
            path.chmod(0o600)
    check = {'result': 'NOT TESTED'}
    if core:
        core = Path(core).resolve()
        result = subprocess.run([str(core), 'version'], capture_output=True, timeout=30)
        require(result.returncode == 0 and ('sing-box version ' + CORE_VERSION) in result.stdout.decode(errors='replace'), 'core_version')
        result = subprocess.run([str(core), 'check', '-c', str(root / 'android.json')], capture_output=True, timeout=30)
        require(result.returncode == 0, 'android_core_check_failed')
        check = {'result': 'PASS', 'version': CORE_VERSION, 'core_sha256': sha(core.read_bytes()), 'scope': 'syntax_only'}
    manifest = {'schema_version': SCHEMA_VERSION, 'public_fixture': fixture,
                'files': {name: sha(content) for name, content in sorted(files.items())},
                'counts': {'nodes': 8, 'groups': 2, 'android_configs': 1},
                'v2rayng_counts': {'nodes': 6, 'native_groups_manual_setup': 2},
                'v2rayng_geo_counts': ng_asset_counts, 'v2rayng_dns_overlap_removed': ng_dns_overlap,
                'default_node': 'HOME-Reality', 'remote_dns': REMOTE_DNS_URI,
                'builder_sha256': sha(Path(__file__).read_bytes()),
                'p7_validator_sha256': sha(Path(p7.__file__).read_bytes()),
                'policy': {'media': 'follow_current', 'ai': 'follow_current', 'auto_groups': 'same_identity_only',
                           'blacklist': 'explicit_source_plus_protected_and_media' if blacklist_source else 'protected_and_media_only',
                           'android_dns_cache': 'disabled_to_avoid_cross_exit_answers'},
                'source_sha256': {name: sha(json_bytes(source)) for name, source in sources.items()},
                'core_check': check, 'runtime_acceptance': 'NOT TESTED', 'kill_switch': 'NOT TESTED'}
    (root / 'manifest.json').write_bytes(json_bytes(manifest))
    if os.name != 'nt':
        (root / 'manifest.json').chmod(0o600)
    return manifest


def main():
    parser = argparse.ArgumentParser(description='只生成P8订阅；不运行代理，不设置网络。')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--parameters', type=Path)
    source.add_argument('--fixture', action='store_true')
    parser.add_argument('--cn-rules', type=Path)
    parser.add_argument('--blacklist-rules', type=Path)
    parser.add_argument('--local-direct-policy', type=Path)
    parser.add_argument('--rules-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--core', type=Path)
    args = parser.parse_args()
    if args.fixture:
        parameters, documents = p7.fixture_parameters()
        cn = {'version': 3, 'rules': [{'domain_suffix': ['domestic.example.cn', 'googleapis.cn']}]}
    else:
        require(args.rules_dir is not None and args.cn_rules is not None, 'policy_sources_required')
        parameters = p7.loads_unique(args.parameters.read_text(encoding='utf-8'))
        p7.validate_parameters(parameters)
        documents = p7.load_rule_documents(parameters['policy'], args.rules_dir)
        cn = p7.loads_unique(args.cn_rules.read_text(encoding='utf-8'))
    if args.cn_rules:
        cn = p7.loads_unique(args.cn_rules.read_text(encoding='utf-8'))
    black = p7.loads_unique(args.blacklist_rules.read_text(encoding='utf-8')) if args.blacklist_rules else None
    local_direct = p7.loads_unique(args.local_direct_policy.read_text(encoding='utf-8')) if args.local_direct_policy else None
    manifest = build_bundle(parameters, cn, documents, args.output, blacklist_source=black,
                            local_direct_policy=local_direct, fixture=args.fixture, core=args.core)
    print(json.dumps({'result': 'BUILT', 'public_fixture': args.fixture, 'files': len(manifest['files']),
                      'core_check': manifest['core_check']['result'], 'network_started': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('STOPPED: p8_bundle_failed') from None
