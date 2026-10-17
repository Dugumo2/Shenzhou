"""资源增量导入：显式旧资源映射、严格白名单、事务与可复核预览。"""
import hashlib
import ipaddress
import json
import re
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime


SCHEMA_VERSION = 'resource-import-v1'
KINDS = ('server', 'ingress', 'core', 'egress', 'line')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}')


class ResourceImportError(ValueError):
    """错误只返回固定代码，避免原始输入中的秘密进入日志。"""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _positive(value):
    return type(value) is int and 1 <= value <= 2147483647


def _identifier(value, limit=120):
    return type(value) is str and len(value) <= limit and IDENTIFIER.fullmatch(value)


def _date(value):
    if type(value) is not str:
        raise ResourceImportError('invalid_time')
    try:
        parsed = parse_datetime(value)
    except (ValueError, OverflowError):
        parsed = None
    if parsed is None or timezone.is_naive(parsed):
        raise ResourceImportError('invalid_time')
    return parsed


def parse_manifest(payload, *, now=None):
    """来源回执只含脱敏资料；全量校验后才允许读取写入计划。"""
    now = now or timezone.now()
    required = {'schema_version', 'source', 'revision', 'observed_at', 'expires_at', 'resources'}
    if type(payload) is not dict or set(payload) != required:
        raise ResourceImportError('invalid_manifest_fields')
    if payload['schema_version'] != SCHEMA_VERSION:
        raise ResourceImportError('unsupported_schema')
    if not _identifier(payload['source'], 80) or not _positive(payload['revision']):
        raise ResourceImportError('invalid_source_revision')
    observed, expires = _date(payload['observed_at']), _date(payload['expires_at'])
    if observed > now + timedelta(seconds=60) or expires <= observed or expires - observed > timedelta(days=7):
        raise ResourceImportError('invalid_time_window')
    if now >= expires:
        raise ResourceImportError('expired_evidence')
    resources = payload['resources']
    if type(resources) is not list or not 1 <= len(resources) <= 200:
        raise ResourceImportError('invalid_resources')
    seen, targets = set(), set()
    for item in resources:
        if (type(item) is not dict or not {'kind', 'source_id', 'data'} <= item.keys()
                or not item.keys() <= {'kind', 'source_id', 'data', 'target_pk', 'expected_revision'}):
            raise ResourceImportError('invalid_resource_fields')
        if item['kind'] not in KINDS or not _identifier(item['source_id']):
            raise ResourceImportError('invalid_resource_identifier')
        key = (item['kind'], item['source_id'])
        if key in seen:
            raise ResourceImportError('duplicate_source_id')
        seen.add(key)
        if 'target_pk' in item:
            if not _positive(item['target_pk']) or not _positive(item.get('expected_revision')):
                raise ResourceImportError('explicit_target_revision_required')
            target = (item['kind'], item['target_pk'])
            if target in targets:
                raise ResourceImportError('duplicate_target')
            targets.add(target)
        if 'expected_revision' in item and not _positive(item['expected_revision']):
            raise ResourceImportError('invalid_expected_revision')
        if type(item['data']) is not dict or not item['data']:
            raise ResourceImportError('invalid_resource_data')
    # 往返序列化隔离调用者可变对象，并拒绝非JSON值。
    try:
        encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError):
        raise ResourceImportError('invalid_json_value') from None
    if len(encoded.encode()) > 262144:
        raise ResourceImportError('manifest_too_large')
    return json.loads(encoded)


def load_manifest(path, expected_sha256):
    """验证受限文件摘要，拒绝重复JSON键；不回显路径或输入。"""
    if type(expected_sha256) is not str or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise ResourceImportError('invalid_file_digest')
    try:
        with open(path, 'rb') as stream:
            raw = stream.read(262145)
    except OSError:
        raise ResourceImportError('source_read_failed') from None
    if len(raw) > 262144:
        raise ResourceImportError('manifest_too_large')
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ResourceImportError('file_digest_mismatch')

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ResourceImportError('duplicate_json_key')
            result[key] = value
        return result

    try:
        payload = json.loads(raw, object_pairs_hook=pairs)
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, ResourceImportError):
            raise
        raise ResourceImportError('invalid_json') from None
    return payload


FIELDS = {
    'server': {'name', 'adapter', 'purchase_meter_id'},
    'ingress': {'name', 'server', 'protocol', 'endpoint_host', 'endpoint_port', 'evidence_source', 'evidence_observed_at'},
    'egress': {'name', 'server', 'kind', 'ownership', 'external_kind', 'protocol', 'endpoint_host', 'endpoint_port',
               'evidence_source', 'evidence_observed_at', 'purchase_meter_id', 'core_instance', 'next_egress'},
    'core': {'name', 'server', 'instance_key', 'core_type', 'registered_version', 'configuration_owner',
             'configuration_version', 'service_unit', 'owner_state', 'evidence_source', 'evidence_observed_at', 'ingresses'},
    'line': {'name', 'ingress', 'egress'},
}
RELATIONS = {'server': 'server', 'ingress': 'ingress', 'egress': 'egress',
             'core_instance': 'core', 'next_egress': 'egress'}
NULLABLE_RELATIONS = {'core_instance', 'next_egress'}
IMMUTABLE = {'server', 'ingress', 'egress', 'adapter', 'instance_key', 'core_type', 'configuration_owner', 'kind'}
REQUIRED_NEW = {
    'server': {'name'}, 'ingress': {'name', 'server', 'protocol'},
    'egress': {'name', 'server', 'kind'},
    'core': {'name', 'server', 'instance_key', 'core_type', 'configuration_owner'},
    'line': {'name', 'ingress', 'egress'},
}


def _models():
    from .models import CoreInstance, Egress, Ingress, Line, Server
    return {'server': Server, 'core': CoreInstance, 'ingress': Ingress, 'egress': Egress, 'line': Line}


def _validate_data(item):
    """资料白名单不接受凭据、授权、计量、人工备注或运行配置。"""
    kind, data = item['kind'], item['data']
    if not data.keys() <= FIELDS[kind]:
        raise ResourceImportError('forbidden_resource_field')
    for field, value in data.items():
        if field in RELATIONS:
            if field in NULLABLE_RELATIONS and value is None:
                continue
            if not _identifier(value):
                raise ResourceImportError('invalid_relation')
            continue
        if field == 'ingresses':
            if (type(value) is not list or len(value) > 100 or any(not _identifier(v) for v in value)
                    or len(set(value)) != len(value)):
                raise ResourceImportError('invalid_relation')
            continue
        model_field = _models()[kind]._meta.get_field(field)
        if value is None:
            if not model_field.null:
                raise ResourceImportError('invalid_null')
            continue
        if field == 'endpoint_port':
            if type(value) is not int or not 1 <= value <= 65535:
                raise ResourceImportError('invalid_endpoint_port')
        elif type(value) is not str or not value or any(ord(c) < 32 for c in value) or '://' in value:
            raise ResourceImportError('invalid_resource_value')
        if field == 'endpoint_host':
            try:
                ipaddress.ip_address(value)
            except ValueError:
                labels = value.removesuffix('.').split('.')
                if not labels or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels):
                    raise ResourceImportError('invalid_endpoint_host') from None
        if field == 'service_unit' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:\-]{0,91}\.service', value):
            raise ResourceImportError('invalid_service_unit')
        if field in {'evidence_source', 'purchase_meter_id', 'configuration_owner'} and not _identifier(value, model_field.max_length):
            raise ResourceImportError('invalid_source_identifier')
        if field == 'protocol' and not re.fullmatch(r'[a-z][a-z0-9_-]{0,23}', value):
            raise ResourceImportError('invalid_protocol')
        if field == 'evidence_observed_at':
            _date(value)
        try:
            model_field.clean(value, None)
        except (ValidationError, ValueError, TypeError):
            raise ResourceImportError('invalid_resource_value') from None


def _wire_value(value):
    return value.isoformat() if hasattr(value, 'isoformat') else value


def _ordered_resources(resources):
    """同批关系按来源引用排序；外部节点先于引用它的本地出口。"""
    pending = {(item['kind'], item['source_id']): item for item in resources}
    ordered = []
    while pending:
        ready = []
        for key, item in pending.items():
            dependencies = {(RELATIONS[field], value) for field, value in item['data'].items()
                            if field in RELATIONS and value is not None}
            dependencies.update(('ingress', value) for value in item['data'].get('ingresses', []))
            if not dependencies.intersection(pending):
                ready.append(key)
        if not ready:
            raise ResourceImportError('resource_relation_cycle')
        for key in sorted(ready, key=lambda key: (KINDS.index(key[0]), key[1])):
            ordered.append(pending.pop(key))
    return ordered


def _ref_key(reference):
    return ('pk', reference['pk']) if reference['pk'] is not None else ('source', reference['key'])


def _validate_egress_chains(operations, rows):
    """将拟补关系覆盖到只读图上验证，最多十六段且不接受回环。"""
    model = _models()['egress']
    links, targets = {}, []
    row_by_key = {(row['kind'], row['source_id']): row for row in rows}
    for item, obj, _, _, relations, _ in operations:
        if item['kind'] != 'egress':
            continue
        key = ('pk', obj.pk) if obj else ('source', ('egress', item['source_id']))
        next_ref = relations.get('next_egress')
        links[key] = _ref_key(next_ref) if next_ref else (('pk', obj.next_egress_id) if obj and obj.next_egress_id else None)
        targets.append((key, row_by_key[('egress', item['source_id'])]))
    for start, row in targets:
        current, seen = start, set()
        while current is not None:
            if current in seen:
                row['conflicts'].append('egress_chain_cycle')
                break
            if len(seen) >= 16:
                row['conflicts'].append('egress_chain_too_long')
                break
            seen.add(current)
            if current not in links:
                item = model.objects.filter(pk=current[1]).only('next_egress_id').first() if current[0] == 'pk' else None
                if item is None:
                    row['conflicts'].append('egress_chain_missing_target')
                    break
                links[current] = ('pk', item.next_egress_id) if item.next_egress_id else None
            current = links[current]
        if row['conflicts']:
            row['state'] = 'conflict'


def _plan(payload):
    from .models import ResourceImportBinding, ResourceImportReceipt
    source = payload['source']
    fingerprint = digest(payload)
    old = ResourceImportReceipt.objects.filter(source=source, revision=payload['revision']).first()
    if old:
        if old.fingerprint != fingerprint:
            raise ResourceImportError('source_revision_conflict')
        return {**old.response, 'replayed': True}, []
    if ResourceImportReceipt.objects.filter(source=source, revision__gt=payload['revision']).exists():
        raise ResourceImportError('source_revision_out_of_order')
    models = _models()
    for item in payload['resources']:
        _validate_data(item)
    ordered = _ordered_resources(payload['resources'])
    rows, operations, resolved = [], [], {}
    for item in ordered:
        kind, source_id, data = item['kind'], item['source_id'], item['data']
        if data.get('evidence_observed_at') and _date(data['evidence_observed_at']) > _date(payload['observed_at']) + timedelta(seconds=60):
            raise ResourceImportError('evidence_time_after_manifest')
        binding = ResourceImportBinding.objects.filter(source=source, resource_kind=kind, source_id=source_id).first()
        pk = binding.target_pk if binding else item.get('target_pk')
        obj = models[kind].objects.filter(pk=pk).first() if pk else None
        row = {'kind': kind, 'source_id': source_id, 'target_pk': pk, 'state': 'new', 'changes': {},
               'unknown_fields': sorted(k for k, v in data.items() if v is None), 'preserved_fields': [], 'conflicts': []}
        if pk and obj is None:
            row['conflicts'].append('target_missing')
        if binding and 'target_pk' in item and item['target_pk'] != binding.target_pk:
            row['conflicts'].append('source_target_conflict')
        if pk and ResourceImportBinding.objects.filter(resource_kind=kind, target_pk=pk).exclude(
                source=source, source_id=source_id).exists():
            row['conflicts'].append('target_already_bound')
        if obj:
            row['revision'] = obj.revision
            if item.get('expected_revision') != obj.revision:
                row['conflicts'].append('target_revision_conflict')
            row['state'] = 'matched'
        elif not REQUIRED_NEW[kind] <= data.keys():
            row['conflicts'].append('new_resource_missing_fields')
        if obj is None and not pk and data.get('name') and models[kind].objects.filter(name=data['name']).exists():
            # 名称只提示可能重复，不能作为自动关联依据。
            row['conflicts'].append('possible_existing_resource_requires_mapping')
        changes, relations, ingress_refs = {}, {}, None
        for field, value in data.items():
            if field == 'name' and obj:
                if value != obj.name:
                    row['preserved_fields'].append('name')
                continue
            if field in RELATIONS or field == 'ingresses':
                if field in NULLABLE_RELATIONS and value is None:
                    if obj and getattr(obj, field + '_id') is not None:
                        row['preserved_fields'].append(field)
                    continue
                reference_kind = RELATIONS.get(field, 'ingress')
                values = value if field == 'ingresses' else [value]
                references = []
                for ref in values:
                    reference = resolved.get((reference_kind, ref))
                    if reference is None:
                        prior = ResourceImportBinding.objects.filter(source=source, resource_kind=reference_kind, source_id=ref).first()
                        if prior and models[reference_kind].objects.filter(pk=prior.target_pk).exists():
                            reference = {'pk': prior.target_pk, 'key': (reference_kind, ref)}
                            if reference_kind == 'core':
                                reference['server_key'] = ('pk', models['core'].objects.get(pk=prior.target_pk).server_id)
                    if reference is None:
                        row['conflicts'].append('unresolved_relation')
                    else:
                        references.append(reference)
                if field == 'ingresses':
                    ingress_refs = references
                    current = set(obj.ingresses.values_list('pk', flat=True)) if obj else set()
                    wanted = {ref['pk'] for ref in references}
                    if obj and not current <= wanted:
                        row['conflicts'].append('relationship_removal_forbidden')
                    if current != wanted:
                        row['changes'][field] = {'before': sorted(current), 'after': values}
                elif references:
                    relations[field] = references[0]
                    before = getattr(obj, field + '_id', None) if obj else None
                    if obj and before != references[0]['pk'] and not (field in NULLABLE_RELATIONS and before is None):
                        row['conflicts'].append('relationship_change_forbidden')
                    elif not obj or (field in NULLABLE_RELATIONS and before is None):
                        row['changes'][field] = {'before': before, 'after': value}
                continue
            before = _wire_value(getattr(obj, field)) if obj else None
            if field == 'evidence_observed_at' and value is not None:
                value = _date(value).isoformat()
            if before != value or not obj:
                if obj and (field in IMMUTABLE or (kind == 'ingress' and field == 'protocol')
                            or (field == 'ownership' and before != 'unknown')):
                    row['conflicts'].append('protected_field_conflict')
                elif obj and value is None and before is not None:
                    # 缺失证据不清空上次已知资料。
                    row['preserved_fields'].append(field)
                else:
                    changes[field] = value
                    row['changes'][field] = {'before': before, 'after': value}
        if kind == 'egress':
            ownership = changes.get('ownership', obj.ownership if obj else 'unknown')
            external_kind = changes.get('external_kind', obj.external_kind if obj else None)
            if external_kind is not None and ownership != 'external':
                row['conflicts'].append('external_kind_requires_external_ownership')
            server_key = ('pk', obj.server_id) if obj else (_ref_key(relations['server']) if 'server' in relations else None)
            if 'core_instance' in relations and relations['core_instance'].get('server_key') != server_key:
                row['conflicts'].append('execution_core_server_mismatch')
        if kind == 'core' and obj is None and relations.get('server', {}).get('pk'):
            if models[kind].objects.filter(server_id=relations['server']['pk'], instance_key=data.get('instance_key')).exists():
                row['conflicts'].append('existing_core_requires_mapping')
        if row['conflicts']:
            row['state'] = 'conflict'
        elif obj and row['changes']:
            row['state'] = 'changed'
        rows.append(row)
        resolved[(kind, source_id)] = {'pk': obj.pk if obj else None, 'key': (kind, source_id)}
        if kind == 'core':
            resolved[(kind, source_id)]['server_key'] = ('pk', obj.server_id) if obj else (
                _ref_key(relations['server']) if 'server' in relations else None)
        operations.append((item, obj, binding, changes, relations, ingress_refs))
    _validate_egress_chains(operations, rows)
    result = {'schema_version': SCHEMA_VERSION, 'source': source, 'revision': payload['revision'],
              'manifest_sha256': fingerprint, 'rows': rows, 'replayed': False,
              'can_apply': all(row['state'] != 'conflict' for row in rows),
              'missing_resources_policy': 'preserve'}
    result['preview_sha256'] = digest(result)
    return result, operations


def preview_resources(payload, *, now=None):
    """只读预览；摘要同时绑定来源回执和当前目标修订。"""
    payload = parse_manifest(payload, now=now)
    with transaction.atomic():
        result, _ = _plan(payload)
        return result


def apply_resources(actor, payload, *, expected_preview_sha256, now=None):
    """资源资料增量写入；不调用核心、授权、计量或网络执行器。"""
    from .models import ResourceImportBinding, ResourceImportReceipt
    if settings.PANEL_LIVE and not getattr(settings, 'RESOURCE_IMPORT_WRITE_ENABLED', False):
        raise ResourceImportError('live_import_disabled')
    payload = parse_manifest(payload, now=now)
    try:
        with transaction.atomic():
            if not get_user_model().objects.select_for_update().filter(pk=getattr(actor, 'pk', None), is_active=True, is_staff=True).exists():
                raise ResourceImportError('admin_required')
            preview, operations = _plan(payload)
            if preview['preview_sha256'] != expected_preview_sha256:
                raise ResourceImportError('preview_changed')
            if preview['replayed']:
                return preview
            if not preview['can_apply']:
                raise ResourceImportError('unresolved_conflicts')
            resolved = {}
            for item, obj, binding, changes, relations, ingress_refs in operations:
                kind, source_id = item['kind'], item['source_id']
                for field, ref in relations.items():
                    changes[field + '_id'] = ref['pk'] or resolved[ref['key']].pk
                if obj is None:
                    obj = _models()[kind](**changes)
                    obj.full_clean()
                    obj.save()
                else:
                    # 即使只有绑定变化，也递增修订使旧预览明确失效。
                    previous_revision = obj.revision
                    for field, value in changes.items():
                        setattr(obj, field, value)
                    obj.full_clean()
                    if not _models()[kind].objects.filter(pk=obj.pk, revision=previous_revision).update(
                            **changes, revision=previous_revision + 1):
                        raise ResourceImportError('target_revision_conflict')
                    obj.revision += 1
                if ingress_refs is not None:
                    obj.ingresses.add(*(ref['pk'] or resolved[ref['key']].pk for ref in ingress_refs))
                ResourceImportBinding.objects.update_or_create(source=payload['source'], resource_kind=kind, source_id=source_id,
                    defaults={'target_pk': obj.pk, 'source_revision': payload['revision'], 'fingerprint': digest(item['data'])})
                resolved[(kind, source_id)] = obj
            result = {**preview, 'applied': True, 'targets': [
                {'kind': kind, 'source_id': source_id, 'target_pk': obj.pk, 'revision': obj.revision}
                for (kind, source_id), obj in resolved.items()]}
            ResourceImportReceipt.objects.create(source=payload['source'], revision=payload['revision'],
                fingerprint=digest(payload), response=result)
            return result
    except (IntegrityError, OperationalError, ValidationError):
        raise ResourceImportError('atomic_import_failed') from None
