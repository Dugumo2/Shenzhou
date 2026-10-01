"""客户端代理／直连域名规则的保守校验、预览和规则材料生成。"""
from __future__ import annotations

import hashlib
import json
import re

from django.core.exceptions import ValidationError


# 与现有 P7 权威保护集对齐；发布器仍须用当时的权威集再次校验。
PROTECTED = frozenset("""
openai.com chatgpt.com anthropic.com claude.ai claude.com claudeusercontent.com
claudemcpclient.com claudemcpcontent.com google.com googleapis.com googleapis.cn
googleusercontent.com googleplay.com gstatic.com oaistatic.com oaiusercontent.com
oaistatsig.com sora.com gemini.google notebooklm.google antigravity.google
deepmind.google generativeai.google ai.google.dev githubcopilot.com
copilot.microsoft.com copilot.cloud.microsoft cursor.com cursor.sh cursorapi.com
cursor-cdn.com windsurf.com codeium.com codeiumdata.com perplexity.ai
perplexity.com pplx.ai x.ai grok.com mistral.ai poe.com poecdn.net
xn--ngstr-lra8j.com gvt1.com gvt2.com ggpht.com coinbase.com binance.com
kraken.com okx.com relay.example.invalid browserleaks.com
""".split())
DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+\Z")
REGEX_CHARS = re.compile(r"[a-z0-9.\\\[\]\-+*?^$]+\Z")


def normal_domain(value: str) -> str:
    if type(value) is not str:
        raise ValidationError('域名格式无效。')
    value = value.strip().rstrip('.').lower()
    try:
        value = value.encode('idna').decode('ascii')
    except UnicodeError:
        raise ValidationError('域名格式无效。') from None
    if len(value) > 253 or not DOMAIN.fullmatch(value):
        raise ValidationError('请输入完整域名，例如 example.com。')
    return value


def overlaps_protected(domain: str) -> bool:
    return any(domain == protected or domain.endswith('.' + protected) or
               protected.endswith('.' + domain) for protected in PROTECTED)


def validate_rule(kind: str, value: str, scope: str = '', action: str = 'direct') -> tuple[str, str]:
    """仅支持可审查的域名正则子集，不接受任意表达式或 IP。"""
    if action not in ('direct', 'proxy'):
        raise ValidationError('规则走向无效。')
    if kind in ('exact', 'suffix'):
        normalized = normal_domain(value)
        if action == 'direct' and overlaps_protected(normalized):
            raise ValidationError('与受保护的 AI、Google、金融或管理域名重叠。')
        return normalized, ''
    if kind != 'regex' or type(value) is not str:
        raise ValidationError('规则类型无效。')
    scope = normal_domain(scope)
    if action == 'direct' and overlaps_protected(scope):
        raise ValidationError('正则作用域与受保护域名重叠。')
    value = value.strip().lower()
    required_end = r'\.' + re.escape(scope).replace(r'\.', r'\.') + '$'
    if (len(value) > 180 or not value.startswith('^') or not value.endswith(required_end)
            or REGEX_CHARS.fullmatch(value) is None or sum(value.count(x) for x in '+*?') > 1
            or '\\' in value.replace(r'\.', '') or re.search(r'(?:\*|\+|\?)\s*(?:\*|\+|\?)', value)):
        raise ValidationError('正则需以 ^ 开头、以作用域域名结尾；仅允许简单字符类和一次量词。')
    try:
        re.compile(value)
    except re.error:
        raise ValidationError('正则语法无效。') from None
    return value, scope


def source_document(rules, action: str = 'direct') -> tuple[dict, str]:
    """由已启用规则生成 sing-box source rule-set；不表示客户端已经加载。"""
    if action not in ('direct', 'proxy'):
        raise ValidationError('规则走向无效。')
    groups = {'domain': set(), 'domain_suffix': set(), 'domain_regex': set()}
    for item in rules:
        if not item.enabled or item.outbound != action:
            continue
        value, _ = validate_rule(item.kind, item.value, item.scope_domain, item.outbound)
        field = {'exact': 'domain', 'suffix': 'domain_suffix', 'regex': 'domain_regex'}[item.kind]
        groups[field].add(value)
    document = {'version': 3, 'rules': [{field: sorted(values)} for field, values in groups.items() if values]}
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return document, hashlib.sha256(raw).hexdigest()


def policy_document(rules) -> tuple[dict, str]:
    """生成供权威客户端构建器复核的规则清单，保留正则作用域。"""
    entries = []
    for item in rules:
        if item.enabled:
            value, scope = validate_rule(item.kind, item.value, item.scope_domain, item.outbound)
            entries.append({'action': item.outbound, 'kind': item.kind, 'value': value, 'scope': scope})
    document = {'schema_version': 2, 'rules': sorted(entries, key=lambda x: (x['action'], x['kind'], x['value']))}
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return document, hashlib.sha256(raw).hexdigest()


def preview_rule(rules, domain: str) -> dict:
    domain = normal_domain(domain)
    related, matched_proxy, matched_direct = [], [], []
    for item in rules:
        value, scope = validate_rule(item.kind, item.value, item.scope_domain, item.outbound)
        matched = (domain == value if item.kind == 'exact' else
                   domain == value or domain.endswith('.' + value) if item.kind == 'suffix' else
                   domain.endswith('.' + scope) and re.fullmatch(value, domain) is not None)
        if item.kind == 'regex':
            relation = '命中正则' if matched else ('处于正则作用域' if domain == scope or domain.endswith('.' + scope)
                                                or scope.endswith('.' + domain) else '')
        else:
            relation = '命中' if matched else ('已配置子域名' if value.endswith('.' + domain) else '')
        if relation:
            related.append({'id': item.pk, 'action': item.outbound, 'kind': item.kind,
                            'value': value, 'enabled': item.enabled, 'relation': relation})
        if matched and item.enabled:
            (matched_proxy if item.outbound == 'proxy' else matched_direct).append(item.pk)
    if matched_proxy:
        result, matched_id = 'proxy_candidate', matched_proxy[0]
    elif overlaps_protected(domain):
        result, matched_id = 'protected_proxy', None
    elif matched_direct:
        result, matched_id = 'local_direct_candidate', matched_direct[0]
    else:
        result, matched_id = 'no_custom_match', None
    return {'domain': domain, 'result': result, 'matched_id': matched_id,
            'related': related[:100], 'related_truncated': len(related) > 100}
