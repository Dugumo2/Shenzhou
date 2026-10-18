"""读取受信投影器发布的非秘密采购权威镜像，不授予面板读取原root配置的权限。"""
import json
import os
from pathlib import Path
import stat
from django.conf import settings
from django.utils import timezone
from .resource_usage import _plan, _time

AUTHORITY_PATH = Path('/var/lib/relay-usage-panel/plan-authority.json')


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_authority_key')
        result[key] = value
    return result


def read_resource_plan_source(meter_id):
    path = AUTHORITY_PATH
    if not settings.PANEL_LIVE:
        path = Path(getattr(settings, 'RESOURCE_PLAN_AUTHORITY_TEST_PATH', path))
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError('unsafe_authority_path')
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 < before.st_size <= 65536:
        raise ValueError('invalid_authority_file')
    if settings.PANEL_LIVE and (os.name != 'posix' or before.st_uid != 0
            or before.st_gid != 998 or stat.S_IMODE(before.st_mode) != 0o640):
        raise ValueError('unsafe_authority_permissions')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(65537)
        after = os.fstat(stream.fileno())
    if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or len(raw) != before.st_size):
        raise ValueError('authority_changed')
    doc = json.loads(raw, object_pairs_hook=_unique,
                     parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_number')))
    if (type(doc) is not dict or set(doc) != {'schema_version', 'observed_at', 'plans'}
            or type(doc['schema_version']) is not int or doc['schema_version'] != 1
            or type(doc['plans']) is not list or not 1 <= len(doc['plans']) <= 16):
        raise ValueError('invalid_authority')
    age = timezone.now().timestamp() - _time(doc['observed_at'])
    if not 0 <= age <= 180:
        raise ValueError('authority_expired')
    plans = {}
    for plan in doc['plans']:
        _plan(plan)
        if plan['id'] in plans:
            raise ValueError('duplicate_meter')
        plans[plan['id']] = plan
    if meter_id not in plans:
        raise ValueError('meter_not_found')
    return plans[meter_id]
