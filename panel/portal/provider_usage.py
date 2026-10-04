"""P11脱敏统计的只读投影；供应商账单/共享出口估算不等于个人套餐余额。"""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import time


MAX_BYTES = 64 * 1024
MAX_INTEGER = 2**53 - 1
RESIDENTIAL_TTL_SECONDS = 180
BWH_TTL_SECONDS = 7 * 3600
HOME_ACCOUNTING = 'observed_xray_outbound_upload_plus_download_not_provider_bill'
BWH_ACCOUNTING = 'provider_counter_no_extra_multiplier'
HOME_KEYS = {'source', 'accounting', 'last_query_ok', 'updated_at', 'upload_bytes', 'download_bytes',
             'used_bytes', 'total_bytes', 'estimate_start', 'historical_usage_known', 'collection_gaps',
             'period_start', 'reset_day', 'reset_day_confirmed', 'expire', 'quota_unit_assumption',
             'auto_period_reset', 'header_eligible'}
BWH_KEYS = {'source', 'accounting', 'last_query_ok', 'updated_at', 'used_bytes', 'total_bytes', 'reset_at', 'last_attempt'}


class SnapshotInvalid(ValueError):
    """固定内部错误码，不透传输入正文或路径。"""


def _require(condition, code='invalid_source'):
    if not condition:
        raise SnapshotInvalid(code)


def _integer(value, minimum=0, maximum=MAX_INTEGER):
    _require(type(value) is int and minimum <= value <= maximum)
    return value


def _timestamp(value, now, *, future=False):
    _integer(value, 946684800, 4102444800)
    _require(future or value <= now, 'future_timestamp')
    return value


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _empty_source(kind, quality='missing', error_code=None):
    home = kind == 'residential'
    common = {'source': 'estimate' if home else 'KiwiVM_getServiceInfo',
              'accounting': HOME_ACCOUNTING if home else BWH_ACCOUNTING,
              'accounting_scope': 'shared_residential_outbound' if home else 'provider_vps_billing',
              'quality': quality, 'updated_at': None, 'expires_at': None, 'last_query_ok': None,
              'error_code': error_code, 'used_bytes': None, 'total_bytes': None, 'remaining_bytes': None}
    if home:
        common.update(upload_bytes=None, download_bytes=None, estimate_start=None, historical_usage_known=False,
                      collection_gaps=None, period_start=None, reset_day=None, reset_day_confirmed=None,
                      expire=None, quota_unit_assumption=None)
    else:
        common.update(reset_at=None, over_limit=None)
    return common


def _result(state, *, error_code=None):
    quality = 'error' if state == 'error' else 'missing'
    return {'state': state, 'snapshot_updated_at': None,
            'residential': _empty_source('residential', quality, error_code),
            'bwh': _empty_source('bwh', quality, error_code)}


def _quality(result, data, updated, now, ttl, normal):
    result.update(updated_at=_iso(updated), expires_at=_iso(updated+ttl), last_query_ok=data['last_query_ok'])
    if not data['last_query_ok']:
        result.update(quality='error', error_code='source_query_failed')
    elif updated+ttl <= now:
        result.update(quality='stale', error_code='source_stale')
    else:
        result.update(quality=normal, error_code=None)


def _source(kind, value, now, snapshot_time):
    if value is None or value == {}:
        return _empty_source(kind)
    try:
        home = kind == 'residential'
        allowed = HOME_KEYS if home else BWH_KEYS
        _require(type(value) is dict and value.keys() <= allowed, 'invalid_source_schema')
        required = allowed if home else allowed - {'last_attempt'}
        _require(required <= value.keys(), 'invalid_source_schema')
        _require(type(value['source']) is str and value['source'] == ('estimate' if home else 'KiwiVM_getServiceInfo'), 'source_mismatch')
        _require(type(value['accounting']) is str and value['accounting'] == (HOME_ACCOUNTING if home else BWH_ACCOUNTING), 'accounting_mismatch')
        _require(type(value['last_query_ok']) is bool)
        updated = _timestamp(value['updated_at'], now)
        _require(updated <= snapshot_time, 'inconsistent_timestamp')
        used, total = _integer(value['used_bytes']), _integer(value['total_bytes'], 1)
        result = _empty_source(kind)
        result.update(used_bytes=str(used), total_bytes=str(total))
        if home:
            upload, download = _integer(value['upload_bytes']), _integer(value['download_bytes'])
            _require(upload+download <= MAX_INTEGER and used == upload+download, 'inconsistent_counter')
            start = _timestamp(value['estimate_start'], now)
            _require(start <= updated, 'inconsistent_timestamp')
            _require(value['historical_usage_known'] is False and value['auto_period_reset'] is False
                     and value['period_start'] is None, 'unsupported_accounting')
            _require(type(value['reset_day_confirmed']) is bool and value['reset_day_confirmed'] is False
                     and type(value['header_eligible']) is bool, 'unsupported_accounting')
            _integer(value['reset_day'], 1, 31)
            gaps = _integer(value['collection_gaps'])
            _require(value['quota_unit_assumption'] == '400_decimal_GB' and total == 400_000_000_000,
                     'quota_assumption_mismatch')
            expire = value['expire']
            if expire is not None:
                _timestamp(expire, now, future=True)
            result.update(upload_bytes=str(upload), download_bytes=str(download), estimate_start=_iso(start),
                          collection_gaps=gaps, reset_day=value['reset_day'], reset_day_confirmed=False,
                          expire=_iso(expire) if expire is not None else None, quota_unit_assumption='400_decimal_GB')
            _quality(result, value, updated, now, RESIDENTIAL_TTL_SECONDS, 'estimate')
        else:
            reset = _timestamp(value['reset_at'], now, future=True)
            if 'last_attempt' in value:
                attempt = _timestamp(value['last_attempt'], now)
                _require(attempt >= updated and attempt <= snapshot_time, 'inconsistent_timestamp')
            result.update(remaining_bytes=str(max(0, total-used)), reset_at=_iso(reset), over_limit=used > total)
            _quality(result, value, updated, now, BWH_TTL_SECONDS, 'provider_reported')
        return result
    except (SnapshotInvalid, ValueError, TypeError, KeyError, OverflowError):
        # 单来源不合格不能污染另一来源，也不能把损坏数字当上次有效样本。
        return _empty_source(kind, 'error', 'invalid_source')


def _unique(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value, 'duplicate_json_key')
        value[key] = item
    return value


def _private_read(path):
    """校路径/同句柄/更换竞争；没有链接、共享写入或开放整个目录的需要。"""
    path = Path(path)
    _require(path.is_absolute() and not any(item.is_symlink() or getattr(item, 'is_junction', lambda: False)()
                                         for item in (path, *path.parents)), 'unsafe_path')
    before = path.stat()
    def validate(info):
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= MAX_BYTES, 'unsafe_file')
        if os.name == 'posix':
            _require(not info.st_mode & 0o027, 'unsafe_permissions')
    def identity(info, with_ctime=True):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns if with_ctime else None)
    validate(before)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        validate(opened)
        _require(identity(before, os.name == 'posix') == identity(opened, os.name == 'posix'), 'file_changed')
        raw = stream.read(MAX_BYTES+1)
        after = os.fstat(stream.fileno())
        validate(after)
        _require(identity(opened) == identity(after) and len(raw) == opened.st_size, 'file_changed')
    # 原路径被替换也失败关闭，避免在竞争更新中返回已不对应当前文件的内容。
    final = path.stat()
    _require(identity(final, os.name == 'posix') == identity(after, os.name == 'posix'), 'file_changed')
    return raw


def read_provider_usage(path=None, *, now=None):
    """调用方先鉴权并提供部署固定路径；本函数不处理owner，也不主动采集。"""
    if path is None or path == '':
        return _result('disabled')
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not 946684800 <= now <= 4102444800 or not math.isfinite(now):
        return _result('error', error_code='invalid_clock')
    try:
        raw = _private_read(path)
        data = json.loads(raw, object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(SnapshotInvalid('nonfinite_json')))
        _require(type(data) is dict and {'schema_version', 'updated_at'} <= data.keys()
                 and data.keys() <= {'schema_version', 'updated_at', 'residential', 'bwh'}
                 and type(data['schema_version']) is int and data['schema_version'] == 1, 'invalid_schema')
        updated = _timestamp(data['updated_at'], now)
        return {'state': 'available', 'snapshot_updated_at': _iso(updated),
                'residential': _source('residential', data.get('residential'), now, updated),
                'bwh': _source('bwh', data.get('bwh'), now, updated)}
    except FileNotFoundError:
        return _result('missing', error_code='snapshot_missing')
    except (OSError, SnapshotInvalid, ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        return _result('error', error_code='snapshot_invalid')
