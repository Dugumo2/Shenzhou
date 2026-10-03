"""R03/R08 本人服务用量只读投影；入账日期不代表流量发生日期。"""
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db.models import Count, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .api import endpoint, error, success
from .api_helpers import byte_string, iso, project_service, service_queryset, visible_legacy_services
from .legacy_binding import verified_legacy_bindings
from .metering_quality import FUTURE_SKEW
from .models import UsageLedger


SHANGHAI = ZoneInfo('Asia/Shanghai')
DAY_LIMIT = 90
PERIODS = ('current', '7d', '30d')
HISTORY_MESSAGE = ('这里只展示已确认入账记录，入账日期不代表流量发生日期。'
                   '现有记录没有完整采样区间，未显示的日期不代表零流量，补采记录不会分摊到其他日期。')


def _totals(queryset):
    """没有确认记录时保持未知；零值只来自实际保存的确认记录。"""
    values = queryset.aggregate(record_count=Count('pk'), upload_bytes=Sum('upload_delta'),
                                download_bytes=Sum('download_delta'), charged_bytes=Sum('weighted_bytes'))
    count = values.pop('record_count')
    return count, {key: byte_string(value) for key, value in values.items()} if count else None


def _confirmed(queryset, now):
    return queryset.filter(quality='metered', weighted_bytes__isnull=False,
                           observed_at__lte=now + FUTURE_SKEW)


def _history(queryset, start, end, now):
    if queryset is None:
        return {'kind': 'confirmed_ledger_postings', 'date_basis': 'created_at',
                'range_start': iso(start), 'range_end': iso(end), 'totals': None,
                'record_count': 0, 'excluded_record_count': 0, 'days': [],
                'day_limit': DAY_LIMIT, 'days_truncated': False, 'message': HISTORY_MESSAGE}
    confirmed = _confirmed(queryset, now)
    count, totals = _totals(confirmed)
    # 聚合在数据库执行；即使账期很长，也最多返回最近90个有入账的日期。
    rows = list(confirmed.annotate(date=TruncDate('created_at', tzinfo=SHANGHAI))
                .values('date').annotate(record_count=Count('pk'), upload_bytes=Sum('upload_delta'),
                    download_bytes=Sum('download_delta'), charged_bytes=Sum('weighted_bytes'))
                .order_by('-date')[:DAY_LIMIT + 1])
    truncated = len(rows) > DAY_LIMIT
    days = [{'date': row['date'].isoformat(), 'record_count': row['record_count'],
             **{key: byte_string(row[key]) for key in ('upload_bytes', 'download_bytes', 'charged_bytes')}}
            for row in reversed(rows[:DAY_LIMIT])]
    return {'kind': 'confirmed_ledger_postings', 'date_basis': 'created_at',
            'range_start': iso(start), 'range_end': iso(end), 'totals': totals,
            'record_count': count, 'excluded_record_count': queryset.count() - count,
            'days': days, 'day_limit': DAY_LIMIT, 'days_truncated': truncated, 'message': HISTORY_MESSAGE}


def _service(record, source_type, period, now):
    projection = project_service(record, source_type, now=now)
    cycle = None
    current_records = None
    records = None
    if source_type == 'entitlement':
        cycles = list(record.cycles.filter(starts_at__lte=now, ends_at__gt=now)
                      .order_by('-starts_at')[:2])
        cycle = cycles[0] if len(cycles) == 1 else None
        # 同时约束账期与采集身份归属，不能把其他服务的记录带入本人视图。
        records = UsageLedger.objects.filter(cycle__entitlement=record,
            stream__identity__subscription__entitlement=record, created_at__lte=now)
        current_records = records.filter(cycle=cycle) if cycle else None
    _, current_totals = _totals(_confirmed(current_records, now)) if current_records is not None else (0, None)
    if period == 'current':
        start, end = (cycle.starts_at, cycle.ends_at) if cycle else (None, None)
        selected = current_records
    else:
        # 上海自然日：包含今天以及此前6/29天，不依浏览器或主机的本地时区。
        today = now.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = today - timedelta(days=int(period[:-1]) - 1), now
        selected = records.filter(created_at__gte=start) if records is not None else None
    return {'service_id': projection['id'], 'source_type': source_type,
            'time_zone': 'Asia/Shanghai', 'generated_at': iso(now), 'period': period,
            'current_cycle': {'starts_at': iso(cycle.starts_at), 'ends_at': iso(cycle.ends_at)} if cycle else None,
            'summary': {'quota_bytes': projection['quota_bytes'], 'quota_state': projection['quota_state'],
                        'charged_bytes': projection['used_bytes'],
                        'upload_bytes': current_totals['upload_bytes'] if current_totals else None,
                        'download_bytes': current_totals['download_bytes'] if current_totals else None,
                        'remaining_bytes': projection['remaining_bytes'],
                        'next_reset_at': projection['next_reset_at']},
            'quality': {'state': projection['usage']['quality'], 'message': projection['usage']['message'],
                        'collected_at': projection['usage']['updated_at']},
            'history': _history(selected, start, end, now)}


@endpoint()
def service_usage(request, public_id):
    periods = request.GET.getlist('period')
    period = periods[0] if periods else 'current'
    if len(periods) > 1 or period not in PERIODS:
        return error('invalid_filter', '请选择本期、近7天或近30天。', 422,
                     {'period': ['仅支持 current、7d、30d。']})
    try:
        value = uuid.UUID(public_id)
    except (ValueError, TypeError, AttributeError):
        return error('not_found', '服务不存在或不可访问。', 404)
    record = service_queryset().filter(user=request.user, public_id=value).first()
    if record is None:
        # 与服务详情保持相同的已核验旧UUID别名；坏绑定不能绕过列表门禁。
        binding = verified_legacy_bindings().filter(owner=request.user, membership__public_id=value).first()
        if binding is not None and binding.entitlement_id is not None:
            record = binding.entitlement
    if record is not None:
        return success(_service(record, 'entitlement', period, timezone.now()))
    legacy = visible_legacy_services().select_related('user').filter(user=request.user, public_id=value).first()
    if legacy is None:
        return error('not_found', '服务不存在或不可访问。', 404)
    return success(_service(legacy, 'membership', period, timezone.now()))
