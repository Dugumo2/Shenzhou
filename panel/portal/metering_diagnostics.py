"""管理员计量诊断的有限只读投影，不返回账本原件、身份或配置指纹。"""
from .api_helpers import iso, service_facts, service_registration_status
from .metering_quality import assess_metering
from .usage_timeseries import TimeseriesUnavailable, build_timeseries


def service_metering_diagnostics(record, service_id, period, now):
    """展示当前证据与范围聚合；任何缺口原因仍需结合实际采集证据核验。"""
    cycles = list(record.cycles.filter(starts_at__lte=now, ends_at__gt=now).order_by('-starts_at')[:2])
    cycle = cycles[0] if len(cycles) == 1 else None
    assessment = assess_metering(record, cycle, now)
    facts = service_facts(record, 'entitlement', now)
    baseline = {'state': 'none', 'charged_bytes': None, 'source_observed_at': None}
    # 只消费既有严格校验后的本期展示投影，不把原始snapshot暴露给管理员页面。
    # 下期、错owner、错服务或不可用基线均不会产生usage_estimate。
    if facts['usage_estimate'] is not None:
        accepted = record.applied_snapshot['accepted_display_estimate']
        baseline = {'state': 'applied', 'charged_bytes': accepted['charged_bytes'],
                    'source_observed_at': accepted['source_observed_at']}
    try:
        timeseries = build_timeseries(record, period, now=now)
        coverage = {'state': timeseries['totals']['state'], 'message_code': timeseries['message_code'],
                    'range_start': timeseries['range_start'], 'range_end': timeseries['range_end'],
                    'unallocated_charged_bytes': timeseries['totals']['unallocated_charged_bytes'],
                    'boundary_pending_count': timeseries['boundary_pending_count']}
    except TimeseriesUnavailable as exc:
        coverage = {'state': 'unavailable', 'message_code': exc.code, 'range_start': None,
                    'range_end': None, 'unallocated_charged_bytes': None, 'boundary_pending_count': 0}
    return {
        'service_id': str(service_id), 'period': period, 'generated_at': iso(now),
        'service_status': service_registration_status(record, 'entitlement', facts, now),
        'metering': {'quality': facts['quality'], 'evidence_quality': assessment['evidence_quality'],
                     'updated_at': iso(record.usage_updated_at), 'persisted_gap': record.metering_gap,
                     'active_identity_count': len(assessment['active_identity_ids']),
                     'evidence_identity_count': len(assessment['evidence_identity_ids']),
                     'unverified_identity_count': len(assessment['missing_identity_ids']),
                     'ambiguous_identity_count': len(assessment['ambiguous_identity_ids']),
                     'partial_observed_identity_count': assessment['partial_observed_identity_count'],
                     'reason_codes': assessment['reason_codes']},
        'historical_baseline': baseline, 'coverage': coverage,
        # 本批未取得这些C0能力的验收证据；未来须接真实能力回执，不从质量或服务状态推断。
        'limitations': {'lossless_tail_verified': False, 'hard_limit_verified': False,
                        'revocation_verified': False},
    }
