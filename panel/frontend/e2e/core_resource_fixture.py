"""M1 专属隔离夹具：真实 Django/状态服务，核心二进制边界为明确的假执行器。"""
import argparse
import hashlib
import io
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import fixture_runtime as base


class IsolatedCoreExecutor:
    """只处理本文件创建的假核心，不读取配置文件或启动任何进程。"""

    def __init__(self, run, outcome='succeeded'):
        self.run, self.outcome = run, outcome

    def binding_status(self, core):
        return {'available': core.instance_key == 'm1-browser-core'}

    def prepare_candidate(self, core, changes):
        assert core.instance_key == 'm1-browser-core'
        return {'base_sha256': 'a' * 64, 'candidate_sha256': 'b' * 64,
                'binding_fingerprint': 'c' * 64, 'artifact_ref': 'd' * 32,
                'previous_level': 'info'}

    def _receipt(self, job):
        assert job.core.instance_key == 'm1-browser-core'
        return {'status': self.outcome, 'job_id': str(job.public_id),
                'core_id': str(job.core.public_id), 'action': job.action, 'fence': job.fence,
                'candidate_sha256': job.candidate.candidate_sha256,
                'binding_fingerprint': job.candidate.binding_fingerprint,
                'config_sha256': job.candidate.base_sha256 if job.action == 'rollback' else job.candidate.candidate_sha256,
                'error_code': 'fixture_check_rejected' if self.outcome == 'failed' else ''}

    def _record(self, job, operation):
        with (self.run / 'm1-executor-events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'job_id': str(job.public_id), 'action': job.action,
                                    'operation': operation, 'outcome': self.outcome}) + '\n')

    def execute_job(self, job):
        self._record(job, 'execute')
        return self._receipt(job)

    def reconcile_job(self, job):
        self._record(job, 'read_receipt')
        return self._receipt(job)


class PurchaseFixtureSettings:
    """按本次采购标记选择来源；原设置对象不变，请求之间不切换全局变量。"""

    def __init__(self, run, original):
        self.root, self.original = run / 'purchase-projection-browser', original

    def __getattr__(self, name):
        marker = self.root / '.isolated-purchase-test'
        if name in ('PROVIDER_USAGE_SOURCE', 'RESOURCE_PLAN_AUTHORITY_TEST_PATH') and marker.is_file():
            assert json.loads(marker.read_text(encoding='utf-8')) == {'scope': 'M1 browser fixture'}
            assert (self.root / 'usage.json').is_file(), '采购来源未完整准备'
            if name == 'RESOURCE_PLAN_AUTHORITY_TEST_PATH':
                return str(self.root / 'plan-authority.json')
            return {'path': str(self.root / 'usage.json'),
                    'source_instance': 'fixture-p8', 'source_id': 'fixture-service'}
        return getattr(self.original, name)


def configure(path):
    run = base.configure(path)
    from django.conf import settings
    from portal import core_control, service_usage_source, resource_plan_source, resource_usage_view
    from django.utils import timezone
    # 继承原夹具的目录、marker、PANEL_LIVE=0 与候选包来源断言。
    settings.CORE_CONTROL_WRITE_ENABLED = True
    settings.INVENTORY_METADATA_WRITE_ENABLED = True
    settings.RESOURCE_PLAN_WRITE_ENABLED = True
    settings.RESOURCE_PLAN_PROJECTION_TEST_ROOT = str(run / 'purchase-projection-browser')
    scoped = PurchaseFixtureSettings(run, settings)
    patch.object(service_usage_source, 'settings', scoped).start()
    patch.object(resource_plan_source, 'settings', scoped).start()
    original_reader = resource_usage_view.read_resource_usage_view

    def read_fixture_view(path=None, *, now=None):
        # 只统一本单专属视图的时钟；原读函数仍执行所有未来时间与结构校验。
        if (now is None and path is not None and Path(path) == scoped.root / 'usage.json'
                and (scoped.root / '.isolated-purchase-test').is_file()):
            assert json.loads((scoped.root / '.isolated-purchase-test').read_text(encoding='utf-8')) == {'scope': 'M1 browser fixture'}
            now = timezone.now()
        return original_reader(path, now=now)

    patch.object(resource_usage_view, 'read_resource_usage_view', side_effect=read_fixture_view).start()
    patch.object(core_control, '_executor', return_value=IsolatedCoreExecutor(run)).start()
    return run


def protected_snapshot():
    """仅比较非秘密业务字段；不导出凭据、URI 或身份认证材料。"""
    from portal.models import BillingCycle, Entitlement, NodeIdentity, Line
    values = {
        'entitlements': list(Entitlement.objects.order_by('pk').values(
            'pk', 'public_id', 'quota_bytes', 'expires_at', 'reset_day', 'reset_hour',
            'reset_minute', 'revision', 'applied_revision', 'enabled')),
        'cycles': list(BillingCycle.objects.order_by('pk').values()),
        'identities': list(NodeIdentity.objects.order_by('pk').values(
            'pk', 'subscription_id', 'line_id', 'ingress_id', 'generation', 'state')),
        'lines': list(Line.objects.exclude(name__startswith='M1 隔离').order_by('pk').values(
            'pk', 'public_id', 'ingress_id', 'egress_id')),
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()


def prepare(run):
    from portal.models import Server, CoreInstance, Ingress, Egress, Line
    import uuid
    fixed = lambda value: uuid.uuid5(uuid.NAMESPACE_URL, 'm1-browser/' + value)
    assert not Server.objects.filter(public_id=fixed('server')).exists(), '上一个 M1 场景未清理'
    baseline = protected_snapshot()
    server = Server.objects.create(public_id=fixed('server'), name='M1 隔离服务器')
    core = CoreInstance.objects.create(public_id=fixed('core'), server=server,
        instance_key='m1-browser-core', name='M1 隔离核心', core_type='sing_box',
        configuration_owner='isolated-browser-fake', owner_state='verified',
        configuration_version='a' * 64, registered_version='fixture-only',
        evidence_source='synthetic-browser-fixture', evidence_observed_at=base.NOW)
    ingress = Ingress.objects.create(server=server, name='M1 隔离入口', protocol='reality',
        endpoint_host='m1.example.invalid', endpoint_port=443)
    core.ingresses.add(ingress)
    egress = Egress.objects.create(server=server, core_instance=core, name='M1 隔离外部节点',
        kind='upstream', ownership='external', external_kind='node', protocol='ss2022',
        endpoint_host='m1-home.example.invalid', endpoint_port=443, fail_closed=True)
    line = Line.objects.create(public_id=fixed('line'), name='M1 隔离线路', ingress=ingress, egress=egress)
    result = {'server_id': str(server.public_id), 'core_id': str(core.public_id),
              'line_id': str(line.public_id), 'ingress_id': ingress.pk, 'egress_id': egress.pk,
              'protected_sha256': baseline, 'execution_boundary': 'fake-core-executor'}
    (run / 'm1-fixture.json').write_text(json.dumps(result), encoding='utf-8')
    (run / 'm1-executor-events.jsonl').write_text('', encoding='utf-8')
    return result


def private_json(path, value):
    """纯假资料也沿用创建即受限的文件写入方式。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False)


def prepare_purchase(run, state):
    from django.contrib.auth import get_user_model
    from portal.models import Egress
    from portal.provider_usage import HOME_ACCOUNTING, read_provider_usage
    from portal.resource_usage import project_resource_usage
    from portal.resource_plan_service import register_resource_plan
    actor = get_user_model().objects.get(username='fixture-p8')
    assert not actor.is_staff, '采购夹具不能覆盖已有管理员状态'
    state['purchase_owner_original_staff'] = actor.is_staff
    (run / 'm1-fixture.json').write_text(json.dumps(state), encoding='utf-8')
    actor.is_staff = True
    actor.save(update_fields=['is_staff'])
    Egress.objects.filter(pk=state['egress_id']).update(purchase_meter_id='home')
    plan = {'id': 'home', 'label': 'HOME', 'source': 'residential', 'scope': 'external_node',
            'quota_bytes': '400000000000',
            'cycle': {'kind': 'fixed_days', 'days': 30, 'anchor': '2026-09-20T00:00:00+08:00'},
            'expires_on': '2027-01-19', 'accepted_initial_gap': True}
    residential = {'source': 'estimate', 'accounting': HOME_ACCOUNTING,
        'updated_at': int(base.NOW.timestamp()), 'last_query_ok': True,
        'used_bytes': 300_000_000_000, 'total_bytes': 400_000_000_000,
        'upload_bytes': 100_000_000_000, 'download_bytes': 200_000_000_000,
        'estimate_start': int((base.NOW - timedelta(days=26)).timestamp()),
        'historical_usage_known': False, 'collection_gaps': 0, 'period_start': None,
        'reset_day': 20, 'reset_day_confirmed': False, 'expire': None,
        'quota_unit_assumption': '400_decimal_GB', 'auto_period_reset': False, 'header_eligible': False}
    root = run / 'purchase-projection-browser'
    root.mkdir(exist_ok=False)
    private_json(root / '.isolated-purchase-test', {'scope': 'M1 browser fixture'})
    private_json(root / 'source.json', {'schema_version': 1,
        'updated_at': int(base.NOW.timestamp()), 'residential': residential})
    normalized = read_provider_usage(root / 'source.json', now=base.NOW.timestamp())
    assert normalized['residential']['quality'] == 'estimate'
    meter_state, view = project_resource_usage(normalized, [plan], now=base.NOW.timestamp())
    private_json(root / 'plans.json', {'schema_version': 1, 'plans': [plan]})
    private_json(root / 'cycle-state.json', meter_state)
    private_json(root / 'usage.json', view)
    # 使用同一真实消费入口发布初始权威镜像；不手写来源校验或消费回执。
    from django.core.management import call_command
    call_command('project_resource_usage', stdout=io.StringIO())
    binding = register_resource_plan(plan, target_kind='egress', target_id=state['egress_id'], actor=actor)
    state['purchase_binding_key'] = binding.purchase_binding_key
    (run / 'm1-fixture.json').write_text(json.dumps(state), encoding='utf-8')
    return {'meter_id': binding.meter_id, 'binding_key': binding.purchase_binding_key, 'revision': binding.revision,
            'used_bytes': view['meters'][0]['used_bytes'], 'plan': plan}


def control(run, operation, job_id=None):
    from portal import core_control
    from portal.core_control_models import CoreControlJob, CoreConfigCandidate, CoreControlState
    from portal.models import CoreInstance, Server, Line, Egress, Ingress
    state = json.loads((run / 'm1-fixture.json').read_text(encoding='utf-8'))
    core = CoreInstance.objects.get(public_id=state['core_id'], instance_key='m1-browser-core')
    if operation == 'prepare-purchase':
        return prepare_purchase(run, state)
    if operation in ('consume-purchase', 'inspect-purchase'):
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from portal.resource_plan_service import resource_plan_state
        result = None
        if operation == 'consume-purchase':
            output = io.StringIO()
            call_command('project_resource_usage', stdout=output)
            result = json.loads(output.getvalue())
        root = run / 'purchase-projection-browser'
        return {'command': result, 'plan': resource_plan_state(get_user_model().objects.get(username='fixture-p8'), state['purchase_binding_key']),
                'view': json.loads((root / 'usage.json').read_text(encoding='utf-8')),
                'state': json.loads((root / 'cycle-state.json').read_text(encoding='utf-8')),
                'protected_sha256': protected_snapshot()}
    if operation in ('check-failed', 'check-succeeded', 'apply-unknown', 'reconcile-succeeded'):
        job = CoreControlJob.objects.get(public_id=job_id, core=core)
        assert job.action == ('check' if operation.startswith('check-') else 'apply')
        outcome = {'check-failed': 'failed', 'check-succeeded': 'succeeded',
                   'apply-unknown': 'unknown', 'reconcile-succeeded': 'succeeded'}[operation]
        executor = IsolatedCoreExecutor(run, outcome)
        with patch.object(core_control, '_executor', return_value=executor):
            result = (core_control.reconcile_job if operation == 'reconcile-succeeded' else core_control.run_job)(job_id)
        return {'job': result, 'execution_boundary': 'fake-core-executor'}
    if operation == 'inspect':
        events = [json.loads(line) for line in (run / 'm1-executor-events.jsonl').read_text().splitlines()]
        return {'jobs': list(core.control_jobs.values('public_id', 'action', 'status', 'fence')),
                'revision': core.revision, 'configuration_version': core.configuration_version,
                'node': Egress.objects.filter(pk=state['egress_id']).values('name', 'revision').get(),
                'events': events, 'protected_sha256': protected_snapshot()}
    if operation == 'cleanup':
        assert protected_snapshot() == state['protected_sha256'], 'M1 修改了既有服务/账本/身份/线路'
        if 'purchase_owner_original_staff' in state:
            from django.contrib.auth import get_user_model
            from portal.resource_plan_models import ResourcePlanBinding, ResourcePlanConsumption, ResourcePlanVersion
            bindings = ResourcePlanBinding.objects.filter(target_kind='egress', target_id=str(state['egress_id']))
            ResourcePlanConsumption.objects.filter(version__binding__in=bindings).delete()
            ResourcePlanVersion.objects.filter(binding__in=bindings).delete()
            bindings.delete()
            get_user_model().objects.filter(username='fixture-p8').update(is_staff=state['purchase_owner_original_staff'])
            root = run / 'purchase-projection-browser'
            for name in ('plans.json', 'source.json', 'cycle-state.json', 'usage.json', 'purchase-journal.json', 'plan-authority.json', '.export.lock', '.isolated-purchase-test'):
                (root / name).unlink(missing_ok=True)
            if root.exists():
                root.rmdir()
        CoreControlState.objects.filter(core=core).delete()
        CoreControlJob.objects.filter(core=core).delete()
        CoreConfigCandidate.objects.filter(core=core).delete()
        Line.objects.filter(public_id=state['line_id']).delete()
        Egress.objects.filter(pk=state['egress_id'], server=core.server).delete()
        Ingress.objects.filter(pk=state['ingress_id'], server=core.server).delete()
        server_pk = core.server_id
        core.delete()
        Server.objects.filter(pk=server_pk, public_id=state['server_id']).delete()
        return {'cleaned': True, 'protected_sha256': protected_snapshot()}
    raise ValueError('不支持的 M1 夹具操作')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['seed', 'serve', 'prepare', 'control'])
    parser.add_argument('--run', required=True)
    parser.add_argument('--port', type=int, default=18871)
    parser.add_argument('--operation', choices=['inspect', 'cleanup', 'prepare-purchase', 'consume-purchase', 'inspect-purchase', 'check-failed', 'check-succeeded', 'apply-unknown', 'reconcile-succeeded'])
    parser.add_argument('--job-id')
    args = parser.parse_args()
    current = configure(args.run)
    if args.action == 'seed':
        base.seed(current)
    elif args.action == 'serve':
        base.serve(current, args.port)
    else:
        result = prepare(current) if args.action == 'prepare' else control(current, args.operation, args.job_id)
        print(json.dumps(result, default=str))
