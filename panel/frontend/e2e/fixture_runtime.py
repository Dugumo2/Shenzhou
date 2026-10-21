"""浏览器验收专用：全新假库、固定时间与正式静态资源读取层。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 18, 4, 0, tzinfo=dt_timezone.utc)


def configure(run):
    run = Path(run).resolve()
    if not run.is_relative_to(ROOT / 'staging/browser-acceptance') or not (run / 'fixture-marker.json').is_file():
        raise ValueError('仅允许浏览器验收的新隔离目录')
    for key in list(os.environ):
        if key.startswith('PANEL_') or key == 'DJANGO_SETTINGS_MODULE':
            os.environ.pop(key)
    os.environ.update(PANEL_DATA_ROOT=str(run / 'data'), PANEL_LIVE='0', PANEL_OPERATOR_ENABLED='0',
        PANEL_FRONTEND_ENABLED='1', PANEL_WORKSPACE_V2='1', PANEL_CANDIDATE_DEMO_DATA='1',
        PANEL_HOSTS='127.0.0.1,localhost,testserver', DJANGO_SETTINGS_MODULE='megabox.settings',
        PANEL_FRONTEND_RELEASE_ROOT=str(run / 'release'),
        PANEL_FRONTEND_MANIFEST_SHA256=hashlib.sha256((run / 'release/frontend-manifest.json').read_bytes()).hexdigest(),
        PANEL_RESOURCE_CHECKS_POLICY=str(run / 'check-policy.json'))
    sys.path.insert(0, str(run / 'candidate'))
    import django
    django.setup()
    from django.conf import settings
    assert not settings.PANEL_LIVE and not settings.OPERATOR_ENABLED
    assert settings.DATA_ROOT.resolve() == run / 'data'
    # 仅隔离验收可以推进自然日/账期；其余场景保持原固定时间。
    def fixture_now():
        clock = run / 'clock-state.json'
        return datetime.fromisoformat(json.loads(clock.read_text())['now']) if clock.exists() else NOW
    patch('django.utils.timezone.now', side_effect=fixture_now).start()
    from portal.models import ResourceCheckJob
    import uuid
    ResourceCheckJob._meta.get_field('public_id').default=lambda: uuid.uuid5(uuid.NAMESPACE_URL,
        'browser-fixture/check-job/'+str(ResourceCheckJob.objects.count()+1))
    from portal.p8_compat import RESOURCE_FORMATS
    settings.P8_COMPAT_ENABLED=True
    settings.P8_COMPAT_SOURCES={('fixture-p8','fixture-service'):{
        'source_instance':'fixture-p8','source_id':'fixture-service','links_path':str(run/'fake-links.json'),
        'origin':'https://fixture.example.invalid:2053','evidence_sha256':'e'*64,
        'resources':{key:{'format':value,'verified':True} for key,value in RESOURCE_FORMATS.items()}}}
    return run


def seed(run):
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from portal.models import (Server, CoreInstance, Ingress, Egress, Line, Entitlement,
        BillingCycle, DeviceSubscription, NodeIdentity, ClientDirectRule, ServerObservation)
    from portal.ingress_metering import set_service_rate, record_ingress_sample
    from portal.observation_ingest import target_fingerprint
    assert not (run / 'data/panel.sqlite3').exists(), '拒绝覆盖任何已有数据库'
    call_command('migrate', verbosity=0, interactive=False)
    import uuid
    fixed_id = lambda name: uuid.uuid5(uuid.NAMESPACE_URL, 'browser-fixture/' + name)
    users = get_user_model()
    # 公开的纯本地假凭据；无真实登录材料。
    admin = users.objects.create_user('fixture-admin', password='Local-fixture-only-2026', is_staff=True)
    ordinary = users.objects.create_user('fixture-user', password='Local-fixture-only-2026')
    p8_user = users.objects.create_user('fixture-p8', password='Local-fixture-only-2026')
    users.objects.create_user('fixture-empty', password='Local-fixture-only-2026')
    unknown_user=users.objects.create_user('fixture-unknown',password='Local-fixture-only-2026')
    server = Server.objects.create(public_id=fixed_id('server'), name='验收服务器', last_seen_at=NOW-timedelta(seconds=1))
    Server.objects.create(public_id=fixed_id('unobserved-server'),name='未采样服务器')
    core = CoreInstance.objects.create(public_id=fixed_id('core'), server=server, instance_key='fixture-core',
        name='验收 sing-box 核心', core_type='sing_box', registered_version='1.12.0',
        configuration_owner='隔离验收', owner_state='observed_only', evidence_source='synthetic-fixture',evidence_observed_at=NOW)
    external = Egress.objects.create(server=server, core_instance=core, name='HOME 外部节点', kind='upstream', ownership='external',
        external_kind='node', protocol='ss2022', endpoint_host='home.example.invalid', endpoint_port=443,
        fail_closed=True, evidence_source='synthetic-fixture', evidence_observed_at=NOW)
    hop = Egress.objects.create(server=server, core_instance=core, next_egress=external, name='本机 SOCKS 中间节点',
        kind='upstream', ownership='self_owned', protocol='socks', endpoint_host='127.0.0.1', endpoint_port=18080,
        evidence_source='synthetic-fixture', evidence_observed_at=NOW)
    direct = Egress.objects.create(server=server, core_instance=core, name='BWH 出站', kind='direct', ownership='self_owned')
    lines = []
    for index, (name, egress) in enumerate([('HOME 线路', hop), ('BWH 线路', direct)]):
        ingress = Ingress.objects.create(server=server, name=name+'入口', protocol='reality', endpoint_host='entry.example.invalid',
            endpoint_port=443+index, evidence_source='synthetic-fixture', evidence_observed_at=NOW)
        core.ingresses.add(ingress)
        lines.append(Line.objects.create(public_id=fixed_id('line'+str(index)), name=name, ingress=ingress, egress=egress))
    start = datetime(2026, 9, 19, 16, tzinfo=dt_timezone.utc)
    end = datetime(2026, 10, 19, 16, tzinfo=dt_timezone.utc)
    service_ids = {}
    for actor in [admin, ordinary, p8_user]:
        ent = Entitlement.objects.create(user=actor, public_id=fixed_id(actor.username), quota_bytes=1_000_000_000_000,
            service_name='验收 1TB 套餐', activated_at=start, expires_at=datetime(2027,12,30,2,49,tzinfo=dt_timezone.utc),
            reset_day=20, reset_hour=0, reset_minute=0, revision=1, applied_revision=1,
            applied_snapshot={'quota_bytes':1_000_000_000_000}, state='simulated')
        BillingCycle.objects.create(entitlement=ent, starts_at=start, ends_at=end, raw_bytes=0)
        ent.lines.set(lines)
        subscription = DeviceSubscription.objects.create(entitlement=ent, name='验收交付', client='windows', state='simulated')
        subscription.lines.set(lines)
        for index, line in enumerate(lines):
            identity = NodeIdentity.objects.create(subscription=subscription, line=line, ingress=line.ingress, generation=1, state='simulated')
            set_service_rate(admin, ent.pk, line.pk, line.ingress_id, '1', start, '固定假样本')
            for sequence in range(30):
                instant = NOW if sequence == 29 else start + timedelta(days=sequence)
                units = 0 if actor==ordinary else sequence - (1 if sequence >= 15 else 0)
                result = record_ingress_sample(identity_id=identity.pk, server_id=server.pk, core_instance_id=core.pk,
                    epoch='fixture-'+str(index), sequence=sequence, upload_bytes=units*(index+1)*100_000_000,
                    download_bytes=units*(index+1)*200_000_000, observed_at=instant,
                    coverage_verified=True, counter_kind='cumulative')
                assert result.status in ('baseline','accepted'), result.status
        service_ids[actor.username] = str(ent.public_id)
        if actor == p8_user:
            from portal.models import P8SourceBinding
            from portal.p8_compat import RESOURCE_FORMATS, binding_verification_sha256
            from portal.p8_entitlement import verify_p8_entitlement
            links={key:'https://fixture.example.invalid:2053/s/'+('w' if key.startswith('windows') else 'f')*43+'/'+key for key in RESOURCE_FORMATS}
            link_path=run/'fake-links.json'
            link_path.write_text(json.dumps(links),encoding='utf-8')
            if os.name=='posix': link_path.chmod(0o600)
            source=P8SourceBinding.objects.create(public_id=ent.public_id,source_instance='fixture-p8',source_id='fixture-service',
                owner=actor,verified_by=admin,enabled=True,state='verified',verified_at=NOW,
                evidence_sha256='e'*64,links_sha256=hashlib.sha256(link_path.read_bytes()).hexdigest())
            source.verification_sha256=binding_verification_sha256(source)
            source.save(update_fields=['verification_sha256'])
            verify_p8_entitlement(admin,source.public_id,ent.pk,evidence_sha256='e'*64)
    unknown=Entitlement.objects.create(user=unknown_user,public_id=fixed_id('fixture-unknown'),quota_bytes=10**12,
        activated_at=start,expires_at=datetime(2027,12,30,2,49,tzinfo=dt_timezone.utc),revision=1,applied_revision=1,
        applied_snapshot={'quota_bytes':10**12},state='simulated',reset_day=20,reset_hour=0,reset_minute=0)
    BillingCycle.objects.create(entitlement=unknown,starts_at=start,ends_at=end)
    service_ids[unknown_user.username]=str(unknown.public_id)
    custom_rule=ClientDirectRule.objects.create(creator=admin, kind='suffix', value='example.com', outbound='proxy')
    from portal.models import RuleSource, RuleSourceVersion
    from portal.rule_policy_models import RulePolicy, RulePolicyVersion
    from portal.rule_sources import normalize_document, digest
    from portal.rule_policies import build_document
    source=RuleSource.objects.create(public_id=fixed_id('rule-source'),name='验收基础来源',revision=2,creator=admin)
    # 当前来源已到v2，方案固定引用v1，用页面反例验证版本不会静默漂移。
    source_versions={}
    for revision,value in [(1,'work.example.net'),(2,'updated.example.net')]:
        document=normalize_document({'schema_version':1,'rules':[{'action':'proxy','kind':'suffix','value':value,'enabled':True}]})
        version=RuleSourceVersion.objects.create(source=source,revision=revision,document=document,sha256=digest(document),count=1,creator=admin)
        source_versions[str(revision)]=version.sha256
    policy=RulePolicy.objects.create(public_id=fixed_id('rule-policy'),name='验收固定方案',creator=admin)
    policy_document=build_document({'components':[
        {'type':'source','source_id':str(source.public_id),'revision':1,'enabled':True},
        {'type':'custom','rule_id':custom_rule.pk,'revision':custom_rule.revision,'enabled':True}], 'overrides':{}})
    policy_version=RulePolicyVersion.objects.create(policy=policy,revision=1,document=policy_document,sha256=digest(policy_document),creator=admin)
    rule_fixture={'custom_id':custom_rule.pk,'source_id':str(source.public_id),'source_versions':source_versions,
        'policy_id':str(policy.public_id),'policy_sha256':policy_version.sha256}
    for index in range(3):
        observed = NOW - timedelta(seconds=121-index*60)
        ServerObservation.objects.create(server=server, source='fixture-host', event_id='fixed-'+str(index),
            observed_at=observed, expires_at=observed+timedelta(seconds=180), target_fingerprint=target_fingerprint('server',server),
            metrics={'online':True,'cpu_percent':21.5,'cpu_window_seconds':60,'memory_percent':42.5,'disk_percent':31.2,
                'network_rx_bytes':1_000_000_000+index*60*123_000,'network_tx_bytes':2_000_000_000+index*60*456_000,
                'network_interfaces':['fixture0'],'boot_id':'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
                'sample_monotonic_seconds':1000+index*60})
    entry = {'target_kind':'ingress','target_id':str(lines[0].ingress_id),
        'target_version':target_fingerprint('ingress',lines[0].ingress),'action':'tcp',
        'ip':'127.0.0.1','port':9,'allow_loopback':True}
    from portal.models import CheckResult
    CheckResult.objects.create(ingress=lines[0].ingress,source='fixture-history',event_id='expired-tcp',
        check_kind='tcp',observed_at=NOW-timedelta(minutes=10),expires_at=NOW-timedelta(minutes=5),
        target_fingerprint=entry['target_version'],result='pass',latency_ms=8)
    (run / 'check-policy.json').write_text(json.dumps({'version':1,'actions':[entry]}),encoding='utf-8')
    metadata = {'fixed_time':NOW.isoformat(),'releases':json.loads((run/'releases.json').read_text()),'expected':{'upload_gb':'8.4 GB','download_gb':'16.8 GB','charged_gb':'25.2 GB','remaining_gb':'974.8 GB'},'service_ids':service_ids,'server_id':str(server.public_id),
        'line_ids':[str(line.public_id) for line in lines],'rules':rule_fixture}
    (run / 'fixture.json').write_text(json.dumps(metadata),encoding='utf-8')
    return metadata


def serve(run, port):
    from django.core.wsgi import get_wsgi_application
    from django.urls import path, include, clear_url_caches
    from portal.frontend_release import release_asset, release_version
    import megabox.urls
    # 只替换夹具进程中的 URL 表；实际运行正式 release_asset、登录/API 与编译产物。
    megabox.urls.urlpatterns = [path('app/',release_asset),path('app/release-version.json',release_version),
        path('app/<path:asset>',release_asset),path('api/v1/',include('portal.api_urls')),
        path('api/v1/',include('portal.billing_urls')),path('',include('portal.urls'))]
    clear_url_caches()
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server
    class ThreadedServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True
    class QuietHandler(WSGIRequestHandler):
        def log_message(self, format, *args):
            pass
    application=get_wsgi_application()
    from django.conf import settings
    releases=json.loads((run/'releases.json').read_text())
    def switched_application(environ,start_response):
        active=json.loads((run/'release-state.json').read_text())['active']
        current=releases[active]
        settings.FRONTEND_RELEASE_ROOT=current['root']
        settings.FRONTEND_MANIFEST_SHA256=current['manifest_sha256']
        settings.FRONTEND_COMPAT_RELEASES=[{'root':releases['old']['root'],'manifest_sha256':releases['old']['manifest_sha256'],'expires_at':int(__import__('time').time())+3600}] if active=='candidate' else []
        return application(environ,start_response)
    with make_server('127.0.0.1',port,switched_application,ThreadedServer,QuietHandler) as server:
        server.serve_forever()


def control(run, operation, job_id):
    """只修改当前验收假库；不运行探测器，不建立网络连接。"""
    from django.contrib.auth import get_user_model
    from portal.models import ResourceCheckJob, CheckResult, ServerObservation, RateBucket
    if operation == 'balance-profile':
        from recorded_balance_fixture import control as balance_control
        return balance_control(run, job_id)
    if operation == 'usage-rate-reset':
        # 新场景开始前隔离假账号/IP计数；保留真实产品阈值和限流函数，不在失败后清理重试。
        from django.utils.crypto import salted_hmac
        identities = [('login-address', '127.0.0.1')] + [
            ('login-account', name) for name in
            ('fixture-admin', 'fixture-user', 'fixture-p8', 'fixture-empty', 'fixture-unknown')]
        keys = [salted_hmac('rate-bucket', scope + ':' + name).hexdigest() for scope, name in identities]
        removed, _ = RateBucket.objects.filter(key__in=keys).delete()
        return {'isolated_fixture_rate_buckets': removed}
    if operation == 'usage-service-state':
        from portal.models import Entitlement
        if job_id not in ('applied', 'waiting', 'disabled', 'expired', 'restore'):
            raise ValueError('仅允许用量业务状态预定义夹具')
        changes = {'state': 'simulated' if job_id == 'restore' else 'active',
            'revision': 2 if job_id == 'waiting' else 1, 'applied_revision': 1,
            'enabled': job_id != 'disabled',
            'expires_at': NOW - timedelta(seconds=1) if job_id == 'expired' else datetime(2027, 12, 30, 2, 49, tzinfo=dt_timezone.utc)}
        count = Entitlement.objects.filter(user__username='fixture-admin').update(**changes)
        assert count == 1
        return {'state': job_id, 'modified_service_count': count}
    if operation == 'usage-clock':
        allowed = {
            NOW.isoformat(),
            '2026-10-18T15:59:59+00:00', '2026-10-18T16:00:01+00:00',
            '2026-10-19T15:59:59+00:00', '2026-10-19T16:00:01+00:00',
            '2026-10-18T16:00:00.010+00:00', '2026-10-19T16:00:00.010+00:00',
            '2026-10-31T15:59:59+00:00', '2026-10-31T16:00:01+00:00',
            '2026-12-31T15:59:59+00:00', '2026-12-31T16:00:01+00:00',
        }
        if job_id not in allowed:
            raise ValueError('仅允许用量边界验收预定义时间')
        clock = run / 'clock-state.json'
        previous = datetime.fromisoformat(json.loads(clock.read_text())['now']) if clock.exists() else NOW
        target = datetime.fromisoformat(job_id)
        if target < previous:
            # 回拨验收时钟时只清未来计数，避免其他场景承接一个永远不会自然到期的窗口。
            RateBucket.objects.filter(started_at__gt=target).delete()
        temporary = run / 'clock-state.tmp'
        temporary.write_text(json.dumps({'now': job_id}), encoding='utf-8')
        temporary.replace(run / 'clock-state.json')
        return {'now': job_id}
    if operation in ('release-old','release-candidate','release-recovery-old'):
        active={'release-old':'old','release-candidate':'candidate','release-recovery-old':'recovery_old'}[operation]
        temporary=run/'release-state.tmp'
        temporary.write_text(json.dumps({'active':active}),encoding='utf-8')
        temporary.replace(run/'release-state.json')
        return json.loads((run/'releases.json').read_text())[active]
    if operation in ('revoke_admin','restore_admin'):
        get_user_model().objects.filter(username='fixture-admin').update(is_staff=operation=='restore_admin')
        return {'status':'OK'}
    if operation=='complete_job':
        job=ResourceCheckJob.objects.get(public_id=job_id)
        assert job.actor.username=='fixture-admin' and job.target_kind=='ingress'
        result=CheckResult.objects.create(ingress_id=int(job.target_id),source='synthetic-fixture',event_id=str(job.public_id),
            check_kind='tcp',observed_at=NOW,expires_at=NOW+timedelta(seconds=180),target_fingerprint=job.target_version,
            result='pass',latency_ms=3.5)
        job.state='completed';job.result=result;job.started_at=NOW;job.finished_at=NOW
        job.save(update_fields=['state','result','started_at','finished_at'])
    if operation=='reset_checks':
        ResourceCheckJob.objects.all().delete()
        CheckResult.objects.filter(source='synthetic-fixture').delete()
    if operation in ('observations_stale','observations_restore'):
        latest=ServerObservation.objects.order_by('-observed_at').first()
        if operation=='observations_stale':
            ServerObservation.objects.all().update(expires_at=NOW-timedelta(microseconds=500000))
        else:
            for row in ServerObservation.objects.all():
                row.expires_at=row.observed_at+timedelta(seconds=180);row.save(update_fields=['expires_at'])
    return {'jobs':list(ResourceCheckJob.objects.values('public_id','idempotency_key','state','target_id'))}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['seed','serve','control'])
    parser.add_argument('--run',required=True)
    parser.add_argument('--port',type=int,default=18871)
    parser.add_argument('--operation',choices=['inspect_jobs','complete_job','revoke_admin','restore_admin','reset_checks','observations_stale','observations_restore','release-old','release-candidate','release-recovery-old','usage-clock','usage-service-state','usage-rate-reset','balance-profile'])
    parser.add_argument('--job-id')
    args=parser.parse_args()
    run=configure(args.run)
    if args.action=='seed':
        seed(run)
    elif args.action=='serve':
        serve(run,args.port)
    else:
        print(json.dumps(control(run,args.operation,args.job_id),default=str))
