"""离线生成八身份客户端；不启动核心、不修改操作系统网络、不打印凭据。"""
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
import uuid
from urllib.parse import quote, urlencode

PROTOCOLS = ('reality', 'hy2', 'ws', 'anytls')
LANES = ('home', 'bwh')
PLATFORMS = ('Windows', 'Android')
MODES = ('daily', 'home-only')
DNS_MODES = ('fakeip', 'real')
SERVICE_NAMES = ('netflix', 'disney', 'hbo', 'hotstar', 'dazn', 'viu',
                 'bilibili', 'hulu-jp', 'kktv', 'viutv')
FAKE_RANGES = ['198.18.0.0/15', 'fc00::/18']
PARAMETER_FIELDS = {'schema_version', 'server_ipv4', 'edge_host', 'ws_host', 'reality_server',
                    'reality_public_key', 'reality_short_id', 'ws_path', 'accounts', 'policy'}
ACCOUNT_FIELDS = {'id', 'protocol', 'lane', 'enabled', 'label', 'credential'}
POLICY_FIELDS = {'streaming_rule_sets', 'protected_suffixes', 'cn_direct_suffixes', 'custom_rules'}
PROJECT = Path(__file__).resolve().parents[2]
CORES = {
    'Windows': PROJECT / 'staging/client-ready/v2rayN-7.24.9-prepared/bin/sing_box/sing-box.exe',
    'Android': PROJECT / 'staging/p2-ready/tools/windows-amd64/sing-box-1.14.1-windows-amd64/sing-box.exe',
}
CORE_VERSIONS = {'Windows': '1.13.19', 'Android': '1.14.1'}


def reject(condition, code):
    if condition:
        raise ValueError(code)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        reject(key in result, 'duplicate_json_key')
        result[key] = value
    return result


def loads_unique(text):
    return json.loads(text, object_pairs_hook=_unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('json_constant')))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _domain(value):
    reject(not isinstance(value, str) or len(value) > 253 or value != value.lower() or
           '.' not in value or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p)
                                   for p in value.split('.')), 'policy_domain')


def _overlap(a, b):
    return a == b or a.endswith('.' + b) or b.endswith('.' + a)


def validate_parameters(parameters, *, fixture=False, initial_eight=True):
    reject(not isinstance(parameters, dict) or set(parameters) != PARAMETER_FIELDS, 'client_parameter_fields')
    reject(type(parameters['schema_version']) is not int or parameters['schema_version'] != 1,
           'client_schema_version')
    reject(parameters['server_ipv4'] != '192.0.2.10' or parameters['edge_host'] != 'edge.relay.example.invalid'
           or parameters['ws_host'] != 'ws.relay.example.invalid'
           or parameters['reality_server'] != 'www.microsoft.com', 'client_endpoint_scope')
    public = parameters['reality_public_key']
    reject(not isinstance(public, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', public), 'reality_public_key')
    reject(len(base64.urlsafe_b64decode(public + '=')) != 32, 'reality_public_key')
    reject(not isinstance(parameters['reality_short_id'], str) or
           not re.fullmatch(r'[0-9a-f]{16}', parameters['reality_short_id']), 'reality_short_id')
    reject(not isinstance(parameters['ws_path'], str) or
           not re.fullmatch(r'/[A-Za-z0-9/_-]{23,511}', parameters['ws_path']), 'ws_path')
    accounts = parameters['accounts']
    reject(not isinstance(accounts, list) or (len(accounts) != 8 if initial_eight else not 1 <= len(accounts) <= 256),
           'eight_accounts_required' if initial_eight else 'account_count')
    pairs, ids, credentials = set(), set(), set()
    for account in accounts:
        reject(not isinstance(account, dict) or set(account) != ACCOUNT_FIELDS, 'account_fields')
        protocol, lane = account['protocol'], account['lane']
        reject(protocol not in PROTOCOLS or lane not in LANES, 'account_lane_protocol')
        reject((initial_eight and (lane, protocol) in pairs) or account['id'] in ids, 'account_duplicate')
        reject(not isinstance(account['id'], str) or not account['id'] or
               not isinstance(account['label'], str) or len(account['label']) > 100 or
               type(account['enabled']) is not bool, 'account_metadata')
        credential = account['credential']
        reject(not isinstance(credential, str) or credential in credentials, 'account_credential_reuse')
        if protocol in ('reality', 'ws'):
            parsed = uuid.UUID(credential)
            reject(str(parsed) != credential or parsed.version != 4, 'account_uuid')
        else:
            reject(not 32 <= len(credential) <= 256, 'account_password_length')
        if not fixture:
            reject('PUBLIC-' in credential or 'SYNTHETIC' in credential or
                   credential.startswith('00000000-0000-4000-8000-'), 'fixture_not_for_delivery')
        pairs.add((lane, protocol)); ids.add(account['id']); credentials.add(credential)
    if not fixture:
        reject(public == base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='), 'fixture_public_key')
    validate_policy(parameters['policy'])


def validate_policy(policy):
    reject(not isinstance(policy, dict) or set(policy) != POLICY_FIELDS, 'policy_fields')
    for field in ('protected_suffixes', 'cn_direct_suffixes'):
        values = policy[field]
        reject(not isinstance(values, list) or len(values) != len(set(values)), 'policy_domains')
        for value in values:
            _domain(value)
    reject(not policy['protected_suffixes'], 'protected_policy_required')
    for domain in policy['cn_direct_suffixes']:
        reject(any(_overlap(domain, p) for p in policy['protected_suffixes']), 'cn_protected_overlap')
    sets = policy['streaming_rule_sets']
    reject(not isinstance(sets, list) or len(sets) != 10, 'ten_streaming_services_required')
    seen = set()
    for material in sets:
        reject(not isinstance(material, dict) or set(material) != {'service', 'path', 'sha256'}, 'streaming_fields')
        reject(material['service'] not in SERVICE_NAMES or material['service'] in seen, 'streaming_service')
        reject(not isinstance(material['path'], str) or
               not re.fullmatch(r'[0-9a-f]{64}', material['sha256']), 'streaming_digest')
        seen.add(material['service'])
    custom = policy['custom_rules']
    reject(not isinstance(custom, list), 'custom_rules_type')
    ids = set()
    for rule in custom:
        reject(not isinstance(rule, dict) or set(rule) != {'id', 'domain', 'match', 'lane', 'enabled', 'priority'},
               'custom_rule_fields')
        reject(not isinstance(rule['id'], str) or rule['id'] in ids or
               rule['match'] not in ('exact', 'suffix') or rule['lane'] not in ('home', 'bwh', 'direct', 'reject')
               or type(rule['enabled']) is not bool or type(rule['priority']) is not int, 'custom_rule_metadata')
        _domain(rule['domain'])
        reject(rule['lane'] in ('bwh', 'direct') and
               any(_overlap(rule['domain'], p) for p in policy['protected_suffixes']), 'custom_protected_overlap')
        ids.add(rule['id'])


def _streaming_rules(policy, documents):
    """消费已经按原件摘要核验的文档；最终规则内联，不导出服务器路径。"""
    reject(set(documents) != set(SERVICE_NAMES), 'streaming_documents')
    rules = []
    for material in policy['streaming_rule_sets']:
        doc = documents[material['service']]
        reject(not isinstance(doc, dict) or set(doc) != {'version', 'rules'} or
               doc['version'] not in (2, 3) or not isinstance(doc['rules'], list) or not doc['rules'], 'streaming_document')
        for rule in doc['rules']:
            reject(not isinstance(rule, dict) or not rule or
                   set(rule) - {'domain', 'domain_suffix', 'domain_regex'}, 'streaming_domain_rules_only')
            for field, values in rule.items():
                reject(not isinstance(values, list) or not values or
                       any(not isinstance(v, str) for v in values), 'streaming_rule_values')
                if field != 'domain_regex':
                    for value in values:
                        _domain(value)
                        reject(any(_overlap(value, p) for p in policy['protected_suffixes']), 'media_protected_overlap')
                else:
                    reject(material['service'] not in ('viu', 'viutv') or
                           values != [r'^ewcdn[0-9]+\.nowe\.com$'], 'media_regex_not_reviewed')
            if rule not in rules:
                rules.append(copy.deepcopy(rule))
    return rules


def load_rule_documents(policy, rules_dir):
    """只读从指定目录按服务定位原件，拒绝路径逃逸及摘要不匹配。"""
    root = Path(rules_dir).resolve()
    documents = {}
    for material in policy['streaming_rule_sets']:
        name = Path(material['path'].replace('\\', '/')).name
        path = (root / name).resolve()
        reject(path.parent != root or not path.is_file(), 'rules_file_scope')
        raw = path.read_bytes()
        reject(hashlib.sha256(raw).hexdigest() != material['sha256'], 'rules_file_hash')
        documents[material['service']] = loads_unique(raw.decode('utf-8'))
    _streaming_rules(policy, documents)
    return documents


def _tag(lane, protocol):
    return lane.upper() + '-' + {'reality': 'Reality', 'hy2': 'HY2', 'ws': 'WS', 'anytls': 'AnyTLS'}[protocol]


def _node(parameters, account, windows):
    protocol = account['protocol']
    tls = {'enabled': True, 'server_name': parameters['edge_host']}
    node = {'tag': _tag(account['lane'], protocol), 'server': parameters['server_ipv4'], 'tls': tls}
    if protocol == 'reality':
        node.update(type='vless', server_port=443, uuid=account['credential'], flow='xtls-rprx-vision')
        tls.update(server_name=parameters['reality_server'], utls={'enabled': True, 'fingerprint': 'chrome'},
                   reality={'enabled': True, 'public_key': parameters['reality_public_key'],
                            'short_id': parameters['reality_short_id']})
        if windows:
            node['bind_interface'] = 'WLAN 2'
    elif protocol == 'hy2':
        node.update(type='hysteria2', server_port=8443, password=account['credential'])
        tls['alpn'] = ['h3']
    elif protocol == 'ws':
        node.update(type='vless', server=parameters['ws_host'], server_port=8443, uuid=account['credential'],
                    domain_resolver={'server': 'bootstrap', 'strategy': 'ipv4_only'},
                    transport={'type': 'ws', 'path': parameters['ws_path'], 'headers': {'Host': parameters['ws_host']}})
        tls['server_name'] = parameters['ws_host']
    else:
        node.update(type='anytls', server_port=9443, password=account['credential'])
    return node


def _business_rules(policy, streaming):
    """连接与真实 DNS 共用同一顺序，避免分类漂移。"""
    rules = [({'domain_suffix': policy['protected_suffixes']}, 'home')]
    rules.extend((rule, 'bwh') for rule in streaming)
    # 自定义规则受敏感/媒体硬边界约束；priority 仅控制自定义规则内部顺序。
    rules.extend(({'domain' if r['match'] == 'exact' else 'domain_suffix': [r['domain']]}, r['lane'])
                 for r in sorted(policy['custom_rules'], key=lambda r: (r['priority'], r['id'])) if r['enabled'])
    if policy['cn_direct_suffixes']:
        rules.append(({'domain_suffix': policy['cn_direct_suffixes']}, 'direct'))
    return rules


def preview_domain(policy, documents, domain):
    """与生成器共用分类顺序的离线预览；不查询 DNS，不保证实机流量已命中。"""
    validate_policy(policy)
    streaming = _streaming_rules(policy, documents)
    reject(not isinstance(domain, str), 'preview_domain')
    try:
        address = ipaddress.ip_address(domain)
    except ValueError:
        _domain(domain)
    else:
        lane = 'reject' if not address.is_global or domain == '192.0.2.10' else 'home'
        return {'lane': lane, 'reason': 'literal_ip_policy', 'runtime_verified': False}
    for index, (match, lane) in enumerate(_business_rules(policy, streaming)):
        if (domain in match.get('domain', []) or
            any(domain == suffix or domain.endswith('.' + suffix) for suffix in match.get('domain_suffix', [])) or
            any(re.search(pattern, domain) for pattern in match.get('domain_regex', []))):
            return {'lane': lane, 'reason': 'domain_policy', 'rule_index': index, 'runtime_verified': False}
    return {'lane': 'home', 'reason': 'unknown_default', 'runtime_verified': False}


def configuration(parameters, rule_documents, platform, mode, dns_mode, *, fixture=False,
                  home_protocol='hy2', bwh_protocol='hy2', local_dns_server='223.5.5.5'):
    validate_parameters(parameters, fixture=fixture)
    reject(platform not in PLATFORMS or mode not in MODES or dns_mode not in DNS_MODES, 'profile_choice')
    reject(home_protocol not in PROTOCOLS or bwh_protocol not in PROTOCOLS, 'selected_protocol')
    reject(local_dns_server != '223.5.5.5', 'local_dns_not_reviewed')
    policy = parameters['policy']; streaming = _streaming_rules(policy, rule_documents)
    daily, windows = mode == 'daily', platform == 'Windows'
    active = [a for a in parameters['accounts'] if a['enabled'] and (daily or a['lane'] == 'home')]
    nodes = [_node(parameters, a, windows) for a in active]
    outbounds = []
    for lane, protocol in [('home', home_protocol)] + ([('bwh', bwh_protocol)] if daily else []):
        names = [_tag(lane, a['protocol']) for a in active if a['lane'] == lane]
        reject(_tag(lane, protocol) not in names, 'selected_account_disabled')
        outbounds.append({'type': 'selector', 'tag': lane.upper(), 'outbounds': names,
                          'default': _tag(lane, protocol), 'interrupt_exist_connections': True})
    outbounds.extend(nodes)
    if daily:
        local = {'type': 'direct', 'tag': 'LOCAL', 'domain_resolver': 'local-dns'}
        if windows:
            local['bind_interface'] = 'WLAN 2'
        outbounds.append(local)
    servers = [{'type': 'tcp', 'tag': 'home-dns', 'server': parameters['server_ipv4'],
                'server_port': 53, 'detour': 'HOME'}]
    if daily:
        servers.extend([{'type': 'tcp', 'tag': 'bwh-dns', 'server': parameters['server_ipv4'],
                         'server_port': 53, 'detour': 'BWH'},
                        {'type': 'udp', 'tag': 'local-dns', 'server': local_dns_server,
                         'server_port': 53, 'detour': 'LOCAL'}])
    if dns_mode == 'fakeip':
        servers.append({'type': 'fakeip', 'tag': 'fakeip', 'inet4_range': FAKE_RANGES[0],
                        'inet6_range': FAKE_RANGES[1]})
    if any(a['protocol'] == 'ws' for a in active):
        bootstrap = {'type': 'https', 'tag': 'bootstrap', 'server': '223.5.5.5', 'server_port': 443,
                     'path': '/dns-query', 'tls': {'enabled': True, 'server_name': 'dns.alidns.com'}}
        if windows:
            bootstrap['bind_interface'] = 'WLAN 2'
        servers.append(bootstrap)
    dns_rules = []
    if dns_mode == 'fakeip':
        dns_rules.append({'inbound': ['client-in'], 'query_type': ['A', 'AAAA'],
                          'action': 'route', 'server': 'fakeip'})
    route_rules = [{'inbound': ['client-in'], 'port': [53], 'action': 'hijack-dns'},
                   {'ip_cidr': list(FAKE_RANGES), 'action': 'reject'},
                   {'ip_is_private': True, 'action': 'reject'},
                   {'ip_cidr': [parameters['server_ipv4'] + '/32'], 'action': 'reject'}]
    if daily and dns_mode == 'real':
        # 真实 DNS 的反向映射记录 CNAME 最终 owner 且共享 IP 会互相覆盖，不能用于直连放行。
        # 只检查裸 IP 请求中可见的协议域名；显式 SOCKS 域名和恢复后的 FakeIP 不被覆盖。
        route_rules.append({'type': 'logical', 'mode': 'or', 'rules': [{'ip_version': 4}, {'ip_version': 6}],
                            'action': 'sniff', 'sniffer': ['tls', 'http', 'quic'], 'timeout': '500ms'})
    if daily:
        for match, lane in _business_rules(policy, streaming):
            route_rule = {**copy.deepcopy(match), 'action': 'reject' if lane == 'reject' else 'route'}
            dns_rule = {**copy.deepcopy(match), 'action': 'reject' if lane == 'reject' else 'route'}
            if lane != 'reject':
                route_rule['outbound'] = {'home': 'HOME', 'bwh': 'BWH', 'direct': 'LOCAL'}[lane]
                dns_rule['server'] = {'home': 'home-dns', 'bwh': 'bwh-dns', 'direct': 'local-dns'}[lane]
            if lane == 'direct':
                # 域名/FakeIP 恢复后先取得实际目标，再做第二次地址检查，防止 DNS 重绑定绕过。
                route_rules.extend([
                    {**copy.deepcopy(match), 'action': 'resolve', 'server': 'local-dns'},
                    {'ip_cidr': list(FAKE_RANGES), 'action': 'reject'},
                    {'ip_is_private': True, 'action': 'reject'},
                    {'ip_cidr': [parameters['server_ipv4'] + '/32'], 'action': 'reject'},
                ])
            route_rules.append(route_rule); dns_rules.append(dns_rule)
    dns = {'servers': servers, 'rules': dns_rules, 'final': 'home-dns', 'strategy': 'prefer_ipv4',
           'reverse_mapping': False}
    if windows:
        dns['independent_cache'] = True
    inbound = {'type': 'mixed', 'tag': 'client-in', 'listen': '127.0.0.1', 'listen_port': 10808}
    if not windows:
        # Android SFA 的 strict_route 官方标为未实现，故不把该字段当保护开关。
        inbound = {'type': 'tun', 'tag': 'client-in', 'address': ['172.19.0.1/30', 'fdfe:dcba:9876::1/126'],
                   'mtu': 1400, 'auto_route': True, 'stack': 'mixed'}
    return {'log': {'level': 'warn', 'timestamp': True}, 'inbounds': [inbound], 'outbounds': outbounds,
            'dns': dns, 'route': {'rules': route_rules, 'default_domain_resolver': 'home-dns', 'final': 'HOME'}}


def build_profiles(parameters, rule_documents, *, fixture=False, home_protocol='hy2', bwh_protocol='hy2'):
    return {platform: {mode + '-' + dns_mode + '.json': configuration(
        parameters, rule_documents, platform, mode, dns_mode, fixture=fixture,
        home_protocol=home_protocol, bwh_protocol=bwh_protocol)
        for mode in MODES for dns_mode in DNS_MODES} for platform in PLATFORMS}


def build_node_links(parameters, *, fixture=False):
    """返回启用账号的标准节点 URI；仅供受限文件导出，绝不打印，不包含完整分流。"""
    validate_parameters(parameters, fixture=fixture, initial_eight=False)
    links = []
    for account in parameters['accounts']:
        if not account['enabled']:
            continue
        protocol = account['protocol']
        query = {}
        server = parameters['server_ipv4']
        if protocol == 'reality':
            scheme, port = 'vless', 443
            query.update(encryption='none', security='reality', type='tcp', flow='xtls-rprx-vision',
                         sni=parameters['reality_server'], fp='chrome',
                         pbk=parameters['reality_public_key'], sid=parameters['reality_short_id'])
        elif protocol == 'ws':
            scheme, port, server = 'vless', 8443, parameters['ws_host']
            query.update(encryption='none', security='tls', type='ws', sni=parameters['ws_host'],
                         host=parameters['ws_host'], path=parameters['ws_path'])
        elif protocol == 'hy2':
            scheme, port = 'hysteria2', 8443
            query.update(sni=parameters['edge_host'], alpn='h3')
        else:
            scheme, port = 'anytls', 9443
            query.update(sni=parameters['edge_host'])
        label = _tag(account['lane'], protocol) + '-' + account['id']
        links.append(scheme + '://' + quote(account['credential'], safe='') + '@' + server + ':' + str(port)
                     + '?' + urlencode(query, quote_via=quote) + '#' + quote(label, safe=''))
    return links


def validate_profile(profile, parameters, rule_documents, platform, mode, dns_mode, **options):
    reject(profile != configuration(parameters, rule_documents, platform, mode, dns_mode, **options),
           'client_profile_drift')
    return 'CLIENT-CANDIDATE'


def fixture_parameters():
    """公开合成材料，生产验证器明确拒绝使用；不包含真实访问能力。"""
    docs = {s: {'version': 3, 'rules': [{'domain_suffix': [s + '.example.org']}]} for s in SERVICE_NAMES}
    policy = {'protected_suffixes': ['sensitive.example.org', 'googleapis.cn'],
              'cn_direct_suffixes': ['domestic.example.cn'], 'custom_rules': [],
              'streaming_rule_sets': [{'service': s, 'path': '/public-fixture/' + s + '.json',
                                       'sha256': hashlib.sha256(json.dumps(docs[s]).encode()).hexdigest()}
                                      for s in SERVICE_NAMES]}
    accounts = []
    for lane in LANES:
        for protocol in PROTOCOLS:
            index = len(accounts) + 1
            credential = (f'00000000-0000-4000-8000-{index:012d}' if protocol in ('reality', 'ws')
                          else 'PUBLIC-SYNTHETIC-NOT-LIVE-' + lane + '-' + protocol + '-PASSWORD')
            accounts.append({'id': protocol + '/' + lane, 'protocol': protocol, 'lane': lane,
                             'enabled': True, 'label': _tag(lane, protocol), 'credential': credential})
    parameters = {'schema_version': 1, 'server_ipv4': '192.0.2.10', 'edge_host': 'edge.relay.example.invalid',
                  'ws_host': 'ws.relay.example.invalid', 'reality_server': 'www.microsoft.com',
                  'reality_public_key': base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='),
                  'reality_short_id': '0011223344556677', 'ws_path': '/PUBLIC-SYNTHETIC-NOT-A-LIVE-PATH',
                  'accounts': accounts, 'policy': policy}
    return parameters, docs


def make_manifest(parameters, profiles, *, fixture, checks=None):
    """只列公开模式、策略摘要及状态；不列凭据、节点 URI 或凭据派生散列。"""
    public_policy = {k: v for k, v in parameters['policy'].items() if k != 'streaming_rule_sets'}
    public_policy['streaming_rule_sets'] = [{k: m[k] for k in ('service', 'sha256')}
                                          for m in parameters['policy']['streaming_rule_sets']]
    return {'schema_version': 1, 'public_fixture': fixture, 'policy_sha256': digest(public_policy),
            'builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'platform_core_versions': CORE_VERSIONS, 'profiles': {p: sorted(x) for p, x in profiles.items()},
            'core_check': checks or {'Windows': 'NOT TESTED', 'Android': 'NOT TESTED'},
            'runtime_acceptance': {'windows_network': 'NOT TESTED', 'android_vpn': 'NOT TESTED',
                                   'dns_fail_closed': 'NOT TESTED', 'media_playback': 'NOT TESTED',
                                   'exit_protection': 'NOT TESTED'},
            'scope': {'Windows': 'mixed_loopback_only', 'Android': 'SFA_TUN_not_activated',
                      'real_dns': 'visible_domain_sniff_only_unknown_home_no_ip_reverse_mapping',
                      'android_strict_route': 'not_implemented', 'system_network_changed': False}}


def check_cores(root, *, cores=None):
    cores = cores or CORES
    checks = {}
    for platform in PLATFORMS:
        core = Path(cores[platform])
        reject(not core.is_file(), 'core_missing')
        result = subprocess.run([str(core), 'version'], capture_output=True, timeout=30)
        text = result.stdout.decode(errors='replace')
        reject(result.returncode != 0 or ('sing-box version ' + CORE_VERSIONS[platform]) not in text,
               'core_version_mismatch')
        for file in sorted((Path(root) / platform).glob('*.json')):
            result = subprocess.run([str(core), 'check', '-c', str(file)], capture_output=True, timeout=30)
            # 核心报错可能引用输入值，不把原始 stderr 输出到控制台。
            reject(result.returncode != 0, 'core_check_failed_' + platform + '_' + file.stem)
        checks[platform] = {'result': 'PASS', 'version': CORE_VERSIONS[platform],
                            'core_sha256': hashlib.sha256(core.read_bytes()).hexdigest(),
                            'kind': 'syntax_only_not_device_runtime'}
    return checks


def write_bundle(parameters, documents, output, *, fixture=False, check=False,
                 home_protocol='hy2', bwh_protocol='hy2'):
    profiles = build_profiles(parameters, documents, fixture=fixture,
                              home_protocol=home_protocol, bwh_protocol=bwh_protocol)
    root = Path(output).resolve()
    reject(root.exists(), 'output_must_be_new')
    if fixture:
        reject(PROJECT not in root.parents, 'fixture_output_must_be_in_project')
    elif os.name == 'nt':
        reject(root.drive.upper() not in ('D:', 'E:') or 'private' not in [p.lower() for p in root.parts]
               or PROJECT == root or PROJECT in root.parents, 'restricted_drive_output_required')
    else:
        reject(Path('/root/relay-exports') not in root.parents, 'restricted_root_export_required')
    # 实际材料只继承调用方已设好的受限目录 ACL；不要用本程序代替 Windows ACL 校验。
    reject(not root.parent.is_dir(), 'output_parent_required')
    root.mkdir(mode=0o777 if os.name == 'nt' else 0o700)
    for platform, files in profiles.items():
        directory = root / platform; directory.mkdir(mode=0o777 if os.name == 'nt' else 0o700)
        for name, config in files.items():
            path = directory / name
            with path.open('x', encoding='utf-8', newline='\n') as stream:
                json.dump(config, stream, ensure_ascii=False, indent=2); stream.write('\n')
            if os.name != 'nt':
                path.chmod(0o600)
    checks = check_cores(root) if check else None
    manifest = make_manifest(parameters, profiles, fixture=fixture, checks=checks)
    (root / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description='离线生成八身份客户端，不启动 TUN 或设置系统代理。')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--fixture', action='store_true')
    source.add_argument('--parameters', type=Path)
    parser.add_argument('--rules-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check-cores', action='store_true')
    parser.add_argument('--home-protocol', choices=PROTOCOLS, default='hy2')
    parser.add_argument('--bwh-protocol', choices=PROTOCOLS, default='hy2')
    args = parser.parse_args()
    if args.fixture:
        parameters, documents = fixture_parameters()
    else:
        reject(args.rules_dir is None, 'rules_directory_required')
        parameters = loads_unique(args.parameters.read_text(encoding='utf-8'))
        validate_parameters(parameters)
        documents = load_rule_documents(parameters['policy'], args.rules_dir)
    manifest = write_bundle(parameters, documents, args.output, fixture=args.fixture, check=args.check_cores,
                            home_protocol=args.home_protocol, bwh_protocol=args.bwh_protocol)
    print(json.dumps({'result': 'BUILT', 'public_fixture': args.fixture, 'profiles': 8,
                      'core_checked': bool(args.check_cores), 'network_started': False}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # 不输出异常对象，避免 UUID、密码、路径或远端返回值进入日志。
        raise SystemExit('STOPPED: client_bundle_failed') from None
