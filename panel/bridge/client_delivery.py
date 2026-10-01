"""独立订阅的隔离客户端交付；复用权威规则，不读取任何生产秘密。

本模块只签发 isolated 产物。真实生产计量及撤销门槛未通过，不能通过
调用参数把隔离产物提升为 production。客户端界面实测仍独立验收。
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import uuid
from urllib.parse import quote, urlencode

from .vendor import client_bundle as legacy

LIMIT = 8 * 1024 * 1024
CLIENTS = frozenset({'windows', 'v2rayng', 'android'})
SOURCE_KEYS = frozenset({'cn', 'cn-domain', 'cn-ip', 'proxy', 'reject', 'local-direct'})


class DeliveryError(ValueError):
    """固定错误代码，不包含节点、路径或凭据。"""


def require(condition, code):
    if not condition:
        raise DeliveryError(code)


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _sources(value):
    require(type(value) is dict and set(value) == SOURCE_KEYS, 'policy_sources_invalid')
    for material in value.values():
        require(type(material) is dict and set(material) == {'version', 'rules'}
                and material['version'] == 3 and type(material['rules']) is list, 'policy_source_invalid')
        for rule in material['rules']:
            require(type(rule) is dict and len(rule) == 1
                    and set(rule) <= {'domain', 'domain_suffix', 'domain_regex', 'ip_cidr'}, 'policy_match_invalid')
            field, values = next(iter(rule.items()))
            require(type(values) is list and values and all(type(x) is str and 0 < len(x) <= 4096
                    and '\x00' not in x for x in values), 'policy_values_invalid')
            if field == 'ip_cidr':
                for item in values:
                    require(ipaddress.ip_network(item, strict=True).prefixlen > 0, 'policy_cidr_invalid')
    require(value['proxy']['rules'] and value['cn']['rules'], 'policy_required_sources_empty')
    return copy.deepcopy(value)


def _nodes(values):
    require(type(values) is list and 0 < len(values) <= 256, 'nodes_invalid')
    nodes, identities, groups, names, ids = [], [], {}, set(), set()
    for item in values:
        require(type(item) is dict and set(item) == {'identity_id', 'line_id', 'name', 'outbound'}, 'node_fields_invalid')
        identity, line = item['identity_id'], item['line_id']
        require(type(identity) is int and identity > 0 and identity not in ids
                and type(line) is int and line > 0, 'node_identity_invalid')
        name = item['name']
        require(type(name) is str and 0 < len(name) <= 128 and name not in names
                and name not in {'PROXY', 'DIRECT'} and not name.startswith('line-auto-')
                and not any(ord(c) < 32 for c in name), 'node_name_invalid')
        node = copy.deepcopy(item['outbound'])
        require(type(node) is dict and node.get('type') in {'vless', 'hysteria2', 'anytls'}
                and type(node.get('server')) is str and node['server']
                and type(node.get('server_port')) is int and 0 < node['server_port'] < 65536,
                'node_endpoint_invalid')
        require(not any(key in node for key in ('detour', 'bind_interface', 'routing_mark', 'protect_path')),
                'node_routing_override_denied')
        tls = node.get('tls', {})
        require(tls.get('enabled') is True and not tls.get('insecure') and not tls.get('key')
                and not tls.get('key_path') and not tls.get('certificate_path'), 'node_tls_invalid')
        node['tag'] = name
        identities.append({'identity_id': identity, 'line_id': line, 'node_tag': name})
        groups.setdefault(line, []).append(name)
        nodes.append(node)
        names.add(name)
        ids.add(identity)
    return nodes, identities, groups


def _uri(node, client):
    """只编码已支持标准链接字段；TLS 可信根无法塞进 URI 时明确拒绝。"""
    tls = node['tls']
    require(not tls.get('certificate'), 'uri_custom_trust_not_supported')
    protocol = node['type']
    require(not (client == 'v2rayng' and protocol == 'anytls'), 'client_protocol_not_supported')
    server = node['server']
    require(not any(c in server for c in '/?#@'), 'node_server_invalid')
    if ':' in server:
        server = '[' + server + ']'
    authority = server + ':' + str(node['server_port'])
    query = {'sni': tls.get('server_name', node['server']), 'allowInsecure': '0'}
    if tls.get('alpn'):
        query['alpn'] = ','.join(tls['alpn'])
    if protocol == 'vless':
        require(type(node.get('uuid')) is str, 'node_credential_invalid')
        try:
            credential = str(uuid.UUID(node['uuid']))
        except ValueError:
            raise DeliveryError('node_credential_invalid') from None
        query.update(encryption='none', security='tls', type='tcp')
        if node.get('flow'):
            query['flow'] = node['flow']
        if tls.get('reality', {}).get('enabled'):
            query.update(security='reality', pbk=tls['reality']['public_key'], sid=tls['reality']['short_id'])
            query['fp'] = tls.get('utls', {}).get('fingerprint', 'chrome')
        if node.get('transport'):
            transport = node['transport']
            require(transport.get('type') == 'ws', 'uri_transport_not_supported')
            query.update(type='ws', path=transport.get('path', '/'))
            host = transport.get('headers', {}).get('Host')
            if host:
                query['host'] = host
        scheme = 'vless'
    else:
        require(type(node.get('password')) is str and node['password'], 'node_credential_invalid')
        credential = node['password']
        scheme = 'hysteria2' if protocol == 'hysteria2' else 'anytls'
        require(not node.get('obfs'), 'uri_obfs_not_supported')
    return scheme + '://' + quote(credential, safe='') + '@' + authority + '?' + urlencode(query) + '#' + quote(node['tag'], safe='')


def render(client, nodes, sources):
    """消费已审核规则材料和此份订阅的身份；仅生成所选软件资源。

sources 应来自 legacy.build_policy_sources 的同一权威规则版本，调用者
不得以客户端选项扩大 Direct。此层不提供黑名单/全局切换或公网发布。
"""
    require(client in CLIENTS, 'client_not_supported')
    sources = _sources(sources)
    outbounds, identities, groups = _nodes(nodes)
    resources = {}

    def add(key, filename, raw, content_type='application/json'):
        require(type(raw) is bytes and 0 < len(raw) <= LIMIT, 'resource_size_invalid')
        resources[key] = {'filename': filename, 'content': raw, 'content_type': content_type}

    if client == 'android':
        # 原生成器在此处硬编码八节点；传空列表仅取审核过的路由/DNS模板，
        # 随即以此独立订阅获准的身份取代整个出站列表，不沿用共享凭据。
        params = {'accounts': [], 'ws_host': outbounds[0]['server'], 'edge_host': outbounds[0]['server']}
        config = legacy.android_config(params, sources)
        selectors = [{'type': 'urltest', 'tag': 'line-auto-' + str(line), 'outbounds': names,
                      'url': 'https://www.gstatic.com/generate_204', 'interval': '5m',
                      'tolerance': 50, 'interrupt_exist_connections': True} for line, names in groups.items()]
        config['outbounds'] = [{'type': 'selector', 'tag': 'PROXY',
                               'outbounds': [n['tag'] for n in outbounds] + [s['tag'] for s in selectors],
                               'default': outbounds[0]['tag'], 'interrupt_exist_connections': True}] + selectors + outbounds + [
                               {'type': 'direct', 'tag': 'DIRECT', 'domain_resolver': 'direct-dns'}]
        hostnames = []
        for node in outbounds:
            try:
                ipaddress.ip_address(node['server'])
            except ValueError:
                hostnames.append(node['server'])
        if hostnames:
            config['dns']['rules'][0]['domain'] = sorted(set(hostnames))
        else:
            config['dns']['rules'].pop(0)
        config['experimental']['cache_file']['cache_id'] = 'xingzhou-isolated-' + digest(encoded(identities))[:16]
        add('subscription', 'subscription.json', encoded(config))
    else:
        links = [_uri(node, client) for node in outbounds]
        if client == 'windows':
            for line, names in groups.items():
                group = {'ConfigVersion': 4, 'ConfigType': 101, 'CoreType': 24,
                         'Remarks': '线路 ' + str(line) + ' 自动选择',
                         'ProtoExtraObj': {'GroupType': 'PolicyGroup', 'SubChildItems': 'self',
                                          'Filter': '^(?:' + '|'.join(re.escape(n) for n in names) + ')$', 'MultipleLoad': 0}}
                links.append(legacy.inner_group_uri(group))
            add('routing', 'routing.json', encoded(legacy.windows_routes(sources)['whitelist']))
            dns = legacy.windows_dns()
            dns['default_node_remarks'] = outbounds[0]['tag']
            dns['default_routing'] = '行舟-规则'
            add('dns', 'dns-settings.json', encoded(dns))
            mapping = []
            all_sources = copy.deepcopy(sources)
            all_sources['private'] = {'version': 3, 'rules': [{'ip_cidr': ['10.0.0.0/8', '172.16.0.0/12',
                '192.168.0.0/16', '127.0.0.0/8', '169.254.0.0/16', '::1/128', 'fc00::/7', 'fe80::/10']}]}
            for name, material in all_sources.items():
                if material['rules']:
                    add('rule-source-' + name, name + '.json', encoded(material))
                    tag = 'geoip-private' if name == 'private' else ('geoip-p8-' if name == 'cn-ip' else 'geosite-p8-') + name
                    mapping.append({'type': 'local', 'format': 'source', 'tag': tag,
                                    'path': '__XINGZHOU_RULES_DIR__/' + name + '.json'})
            add('ruleset-map', 'ruleset-map.json', encoded(mapping))
        else:
            add('routing', 'routing.json', encoded(legacy.v2rayng_routes(sources)))
            cleaned, overlap = legacy.v2rayng_clean_sources(sources)
            assets, _ = legacy.encode_geo_assets(cleaned)
            for filename, raw in assets.items():
                add('geosite' if filename.endswith('geosite.dat') else 'geoip', filename, raw, 'application/octet-stream')
            add('dns', 'dns-guidance.json', encoded({'schema': 1, 'automatic_import': False,
                'remote_dns': legacy.REMOTE_DNS_URI, 'direct_dns': 'https://dns.alidns.com/dns-query',
                'policy': '使用经审核直连域名集合进行DNS分流；配置效果需客户端验收',
                'removed_proxy_overlaps': overlap}))
        add('subscription', 'subscription.txt', ('\n'.join(links) + '\n').encode(), 'text/plain; charset=utf-8')
    return {'client': client, 'evidence_scope': 'isolated', 'identities': identities,
            'policy_sha256': digest(encoded(sources)), 'resources': resources,
            'runtime_acceptance': 'NOT TESTED'}


def _write(path, raw):
    with path.open('xb') as stream:
        if os.name == 'posix':
            os.chmod(path, 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _sync(path):
    if os.name == 'posix':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def publish(root, subscription, generation, rendered, *, expected_release=None):
    """先落不可变整批资源，再原子切换清单；固定URL和身份均无需变化。

锁和 expected_release 防止并发任务把新内容覆盖为旧版。失败留下的未引用
release 不是有效发布；previous current 保持原状。不会清理任何旧产物。
"""
    try:
        subscription = str(uuid.UUID(str(subscription)))
    except (ValueError, TypeError, AttributeError):
        raise DeliveryError('subscription_invalid') from None
    require(type(generation) is int and generation > 0, 'generation_invalid')
    require(rendered.get('evidence_scope') == 'isolated' and rendered.get('client') in CLIENTS,
            'production_publication_not_verified')
    root = Path(root).absolute()
    require(not root.is_symlink() and not any(p.is_symlink() for p in root.parents), 'root_symlink')
    root.mkdir(parents=True, exist_ok=True)
    directory = root
    for component in ('subscriptions', subscription, str(generation)):
        directory = directory / component
        require(not directory.is_symlink(), 'publication_symlink')
        directory.mkdir(exist_ok=True)
    lock = directory / '.publish-lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise DeliveryError('publication_busy') from None
    temporary = directory / ('.current-' + secrets.token_hex(16))
    try:
        current = directory / 'current.json'
        require(not current.is_symlink(), 'manifest_symlink')
        previous = json.loads(current.read_bytes()).get('release') if current.exists() else None
        require(previous == expected_release, 'publication_version_conflict')
        release = secrets.token_hex(16)
        releases = directory / 'releases'
        require(not releases.is_symlink(), 'release_symlink')
        releases.mkdir(exist_ok=True)
        destination = releases / release
        destination.mkdir()
        manifest = {key: copy.deepcopy(rendered[key]) for key in ('client', 'evidence_scope', 'identities', 'policy_sha256', 'runtime_acceptance')}
        manifest.update(schema=2, subscription=subscription, generation=generation, release=release,
                        primary='subscription', resources={})
        names = set()
        require('subscription' in rendered['resources'] and len(rendered['resources']) <= 32, 'resources_invalid')
        for key, resource in rendered['resources'].items():
            filename, raw = resource['filename'], resource['content']
            require(re.fullmatch(r'[a-z][a-z0-9-]{0,63}', key) and re.fullmatch(r'[a-z][a-z0-9.-]{0,90}', filename)
                    and filename not in names and type(raw) is bytes and 0 < len(raw) <= LIMIT, 'resource_invalid')
            names.add(filename)
            _write(destination / filename, raw)
            manifest['resources'][key] = {'filename': filename, 'bytes': len(raw), 'sha256': digest(raw),
                                         'content_type': resource['content_type']}
        _sync(destination)
        _sync(releases)
        _write(temporary, encoded(manifest))
        os.replace(temporary, current)
        _sync(directory)
        return manifest
    finally:
        temporary.unlink(missing_ok=True)
        lock.rmdir()
