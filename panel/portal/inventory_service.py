"""安全资料编辑：仅别名与备注，数据库修订和唯一回执共同保护重试。"""
import hashlib
import json
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F

from .models import InventoryMutationReceipt, Line, Server
from .services import audit


class InventoryError(ValueError):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status


def metadata_edit_enabled():
    """生产由独立开关放行资料编辑，不解除规则或执行器的门禁。"""
    return getattr(settings, 'INVENTORY_METADATA_WRITE_ENABLED', not settings.PANEL_LIVE) is True


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def edit_metadata(actor, kind, public_id, payload):
    if not metadata_edit_enabled():
        raise InventoryError('metadata_read_only', '当前环境未开放资源资料修改。', 403)
    if (kind not in ('server', 'line') or type(payload) is not dict
            or not {'revision', 'idempotency_key'} <= payload.keys()
            or not payload.keys() <= {'revision', 'idempotency_key', 'name', 'notes'}
            or not payload.keys() & {'name', 'notes'}):
        raise InventoryError('invalid_fields', '仅可修改别名和备注。')
    revision, key = payload['revision'], payload['idempotency_key']
    if type(revision) is not int or not 1 <= revision <= 2147483647:
        raise InventoryError('invalid_revision', '请先读取资源的当前版本。')
    if type(key) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', key):
        raise InventoryError('invalid_idempotency_key', '修改请求标识无效。')
    changes = {}
    for field, limit in (('name', 100), ('notes', 1000)):
        if field in payload:
            value = payload[field]
            if (type(value) is not str or len(value) > limit or (field == 'name' and not value.strip())
                    or any(ord(c) < 32 and (field == 'name' or c not in '\n\t') for c in value)):
                raise InventoryError('invalid_metadata', '别名不能为空，且别名/备注须在长度和字符限制内。')
            changes[field] = value.strip()
    fingerprint = digest({'kind': kind, 'target': str(public_id), 'payload': payload})
    model = Server if kind == 'server' else Line
    try:
        with transaction.atomic():
            current_actor = get_user_model().objects.select_for_update().filter(pk=actor.pk, is_active=True, is_staff=True).first()
            if current_actor is None:
                raise InventoryError('permission_denied', '此操作仅限当前有效管理员。', 403)
            receipt = InventoryMutationReceipt.objects.filter(actor=current_actor, key=key).first()
            if receipt:
                if receipt.fingerprint != fingerprint:
                    raise InventoryError('idempotency_conflict', '此请求标识已用于不同修改。', 409)
                return {**receipt.response, 'replayed': True,
                        'message': '此前修改已保存；这是原请求回执，请刷新当前资料，不代表当前运行状态。'}
            item = model.objects.filter(public_id=public_id).first()
            if item is None:
                raise InventoryError('not_found', '资源不存在或不可访问。', 404)
            if not model.objects.filter(pk=item.pk, revision=revision).update(**changes, revision=F('revision') + 1):
                raise InventoryError('revision_conflict', '资料已被其他操作修改；请刷新并核对后重试。', 409)
            item.refresh_from_db(fields=['name', 'notes', 'revision'])
            result = {'item': {'id': str(item.public_id), 'name': item.name, 'notes': item.notes, 'revision': item.revision},
                      'replayed': False, 'message': '资料已保存；节点地址、权限和运行配置保持不变。'}
            InventoryMutationReceipt.objects.create(actor=current_actor, key=key, fingerprint=fingerprint, response=result)
            audit(actor, 'inventory_metadata_saved', str(item.public_id), 'METADATA_ONLY')
            return result
    except (OperationalError, IntegrityError):
        raise InventoryError('write_conflict', '资料正在更新；请保留原请求标识重试。', 409) from None
