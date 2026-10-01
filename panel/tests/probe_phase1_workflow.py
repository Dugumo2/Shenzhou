"""内存 Django 数据库→事务任务→真实回环核心→核验回执。

不读取生产数据库；隔离结果始终 simulated，且真实下载入口必须拒绝。
"""
import argparse
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import time


def run(core):
    if os.environ.get('PANEL_LIVE') == '1' or os.environ.get('PANEL_OPERATOR_ENABLED') == '1':
        raise ValueError('production_environment_not_allowed')
    project = Path(__file__).resolve().parents[2]
    os.environ['PANEL_DATA_ROOT'] = str(project / 'staging' / 'phase1-core')
    os.environ['DJANGO_SETTINGS_MODULE'] = 'megabox.settings'
    sys.path.insert(0, str(project / 'panel'))
    import django
    from django.conf import settings
    settings.DATABASES['default']['NAME'] = ':memory:'
    django.setup()
    from django.core.management import call_command
    from django.contrib.auth import get_user_model
    from django.core.exceptions import ValidationError
    from django.utils import timezone
    from portal.models import Server, Ingress, Egress, Line
    from portal.entitlements import assign_entitlement, record_usage, current_cycle
    from portal.billing import set_line_rate
    from portal.delivery import create_subscription, reset_subscription, disable_subscription, token_for_subscription
    from portal.jobs import run_job
    from bridge.loopback_adapter import LoopbackAdapter, _port
    import grpc
    from google.protobuf.empty_pb2 import Empty
    from bridge.proto import started_service_pb2 as pb
    from bridge.proto import started_service_pb2_grpc as rpc

    class MeteredFixtureAdapter(LoopbackAdapter):
        """仅给本脚本自有回环核心启用临时认证 API，不涉及生产配置。"""
        def __init__(self, binary):
            self.api_port, self.api_secret = _port(), secrets.token_urlsafe(32)
            super().__init__(binary)

        def _server_config(self, ids):
            config = super()._server_config(ids)
            config['services'] = [{'type': 'api', 'listen': '127.0.0.1', 'listen_port': self.api_port,
                                   'secret': self.api_secret, 'dashboard': False}]
            return config

        def measured_totals(self):
            # 本轮短连接即时快照只证明测试窗口，不冒充持久可靠计费采集器。
            with grpc.insecure_channel(f'127.0.0.1:{self.api_port}') as channel:
                grpc.channel_ready_future(channel).result(timeout=8)
                stub = rpc.StartedServiceStub(channel)
                metadata = [('authorization', 'Bearer ' + self.api_secret)]
                started = stub.GetStartedAt(Empty(), metadata=metadata, timeout=3).startedAt
                stream = stub.SubscribeConnections(pb.SubscribeConnectionsRequest(interval=100_000_000),
                                                   metadata=metadata, timeout=3)
                batch = next(stream)
                stream.cancel()
                totals = {}
                seen = set()
                for event in batch.events:
                    if event.HasField('connection') and event.id not in seen:
                        seen.add(event.id)
                        item = event.connection
                        if item.user.startswith('fixture-'):
                            pair = totals.setdefault(int(item.user.removeprefix('fixture-')), [0, 0])
                            pair[0] += item.uplinkTotal
                            pair[1] += item.downlinkTotal
                return hashlib.sha256(str(started).encode()).hexdigest(), totals
    call_command('migrate', verbosity=0)
    users = get_user_model()
    admin = users.objects.create_user('fixture-admin', is_staff=True)
    user = users.objects.create_user('fixture-user')
    server = Server.objects.create(name='纯回环测试机')
    ingress = Ingress.objects.create(server=server, name='纯回环入口', protocol='ws')
    line = Line.objects.create(name='纯回环出口', ingress=ingress,
                              egress=Egress.objects.create(server=server, name='echo', kind='direct'))
    second_line = Line.objects.create(name='第二测试计费线路', ingress=ingress, egress=line.egress)
    # 这些数值仅属于内存夹具，真实线路倍率绝不猜测为 1x。
    effective = timezone.now() - timedelta(seconds=1)
    rate1 = set_line_rate(admin, line.pk, '1.25', effective, '仅回环夹具显式测试倍率')
    rate2 = set_line_rate(admin, second_line.pk, '2', effective, '仅回环夹具显式测试倍率')
    checks = {}
    observations = {}
    with MeteredFixtureAdapter(core) as adapter:
        record, job = assign_entitlement(admin, user, [line.pk, second_line.pk], 1_000_000_000, idempotency_key='fixture-assign')
        done = run_job(job.public_id, adapter)
        checks['assignment_db_to_real_core_receipt'] = done.state == 'simulated'
        checks['explicit_fixture_rates_no_production_default'] = str(rate1.multiplier) == '1.25' and str(rate2.multiplier) == '2'
        phone, job = create_subscription(user, '测试手机', 'android', [line.pk], 'fixture-phone')
        done = run_job(job.public_id, adapter)
        phone_id = phone.identities.get().pk
        checks['create_first_identity_real_core'] = done.state == 'simulated' and adapter._probe([phone_id])[phone_id]
        desktop, job = create_subscription(user, '测试电脑', 'windows', [second_line.pk], 'fixture-desktop')
        done = run_job(job.public_id, adapter)
        desktop_id = desktop.identities.get().pk
        checks['create_second_identity_real_core'] = done.state == 'simulated' and adapter._probe([desktop_id])[desktop_id]
        adapter._probe([phone_id, desktop_id])
        time.sleep(.25)
        epoch, totals = adapter.measured_totals()
        observed = timezone.now()
        raw = sum(sum(totals.get(identity_id, [0, 0])) for identity_id in (phone_id, desktop_id))
        checks['real_core_has_both_identity_counters'] = all(sum(totals.get(i, [0, 0])) > 0 for i in (phone_id, desktop_id))
        for identifier in (phone_id, desktop_id):
            up, down = totals[identifier]
            record_usage(identifier, server.pk, epoch, 1, up, down, observed)
        record.refresh_from_db()
        cycle = current_cycle(record)
        expected_weighted = (sum(totals[phone_id]) * 5) // 4 + sum(totals[desktop_id]) * 2
        checks['real_counters_weighted_once_in_shared_service'] = cycle.raw_bytes == raw and cycle.used_bytes == expected_weighted
        checks['ledger_keeps_line_rate_versions'] = set(cycle.ledger.values_list('rate_version_id', flat=True)) == {rate1.pk, rate2.pk}
        before = cycle.used_bytes
        for identifier in (phone_id, desktop_id):
            record_usage(identifier, server.pk, epoch, 1, *totals[identifier], observed)
        cycle.refresh_from_db()
        checks['real_sample_replay_not_double_charged'] = cycle.used_bytes == before
        observations.update(raw_bytes=raw, weighted_bytes=cycle.used_bytes, test_line_multipliers=['1.25', '2'],
                            scope='只统计当前核心代际即时可见连接，不保证断采集无损恢复')
        phone, job = reset_subscription(user, phone.public_id, 'fixture-reset')
        done = run_job(job.public_id, adapter)
        next_phone = phone.identities.get(generation=phone.generation).pk
        checks['reset_db_generation_matches_real_core'] = done.state == 'simulated' and adapter._probe([phone_id, next_phone, desktop_id]) == {
            phone_id: False, next_phone: True, desktop_id: True}
        desktop, job = disable_subscription(user, desktop.public_id, 'fixture-disable')
        done = run_job(job.public_id, adapter)
        checks['disable_db_matches_real_core'] = done.state == 'simulated' and adapter._probe([desktop_id, next_phone]) == {desktop_id: False, next_phone: True}
        try:
            token_for_subscription(user, phone.public_id)
            checks['isolated_cannot_issue_production_download'] = False
        except ValidationError:
            checks['isolated_cannot_issue_production_download'] = True
        # 下调本内存服务额度后，以新核心代际的真实观察值触发停用；不创建真实用户套餐。
        record, job = assign_entitlement(admin, user, [line.pk, second_line.pk], before + 1, idempotency_key='fixture-low-quota')
        done = run_job(job.public_id, adapter)
        checks['lower_fixture_quota_applied_above_existing_usage'] = done.state == 'simulated'
        time.sleep(.25)
        epoch, totals = adapter.measured_totals()
        up, down = totals[next_phone]
        record_usage(next_phone, server.pk, epoch, 1, up, down, timezone.now())
        enforcement = record.jobs.filter(kind='enforce', state='queued').order_by('-pk').first()
        checks['real_metered_overquota_queues_enforcement'] = enforcement is not None
        if enforcement:
            enforced = run_job(enforcement.public_id, adapter)
            checks['overquota_real_core_denies_existing_identity'] = enforced.state == 'simulated' and not adapter._probe([next_phone])[next_phone]
        else:
            checks['overquota_real_core_denies_existing_identity'] = False
        return {'result': 'PASS' if all(checks.values()) else 'FAIL', 'checks': checks,
                'scope': 'isolated-memory-db-real-loopback-core', 'core_sha256': adapter.core_sha256,
                'production_changed': False, 'impact': adapter.impact, 'observations': observations,
                'production_persistent_metering': 'NOT VERIFIED', 'distinct_real_egresses': 'NOT TESTED'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--core', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    result = run(args.core)
    args.report.write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result))
    raise SystemExit(0 if result['result'] == 'PASS' else 1)
