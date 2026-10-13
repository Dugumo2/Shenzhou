"""显式接受的展示估算；不改个人账、覆盖质量、周期或客户端可靠标头。"""
from datetime import datetime
import re

FIELDS = {'version', 'owner_id', 'service_id', 'cycle_starts_at', 'cycle_ends_at',
          'accepted_at', 'source_observed_at', 'source_sha256', 'approval_ref',
          'upload_bytes', 'download_bytes', 'charged_bytes', 'ledger_baseline'}


def _time(value):
    if type(value) is not str:
        raise ValueError()
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError()
    return result


def _bytes(value):
    if type(value) is not str or not re.fullmatch(r'0|[1-9][0-9]{0,18}', value):
        raise ValueError()
    result = int(value)
    if result > 2**63 - 1:
        raise ValueError()
    return result


def project_estimate(record, cycle, quota, recorded, now):
    """只在相同服务、用户与当前周期中展示；旧基数永不自动滚入下期。"""
    try:
        snapshot = record.applied_snapshot
        value = snapshot.get('accepted_display_estimate') if type(snapshot) is dict else None
        if type(value) is not dict or set(value) != FIELDS or type(value['version']) is not int or value['version'] != 1:
            return None
        if (cycle is None or cycle.entitlement_id != record.pk or not cycle.starts_at <= now < cycle.ends_at
                or type(value['owner_id']) is not int or value['owner_id'] != record.user_id
                or value['service_id'] != str(record.public_id)
                or _time(value['cycle_starts_at']) != cycle.starts_at or _time(value['cycle_ends_at']) != cycle.ends_at
                or not cycle.starts_at <= _time(value['source_observed_at']) <= _time(value['accepted_at']) <= now
                or _time(value['accepted_at']) >= cycle.ends_at
                or type(value['source_sha256']) is not str or not re.fullmatch(r'[a-f0-9]{64}', value['source_sha256'])
                or type(value['approval_ref']) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}', value['approval_ref'])
                or type(quota) is not int or quota <= 0):
            return None
        keys = ('charged_bytes', 'upload_bytes', 'download_bytes')
        baseline = value['ledger_baseline']
        if type(baseline) is not dict or set(baseline) != set(keys):
            return None
        opening = [_bytes(value[key]) for key in keys]
        if opening[0] != opening[1] + opening[2]:
            return None
        actual_keys = ('recorded_used_bytes', 'recorded_upload_bytes', 'recorded_download_bytes')
        totals = []
        for index, key in enumerate(keys):
            previous = _bytes(baseline[key])
            actual = recorded[actual_keys[index]]
            # 无确认行只允许“迁入时没有个人账行”的明确基线，不补未知历史零。
            current = 0 if actual is None and previous == 0 else _bytes(actual)
            if current < previous:
                return None
            totals.append(opening[index] + current - previous)
        if any(total > 2**63 - 1 for total in totals):
            return None
        latest = record.usage_updated_at
        if latest is None or latest.utcoffset() is None or latest > now:
            latest = _time(value['source_observed_at'])
        return {'state': 'estimated', 'used_bytes': str(totals[0]),
                'remaining_bytes': str(max(0, quota - totals[0])),
                'upload_bytes': str(totals[1]), 'download_bytes': str(totals[2]),
                'as_of': max(_time(value['source_observed_at']), latest).isoformat()}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None
