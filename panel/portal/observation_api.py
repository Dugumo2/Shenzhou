"""管理员已采集现场快照；只读脱敏文件，不主动探测或修改现场。"""

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from django.conf import settings

from .api import endpoint, error, success


MAX_BYTES = 256 * 1024


def _check(condition):
    if not condition:
        raise ValueError('invalid_snapshot')


def _object(value, keys):
    _check(type(value) is dict and set(value) == set(keys))


def _text(value, limit=240, *, empty=True):
    _check(type(value) is str and len(value) <= limit and (empty or bool(value))
           and not any(ord(character) < 32 for character in value))


def _integer(value, minimum=0, maximum=2**31 - 1):
    _check(type(value) is int and minimum <= value <= maximum)


def _timestamp(value, *, nullable=False):
    if nullable and value is None:
        return
    _text(value, 64, empty=False)
    parsed = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    _check(parsed.tzinfo is not None and parsed.utcoffset() is not None)


def _rows(value):
    _check(type(value) is list and len(value) <= 500)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _check(key not in result)
        result[key] = value
    return result


def validate_snapshot(value):
    """严格验证整份白名单，任何意外字段都不能透传到管理页面。"""
    _object(value, ('schema_version', 'source_label', 'captured_at', 'assembled_at', 'units', 'accounts',
                    'membership', 'rules', 'resources', 'limitations'))
    _check(type(value['schema_version']) is int and value['schema_version'] == 1)
    _text(value['source_label'], 200, empty=False)
    _timestamp(value['captured_at'], nullable=True)
    _timestamp(value['assembled_at'])
    _rows(value['units'])
    for row in value['units']:
        _object(row, ('name', 'status'))
        _text(row['name'], 128, empty=False)
        _text(row['status'], 64, empty=False)
    accounts = value['accounts']
    _object(accounts, ('total', 'active_admins'))
    _integer(accounts['total'])
    _integer(accounts['active_admins'], maximum=accounts['total'])
    member = value['membership']
    _object(member, ('registered_quota_bytes', 'expires_at', 'provisioning_state', 'usage_state'))
    quota = member['registered_quota_bytes']
    _check(type(quota) is str and re.fullmatch(r'0|[1-9][0-9]{0,18}', quota)
           and int(quota) <= 2**63 - 1)
    # 原SQL时间未含时区时只保留原文，不能补时区冒充已核验时刻。
    if member['expires_at'] is not None:
        _text(member['expires_at'], 64, empty=False)
    _text(member['provisioning_state'], 64, empty=False)
    _text(member['usage_state'], 64, empty=False)
    _rows(value['rules'])
    rule_ids = set()
    for row in value['rules']:
        _object(row, ('id', 'action', 'kind', 'value', 'scope_domain', 'enabled', 'revision'))
        _integer(row['id'], 1)
        _check(row['id'] not in rule_ids)
        rule_ids.add(row['id'])
        _check(type(row['action']) is str and row['action'] in ('direct', 'proxy'))
        _check(type(row['kind']) is str and row['kind'] in ('exact', 'suffix', 'regex'))
        _text(row['value'], 2048, empty=False)
        _text(row['scope_domain'], 253)
        _check(type(row['enabled']) is bool)
        _integer(row['revision'], 1)
    _rows(value['resources'])
    resource_keys = set()
    for row in value['resources']:
        _object(row, ('key', 'label', 'http_status', 'format', 'status', 'message'))
        _text(row['key'], 80, empty=False)
        _check(re.fullmatch(r'[a-z0-9-]+', row['key']) and row['key'] not in resource_keys)
        resource_keys.add(row['key'])
        _text(row['label'], 200, empty=False)
        if row['http_status'] is not None:
            _integer(row['http_status'], 100, 599)
        _text(row['format'], 80, empty=False)
        _text(row['status'], 80, empty=False)
        _text(row['message'], 2000)
    _rows(value['limitations'])
    for limitation in value['limitations']:
        _text(limitation, 2000, empty=False)
    return value


def _load(path_value, expected_sha256):
    _check(type(expected_sha256) is str and re.fullmatch(r'[a-f0-9]{64}', expected_sha256))
    _check(isinstance(path_value, (str, Path)))
    path = Path(path_value)
    _check(path.is_absolute() and not any(item.is_symlink()
        or getattr(item, 'is_junction', lambda: False)() for item in (path, *path.parents)))
    before = path.stat()
    _check(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and 0 < before.st_size <= MAX_BYTES)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        _check(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1)
        _check((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
               == (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns))
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        _check((opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns, opened.st_nlink)
               == (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink))
    _check(len(raw) == opened.st_size and 0 < len(raw) <= MAX_BYTES
           and hashlib.sha256(raw).hexdigest() == expected_sha256)
    return validate_snapshot(json.loads(raw, object_pairs_hook=_unique))


def _no_store(response):
    response['Cache-Control'] = 'no-store, private'
    return response


@endpoint(staff=True)
def observed_site(request):
    path = getattr(settings, 'OBSERVATION_PATH', None)
    digest = getattr(settings, 'OBSERVATION_SHA256', '')
    if not path or not digest:
        return _no_store(error('not_configured', '尚未配置已核验的现场观测快照。', 404))
    try:
        snapshot = _load(path, digest)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return _no_store(error('snapshot_unavailable', '现场快照无法通过完整性校验，请联系管理员核对。', 503))
    return _no_store(success(snapshot))
