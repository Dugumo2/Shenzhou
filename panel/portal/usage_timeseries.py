"""Q5：按已确认入口区间读取自然时间账；不补造零值或按时间均摊。"""
import hashlib
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core import signing
from django.db.models import F, Max, Q
from django.utils import timezone

from .metering_quality import FRESH_WINDOW
from .models import NodeIdentity, ServiceRateVersion, UsageLedger


SHANGHAI = ZoneInfo('Asia/Shanghai')
PERIODS = ('7d', 'month', 'year')
UNALLOCATED_PAGE_SIZE = 100
CURSOR_MAX_AGE = 900
CURSOR_SALT = 'portal.natural-usage.v1'
BYTE_FIELDS = ('charged_bytes', 'upload_bytes', 'download_bytes')


class TimeseriesUnavailable(ValueError):
    """查询无法安全确认时交由 API 返回有限错误码。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _iso(value):
    return value.astimezone(SHANGHAI).isoformat() if value is not None else None


def _next_month(value):
    return value.replace(year=value.year + 1, month=1) if value.month == 12 else value.replace(month=value.month + 1)


def _ranges(period, now):
    today = now.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == '7d':
        start, count, increment = today - timedelta(days=6), 7, lambda value: value + timedelta(days=1)
    elif period == 'month':
        start = today.replace(day=1)
        count, increment = (_next_month(start) - start).days, lambda value: value + timedelta(days=1)
    elif period == 'year':
        start, count, increment = today.replace(month=1, day=1), 12, _next_month
    else:
        raise TimeseriesUnavailable('invalid_period')
    result, current = [], start
    for _ in range(count):
        end = increment(current)
        result.append((current, end))
        current = end
    return start, result


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def _restore_cursor(cursor, entitlement, period):
    if not isinstance(cursor, str) or len(cursor) > 2048:
        raise TimeseriesUnavailable('invalid_cursor')
    try:
        value = signing.loads(cursor, salt=CURSOR_SALT, max_age=CURSOR_MAX_AGE)
        if (set(value) != {'service', 'period', 'as_of', 'watermark', 'offset', 'shape'}
                or value['service'] != str(entitlement.public_id) or value['period'] != period
                or type(value['watermark']) is not int or value['watermark'] < 0
                or type(value['offset']) is not int or value['offset'] < 0):
            raise ValueError
        now = datetime.fromisoformat(value['as_of'])
        if timezone.is_naive(now):
            raise ValueError
        return value, now
    except (signing.BadSignature, ValueError, TypeError, KeyError, AttributeError):
        raise TimeseriesUnavailable('invalid_cursor') from None


def _bytes(values):
    return dict(zip(BYTE_FIELDS, (str(value) for value in values)))


def _amount(row):
    return row['weighted_bytes'], row['upload_delta'], row['download_delta']


def _add(target, values):
    for index, value in enumerate(values):
        target[index] += value


class _Coverage:
    """每个身份只保存连续覆盖水位，不把全年样本载入内存。"""

    def __init__(self, start, end, identities):
        self.start, self.end = start, end
        self.required = {pk: min(end, revoked) if revoked else end
                         for pk, revoked in identities if revoked is None or revoked > start}
        self.through = {pk: start for pk in self.required}
        self.latest = {pk: start for pk in self.required}

    def add(self, identity, start, end):
        if identity not in self.through:
            return
        self.latest[identity] = min(self.required[identity], max(self.latest[identity], end))
        if start <= self.through[identity]:
            self.through[identity] = min(self.required[identity], max(self.through[identity], end))

    @property
    def complete(self):
        return self.end > self.start and bool(self.required) and all(self.through[pk] >= end for pk, end in self.required.items())

    @property
    def sampling_pending(self):
        """只允许新鲜、连续覆盖的尾部等待，不掩盖任何已知中间缺口。"""
        if not self.required or self.complete:
            return False
        for identity, required_end in self.required.items():
            through = self.through[identity]
            if through >= required_end:
                continue
            if (required_end != self.end or through <= self.start
                    or through < self.end - FRESH_WINDOW or self.latest[identity] != through):
                return False
        return True

    @property
    def covered_through(self):
        if not self.required:
            return None
        # 已撤销入口完成自身有效段后不限制其余入口后续的覆盖水位。
        values = [self.end if value >= self.required[pk] else value for pk, value in self.through.items()]
        result = min(values)
        return result if result > self.start else None


def _read(entitlement, period, now, cursor, include_lines):
    restored = None
    if cursor is not None:
        restored, now = _restore_cursor(cursor, entitlement, period)
    now = now or timezone.now()
    if timezone.is_naive(now):
        raise TimeseriesUnavailable('invalid_as_of')
    start, ranges = _ranges(period, now)
    all_rows = UsageLedger.objects.filter(cycle__entitlement=entitlement,
        stream__identity__subscription__entitlement=entitlement, created_at__lte=now)
    watermark = restored['watermark'] if restored else all_rows.aggregate(value=Max('pk'))['value'] or 0
    identities = list(NodeIdentity.objects.filter(subscription__entitlement=entitlement)
                      .order_by('pk').values_list('pk', 'revoked_at'))
    rates = list(ServiceRateVersion.objects.filter(entitlement=entitlement)
                 .order_by('effective_at', 'pk').values('pk', 'line_id', 'ingress_id', 'multiplier', 'effective_at'))
    shape = _digest([identities, rates])
    if restored and restored['shape'] != shape:
        raise TimeseriesUnavailable('invalid_cursor')
    revision = _digest([str(entitlement.public_id), watermark, shape])
    rate_groups = defaultdict(list)
    for rate in rates:
        rate_groups[rate['line_id'], rate['ingress_id']].append(rate)
    effective_dates = {key: [rate['effective_at'] for rate in group] for key, group in rate_groups.items()}
    buckets = []
    for left, right in ranges:
        buckets.append({'start': left, 'end': right, 'values': [0, 0, 0], 'count': 0,
            'unallocated': False, 'coverage': _Coverage(left, min(right, now), identities)})
    coverage = _Coverage(start, now, identities)
    bucket_starts = [bucket['start'] for bucket in buckets]
    totals, unallocated_totals, count, unallocated_count, boundary_count = [0, 0, 0], [0, 0, 0], 0, 0, 0
    unallocated, lines = [], {}
    offset = restored['offset'] if restored else 0
    last_end = {}
    rows = all_rows.filter(interval_start__lt=now).filter(pk__lte=watermark, quality='metered', weighted_bytes__isnull=False,
        interval_end__gt=start, interval_start__lt=F('interval_end'),
        interval_end__lte=F('observed_at'), observed_at__lte=now,
        cycle__starts_at__lte=F('interval_start'), cycle__ends_at__gte=F('interval_end'),
        grant_rate_version__entitlement=entitlement,
        grant_rate_version__line_id=F('stream__identity__line_id'),
        grant_rate_version__ingress_id=F('stream__identity__ingress_id')).filter(
            Q(stream__identity__revoked_at__isnull=True) | Q(stream__identity__revoked_at__gte=F('interval_end')))
    fields = ('pk', 'interval_start', 'interval_end', 'weighted_bytes', 'upload_delta', 'download_delta',
        'stream__identity_id', 'stream__identity__line_id', 'stream__identity__ingress_id', 'grant_rate_version_id')
    if include_lines:
        fields += ('stream__identity__line__public_id', 'stream__identity__line__name', 'stream__identity__ingress__name')
    for row in rows.order_by('interval_start', 'pk').values(*fields).iterator(chunk_size=1000):
        left, right = row['interval_start'], row['interval_end']
        group_key = row['stream__identity__line_id'], row['stream__identity__ingress_id']
        group, dates = rate_groups.get(group_key, []), effective_dates.get(group_key, [])
        index = bisect_right(dates, left) - 1
        # 不让错服务、错入口或跨倍率边界的区间混入已确认统计。
        if (index < 0 or group[index]['pk'] != row['grant_rate_version_id']
                or (index + 1 < len(group) and group[index + 1]['effective_at'] < right)
                or any(value < 0 for value in _amount(row))):
            continue
        identity = row['stream__identity_id']
        if identity in last_end and left < last_end[identity]:
            raise TimeseriesUnavailable('overlapping_intervals')
        last_end[identity] = right
        touched = buckets[max(0, bisect_right(bucket_starts, left) - 1):bisect_left(bucket_starts, right)]
        if left < start or right > now:
            boundary_count += 1
            for bucket in touched:
                bucket['unallocated'] = True
            continue
        values = _amount(row)
        _add(totals, values)
        count += 1
        coverage.add(identity, left, right)
        allocated = len(touched) == 1
        for bucket in touched:
            bucket['coverage'].add(identity, left, right)
            if allocated:
                _add(bucket['values'], values)
                bucket['count'] += 1
            else:
                bucket['unallocated'] = True
        if not allocated:
            if offset <= unallocated_count < offset + UNALLOCATED_PAGE_SIZE:
                unallocated.append({'starts_at': _iso(left), 'ends_at': _iso(right), **_bytes(values)})
            unallocated_count += 1
            _add(unallocated_totals, values)
        if include_lines:
            rate = group[index]
            key = (group_key, rate['pk'])
            if key not in lines:
                lines[key] = {'line_id': str(row['stream__identity__line__public_id']),
                    'line_name': row['stream__identity__line__name'], 'node_name': row['stream__identity__ingress__name'],
                    'multiplier': format(Decimal(rate['multiplier']), 'f'), 'effective_from': _iso(rate['effective_at']),
                    'effective_to': _iso(group[index + 1]['effective_at']) if index + 1 < len(group) else None,
                    'values': [0, 0, 0]}
            _add(lines[key]['values'], values)
    rendered = []
    for bucket in buckets:
        future = bucket['start'] > now
        complete = bucket['coverage'].complete and not bucket['unallocated']
        state = ('future' if future else 'complete' if complete else
                 'partial' if bucket['count'] or bucket['unallocated'] else 'missing')
        values = _bytes(bucket['values']) if not future and (bucket['count'] or complete) else dict.fromkeys(BYTE_FIELDS)
        rendered.append({'start': _iso(bucket['start']), 'end': _iso(bucket['end']), 'state': state,
            'is_open': bucket['start'] <= now < bucket['end'] if not future else False,
            'covered_through': _iso(bucket['coverage'].covered_through) if not future else None, **values})
    state = 'complete' if coverage.complete and not boundary_count else 'partial' if count or boundary_count else 'missing'
    message = ('boundary_pending' if boundary_count else 'unallocated_intervals' if unallocated_count else
               'sampling_pending' if state == 'partial' and coverage.sampling_pending else
               'partial_coverage' if state == 'partial' else 'missing_history' if state == 'missing' else None)
    next_cursor = None
    if offset + UNALLOCATED_PAGE_SIZE < unallocated_count:
        next_cursor = signing.dumps({'service': str(entitlement.public_id), 'period': period, 'as_of': _iso(now),
            'watermark': watermark, 'offset': offset + UNALLOCATED_PAGE_SIZE, 'shape': shape}, salt=CURSOR_SALT, compress=True)
    value = {'service_id': str(entitlement.public_id), 'time_zone': 'Asia/Shanghai', 'period': period,
        'granularity': 'month' if period == 'year' else 'day', 'range_start': _iso(start), 'range_end': _iso(now),
        'as_of': _iso(now), 'summary_revision': revision,
        'totals': {'state': state, **(_bytes(totals) if count else dict.fromkeys(BYTE_FIELDS)),
            'unallocated_charged_bytes': str(unallocated_totals[0]) if count else None},
        'buckets': rendered, 'unallocated': unallocated, 'unallocated_next_cursor': next_cursor,
        'boundary_pending_count': boundary_count, 'message_code': message}
    if include_lines:
        line_usage = {key: value[key] for key in ('service_id', 'time_zone', 'period', 'range_start', 'range_end',
            'as_of', 'summary_revision', 'totals', 'boundary_pending_count', 'message_code')}
        line_usage['lines'] = [{**{key: item[key] for key in item if key != 'values'}, **_bytes(item['values'])}
                          for item in lines.values()]
        return {'timeseries': value, 'line_usage': line_usage}
    return value


def build_timeseries(entitlement, period, now=None, cursor=None):
    """只读固定的追加账本水位；分页每页返回完整范围总量。"""
    return _read(entitlement, period, now, cursor, False)


def build_line_usage(entitlement, period, now=None):
    """线路明细消费相同区间账，按授权倍率版本分组且不返回供应商原件。"""
    return _read(entitlement, period, now, None, True)['line_usage']


def build_usage_views(entitlement, period, now=None, cursor=None):
    """同页两种投影只扫描一次，保持完全相同的账本水位及合计。"""
    return _read(entitlement, period, now, cursor, True)


def empty_usage_views(service_id, period, now=None):
    """未建立归属时只生成未知时间轴，不读取供应商或推断个人用量。"""
    now = now or timezone.now()
    if timezone.is_naive(now):
        raise TimeseriesUnavailable('invalid_as_of')
    start, ranges = _ranges(period, now)
    totals = {'state': 'missing', **dict.fromkeys(BYTE_FIELDS), 'unallocated_charged_bytes': None}
    common = {'service_id': str(service_id), 'time_zone': 'Asia/Shanghai', 'period': period,
        'range_start': _iso(start), 'range_end': _iso(now), 'as_of': _iso(now),
        'summary_revision': None, 'totals': totals, 'boundary_pending_count': 0, 'message_code': 'missing_history'}
    buckets = [{'start': _iso(left), 'end': _iso(right), 'state': 'future' if left > now else 'missing',
        'is_open': left <= now < right, 'covered_through': None, **dict.fromkeys(BYTE_FIELDS)} for left, right in ranges]
    return {'timeseries': {**common, 'granularity': 'month' if period == 'year' else 'day', 'buckets': buckets,
                          'unallocated': [], 'unallocated_next_cursor': None},
            'line_usage': {**common, 'lines': []}}
