"""订阅标头只读同一套餐投影，不推导供应商用量或原始方向。"""
import re
from datetime import datetime

from django.contrib.auth import get_user_model
from django.utils import timezone

from .api_helpers import project_service
from .metering_quality import FRESH_WINDOW, FUTURE_SKEW


def _bytes(value):
    """投影的字节字符串必须保持精确，不接受浮点、负数或布尔。"""
    if type(value) is not str or not re.fullmatch(r'0|[1-9][0-9]{0,18}', value):
        return None
    parsed = int(value)
    return parsed if parsed <= 2**63 - 1 else None


def _time(value):
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if timezone.is_aware(parsed) else None


def userinfo_from_projection(item, *, now=None):
    """标头无未知/部分/陈旧状态，无法完整表达时省略整个标头。"""
    now = now or timezone.now()
    if (type(item) is not dict or item.get('state') != 'active'
            or item.get('enabled') is not True or item.get('quota_state') != 'applied'
            or type(item.get('application')) is not dict
            or item['application'].get('state') != 'applied'
            or type(item.get('usage')) is not dict
            or item['usage'].get('quality') != 'measured'):
        return None
    quota, used = _bytes(item.get('quota_bytes')), _bytes(item.get('used_bytes'))
    expires, reset = _time(item.get('expires_at')), _time(item.get('next_reset_at'))
    observed = _time(item['usage'].get('updated_at'))
    if (quota is None or quota == 0 or used is None or expires is None or expires <= now
            or reset is None or reset <= now or observed is None
            or observed >= reset
            or not now - FRESH_WINDOW <= observed <= now + FUTURE_SKEW):
        return None
    # 沿用双向套餐消耗合计约定；download不是原始下载量，upload=0不代表无上传。
    # 标准字段不含重置、倍率或质量；不添加客户端未经验证的自定义字段。
    return f'upload=0; download={used}; total={quota}; expire={int(expires.timestamp())}'


def _projection_for_service(owner, public_id, now, *, p8_only=False):
    """下载鉴权通过后按本人稳定编号重新解析当前来源，坏映射不回退旧账。"""
    from .service_projection import resolve, valid_p8
    from .p8_entitlement import current_entitlement

    if (not owner.is_authenticated or not owner.is_active
            or not get_user_model().objects.filter(pk=owner.pk, is_active=True).exists()):
        return None
    resolved = resolve(owner, public_id)
    if resolved is None:
        return None
    kind, record = resolved
    if p8_only and kind != 'p8':
        return None
    if kind == 'p8':
        if not valid_p8(record):
            return None
        record = current_entitlement(record)
        if record is None:
            return None
        kind = 'entitlement'
    # 独立旧会员的个人计量仍未核验；project_service保持unknown，不能借used_bytes充数。
    return project_service(record, kind, now=now)


def userinfo_for_service(owner, public_id, *, now=None):
    now = now or timezone.now()
    item = _projection_for_service(owner, public_id, now)
    return userinfo_from_projection(item, now=now)


def p8_usage_snapshot(owner, public_id, manifest_sha256, *, now=None):
    """受信发布端生成非秘密套餐快照；原授权清单摘要由调用端就地提供。"""
    from django.core.exceptions import ValidationError
    if type(manifest_sha256) is not str or not re.fullmatch(r'[a-f0-9]{64}', manifest_sha256):
        raise ValidationError('原授权清单摘要无效')
    now = now or timezone.now()
    item = _projection_for_service(owner, public_id, now, p8_only=True)
    userinfo = userinfo_from_projection(item, now=now)
    # generated_at不能代替measured_at；采集失败时必须发布明确不可用或保留随后过期的旧快照。
    return {'schema_version': 1, 'service_id': item['id'] if item else None,
        'manifest_sha256': manifest_sha256, 'generated_at': int(now.timestamp()),
        'measured_at': int(_time(item['usage']['updated_at']).timestamp()) if userinfo else None,
        'next_reset_at': int(_time(item['next_reset_at']).timestamp()) if userinfo else None,
        'state': 'available' if userinfo else 'unavailable', 'userinfo': userinfo}
