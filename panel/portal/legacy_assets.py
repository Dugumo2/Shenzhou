"""生产旧规则的可追溯隔离副本；不导入订阅秘密、不发布线上配置。"""
import hashlib
import json
from pathlib import Path
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction

from .client_rules import validate_rule
from .models import AuditEvent, ClientDirectRule


class LegacyAssetError(ValueError):
    """固定错误码，防止把来源配置或凭据带到日志。"""


def require(value, code):
    if not value:
        raise LegacyAssetError(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def load_snapshot(path, expected_sha256):
    """指定文件、指定hash；保留规则和元数据，但拒绝夹带凭据字段。"""
    path = Path(path)
    require(re.fullmatch('[a-f0-9]{64}', expected_sha256 or ''), 'SOURCE_HASH_REQUIRED')
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 1024 * 1024,
            'SOURCE_FILE_INVALID')
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected_sha256, 'SOURCE_HASH_MISMATCH')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise LegacyAssetError('SOURCE_JSON_INVALID') from None
    required = {'schema', 'source', 'collected_at', 'rule_count', 'rules_sha256', 'rules',
                'published_policy', 'publish_status', 'legacy_reference'}
    require(type(value) is dict and set(value) == required and value['schema'] == 1
            and value['source'] == 'production-panel-readonly', 'SOURCE_SCHEMA_INVALID')
    rules = value['rules']
    require(type(rules) is list and 0 <= len(rules) <= 200 and len(rules) == value['rule_count'], 'SOURCE_RULE_COUNT_INVALID')
    require(hashlib.sha256(canonical(rules)).hexdigest() == value['rules_sha256'], 'SOURCE_RULE_HASH_MISMATCH')
    keys, ids = set(), set()
    for row in rules:
        require(type(row) is dict and set(row) == {'id', 'outbound', 'kind', 'value', 'scope_domain', 'enabled', 'revision'},
                'SOURCE_RULE_FIELDS_INVALID')
        require(type(row['id']) is int and row['id'] > 0 and row['id'] not in ids
                and type(row['revision']) is int and row['revision'] > 0 and type(row['enabled']) is bool,
                'SOURCE_RULE_ID_INVALID')
        try:
            domain, scope = validate_rule(row['kind'], row['value'], row['scope_domain'], row['outbound'])
        except (ValidationError, TypeError, ValueError):
            raise LegacyAssetError('SOURCE_RULE_VALIDATION_FAILED') from None
        require(domain == row['value'] and scope == row['scope_domain'], 'SOURCE_RULE_NORMALIZATION_DRIFT')
        key = (row['kind'], row['value'])
        require(key not in keys, 'SOURCE_RULE_DUPLICATE')
        keys.add(key)
        ids.add(row['id'])
    reference = value['legacy_reference']
    require(type(reference) is dict and reference.keys() <= {'available', 'artifact_count', 'kinds', 'sha256', 'mode'},
            'SOURCE_REFERENCE_SECRET_DENIED')
    require(type(reference.get('available')) is bool and type(reference.get('artifact_count')) is int
            and 0 <= reference['artifact_count'] <= 7, 'SOURCE_REFERENCE_INVALID')
    allowed = {'windows', 'android', 'v2rayng', 'windows-rules', 'v2rayng-routes', 'v2rayng-geosite', 'v2rayng-geoip'}
    require(type(reference.get('kinds', [])) is list and set(reference.get('kinds', [])) <= allowed, 'SOURCE_REFERENCE_KIND_INVALID')
    for key in ('published_policy', 'publish_status'):
        require(type(value[key]) is dict and value[key].keys() <= {'exists', 'sha256', 'schema_version', 'rule_count', 'state'},
                'SOURCE_METADATA_FIELDS_INVALID')
    return value


def _target(target_root, confirmed):
    require(confirmed and not settings.PANEL_LIVE, 'ISOLATED_CONFIRMATION_REQUIRED')
    root = Path(target_root).resolve()
    project = Path(__file__).resolve().parents[2]
    default_root = Path(settings.BASE_DIR) / 'var'
    require(root.is_relative_to(project) and root != default_root.resolve()
            and root == Path(settings.DATA_ROOT).resolve(), 'EXPLICIT_ISOLATED_TARGET_MISMATCH')
    require(connection.vendor == 'sqlite' and Path(connection.settings_dict['NAME']).resolve() == root / 'panel.sqlite3',
            'ISOLATED_DATABASE_MISMATCH')
    require(not Path(target_root).is_symlink() and not any(path.is_symlink() for path in root.parents),
            'TARGET_SYMLINK_DENIED')
    return root


def import_rules(snapshot_path, expected_sha256, *, target_root, owner, confirm_isolated=False):
    """仅合并同义规则；任何已有同名冲突整批拒绝，不覆盖用户修改。

源hash写入同一数据库事务的审计事件，归属映射写入本地回执。
不会生成订阅、修改套餐额度、申请任务或刷新规则发布文件。
"""
    root = _target(target_root, confirm_isolated)
    require(owner.is_active and owner.is_staff and owner.pk is not None, 'EXPLICIT_ADMIN_OWNER_REQUIRED')
    snapshot = load_snapshot(snapshot_path, expected_sha256)
    private = root / 'private'
    require(not private.is_symlink(), 'RECEIPT_SYMLINK_DENIED')
    private.mkdir(exist_ok=True)
    receipt_path = private / 'legacy-assets-receipt.json'
    require(not receipt_path.is_symlink(), 'RECEIPT_SYMLINK_DENIED')
    temporary = private / ('legacy-assets-receipt-' + expected_sha256 + '.tmp')
    require(not temporary.exists() and not temporary.is_symlink(), 'RECEIPT_TEMP_CONFLICT')
    created, unchanged, mapping = 0, 0, []
    fields = ('outbound', 'kind', 'value', 'scope_domain', 'enabled', 'revision')
    with transaction.atomic():
        for source in snapshot['rules']:
            existing = ClientDirectRule.objects.select_for_update().filter(kind=source['kind'], value=source['value']).first()
            if existing:
                require(all(getattr(existing, field) == source[field] for field in fields), 'EXISTING_RULE_CONFLICT')
                unchanged += 1
                row = existing
            else:
                row = ClientDirectRule.objects.create(creator=owner, **{field: source[field] for field in fields})
                created += 1
            mapping.append({'source_id': source['id'], 'local_id': row.pk})
        AuditEvent.objects.get_or_create(actor=owner, action='legacy_rules_imported', subject=expected_sha256,
                                        result='ISOLATED_COPY')
    receipt = {'schema': 1, 'scope': 'isolated-readonly-copy', 'owner_user_id': owner.pk,
               'source_sha256': expected_sha256, 'rules_sha256': snapshot['rules_sha256'],
               'source_collected_at': snapshot['collected_at'], 'created': created, 'unchanged': unchanged,
               'rule_count': len(mapping), 'mapping': mapping, 'legacy_reference': snapshot['legacy_reference'],
               'legacy_links_copied': False, 'production_changed': False}
    try:
        with temporary.open('xb') as stream:
            stream.write((json.dumps(receipt, ensure_ascii=False, indent=2) + '\n').encode())
        temporary.replace(receipt_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {key: value for key, value in receipt.items() if key != 'mapping'}


def imported_status(actor):
    """前端只读摘要；绑定导入时明确选择的管理员，绝不返回URL。"""
    if not actor.is_authenticated or not actor.is_active or not actor.is_staff:
        return None
    path = Path(settings.DATA_ROOT) / 'private' / 'legacy-assets-receipt.json'
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            return None
        value = json.loads(path.read_bytes())
        if value.get('owner_user_id') != actor.pk or value.get('scope') != 'isolated-readonly-copy':
            return None
        return {key: value[key] for key in ('source_sha256', 'rules_sha256', 'source_collected_at', 'rule_count',
                'legacy_reference', 'legacy_links_copied', 'production_changed')}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def legacy_service_metadata(actor):
    """既有服务卡片：只有明确绑定的管理员可看，线上入口不含令牌。"""
    value = imported_status(actor)
    if value is None:
        return None
    reference = value['legacy_reference']
    return {'name': '既有服务', 'available': reference.get('available') is True,
            'artifact_count': reference.get('artifact_count', 0), 'formats': reference.get('kinds', []),
            'rule_count': value['rule_count'], 'rules_loaded': True,
            'source_collected_at': value['source_collected_at'], 'source_sha256': value['source_sha256'],
            'rules_sha256': value['rules_sha256'], 'links_local': False,
            'usage_status': '历史用量未接入',
            'online_service_url': 'https://panel.relay.example.invalid:2083/manage/subscriptions/',
            'link_status': '线上受限引用已核对；本地未复制订阅凭据'}
