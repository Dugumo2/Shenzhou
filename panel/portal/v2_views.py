"""行舟工作区：用户交付与管理员资源分离，页面只提交事务任务。"""
import uuid
import json
from decimal import Decimal
from datetime import timedelta, time
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, F
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from .entitlements import assign_entitlement, summary_for
from .models import DeploymentJob, DeviceSubscription, Entitlement, Line, NodeIdentity, Server
from .services import audit, throttle
from .v2_forms import AllocationForm, RenameResourceForm, SubscriptionActionForm, SubscriptionForm
from .v2_forms import (ServiceAllocationForm, ServiceDeliveryForm, ServiceResetForm, ServiceFilterForm,
    LineRateForm, UserFilterForm, CapacityPoolForm, CapacitySampleForm, CapacityAdditionForm, LineCapacityForm)
from .views import staff_required

STATES = {'pending': '等待应用', 'queued': '等待执行', 'running': '执行中', 'active': '已生效',
          'applied': '执行完成', 'succeeded': '执行完成', 'failed': '执行失败', 'blocked': '等待条件满足',
          'disabled': '已停用', 'enforcement_pending': '等待服务器停用', 'superseded': '已由新任务替代',
          'candidate': '待发布', 'revocation_pending': '等待撤销', 'revoked': '已撤销',
          'simulated': '隔离验证完成（未上线）', 'resetting': '正在重置', 'disabling': '正在停用'}
STATES.update(suspended='已暂停', suspension_pending='等待服务器暂停', resuming='等待恢复')
RESULTS = {'PRODUCTION_ADAPTER_NOT_CONFIGURED': '服务器执行器尚未接入，当前任务未应用。',
           'PRODUCTION_ADAPTER_NOT_APPROVED': '执行器尚未通过生产验收。',
           'ENTITLEMENT_NOT_APPLIED': '请先完成套餐应用。',
           'PROTOCOL_CAPABILITY_NOT_VERIFIED': '此协议的发布能力尚未核验。',
           'RECEIPT_VERIFICATION_FAILED': '执行回执未通过校验，需管理员检查。',
           'ADAPTER_EXECUTION_FAILED': '执行器失败，需管理员检查后重试。',
           'NEWER_REVISION_EXISTS': '已提交更新的配置，以新任务为准。',
           'VERIFIED': '已通过当前环境核验；隔离结果不代表生产上线。'}


def workspace_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not settings.WORKSPACE_V2:
            raise Http404('新工作区尚未启用')
        return view(request, *args, **kwargs)
    return wrapped


def gb(value):
    return None if value is None else Decimal(value) / Decimal(1_000_000_000)


def _service_name(record):
    return record.service_name or ' + '.join(record.lines.values_list('name', flat=True)) or '待分配服务'


def _service_data(record, actor=None):
    from .billing import current_line_rate
    summary = _summary(record.user, actor=actor)
    data = dict(summary or {})
    lines = []
    for line in record.lines.prefetch_related('additional_ingresses').select_related('ingress'):
        rate = current_line_rate(line)
        lines.append({'pk': line.pk, 'name': line.name,
            'protocols': ' / '.join(ingress.get_protocol_display() for ingress in line.all_ingresses()),
            'multiplier': rate.multiplier if rate else None, 'state_label': '可用' if line.enabled else '已停用'})
    ready = (record.enabled and record.state in ('active', 'simulated') and record.applied_revision == record.revision
        and record.expires_at is not None and record.expires_at > timezone.now() and not record.metering_gap)
    block_reason = ('服务已到期，请联系管理员续期。' if record.expires_at and record.expires_at <= timezone.now()
        else '计量存在缺口，请等待管理员核对。' if record.metering_gap
        else '服务尚未完成应用。请等待管理员开通；当前没有可交付链接。')
    data.update(public_id=record.public_id, name=_service_name(record), lines=lines,
        detail_url=reverse('service_detail', kwargs={'public_id': record.public_id}),
        delivery_url=reverse('service_delivery', kwargs={'public_id': record.public_id}),
        action_url=reverse('service_action', kwargs={'public_id': record.public_id}),
        status_label=STATES.get(record.state, '待核验'), revision=record.revision,
        can_deliver=ready,
        can_reset=ready and record.subscriptions.exists(),
        block_reason=block_reason,
        idempotency_key=uuid.uuid4(), expires_at=record.expires_at)
    return data


def _summary(user, actor=None):
    summary = summary_for(user, actor=actor)
    if summary:
        reasons = {'quota': '本期额度已用完，等待下个账期与恢复核验',
                   'expired': '套餐已到期，请联系管理员续期',
                   'metering_stale': '计量上报中断，等待恢复核验',
                   'metering_gap': '计量存在缺口，等待核对'}
        summary.update(status_label=STATES.get(summary['state'], '待核验'),
            used_gb=gb(summary['used_bytes']), remaining_gb=gb(summary['remaining_bytes']),
            reset_at=summary['resets_at'], is_measured=summary['used_bytes'] is not None,
            suspension_reason_label=reasons.get(summary.get('suspension_reason'), ''))
    return summary


@workspace_required
@login_required
def dashboard(request):
    return subscriptions(request)


@workspace_required
@login_required
def nodes(request):
    summary = _summary(request.user)
    rows = []
    # 节点目录来自已分配入口，创建订阅才生成设备身份；目录不会泄露凭据。
    if summary:
        for line in summary['lines']:
            for ingress in line.all_ingresses():
                if not line.enabled or not ingress.enabled:
                    continue
                rows.append({'name': ingress.name, 'line_name': line.name,
                    'protocol': ingress.get_protocol_display(), 'state_label': summary['status_label'],
                    'description': '在服务详情获取对应客户端配置。'})
    return render(request, 'portal/v2_nodes.html', {'nodes': rows, 'entitlement_summary': summary})


@workspace_required
@staff_required
def users(request):
    rows = []
    form = UserFilterForm(request.GET)
    users_query = User.objects.order_by('username')
    if form.is_valid():
        if form.cleaned_data['q']:
            users_query = users_query.filter(username__icontains=form.cleaned_data['q'])
        state = form.cleaned_data['state']
        if state in ('none', 'assigned'):
            assigned_ids = Entitlement.objects.values_list('user_id', flat=True)
            users_query = users_query.exclude(pk__in=assigned_ids) if state == 'none' else users_query.filter(pk__in=assigned_ids)
        elif state == 'disabled':
            users_query = users_query.filter(is_active=False)
    else:
        users_query = users_query.none()
    page = Paginator(users_query, 25).get_page(request.GET.get('page'))
    records = {r.user_id: r for r in Entitlement.objects.filter(user__in=page.object_list).prefetch_related('lines')}
    for user in page:
        record = records.get(user.pk)
        rows.append({'user': user, 'state_label': STATES.get(record.state, '待核验') if record else '未分配套餐',
                     'quota_gb': gb(record.applied_snapshot.get('quota_bytes')) if record else None,
                     'expires_at': record.expires_at if record else None,
                     'service_name': _service_name(record) if record else None,
                     'has_service': record is not None,
                     'edit_url': reverse('user_edit', kwargs={'pk': user.pk})})
    def page_url(number):
        params = request.GET.copy()
        params['page'] = number
        return '?' + params.urlencode()
    return render(request, 'portal/v2_users.html', {'users': rows, 'filter_form': form, 'page_obj': page,
        'previous_url': page_url(page.previous_page_number()) if page.has_previous() else '',
        'next_url': page_url(page.next_page_number()) if page.has_next() else ''})


def _subscription_rows(request):
    from . import delivery
    from .subscription_files import available
    rows = []
    for item in DeviceSubscription.objects.filter(entitlement__user=request.user).prefetch_related('lines').order_by('-id'):
        job = item.jobs.order_by('-id').first()
        row = {'id': item.public_id, 'public_id': item.public_id, 'name': item.name,
               'client_label': item.get_client_display(), 'client': item.client, 'lines': list(item.lines.all()),
               'state_label': STATES.get(item.state, '待核验'), 'updated_at': item.created_at,
               'revision': item.generation, 'action_idempotency_key': uuid.uuid4(), 'resources': [],
               'status_message': RESULTS.get(job.result_code, '等待处理完成后提供链接。') if job else '等待提交发布任务。',
               'download_url': '', 'can_update': item.state in ('active', 'simulated'),
               'can_reset': item.state in ('active', 'simulated'), 'can_disable': item.state not in ('disabled', 'disabling')}
        if job and job.state in ('failed', 'blocked'):
            row['state_label'] += ' · ' + STATES[job.state]
        # 只有业务服务验证了服务器回执、产物与权益，才显示下载地址。
        try:
            token = delivery.token_for_subscription(request.user, item.public_id)
        except (ValidationError, PermissionDenied):
            token = None
        if token and available(item):
            row['download_url'] = request.build_absolute_uri(reverse('v2_download', kwargs={'token': token}))
            for key, label, help_text in (
                ('routing', '路由方案', '用于此客户端的路由导入，独立于节点订阅。'),
                ('dns', 'DNS 配置说明', '按客户端核对后应用，不能当作节点订阅导入。'),
                ('geosite', '域名规则资源', '在客户端规则资源管理中更新。'),
                ('geoip', 'IP 规则资源', '在客户端规则资源管理中更新。'),
                ('ruleset-map', '规则文件映射', 'Windows 规则材料映射，需完成客户端路径适配。')):
                if available(item, key):
                    row['resources'].append({'label': label, 'help': help_text,
                        'url': request.build_absolute_uri(reverse('v2_resource', kwargs={'token': token, 'resource': key}))})
        rows.append(row)
    return rows


@workspace_required
@login_required
def subscriptions(request):
    record = Entitlement.objects.filter(user=request.user).first()
    from .services import existing_admin_links
    from .legacy_assets import legacy_service_metadata
    legacy = existing_admin_links() if request.user.is_staff else []
    metadata = legacy_service_metadata(request.user)
    return render(request, 'portal/v2_subscriptions.html', {'services': [_service_data(record)] if record else [],
        'legacy_links': legacy, 'legacy_service': metadata,
        'legacy_service_url': reverse('legacy_service') if legacy or metadata else '',
        'entitlement_summary': _summary(request.user)})


@workspace_required
@login_required
def service_detail(request, public_id):
    record = get_object_or_404(Entitlement, public_id=public_id, user=request.user)
    service = _service_data(record)
    deliveries = _subscription_rows(request)
    for item in deliveries:
        item['scope_label'] = ' + '.join(line.name for line in item['lines'])
    cards = [{'code': code, 'title': title, 'device': 'computer' if code == 'windows' else 'phone',
              'system': 'Windows' if code == 'windows' else 'Android',
              'description': '节点和规则分别更新' if code != 'android' else '完整远程配置',
              'supported': True, 'post_url': service['delivery_url'], 'idempotency_key': uuid.uuid4()}
             for code, title in DeviceSubscription.CLIENTS]
    return render(request, 'portal/v2_service_detail.html', {'service': service, 'deliveries': deliveries,
        'client_cards': cards, 'delivery_form': ServiceDeliveryForm(), 'reset_form': ServiceResetForm(initial={'revision': record.revision}),
        'recent_jobs': _job_rows(record.jobs.order_by('-pk')[:5])})


@workspace_required
@staff_required
def legacy_service(request):
    from .services import existing_admin_links
    from .legacy_assets import legacy_service_metadata
    links = existing_admin_links()
    return render(request, 'portal/v2_legacy_service.html', {'legacy_links': links,
        'legacy_service': legacy_service_metadata(request.user)})


@workspace_required
@login_required
@require_POST
def service_delivery(request, public_id):
    from .delivery import obtain_delivery
    get_object_or_404(Entitlement, public_id=public_id, user=request.user)
    form = ServiceDeliveryForm(request.POST)
    if not throttle('service-delivery', str(request.user.pk), limit=30):
        return HttpResponse('获取过于频繁，请稍后再试。', status=429)
    if form.is_valid():
        try:
            _, job, created = obtain_delivery(request.user, public_id, form.cleaned_data['client'],
                form.cleaned_data['scope'], str(form.cleaned_data['idempotency_key']))
        except ValidationError as exc:
            messages.error(request, '；'.join(exc.messages))
        else:
            messages.info(request, '已提交此格式的生成请求，处理结果会显示在本页。' if created else '已找到此格式的订阅，请查看下方链接或处理状态。')
    else:
        messages.error(request, '获取参数无效，请重新选择客户端。')
    return redirect('service_detail', public_id=public_id)


@workspace_required
@login_required
@require_POST
def service_action(request, public_id):
    from .delivery import reset_service_deliveries
    record = get_object_or_404(Entitlement, public_id=public_id, user=request.user)
    form = ServiceResetForm(request.POST)
    if not form.is_valid():
        return HttpResponse('请确认重置影响范围。', status=400)
    try:
        with transaction.atomic():
            record = Entitlement.objects.select_for_update().get(pk=record.pk)
            if record.revision != form.cleaned_data['revision']:
                raise ValidationError('服务配置已变化，请刷新后操作。')
            reset_service_deliveries(request.user, public_id, str(form.cleaned_data['idempotency_key']))
    except ValidationError as exc:
        messages.error(request, '；'.join(exc.messages))
    else:
        messages.info(request, '已提交此服务的订阅重置。额度和到期时间保持不变，旧身份撤销以执行结果为准。')
    return redirect('service_detail', public_id=public_id)


@workspace_required
@login_required
@require_POST
def subscription_create(request):
    return HttpResponse('请从我的服务进入已开通服务，再选择客户端获取订阅。', status=409)


@workspace_required
@login_required
@require_POST
def subscription_action(request, public_id):
    get_object_or_404(DeviceSubscription, public_id=public_id, entitlement__user=request.user)
    return HttpResponse('此旧入口已停用，请进入我的服务操作。', status=409)


@workspace_required
@staff_required
@require_http_methods(['GET'])
def allocations(request):
    if request.GET.get('user', '').isdigit():
        return redirect('user_edit', pk=int(request.GET['user']))
    form = ServiceFilterForm(request.GET)
    query = Entitlement.objects.select_related('user').prefetch_related('lines', 'subscriptions').order_by('user__username')
    if form.is_valid():
        data = form.cleaned_data
        if data['q']:
            query = query.filter(Q(user__username__icontains=data['q']) | Q(service_name__icontains=data['q']))
        if data['line']:
            query = query.filter(lines=data['line'])
        now = timezone.now()
        state = data['state']
        if state == 'expired':
            query = query.filter(expires_at__lte=now)
        elif state == 'expiring':
            query = query.filter(expires_at__gt=now, expires_at__lte=now + timedelta(days=7))
        elif state == 'exhausted':
            query = query.filter(cycles__starts_at__lte=now, cycles__ends_at__gt=now, cycles__used_bytes__gte=F('quota_bytes'))
        elif state:
            query = query.filter(state=state)
        if data['client']:
            query = query.filter(subscriptions__client=data['client'])
    else:
        query = query.none()
    page = Paginator(query.distinct(), 25).get_page(request.GET.get('page'))
    rows = [{'user': r.user, 'name': _service_name(r), 'lines': list(r.lines.all()), 'quota_gb': gb(r.quota_bytes),
             'expires_at': r.expires_at, 'state_label': STATES.get(r.state, '待核验'),
             'formats': ' / '.join(sorted({s.get_client_display() for s in r.subscriptions.all()})),
             'edit_url': reverse('user_edit', kwargs={'pk': r.user_id})} for r in page]
    def page_url(number):
        params = request.GET.copy()
        params['page'] = number
        return '?' + params.urlencode()
    return render(request, 'portal/v2_allocations.html', {'filter_form': form, 'page_obj': page, 'services': rows,
        'previous_url': page_url(page.previous_page_number()) if page.has_previous() else '',
        'next_url': page_url(page.next_page_number()) if page.has_next() else ''})


@workspace_required
@staff_required
@require_http_methods(['GET', 'POST'])
def user_detail(request, pk):
    user = get_object_or_404(User, pk=pk)
    target = Entitlement.objects.filter(user=user).first()
    initial = {'revision': 0, 'validity_mode': 'months', 'reset_mode': 'activation'}
    if target:
        initial.update(service_name=target.service_name, lines=target.lines.all(), quota_gb=gb(target.quota_bytes),
            reset_day=target.reset_day, reset_time=time(target.reset_hour, target.reset_minute or 0) if target.reset_hour is not None else None,
            service_months=target.service_months, enabled=target.enabled, expires_at=target.requested_expires_at,
            validity_mode='date' if target.requested_expires_at else 'months',
            reset_mode='custom' if target.reset_day is not None else 'activation', revision=target.revision)
    form = ServiceAllocationForm(request.POST if request.method == 'POST' else None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        try:
            with transaction.atomic():
                User.objects.select_for_update().get(pk=pk)
                current = Entitlement.objects.select_for_update().filter(user=user).first()
                if (current.revision if current else 0) != data['revision']:
                    raise ValidationError('服务已被修改，请刷新页面后再保存。')
                reset_time = data.get('reset_time')
                _, job = assign_entitlement(request.user, user, [x.pk for x in data['lines']],
                    int(data['quota_gb'] * 1_000_000_000), reset_day=data['reset_day'],
                    reset_hour=reset_time.hour if reset_time else None, reset_minute=reset_time.minute if reset_time else None,
                    service_name=data['service_name'], service_months=data['service_months'], expires_at=data['expires_at'],
                    enabled=data['enabled'], renew=data['operation'] == 'renew', idempotency_key=str(data['idempotency_key']))
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.info(request, '服务设置已保存并提交应用；实际开通结果将在此显示。')
            return redirect('user_edit', pk=pk)
    start = max(timezone.now(), target.expires_at) if target and target.expires_at else timezone.now()
    return render(request, 'portal/v2_user_detail.html', {'target_user': user,
        'service': _service_data(target, actor=request.user) if target else None, 'form': form,
        'start_at': timezone.localtime(start).isoformat(), 'renew_start_at': timezone.localtime(start).isoformat(),
        'activation_at': timezone.localtime(target.activated_at).isoformat() if target and target.activated_at else timezone.localtime().isoformat(),
        'recent_jobs': _job_rows(target.jobs.order_by('-pk')[:5]) if target else []})


@workspace_required
@staff_required
@require_http_methods(['GET', 'POST'])
def resources(request):
    from .billing import current_line_rate, set_line_rate
    rate_form, rate_line = None, None
    if request.method == 'POST':
        if request.POST.get('action') == 'set_rate':
            raw_id = request.POST.get('line_id', '')
            if not raw_id.isdigit():
                return HttpResponse('线路参数无效。', status=400)
            rate_line = get_object_or_404(Line, pk=int(raw_id))
            rate_form = LineRateForm(request.POST, prefix=f'rate-{rate_line.pk}')
            if rate_form.is_valid():
                try:
                    set_line_rate(request.user, rate_line.pk, **rate_form.cleaned_data)
                except ValidationError as exc:
                    rate_form.add_error(None, exc)
                else:
                    messages.success(request, '倍率版本已保存；历史扣费保持不变。')
                    return redirect('resources')
            return _resources_page(request, rate_line, rate_form)
        form = RenameResourceForm(request.POST)
        if not form.is_valid():
            return HttpResponse('名称或资源参数无效。', status=400)
        data = form.cleaned_data
        model = Server if data['action'] == 'rename_server' else Line
        with transaction.atomic():
            record = get_object_or_404(model.objects.select_for_update(), public_id=data['public_id'])
            record.name = data['name']
            record.save(update_fields=['name'])
            audit(request.user, data['action'], record.public_id, 'APPLIED')
        messages.success(request, '显示名称已更新，业务身份保持不变。')
        return redirect('resources')
    return _resources_page(request)


def _resources_page(request, rate_line=None, rate_form=None):
    from .billing import current_line_rate
    lines = []
    for r in Line.objects.select_related('ingress__server', 'egress').prefetch_related('rate_versions'):
        rate = current_line_rate(r)
        lines.append({'pk': r.pk, 'public_id': r.public_id, 'name': r.name, 'server_name': r.ingress.server.name,
            'egress_name': r.egress.name, 'description': ' / '.join(i.get_protocol_display() for i in r.all_ingresses()),
            'state_label': '已登记（执行状态另行核验）' if r.enabled else '已禁用',
            'multiplier': rate.multiplier if rate else None,
            'multiplier_form': rate_form if rate_line and rate_line.pk == r.pk else LineRateForm(prefix=f'rate-{r.pk}',
                initial={'effective_at': timezone.localtime().replace(second=0, microsecond=0)}),
            'rate_history': [{'multiplier': v.multiplier, 'effective_at': v.effective_at,
                'state_label': '待生效' if v.effective_at > timezone.now() else '当前' if rate and rate.pk == v.pk else '历史'}
                for v in r.rate_versions.order_by('-effective_at')[:20]]})
    return render(request, 'portal/v2_resources.html', {'servers': Server.objects.all(), 'lines': lines})


@workspace_required
@staff_required
@require_http_methods(['GET', 'POST'])
def capacities(request):
    from . import capacity
    from .models import CapacityPool
    from .entitlements import add_months
    now = timezone.localtime().replace(second=0, microsecond=0)
    initial = {'period_start': now, 'period_end': add_months(now, 1), 'source_priority': 'api,manual,agent'}
    edit_id = request.GET.get('edit', '')
    if edit_id.isdigit():
        pool = get_object_or_404(CapacityPool, pk=int(edit_id))
        initial.update({key: getattr(pool, key) for key in ('name', 'owner', 'server', 'provider', 'capacity_mode',
            'period_start', 'period_end', 'accounting_basis', 'warning_percent', 'critical_percent', 'urgent_percent',
            'overage_policy', 'enabled', 'stale_after_seconds', 'original_unit')})
        initial.update(pool=pool.pk, planned_gb=gb(pool.planned_bytes), safety_gb=gb(pool.safety_bytes),
            source_priority=','.join(pool.source_priority))
    classes = {'save_pool': CapacityPoolForm, 'record_sample': CapacitySampleForm,
               'add_capacity': CapacityAdditionForm, 'link_line': LineCapacityForm}
    action = request.POST.get('action') if request.method == 'POST' else None
    if request.method == 'POST' and action not in classes:
        return HttpResponse('操作类型无效。', status=400)
    forms = {key: cls(request.POST if key == action else None,
        initial=initial if key == 'save_pool' else {'observed_at': now} if key == 'record_sample' else {})
        for key, cls in classes.items()}
    if action and forms[action].is_valid():
        data = dict(forms[action].cleaned_data)
        try:
            with transaction.atomic():
                if action == 'save_pool':
                    pool = data.pop('pool')
                    planned = data.pop('planned_gb')
                    data['planned_bytes'] = int(planned * 1_000_000_000) if planned is not None else None
                    data['safety_bytes'] = int(data.pop('safety_gb') * 1_000_000_000)
                    data['source_priority'] = data['source_priority'].split(',')
                    capacity.save_pool(request.user, pool_id=pool.pk if pool else None, **data)
                elif action == 'record_sample':
                    capacity.record_sample(request.user, data['pool'].pk, source='manual',
                        source_key=str(data['idempotency_key']), used_bytes=int(data['used_gb'] * 1_000_000_000),
                        observed_at=data['observed_at'], accounting_basis=data['accounting_basis'], reason=data['reason'])
                elif action == 'add_capacity':
                    capacity.add_capacity(request.user, data['pool'].pk, int(data['added_gb'] * 1_000_000_000),
                        data['reason'], str(data['idempotency_key']))
                else:
                    capacity.link_line(request.user, data['line'].pk, data['pool'].pk, data['consumption_factor'],
                        data['topology_verified'], data['note'])
                capacity.refresh_alerts(request.user)
        except ValidationError as exc:
            forms[action].add_error(None, exc)
        else:
            messages.success(request, '资源记录已保存；用户套餐额度保持不变。')
            return redirect('capacities')
    overview = capacity.capacity_overview(request.user)
    sources = {'api': '供应商 API', 'agent': 'Agent 观测', 'manual': '人工校准'}
    pools = []
    for summary in overview['summaries']:
        pool = summary['pool']
        pools.append({'pk': pool.pk, 'name': pool.name, 'edit_url': '?edit=' + str(pool.pk),
            'is_unlimited': pool.capacity_mode == 'unlimited', 'capacity_gb': gb(summary['capacity_bytes']),
            'used_gb': gb(summary['used_bytes']), 'remaining_gb': gb(summary['remaining_bytes']),
            'quality_label': '口径不匹配' if not summary['basis_matches'] else '数据陈旧或缺失' if summary['stale'] else sources.get(summary['source'], '待核验'),
            'source_label': sources.get(summary['source'], '未接入'), 'measurement_label': summary['selected_accounting_basis'] or pool.accounting_basis,
            'updated_at': summary['observed_at'], 'period_start': pool.period_start, 'period_end': pool.period_end,
            'trend_label': f"预计账期末 {gb(summary['forecast_bytes']):.3f} GB" if summary['forecast_bytes'] is not None else summary['forecast_reason'],
            'commitment_label': f"最坏路径约 {gb(summary['commitment_bytes']):.3f} GB" if summary['commitment_bytes'] is not None else summary['commitment_reason'],
            'assumption_message': summary['commitment_reason'],
            'lines': [link.line for link in pool.line_links.select_related('line')]})
    alerts = [{'pool_name': a.pool.name, 'message': a.message,
        'level_label': {'urgent': '紧张', 'critical': '警告', 'warning': '提醒', 'info': '待核验'}.get(a.severity, '提醒')}
        for a in overview['active_alerts']]
    return render(request, 'portal/v2_capacity.html', {'pools': pools, 'alerts': alerts,
        'form': forms['save_pool'], 'sample_form': forms['record_sample'],
        'addition_form': forms['add_capacity'], 'link_form': forms['link_line']})


def _job_rows(records):
    names = {'assign': '分配套餐', 'create': '生成订阅', 'reset': '重置订阅链接',
             'refresh': '更新订阅内容', 'disable': '停用订阅', 'enforce': '执行套餐限制', 'resume': '恢复套餐'}
    return [{'public_id': r.public_id, 'action': names.get(r.kind, '配置任务'),
             'state_label': STATES.get(r.state, '待核验'), 'result_code': r.result_code,
             'result_message': RESULTS.get(r.result_code, r.result_code),
             'impact_message': '本次隔离应用会重启测试核心，测试连接重建。' if r.receipt.get('scope') == 'isolated' else '',
             'created_at': r.created_at, 'can_retry': r.state in ('failed', 'blocked')}
            for r in records]


@workspace_required
@staff_required
def jobs(request):
    return render(request, 'portal/v2_jobs.html', {'jobs': _job_rows(DeploymentJob.objects.order_by('-id')[:100]),
        'operator_enabled': settings.OPERATOR_ENABLED})


@workspace_required
@staff_required
def operations(request):
    snapshot = {}
    try:
        if settings.SNAPSHOT_PATH.stat().st_size <= 32768:
            value = json.loads(settings.SNAPSHOT_PATH.read_text(encoding='utf-8'))
            allowed = {'updated_at', 'sing_box_status', 'xray_status', 'subscription_status'}
            snapshot = {key: val for key, val in value.items() if key in allowed
                        and isinstance(val, (str, int, float, type(None)))}
    except (OSError, ValueError, AttributeError):
        pass
    return render(request, 'portal/v2_operations.html', {'snapshot': snapshot,
        'servers': Server.objects.all(), 'operator_enabled': False,
        'jobs': _job_rows(DeploymentJob.objects.order_by('-id')[:5])})


@workspace_required
@staff_required
@require_POST
def retry_job(request, public_id):
    from .jobs import retry_job as retry
    get_object_or_404(DeploymentJob, public_id=public_id)
    try:
        retry(request.user, public_id)
    except ValidationError as exc:
        messages.error(request, '；'.join(exc.messages))
    else:
        messages.info(request, '任务已重新排队。执行条件未满足时仍会显示阻塞。')
    return redirect('jobs')


@workspace_required
@require_http_methods(['GET', 'HEAD'])
def download(request, token, resource='subscription'):
    from .subscription_files import download_artifact
    try:
        result = download_artifact(token, resource)
    except (ValidationError, PermissionDenied):
        raise Http404('订阅不可用')
    response = HttpResponse(result['content'], content_type=result['content_type'])
    if result.get('userinfo'):
        response['Subscription-Userinfo'] = result['userinfo']
    return response
