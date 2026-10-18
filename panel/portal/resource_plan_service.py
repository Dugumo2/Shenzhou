"""采购资料独立预览、版本保存与实际消费回执；不读写个人账。"""
from copy import deepcopy
from datetime import datetime
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F
from django.utils import timezone

from .inventory_purchase import InventoryPurchases
from .resource_plan_models import ResourcePlanBinding, ResourcePlanConsumption, ResourcePlanVersion
from .resource_usage import MAX_BYTES, ResourceUsageError, _bytes, _date, _digest, _plan, _time


SALT = 'purchase-plan-preview-v1'
PREVIEW_AGE = 15 * 60
BASE_FIELDS = {'expected_revision', 'source_fingerprint', 'operation', 'changes'}
EFFECTS = ['仅修改采购资源计划，不修改个人套餐或累计用量。', '保存后等待采集定时器消费，以实际回执确认生效。']


class ResourcePlanError(ValueError):
    def __init__(self, code, message='采购计划校验未通过，请重新读取后核对。', status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(code)


def _need(condition, code, status=422):
    if not condition:
        raise ResourcePlanError(code, status=status)


def _staff(actor):
    current = get_user_model().objects.filter(pk=getattr(actor, 'pk', None), is_active=True, is_staff=True).first()
    _need(current is not None, 'permission_denied', 403)
    return current


def resource_plan_write_enabled():
    return getattr(settings, 'RESOURCE_PLAN_WRITE_ENABLED', not settings.PANEL_LIVE) is True


def _validated_plan(plan):
    try:
        _plan(plan)
    except (ResourceUsageError, TypeError, KeyError):
        raise ResourcePlanError('invalid_source_plan') from None
    return deepcopy(plan)


def _target(plan, kind, identifier):
    from .models import Egress, Server
    expected = 'server' if plan['scope'] == 'server' else 'egress'
    _need(kind == expected and type(identifier) is str and identifier.isdecimal(), 'invalid_purchase_target', 409)
    model = Server if kind == 'server' else Egress
    target = model.objects.filter(pk=identifier, purchase_meter_id=plan['id']).first()
    _need(target is not None, 'purchase_target_changed', 409)
    if kind == 'egress':
        _need(target.ownership == 'external', 'purchase_target_changed', 409)
    return target


def _purchase(actor, plan):
    value = InventoryPurchases(actor).for_resource(plan['id'], plan['scope'])
    _need(value['purchase_meter_state'] == 'ready', 'purchase_source_unavailable', 403)
    return value['purchase_binding_key']


def _source(binding):
    reader = getattr(settings, 'RESOURCE_PLAN_SOURCE_READER', None)
    _need(callable(reader) or not settings.PANEL_LIVE, 'purchase_source_not_configured', 503)
    try:
        plan = reader(binding.meter_id) if callable(reader) else binding.source_plan
        plan = _validated_plan(plan)
    except ResourcePlanError:
        raise
    except Exception:
        raise ResourcePlanError('purchase_source_unavailable', status=503) from None
    _need(_digest(plan) == binding.source_fingerprint, 'purchase_source_changed', 409)


def _authorize(actor, binding):
    _need(binding.owner_id == actor.pk, 'permission_denied', 403)
    _target(binding.source_plan, binding.target_kind, binding.target_id)
    key = _purchase(actor, binding.source_plan)
    _need(key == binding.purchase_binding_key, 'purchase_source_changed', 409)
    _source(binding)


def _binding(actor, key, lock=False):
    _need(type(key) is str and re.fullmatch(r'[a-f0-9]{64}', key), 'invalid_binding_key', 404)
    query = ResourcePlanBinding.objects.select_for_update() if lock else ResourcePlanBinding.objects
    binding = query.filter(purchase_binding_key=key).first()
    _need(binding is not None, 'purchase_plan_not_registered', 404)
    _authorize(actor, binding)
    return binding


def register_resource_plan(plan, *, target_kind, target_id, actor):
    """受信导入器登记原权威文件；重跑只核对，不覆盖已存在的版本或来源。"""
    actor = _staff(actor)
    plan = _validated_plan(plan)
    target_id = str(target_id)
    _target(plan, target_kind, target_id)
    purchase_key = _purchase(actor, plan)
    with transaction.atomic():
        binding, created = ResourcePlanBinding.objects.get_or_create(meter_id=plan['id'], defaults={
            'owner': actor, 'target_kind': target_kind, 'target_id': target_id,
            'purchase_binding_key': purchase_key, 'source_plan': plan, 'source_fingerprint': _digest(plan)})
        _need((binding.owner_id, binding.target_kind, binding.target_id, binding.purchase_binding_key,
               binding.source_fingerprint) == (actor.pk, target_kind, target_id, purchase_key, _digest(plan)),
              'purchase_registration_conflict', 409)
        return binding


def _current(binding):
    latest = binding.versions.order_by('-revision').first()
    return deepcopy(latest.plan if latest else binding.source_plan)


def _state(binding):
    processed = list(ResourcePlanConsumption.objects.filter(version__binding=binding).select_related('version').order_by('version__revision'))
    effective = _effective_plan(binding, processed)
    consumed = processed[-1] if processed else None
    consumption = {'status': 'not_consumed', 'revision': None, 'consumed_at': None,
                   'error_code': '', 'effective_plan_fingerprint': None}
    if consumed:
        consumption = {'status': consumed.status, 'revision': consumed.version.revision,
                       'consumed_at': consumed.consumed_at.isoformat(), 'error_code': consumed.error_code,
                       'effective_plan_fingerprint': consumed.effective_plan_fingerprint}
    if binding.revision > (consumption['revision'] or 1):
        consumption['status'] = 'pending'
    return {'meter_id': binding.meter_id, 'revision': binding.revision,
            'source_fingerprint': binding.source_fingerprint, 'plan': _current(binding), 'effective_plan': effective,
            'consumption': consumption,
            'allowed_operations': ['quota', 'expiry', 'cycle'] if binding.source_plan['source'] == 'residential' else ['expiry']}


def resource_plan_state(actor, binding_key):
    return _state(_binding(_staff(actor), binding_key))


def _payload(payload, confirmation=False):
    fields = BASE_FIELDS | ({'preview_token', 'idempotency_key'} if confirmation else set())
    _need(type(payload) is dict and set(payload) == fields, 'invalid_fields')
    _need(type(payload['expected_revision']) is int and 1 <= payload['expected_revision'] < 2147483647, 'invalid_revision')
    _need(type(payload['source_fingerprint']) is str and re.fullmatch('[a-f0-9]{64}', payload['source_fingerprint']), 'invalid_source_fingerprint')
    _need(payload['operation'] in ('quota', 'expiry', 'cycle') and type(payload['changes']) is dict, 'invalid_operation')
    if confirmation:
        _need(type(payload['preview_token']) is str and 1 <= len(payload['preview_token']) <= 4096, 'invalid_preview')
        _need(type(payload['idempotency_key']) is str and re.fullmatch('[A-Za-z0-9_-]{8,100}', payload['idempotency_key']), 'invalid_idempotency_key')
    return {key: deepcopy(payload[key]) for key in BASE_FIELDS}


def _preview(actor, binding, request):
    _need(binding.revision == request['expected_revision'], 'revision_conflict', 409)
    _need(binding.source_fingerprint == request['source_fingerprint'], 'purchase_source_changed', 409)
    before, operation, changes = _current(binding), request['operation'], request['changes']
    after = deepcopy(before)
    _need(before['source'] == 'residential' or operation == 'expiry', 'official_plan_read_only')
    try:
        if operation == 'quota':
            _need(set(changes) in ({'quota_bytes'}, {'add_bytes'}), 'invalid_changes')
            if 'quota_bytes' in changes:
                amount = _bytes(changes['quota_bytes'])
            else:
                amount = _bytes(changes['add_bytes'])
                _need(amount > 0 and before['quota_bytes'] is not None, 'invalid_quota')
                amount += _bytes(before['quota_bytes'])
            _need(0 < amount <= MAX_BYTES, 'invalid_quota')
            after['quota_bytes'] = str(amount)
        elif operation == 'expiry':
            _need(set(changes) == {'expires_on'}, 'invalid_changes')
            _date(changes['expires_on'])
            after['expires_on'] = changes['expires_on']
        else:
            _need(set(changes) == {'days', 'next_reset_at'}, 'invalid_changes')
            _need(type(changes['days']) is int and 1 <= changes['days'] <= 366, 'invalid_cycle')
            reset = _time(changes['next_reset_at'])
            _need(reset > timezone.now().timestamp(), 'reset_must_be_future')
            after['cycle'] = {'kind': 'fixed_days', 'days': changes['days'], 'anchor': changes['next_reset_at']}
        _plan(after)
    except ResourceUsageError:
        raise ResourcePlanError('invalid_changes') from None
    _need(before != after, 'no_changes')
    effects = list(EFFECTS)
    if operation == 'cycle':
        effects.append('新重置边界只影响未来；原周期快照与累计保留，不重算历史。')
    signed = {'actor': actor.pk, 'meter_id': binding.meter_id, 'purchase_binding_key': binding.purchase_binding_key,
              'request': request, 'before': before, 'after': after}
    return {**_state(binding), 'operation': operation, 'changes': deepcopy(changes),
            'before': before, 'after': after, 'effects': effects}, _digest(signed)


def preview_resource_plan(actor, binding_key, payload):
    actor = _staff(actor)
    request = _payload(payload)
    binding = _binding(actor, binding_key)
    value, fingerprint = _preview(actor, binding, request)
    return {**value, 'preview_token': signing.dumps({'fingerprint': fingerprint}, salt=SALT)}


def _response(binding, version, replayed):
    consumption = ResourcePlanConsumption.objects.filter(version=version).first()
    status = consumption.status if consumption else 'pending'
    return {**_state(binding), 'receipt': {'id': str(version.public_id), 'revision': version.revision,
        'status': status, 'replayed': replayed, 'error_code': consumption.error_code if consumption else '',
        'effective_plan_fingerprint': consumption.effective_plan_fingerprint if consumption else None}}


def confirm_resource_plan(actor, binding_key, payload):
    actor = _staff(actor)
    _need(resource_plan_write_enabled(), 'purchase_plan_read_only', 403)
    request = _payload(payload, confirmation=True)
    try:
        with transaction.atomic():
            binding = _binding(actor, binding_key, lock=True)
            request_fingerprint = _digest({'meter_id': binding.meter_id, 'request': request})
            previous = ResourcePlanVersion.objects.filter(actor=actor, idempotency_key=payload['idempotency_key']).first()
            if previous:
                _need(previous.binding_id == binding.pk and previous.request_fingerprint == request_fingerprint,
                      'idempotency_conflict', 409)
                return _response(binding, previous, True)
            value, fingerprint = _preview(actor, binding, request)
            try:
                signed = signing.loads(payload['preview_token'], salt=SALT, max_age=PREVIEW_AGE)
            except signing.BadSignature:
                raise ResourcePlanError('preview_expired_or_changed', status=409) from None
            _need(type(signed) is dict and signed == {'fingerprint': fingerprint}, 'preview_changed', 409)
            updated = ResourcePlanBinding.objects.filter(pk=binding.pk, revision=request['expected_revision']).update(revision=F('revision') + 1)
            _need(updated == 1, 'revision_conflict', 409)
            binding.refresh_from_db()
            version = ResourcePlanVersion.objects.create(binding=binding, revision=binding.revision, actor=actor,
                idempotency_key=payload['idempotency_key'], request_fingerprint=request_fingerprint,
                source_fingerprint=binding.source_fingerprint, operation=request['operation'], changes=request['changes'],
                before=value['before'], plan=value['after'])
            return _response(binding, version, False)
    except (IntegrityError, OperationalError):
        raise ResourcePlanError('revision_conflict', status=409) from None


def export_resource_plan_overrides():
    """内部导出器输出全部顺序版本；须由固定受限文件送入现有定时器。"""
    entries = []
    for binding in ResourcePlanBinding.objects.order_by('meter_id'):
        _authorize(_staff(binding.owner), binding)
        versions = [{'id': str(v.public_id), 'revision': v.revision, 'operation': v.operation,
                     'changes': deepcopy(v.changes), 'before': deepcopy(v.before), 'plan': deepcopy(v.plan),
                     'created_at': v.created_at.isoformat()} for v in binding.versions.order_by('revision')]
        entries.append({'meter_id': binding.meter_id, 'source_fingerprint': binding.source_fingerprint,
                        'source_plan': deepcopy(binding.source_plan), 'revision': binding.revision, 'versions': versions})
    return {'schema_version': 1, 'entries': entries}


def record_resource_plan_consumption(receipt):
    """内部受信导入器专用；调用者必须先验证受限文件及定时器实际输出。"""
    fields = {'version_id', 'revision', 'source_fingerprint', 'plan_fingerprint', 'state_before_sha256',
              'state_after_sha256', 'consumed_at', 'status', 'error_code', 'effective_plan_fingerprint'}
    _need(type(receipt) is dict and set(receipt) == fields
          and (receipt['status'], receipt['error_code']) in (('consumed', ''), ('rejected', 'cycle_reset_expired')),
          'invalid_consumption')
    for field in ('source_fingerprint', 'plan_fingerprint', 'state_before_sha256', 'state_after_sha256', 'effective_plan_fingerprint'):
        _need(type(receipt[field]) is str and re.fullmatch('[0-9a-f]{64}', receipt[field]), 'invalid_consumption')
    _need(type(receipt['revision']) is int, 'invalid_consumption')
    _need(type(receipt['version_id']) is str and re.fullmatch(
        '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', receipt['version_id']), 'invalid_consumption')
    try:
        consumed = _time(receipt['consumed_at'])
        _need(consumed <= timezone.now().timestamp(), 'invalid_consumption')
        version = ResourcePlanVersion.objects.select_related('binding').get(public_id=receipt['version_id'])
    except (ResourceUsageError, ValueError, ValidationError, ResourcePlanVersion.DoesNotExist):
        raise ResourcePlanError('invalid_consumption') from None
    _need(version.revision == receipt['revision'] and version.source_fingerprint == receipt['source_fingerprint']
          and _digest(version.plan) == receipt['plan_fingerprint'] and consumed >= version.created_at.timestamp(),
          'consumption_version_conflict', 409)
    fingerprint = _digest(receipt)
    with transaction.atomic():
        # 同一采购对象串行回执，不能跳过尚未消费的中间版本。
        ResourcePlanBinding.objects.select_for_update().get(pk=version.binding_id)
        existing = ResourcePlanConsumption.objects.filter(version=version).first()
        if existing:
            _need(existing.receipt_fingerprint == fingerprint, 'consumption_conflict', 409)
            return existing
        _need(not ResourcePlanVersion.objects.filter(binding=version.binding, revision__lt=version.revision,
                    consumption__isnull=True).exists(), 'consumption_out_of_order', 409)
        prior = ResourcePlanConsumption.objects.filter(version__binding=version.binding,
            version__revision__lt=version.revision).select_related('version').order_by('version__revision')
        effective = _effective_plan(version.binding, prior)
        if receipt['status'] == 'rejected':
            _need(version.operation == 'cycle' and consumed >= _time(version.changes['next_reset_at']),
                  'invalid_rejection', 409)
        else:
            if version.operation == 'cycle':
                _need(consumed < _time(version.changes['next_reset_at']), 'expired_cycle_cannot_be_consumed', 409)
            _apply_effective_operation(effective, version)
        _need(_digest(effective) == receipt['effective_plan_fingerprint'], 'effective_plan_conflict', 409)
        return ResourcePlanConsumption.objects.create(version=version, receipt_fingerprint=fingerprint,
            plan_fingerprint=receipt['plan_fingerprint'], status=receipt['status'], error_code=receipt['error_code'],
            effective_plan_fingerprint=receipt['effective_plan_fingerprint'], state_before_sha256=receipt['state_before_sha256'],
            state_after_sha256=receipt['state_after_sha256'], consumed_at=datetime.fromisoformat(receipt['consumed_at'].replace('Z', '+00:00')))


def _apply_effective_operation(effective, version):
    """每次只应用本操作字段，不能从后续完整候选复活曾被拒绝的周期。"""
    field = {'quota': 'quota_bytes', 'expiry': 'expires_on', 'cycle': 'cycle'}[version.operation]
    effective[field] = deepcopy(version.plan[field])


def _effective_plan(binding, processed):
    """只读重建已核回执中的实际计划，已拒绝的目标留在版本审计里。"""
    effective = deepcopy(binding.source_plan)
    expected_revision = 2
    for receipt in processed:
        _need(receipt.version.revision == expected_revision, 'consumption_out_of_order', 409)
        _need((receipt.status, receipt.error_code) in (('consumed', ''), ('rejected', 'cycle_reset_expired')),
              'invalid_consumption', 409)
        if receipt.status == 'consumed':
            _apply_effective_operation(effective, receipt.version)
        _need(_digest(effective) == receipt.effective_plan_fingerprint, 'effective_plan_conflict', 409)
        expected_revision += 1
    return effective
