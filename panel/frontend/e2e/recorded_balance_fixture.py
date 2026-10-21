"""CP15 专属账期夹具：真实差分入账，结束后仅清理本场景新增记录。"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

OLD = '2026-10-18T04:00:00+00:00'
NEW = '2026-10-21T04:00:00+00:00'
TAG = 'cp15-recorded-balance'


def control(run, phase):
    from django.conf import settings
    from django.core.serializers.json import DjangoJSONEncoder
    from django.db import transaction
    from portal.models import (Entitlement, BillingCycle, DeviceSubscription, NodeIdentity,
        UsageStream, UsageLedger, IngressUsageSample, ServiceRateVersion, CoreInstance, RateBucket, AuditEvent)
    from portal.ingress_metering import record_ingress_sample, set_service_rate

    run = Path(run).resolve()
    root = Path(__file__).resolve().parents[3]
    if (phase not in ('old', 'new', 'restore')
            or not run.is_relative_to(root / 'staging/browser-acceptance')
            or not (run / 'fixture-marker.json').is_file()
            or os.environ.get('PANEL_LIVE') != '0' or settings.PANEL_LIVE
            or settings.OPERATOR_ENABLED or settings.DATA_ROOT.resolve() != run / 'data'):
        raise ValueError('仅允许带标记的当前隔离假库')
    state_path, clock = run / 'cp15-balance-state.json', run / 'clock-state.json'
    ent = Entitlement.objects.get(user__username='fixture-unknown')
    fields = ('applied_snapshot', 'metering_gap', 'usage_updated_at', 'updated_at')

    def write_clock(value):
        temporary = run / 'cp15-clock.tmp'
        temporary.write_text(json.dumps({'now': value}), encoding='utf-8')
        temporary.replace(clock)

    if phase == 'restore':
        if not state_path.exists():
            return {'restored': True, 'already_clean': True}
        state = json.loads(state_path.read_text(encoding='utf-8'))
        assert state['service_id'] == str(ent.public_id) and state['tag'] == TAG
        with transaction.atomic():
            subscriptions = DeviceSubscription.objects.filter(entitlement=ent, name=TAG)
            identities = NodeIdentity.objects.filter(subscription__in=subscriptions)
            UsageLedger.objects.filter(stream__identity__in=identities).delete()
            IngressUsageSample.objects.filter(identity__in=identities).delete()
            UsageStream.objects.filter(identity__in=identities).delete()
            identities.delete()
            subscriptions.delete()
            ServiceRateVersion.objects.filter(entitlement=ent, reason=TAG).delete()
            AuditEvent.objects.filter(action='service_rate_set', subject=str(ent.public_id)).exclude(pk__in=state['rate_audits']).delete()
            BillingCycle.objects.filter(entitlement=ent).exclude(pk__in=state['cycles']).delete()
            values = state['fields']
            for key in ('usage_updated_at', 'updated_at'):
                values[key] = datetime.fromisoformat(values[key]) if values[key] else None
            Entitlement.objects.filter(pk=ent.pk).update(**values)
            ent.lines.set(state['lines'])
            # 回拨只移除本场景产生的未来限流窗口，原窗口不动。
            original_time = datetime.fromisoformat(json.loads(state['clock'])['now']) if state['clock'] else datetime.fromisoformat(OLD)
            RateBucket.objects.filter(started_at__gt=original_time).exclude(pk__in=state['rate_buckets']).delete()
        if state['clock'] is None:
            clock.unlink(missing_ok=True)
        else:
            clock.write_text(state['clock'], encoding='utf-8')
        ent.refresh_from_db()
        restored = all(getattr(ent, key) == values[key] for key in fields)
        assert restored and list(ent.cycles.order_by('pk').values_list('pk', flat=True)) == state['cycles']
        assert list(ent.lines.order_by('pk').values_list('pk', flat=True)) == state['lines']
        assert json.loads(json.dumps(list(ent.cycles.order_by('pk').values()), cls=DjangoJSONEncoder)) == state['cycle_rows']
        assert not DeviceSubscription.objects.filter(entitlement=ent, name=TAG).exists()
        state_path.unlink()
        return {'restored': True, 'new_records_removed': True, 'original_service_preserved': True}

    if phase == 'old':
        if state_path.exists():
            raise ValueError('前次余额夹具尚未恢复')
        assert not ent.subscriptions.exists() and not ent.grant_rates.exists()
        state = {'tag': TAG, 'service_id': str(ent.public_id),
            'fields': {key: getattr(ent, key) for key in fields},
            'cycles': list(ent.cycles.order_by('pk').values_list('pk', flat=True)),
            'cycle_rows': list(ent.cycles.order_by('pk').values()),
            'rate_audits': list(AuditEvent.objects.filter(action='service_rate_set', subject=str(ent.public_id)).values_list('pk', flat=True)),
            'lines': list(ent.lines.order_by('pk').values_list('pk', flat=True)),
            'rate_buckets': list(RateBucket.objects.values_list('pk', flat=True)),
            'clock': clock.read_text(encoding='utf-8') if clock.exists() else None}
        state_path.write_text(json.dumps(state, cls=DjangoJSONEncoder), encoding='utf-8')
        write_clock(OLD)
        now = datetime.fromisoformat(OLD)
        cycle = ent.cycles.get(starts_at__lte=now, ends_at__gt=now)
        ent.applied_snapshot = {**ent.applied_snapshot, 'accepted_display_estimate': {
            'version': 1, 'owner_id': ent.user_id, 'service_id': str(ent.public_id),
            'cycle_starts_at': cycle.starts_at.isoformat(), 'cycle_ends_at': cycle.ends_at.isoformat(),
            'accepted_at': now.isoformat(), 'source_observed_at': now.isoformat(),
            'source_sha256': 'a' * 64, 'approval_ref': TAG,
            'charged_bytes': '98253546067', 'upload_bytes': '40000000000', 'download_bytes': '58253546067',
            'ledger_baseline': {'charged_bytes': '0', 'upload_bytes': '0', 'download_bytes': '0'}}}
        ent.save(update_fields=['applied_snapshot'])
        return {'phase': phase, 'now': OLD, 'service_id': str(ent.public_id)}

    assert state_path.exists(), '必须先设置旧账期'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    assert state['tag'] == TAG and state['service_id'] == str(ent.public_id)
    assert not ent.subscriptions.exists(), '新账期夹具不可重复入账'
    write_clock(NEW)
    now = datetime.fromisoformat(NEW)
    with transaction.atomic():
        old_cycle = ent.cycles.get(pk=state['cycles'][0])
        BillingCycle.objects.create(entitlement=ent, starts_at=old_cycle.ends_at,
            ends_at=datetime(2026, 11, 19, 16, tzinfo=timezone.utc), raw_bytes=0)
        core = CoreInstance.objects.get(instance_key='fixture-core')
        line = core.ingresses.first().lines.order_by('pk').first()
        ent.lines.add(line)
        fixed = lambda name: uuid.uuid5(uuid.NAMESPACE_URL, TAG + '/' + name)
        sub = DeviceSubscription.objects.create(entitlement=ent, public_id=fixed('subscription'),
            name=TAG, client='windows', state='simulated')
        sub.lines.add(line)
        identity = NodeIdentity.objects.create(subscription=sub, public_id=fixed('identity'),
            line=line, ingress=line.ingress, generation=1, state='simulated')
        from django.contrib.auth import get_user_model
        set_service_rate(get_user_model().objects.get(username='fixture-admin'), ent.pk,
            line.pk, line.ingress_id, '1', old_cycle.ends_at, TAG)
        for sequence, upload, download in [(1, 0, 0), (2, 1_240_000_000, 4_450_000_000)]:
            end = now - timedelta(seconds=120 - sequence * 10)
            start = end - timedelta(milliseconds=4)
            result = record_ingress_sample(identity_id=identity.pk, server_id=core.server_id,
                core_instance_id=core.pk, epoch=TAG, sequence=sequence, upload_bytes=upload,
                download_bytes=download, observed_at=end, coverage_verified=False,
                counter_kind='cumulative', observation_verified=True, source_config_fingerprint='a' * 64,
                read_windows={'read_started_at': start, 'read_ended_at': end,
                    'upload_read_started_at': start, 'upload_read_ended_at': start + timedelta(milliseconds=1),
                    'download_read_started_at': start + timedelta(milliseconds=2), 'download_read_ended_at': end})
            assert result.status == ('baseline' if sequence == 1 else 'accepted'), result.status
        ledger = UsageLedger.objects.get(stream__identity=identity)
        assert ledger.quality == 'partial_observed' and ledger.weighted_bytes == 5_690_000_000
    return {'phase': phase, 'now': NEW, 'service_id': str(ent.public_id), 'ledger_quality': ledger.quality}
