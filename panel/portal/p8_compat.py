"""P8 受限文件适配器：读取原链接结构，不签发凭据或改变旧下载门禁。

调用方须先重新核对本人、核验者和显式来源绑定；本模块只核对当前来源证据。
真实资源文件只能在获批部署环境消费，测试使用临时目录中的假链接。
"""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit


MAX_LINK_BYTES = 8192
RESOURCE_FORMATS = {
    'windows': 'node_list_with_policygroup',
    'android': 'singbox_json',
    'v2rayng': 'node_list',
    'windows-rules': 'windows_rules_bundle',
    'v2rayng-routes': 'json_array',
    'v2rayng-geosite': 'binary',
    'v2rayng-geoip': 'binary',
    'windows-routing': 'v2rayn_routing_json',
}
LINK_PATH = re.compile(r'/s/[A-Za-z0-9_-]{43}/([a-z0-9-]+)\Z')
DIGEST = re.compile(r'[a-f0-9]{64}\Z')
SOURCE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z')
RESOURCE_FAMILIES = {key: 'windows' if key.startswith('windows') else 'android'
                     for key in RESOURCE_FORMATS}


class P8SourceError(ValueError):
    """只暴露固定错误码，不包含文件正文、路径或链接。"""


def _require(condition, code):
    if not condition:
        raise P8SourceError(code)


@dataclass(frozen=True)
class P8Resource:
    kind: str
    format: str
    format_verified: bool
    url: str = field(repr=False)


@dataclass(frozen=True)
class P8Source:
    source_instance: str
    source_id: str
    links_sha256: str
    evidence_sha256: str
    resources: tuple[P8Resource, ...] = field(repr=False)

    def resource(self, kind):
        """仅返回现场格式已核验且实际存在的原地址；不推导额外别名。"""
        return next((item for item in self.resources if item.kind == kind and item.format_verified), None)


def _read_private_file(path_value):
    """有界读取同一文件句柄，拒绝符号链接、共享写入及换文件竞争。"""
    try:
        path = Path(path_value)
        _require(path.is_absolute(), 'P8_FILE_INVALID')
        _require(not any(item.is_symlink() or getattr(item, 'is_junction', lambda: False)()
                         for item in (path, *path.parents)), 'P8_FILE_INVALID')
        before = path.stat()
        _private_stat(before)
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            _private_stat(opened)
            # Windows 的路径stat与句柄fstat可能使用不同ctime语义；同句柄前后仍完整比较。
            include_ctime = os.name == 'posix'
            _require(_stat_identity(before, include_ctime) == _stat_identity(opened, include_ctime), 'P8_FILE_CHANGED')
            raw = stream.read(MAX_LINK_BYTES + 1)
            after = os.fstat(stream.fileno())
            _private_stat(after)
            _require(_stat_identity(opened) == _stat_identity(after)
                     and len(raw) == opened.st_size, 'P8_FILE_CHANGED')
        _require(0 < len(raw) <= MAX_LINK_BYTES, 'P8_FILE_INVALID')
        return raw
    except P8SourceError:
        raise
    except (OSError, ValueError, TypeError):
        raise P8SourceError('P8_FILE_UNAVAILABLE') from None


def _private_stat(value):
    _require(stat.S_ISREG(value.st_mode) and value.st_nlink == 1
             and 0 < value.st_size <= MAX_LINK_BYTES, 'P8_FILE_INVALID')
    if os.name == 'posix':
        _require(not value.st_mode & 0o027, 'P8_FILE_PERMISSIONS')


def _stat_identity(value, include_ctime=True):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns if include_ctime else None,
            value.st_mode, value.st_nlink)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'P8_DUPLICATE_RESOURCE')
        result[key] = value
    return result


def _origin(value):
    _require(type(value) is str and len(value) <= 256
             and not any(char.isspace() or ord(char) < 32 for char in value), 'P8_ORIGIN_INVALID')
    parsed = urlsplit(value)
    _require(parsed.scheme == 'https' and parsed.hostname and parsed.path == ''
             and not parsed.username and not parsed.password and not parsed.query
             and not parsed.fragment and parsed.netloc == parsed.hostname + ':' + str(parsed.port),
             'P8_ORIGIN_INVALID')
    return parsed


def load_p8_source(config, expected_links_sha256, expected_evidence_sha256):
    """用显式部署配置与绑定摘要重查文件，失败时不返回部分资源。

resources 的结构为 {资源键: {format: 固定格式名, verified: 布尔值}}。
verified 仅代表已核验响应格式，不代表核心连接、客户端导入或防泄漏验收。
windows-rules 是素材包，不转称原生路由；windows-routing 必须独立存在与核验。
"""
    try:
        _require(type(config) is dict and set(config) == {
            'source_instance', 'source_id', 'links_path', 'origin', 'evidence_sha256', 'resources',
        }, 'P8_CONFIG_INVALID')
        for name in ('source_instance', 'source_id'):
            _require(type(config[name]) is str and SOURCE_ID.fullmatch(config[name]), 'P8_SOURCE_INVALID')
        _require(type(expected_links_sha256) is str and DIGEST.fullmatch(expected_links_sha256)
                 and type(expected_evidence_sha256) is str and DIGEST.fullmatch(expected_evidence_sha256),
                 'P8_EVIDENCE_REQUIRED')
        _require(config['evidence_sha256'] == expected_evidence_sha256, 'P8_EVIDENCE_CHANGED')
        origin = _origin(config['origin'])
        declared = config['resources']
        _require(type(declared) is dict and declared and declared.keys() <= RESOURCE_FORMATS.keys(),
                 'P8_RESOURCE_CONFIG_INVALID')
        for key, facts in declared.items():
            _require(type(facts) is dict and set(facts) == {'format', 'verified'}
                     and facts['format'] == RESOURCE_FORMATS[key] and type(facts['verified']) is bool,
                     'P8_RESOURCE_CONFIG_INVALID')
        raw = _read_private_file(config['links_path'])
        actual_digest = hashlib.sha256(raw).hexdigest()
        _require(actual_digest == expected_links_sha256, 'P8_SOURCE_CHANGED')
        value = json.loads(raw, object_pairs_hook=_unique_object)
        _require(type(value) is dict and value.keys() == declared.keys(), 'P8_RESOURCE_SET_CHANGED')
        resources, family_tokens = [], {}
        for kind, url in value.items():
            _require(type(url) is str and len(url) <= 300
                     and not any(char.isspace() or ord(char) < 32 for char in url), 'P8_LINK_INVALID')
            parsed = urlsplit(url)
            matched = LINK_PATH.fullmatch(parsed.path)
            _require(parsed.scheme == 'https' and parsed.netloc == origin.netloc
                     and not parsed.username and not parsed.password and not parsed.query
                     and not parsed.fragment and matched and matched[1] == kind,
                     'P8_LINK_INVALID')
            family = RESOURCE_FAMILIES[kind]
            token = parsed.path.split('/')[2]
            _require(family_tokens.get(family, token) == token, 'P8_RESOURCE_FAMILY_CHANGED')
            family_tokens[family] = token
            resources.append(P8Resource(kind, declared[kind]['format'], declared[kind]['verified'], url))
        return P8Source(config['source_instance'], config['source_id'], actual_digest,
                        expected_evidence_sha256, tuple(resources))
    except P8SourceError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise P8SourceError('P8_SOURCE_INVALID') from None


def _bindings(user):
    """所有入口先查开关和数据库当前账号，不能信任旧 Session 对象属性。"""
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.db.models import F, Q
    from django.utils import timezone
    from .models import DeviceSubscription, Entitlement, Membership, P8SourceBinding
    if (getattr(settings, 'P8_COMPAT_ENABLED', False) is not True
            or not getattr(user, 'is_authenticated', False) or user.pk is None):
        return P8SourceBinding.objects.none()
    if not get_user_model().objects.filter(pk=user.pk, is_active=True).exists():
        return P8SourceBinding.objects.none()
    return eligible_bindings().filter(owner_id=user.pk)


def eligible_bindings():
    """仅元数据资格查询；管理投影复用，不提供管理员秘密读取权。"""
    from django.conf import settings
    from django.db.models import F, Q
    from django.utils import timezone
    from .models import DeviceSubscription, Entitlement, Membership, P8SourceBinding
    from .p8_entitlement import mappings, valid_mapping
    if getattr(settings, 'P8_COMPAT_ENABLED', False) is not True:
        return P8SourceBinding.objects.none()
    # 旧服务和设备UUID发生冲突时，禁止借新兼容入口获得不同资源。
    claimed = mappings().values('p8_binding_id')
    verified = [binding.p8_binding_id for binding in mappings() if valid_mapping(binding)]
    ordinary = P8SourceBinding.objects.filter(owner__is_active=True,
        enabled=True, state='verified', revision__gte=1, verified_at__lte=timezone.now(),
        verified_by__is_active=True, verified_by__is_staff=True).filter(
        Q(legacy_membership__isnull=True) | Q(legacy_membership__user_id=F('owner_id'),
            legacy_membership__legacy_binding__isnull=True,
            owner__entitlement__isnull=True)).exclude(
        public_id__in=Entitlement.objects.values('public_id')).exclude(
        public_id__in=Membership.objects.values('public_id')).exclude(
        public_id__in=DeviceSubscription.objects.values('public_id')).exclude(pk__in=claimed)
    return P8SourceBinding.objects.filter(Q(pk__in=ordinary.values('pk')) | Q(pk__in=verified)).order_by('public_id')


def _source_config(binding):
    """元数据可读不消费秘密；每次重新对照已批准来源身份和当前证据。"""
    from django.conf import settings
    configs = getattr(settings, 'P8_COMPAT_SOURCES', {})
    if type(configs) is not dict:
        return None
    config = configs.get((binding.source_instance, binding.source_id))
    if (type(config) is not dict or config.get('source_instance') != binding.source_instance
            or config.get('source_id') != binding.source_id
            or not SOURCE_ID.fullmatch(binding.source_instance)
            or not SOURCE_ID.fullmatch(binding.source_id)
            or not DIGEST.fullmatch(binding.evidence_sha256)
            or not DIGEST.fullmatch(binding.links_sha256)
            or binding.verification_sha256 != binding_verification_sha256(binding)
            or config.get('evidence_sha256') != binding.evidence_sha256):
        return None
    return config


def binding_verification_sha256(binding):
    """核验回执绑定完整归属元组；普通保存不自动生成或更新此摘要。"""
    values = {key: str(getattr(binding, key)) for key in (
        'public_id', 'source_instance', 'source_id', 'owner_id', 'verified_by_id', 'revision',
        'links_sha256', 'evidence_sha256', 'legacy_membership_id')}
    values['verified_at'] = binding.verified_at.isoformat() if binding.verified_at is not None else None
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def current_binding(user, public_id):
    """按本人绑定查询；其他管理员没有兜底读取权。"""
    from uuid import UUID
    from django.db.models import Q
    try:
        value = UUID(str(public_id))
    except (ValueError, TypeError, AttributeError):
        return None
    matches = list(_bindings(user).filter(Q(public_id=value) | Q(legacy_membership__public_id=value))[:2])
    binding = matches[0] if len(matches) == 1 else None
    return binding if binding is not None and _source_config(binding) is not None else None


def bound_membership_ids(user):
    """仅显式、当前有效的同一服务关联可在本人列表去重。"""
    return [binding.legacy_membership_id for binding in _bindings(user)
            if binding.legacy_membership_id is not None and _source_config(binding) is not None]


def claimed_membership_ids(user):
    """坏绑定的旧登记仍须标待核验，不能重新显示成已可信套餐。"""
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from .models import P8SourceBinding
    if (getattr(settings, 'P8_COMPAT_ENABLED', False) is not True
            or not getattr(user, 'is_authenticated', False) or user.pk is None
            or not get_user_model().objects.filter(pk=user.pk, is_active=True).exists()):
        return []
    return list(P8SourceBinding.objects.filter(owner_id=user.pk,
        legacy_membership__user_id=user.pk).values_list('legacy_membership_id', flat=True))


def _project(binding, *, detail=False):
    delivery = {'state': 'verification_required', 'message': '请选择软件核对可用资源。', 'download_url': None}
    result = {'id': str(binding.public_id), 'source_type': 'p8', 'name': '神舟云',
        'quota_bytes': None, 'quota_state': 'unknown', 'used_bytes': None, 'raw_bytes': None,
        'remaining_bytes': None, 'next_reset_at': None, 'expires_at': None,
        'state': 'verification_required', 'enabled': True, 'business_state': 'verification_required',
        'status_label': '订阅已接入',
        'usage': {'quality': 'unknown', 'updated_at': None, 'message': '暂无可靠统计，当前用量和剩余待核算。'},
        'application': {'state': 'verification_required', 'desired_revision': None, 'applied_revision': None},
        'delivery': delivery,
        'actions': {key: False for key in ('billing', 'quota', 'renew', 'grants', 'enable', 'reset')}}
    if detail:
        result['compatibility'] = {'state': 'clear', 'message': None}
        result['clients'] = [{'id': key, 'delivery': dict(delivery)} for key in ('windows', 'v2rayng', 'android')]
        result['clients'].append({'id': 'router', 'delivery': {
            'state': 'blocked', 'message': '路由器适配尚未验收。', 'download_url': None}})
    from .p8_entitlement import current_entitlement
    target = current_entitlement(binding)
    if target is not None:
        from .api_helpers import project_service
        summary = project_service(target, 'entitlement')
        for key in ('quota_bytes', 'quota_state', 'used_bytes', 'raw_bytes', 'remaining_bytes',
                    'next_reset_at', 'expires_at', 'state', 'enabled', 'business_state',
                    'status_label', 'usage', 'application'):
            result[key] = summary[key]
    return result


def list_services(user):
    return [_project(binding) for binding in _bindings(user) if _source_config(binding) is not None]


def service_detail(user, public_id):
    binding = current_binding(user, public_id)
    return _project(binding, detail=True) if binding is not None else None


def service_usage(user, public_id, period='current'):
    """只读取显式核验的同服务套餐；无映射时保持未知。"""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from django.utils import timezone
    if period not in ('current', '7d', '30d'):
        return None
    binding = current_binding(user, public_id)
    if binding is None:
        return None
    from .p8_entitlement import current_entitlement
    target = current_entitlement(binding)
    if target is not None:
        from .usage_api import _service
        value = _service(target, 'entitlement', period, timezone.now())
        value.update(service_id=str(binding.public_id), source_type='p8')
        return value
    return unknown_usage(binding.public_id, period)


def unknown_usage(public_id, period='current', *, source_type='p8'):
    """共享未知投影；失效来源不回退到会员登记额度或其他服务账本。"""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from django.utils import timezone
    now = timezone.now()
    start = (None if period == 'current' else now.astimezone(ZoneInfo('Asia/Shanghai')).replace(
        hour=0, minute=0, second=0, microsecond=0) - timedelta(days=int(period[:-1]) - 1))
    return {'service_id': str(public_id), 'source_type': source_type, 'time_zone': 'Asia/Shanghai',
        'generated_at': now.isoformat(), 'period': period, 'current_cycle': None,
        'summary': {'quota_bytes': None, 'quota_state': 'unknown', 'charged_bytes': None,
                    'upload_bytes': None, 'download_bytes': None, 'remaining_bytes': None, 'next_reset_at': None},
        'quality': {'state': 'unknown', 'message': '暂无可靠统计，当前用量和剩余待核算。', 'collected_at': None},
        'history': {'kind': 'confirmed_ledger_postings', 'date_basis': 'created_at',
            'range_start': start.isoformat() if start else None,
            'range_end': now.isoformat() if start else None, 'totals': None,
            'record_count': 0, 'excluded_record_count': 0, 'days': [], 'day_limit': 90,
            'days_truncated': False, 'message': '历史用量未接入，没有记录不代表零流量。'}}
