"""按当前账期和未撤销入口判断计量证据质量。

这个模块只读数据库中的已持久样本，不会创建账期、推进计划或修改权益。
"""
from datetime import timedelta

from django.db.models import OuterRef, Subquery
from django.utils import timezone

from .models import NodeIdentity, UsageLedger, UsageStream


FRESH_WINDOW = timedelta(minutes=3)
FUTURE_SKEW = timedelta(seconds=30)


def assess_metering(record, cycle, now=None):
    """返回服务当前账期的统一计量质量判断。

    显示一份服务剩余的前提，是每个未撤销入口都在当前账期留下计量样本，且
    服务级更新时间和各入口样本都没有超前。撤销入口不再
    要求新样本；计划中的 candidate 仍属于未撤销入口，不能用来证明已计量。
    本判断不证明核心崩溃尾部或采集器投递已经完整持久化。
    """
    now = now or timezone.now()
    observed = record.usage_updated_at
    active_ids = set(NodeIdentity.objects.filter(
        subscription__entitlement=record, revoked_at__isnull=True
    ).values_list('pk', flat=True))
    # 每个代际只取持久账本中的最后序列，使用已有(stream, sequence)索引，
    # 不能把整月的累计样本全部读入内存，也不能用较旧的好样本遮盖最新坏样本。
    last_sample = UsageLedger.objects.filter(stream_id=OuterRef('pk')).order_by('-sequence')
    streams = UsageStream.objects.filter(identity_id__in=active_ids, retired_at__isnull=True).annotate(
        sample_cycle_id=Subquery(last_sample.values('cycle_id')[:1]),
        sample_quality=Subquery(last_sample.values('quality')[:1]),
        sample_observed_at=Subquery(last_sample.values('observed_at')[:1]),
    ).values('identity_id', 'sample_cycle_id', 'sample_quality', 'sample_observed_at')
    current_streams = {}
    for stream in streams:
        current_streams.setdefault(stream['identity_id'], []).append(stream)
    ambiguous_ids = {identity_id for identity_id, streams in current_streams.items() if len(streams) != 1}
    latest_by_identity = {}
    if cycle:
        for identity_id, stream_rows in current_streams.items():
            if len(stream_rows) != 1:
                continue
            sample = stream_rows[0]
            observed_at = sample['sample_observed_at']
            if (sample['sample_cycle_id'] == cycle.pk and sample['sample_quality'] == 'metered'
                    and observed_at and cycle.starts_at <= observed_at < cycle.ends_at):
                latest_by_identity[identity_id] = observed_at

    evidence_ids = set(latest_by_identity)
    missing_ids = active_ids - evidence_ids
    any_evidence = bool(evidence_ids)
    service_in_cycle = bool(cycle and observed and cycle.starts_at <= observed < cycle.ends_at)
    service_future = bool(observed and observed > now + FUTURE_SKEW)
    service_fresh = bool(observed and now - FRESH_WINDOW <= observed <= now + FUTURE_SKEW)
    complete = bool(active_ids) and not missing_ids and any_evidence

    if cycle is None and record.cycles.filter(starts_at__lte=now, ends_at__gt=now).count() > 1:
        quality = 'gap'
    elif ambiguous_ids:
        quality = 'gap'
    elif not cycle or not active_ids or not any_evidence or service_future or not service_in_cycle:
        quality = 'unknown'
    elif missing_ids:
        # 至少一个入口有当前期证据、另一个未撤销入口没有时，不能把
        # 部分账本投影成完整剩余；这是读投影中的缺口，不在只读查询中落库。
        quality = 'gap'
    elif any(observed_at > now + FUTURE_SKEW for observed_at in latest_by_identity.values()):
        quality = 'unknown'
    elif service_fresh and all(now - FRESH_WINDOW <= observed_at <= now + FUTURE_SKEW
                               for observed_at in latest_by_identity.values()):
        quality = 'fresh'
    else:
        quality = 'stale'

    evidence_quality = quality
    if record.metering_gap:
        quality = 'gap'
    usable = quality in ('fresh', 'stale') and complete
    return {
        'quality': quality,
        'evidence_quality': evidence_quality,
        'usable': usable,
        'active_identity_ids': active_ids,
        'evidence_identity_ids': evidence_ids,
        'missing_identity_ids': missing_ids,
        'ambiguous_identity_ids': ambiguous_ids,
        'latest_by_identity': latest_by_identity,
    }
