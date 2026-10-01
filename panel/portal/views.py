"""浏览器页面不执行 Shell；服务器操作排队并保留应用状态。"""
import json
import os
import secrets
from io import BytesIO
from bridge.artifacts import ARTIFACTS as RELEASE_ARTIFACTS, ArtifactError, available as artifact_available, read_artifact
from functools import wraps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import FileResponse, Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from .forms import AdminUserCreateForm, ClientDirectRuleForm, InvitationForm, LoginForm, MemberForm, OperationForm, ProfileForm, RegisterForm, clean_username
from .models import AuditEvent, ClientDirectRule, Invitation, Membership, OperationJob, SubscriptionGrant
from .client_rules import policy_document, preview_rule, source_document
from .services import DEVICES, artifact_path, audit, existing_admin_links, grant_from_token, issue_invitation, register, throttle, token_for


def write_routing_policy(rules):
    """把候选规则原子写入受限队列，供固定 root 发布器复核。"""
    document, digest = policy_document(rules)
    path = settings.ROUTING_POLICY_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    temporary.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return digest


def staff_required(view):
    @wraps(view)
    @login_required
    def guarded(request, *args, **kwargs):
        if not request.user.is_staff:
            return HttpResponseForbidden('此操作仅限管理员。')
        return view(request, *args, **kwargs)
    return guarded


def rate_allowed(request, scope):
    address = request.META.get('REMOTE_ADDR', 'unknown')
    # 不信任由客户端伪造的 X-Forwarded-For。
    return throttle(scope + '-address', address, limit=30) and throttle(
        scope + '-account', User.normalize_username(request.POST.get('username', '').strip()).casefold(), limit=10)


@sensitive_post_parameters('password', 'password1', 'password2', 'invitation')
@require_http_methods(['GET', 'POST'])
def login_page(request):
    if request.user.is_authenticated:
        return redirect('dashboard')
    if request.method == 'POST' and not rate_allowed(request, 'login'):
        messages.error(request, '尝试过于频繁，请稍后再试。')
        return render(request, 'portal/login.html', {'form': LoginForm(request)}, status=429)
    form = LoginForm(request, data=request.POST if request.method == 'POST' else None)
    if request.method == 'POST':
        if form.is_valid():
            login(request, form.get_user())
            audit(request.user, 'login')
            return redirect('dashboard')
    return render(request, 'portal/login.html', {'form': form})


@sensitive_post_parameters('password1', 'password2', 'invitation')
@require_http_methods(['GET', 'POST'])
def register_page(request):
    if request.method == 'POST' and not rate_allowed(request, 'register'):
        messages.error(request, '尝试过于频繁，请稍后再试。')
        return render(request, 'portal/register.html', {'form': RegisterForm()}, status=429)
    form = RegisterForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST':
        if form.is_valid():
            try:
                user = register(form)
            except (ValidationError, IntegrityError):
                form.add_error(None, '注册未完成，请确认账号名称和邀请码是否可用。')
            else:
                login(request, user)
                messages.success(request, '注册成功。套餐待管理员开通，成功前不会发放可用节点。')
                return redirect('dashboard')
    return render(request, 'portal/register.html', {'form': form})


@require_POST
def logout_page(request):
    logout(request)
    return redirect('login')


@login_required
def dashboard(request):
    if settings.WORKSPACE_V2:
        from .v2_views import dashboard as workspace
        return workspace(request)
    member = Membership.objects.filter(user=request.user).first()
    snapshot = {}
    if request.user.is_staff and settings.SNAPSHOT_PATH.is_file():
        try:
            candidate = json.loads(settings.SNAPSHOT_PATH.read_text(encoding='utf-8'))
            allowed = {'updated_at', 'sing_box_status', 'xray_status', 'subscription_status',
                       'residential_used_bytes', 'residential_quota_bytes', 'bwh_used_bytes', 'bwh_quota_bytes'}
            snapshot = {k: v for k, v in candidate.items() if k in allowed and isinstance(v, (str, int, float, type(None)))}
        except (OSError, ValueError, AttributeError):
            snapshot = {}
    return render(request, 'portal/dashboard.html', {'membership': member, 'snapshot': snapshot})


@login_required
@sensitive_post_parameters('old_password', 'new_password1', 'new_password2')
@require_http_methods(['GET', 'POST'])
def account(request):
    action = request.POST.get('action')
    profile_form = ProfileForm(request.POST if action == 'profile' else None, instance=request.user)
    password_form = PasswordChangeForm(request.user, request.POST if action == 'password' else None)
    # 即使浏览器脚本未加载，也让原生表单先阻止明显无效的输入；服务器校验仍是最终依据。
    password_form.fields['old_password'].widget.attrs.update({'autocomplete': 'current-password'})
    password_form.fields['new_password1'].widget.attrs.update({'autocomplete': 'new-password', 'minlength': '12'})
    password_form.fields['new_password2'].widget.attrs.update({'autocomplete': 'new-password', 'minlength': '12'})
    if request.method == 'POST':
        if not throttle('account-change', str(request.user.pk), limit=10):
            messages.error(request, '修改过于频繁，请稍后再试。')
        elif action == 'profile' and profile_form.is_valid():
            try:
                with transaction.atomic():
                    clean_username(profile_form.cleaned_data['username'], exclude=request.user.pk)
                    profile_form.save()
                    audit(request.user, 'username_changed', request.user.pk)
            except (ValidationError, IntegrityError):
                profile_form.add_error('username', '此账号名称已被使用。')
            else:
                messages.success(request, '账号名称已修改。订阅归属保持不变。')
                return redirect('account')
        elif action == 'password' and password_form.is_valid():
            user = password_form.save()
            update_session_auth_hash(request, user)
            audit(user, 'password_changed', user.pk)
            messages.success(request, '密码已修改；其他旧登录会话将失效。')
            return redirect('account')
    return render(request, 'portal/account.html', {'profile_form': profile_form, 'password_form': password_form})


@staff_required
def users(request):
    if settings.WORKSPACE_V2:
        from .v2_views import users as workspace
        return workspace(request)
    return render(request, 'portal/users.html', {'members': Membership.objects.select_related('user').order_by('user_id')})


@staff_required
@require_http_methods(['GET', 'POST'])
@never_cache
@sensitive_variables()
def user_create(request):
    """初始密码仅放在成功响应中；不写入会话、审计或消息队列。"""
    form = AdminUserCreateForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST':
        if not throttle('admin-user-create', str(request.user.pk), limit=20):
            form.add_error(None, '创建过于频繁，请稍后再试。')
            return render(request, 'portal/user_create.html', {'form': form}, status=429)
        if form.is_valid():
            try:
                with transaction.atomic():
                    # 在写事务内复核重名，避免校验与保存之间发生抢占。
                    clean_username(form.cleaned_data['username'])
                    user = form.save(commit=False)
                    user.is_staff, user.is_superuser, user.is_active = False, False, True
                    initial_password = secrets.token_urlsafe(24)
                    validate_password(initial_password, user)
                    user.set_password(initial_password)
                    user.save()
                    member = Membership.objects.create(
                        user=user, status='pending',
                        quota_bytes=int(form.cleaned_data['quota_gb'] * 1_000_000_000),
                        service_days=form.cleaned_data['service_days'],
                        provisioning_state='not_provisioned', usage_state='not_connected', expires_at=None,
                    )
                    if not settings.WORKSPACE_V2:
                        for device in DEVICES:
                            SubscriptionGrant.objects.create(membership=member, device=device)
                    audit(request.user, 'admin_user_created', member.public_id, 'PENDING')
            except (ValidationError, IntegrityError):
                form.add_error(None, '创建未完成，请确认账号名称可用后重试。')
            else:
                # 返回即不再保存明文；重复提交因账号已存在而不能重新展示。
                return render(request, 'portal/user_create.html', {
                    'form': AdminUserCreateForm(), 'created_username': user.username,
                    'created_user_id': user.pk, 'initial_password': initial_password,
                })
    return render(request, 'portal/user_create.html', {'form': form})


@staff_required
@require_http_methods(['GET', 'POST'])
def user_edit(request, pk):
    if settings.WORKSPACE_V2:
        from .v2_views import user_detail
        return user_detail(request, pk)
    member = get_object_or_404(Membership.objects.select_related('user'), user_id=pk)
    form = MemberForm(request.POST or None, instance=member, initial={
        'quota_gb': member.quota_bytes / 1_000_000_000, 'revision': member.revision})
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            current = Membership.objects.select_for_update().get(pk=member.pk)
            if current.revision != form.cleaned_data['revision']:
                form.add_error(None, '资料已被其他操作更新，请刷新后再修改。')
            else:
                current.status = form.cleaned_data['status']
                current.expires_at = form.cleaned_data['expires_at']
                current.quota_bytes = int(form.cleaned_data['quota_gb'] * 1_000_000_000)
                current.provisioning_state = 'pending_apply'
                current.revision += 1
                current.save()
                audit(request.user, 'membership_candidate_saved', current.public_id, 'PENDING')
                messages.success(request, '已保存套餐候选，服务器应用待完成；不会将候选状态冒充实际停用或开通。')
                return redirect('users')
    return render(request, 'portal/user_edit.html', {'form': form, 'target_member': member})


@staff_required
@require_http_methods(['GET', 'POST'])
def invites(request):
    form = InvitationForm(request.POST or None)
    code = None
    if request.method == 'POST' and form.is_valid():
        if throttle('invite-create', str(request.user.pk), limit=30):
            code = issue_invitation(request.user, form.cleaned_data)
            form = InvitationForm()
        else:
            form.add_error(None, '创建过于频繁，请稍后再试。')
    return render(request, 'portal/invites.html', {'form': form, 'created_code': code,
        'invitations': Invitation.objects.order_by('-id')[:100]})


@staff_required
@require_POST
def revoke_invite(request, pk):
    with transaction.atomic():
        record = get_object_or_404(Invitation.objects.select_for_update(), pk=pk)
        record.revoked = True
        record.save(update_fields=['revoked'])
        audit(request.user, 'invite_revoked', pk)
    return redirect('invites')


@login_required
def subscriptions(request):
    if settings.WORKSPACE_V2:
        from .v2_views import subscriptions as workspace
        return workspace(request)
    legacy_links = existing_admin_links() if request.user.is_staff else []
    if legacy_links:
        return render(request, 'portal/subscriptions.html', {
            'grants': [], 'links': legacy_links, 'membership': None,
            'legacy_mode': True, 'admin_mode': request.path.startswith('/manage/')})
    member = Membership.objects.filter(user=request.user).first()
    grants, links = [], []
    if member:
        for grant in member.grants.order_by('id'):
            available = member.eligible and grant.enabled and artifact_path(grant) is not None
            grants.append({'device': grant.device, 'display_name': DEVICES[grant.device], 'available': available})
            if available:
                path = reverse('download', kwargs={'token': token_for(grant), 'device': grant.device})
                links.append({'label': DEVICES[grant.device], 'url': request.build_absolute_uri(path), 'kind': '订阅'})
                resource_labels = {'windows-rules': 'Windows 路由材料', 'v2rayng-routes': 'v2rayNG 路由',
                                   'v2rayng-geosite': 'megabox-geosite.dat', 'v2rayng-geoip': 'megabox-geoip.dat'}
                for artifact, label in resource_labels.items():
                    if RELEASE_ARTIFACTS[artifact][1] == grant.device and artifact_available(settings.ARTIFACT_ROOT, member.public_id, artifact):
                        path = reverse('resource_download', kwargs={'token': token_for(grant), 'artifact': artifact})
                        links.append({'label': label, 'url': request.build_absolute_uri(path), 'kind': '规则资源'})
    return render(request, 'portal/subscriptions.html', {'grants': grants, 'links': links,
        'membership': member, 'admin_mode': request.path.startswith('/manage/')})


@staff_required
def admin_subscriptions(request):
    if settings.WORKSPACE_V2:
        return redirect('allocations')
    return subscriptions(request)


@login_required
@require_POST
def rotate_subscriptions(request):
    if settings.WORKSPACE_V2:
        return HttpResponse('请在我的订阅中选择具体订阅，再重置订阅链接。', status=409)
    if request.POST.get('confirm') != 'yes':
        return HttpResponse('需要明确确认。', status=400)
    with transaction.atomic():
        member = get_object_or_404(Membership.objects.select_for_update(), user=request.user)
        for grant in member.grants.select_for_update():
            grant.version += 1
            grant.save(update_fields=['version'])
        audit(request.user, 'subscription_links_rotated', member.public_id)
    messages.success(request, '下载地址已重置。已下载的节点凭据不会因此失效。')
    return redirect('subscriptions')


@require_http_methods(['GET', 'HEAD'])
def download(request, token, device):
    try:
        grant = grant_from_token(token, device)
    except ValidationError:
        raise Http404('订阅不可用。')
    try:
        raw = read_artifact(settings.ARTIFACT_ROOT, grant.membership.public_id, device)
    except (ArtifactError, OSError):
        raise Http404('订阅尚未发布。')
    response = FileResponse(BytesIO(raw), content_type='application/json' if device == 'android' else 'text/plain; charset=utf-8')
    member = grant.membership
    # 这里只提供面板自己的计量口径；生产计量未接入时不允许下载。
    response['Subscription-Userinfo'] = f'upload=0; download={member.used_bytes}; total={member.quota_bytes}; expire={int(member.expires_at.timestamp())}'
    return response


@require_http_methods(['GET', 'HEAD'])
def resource_download(request, token, artifact):
    if artifact not in RELEASE_ARTIFACTS or artifact in ('windows', 'android', 'v2rayng'):
        raise Http404('资源不存在。')
    device = RELEASE_ARTIFACTS[artifact][1]
    try:
        grant = grant_from_token(token, device)
        raw = read_artifact(settings.ARTIFACT_ROOT, grant.membership.public_id, artifact)
    except (ValidationError, ArtifactError, OSError):
        raise Http404('资源尚未发布。')
    content_type = 'application/octet-stream' if artifact.endswith(('geosite', 'geoip')) else 'application/json'
    return FileResponse(BytesIO(raw), content_type=content_type)


@staff_required
@require_http_methods(['GET', 'POST'])
def operations(request):
    if settings.WORKSPACE_V2:
        from .v2_views import operations as workspace
        return workspace(request)
    if not settings.OPERATOR_ENABLED:
        if request.method == 'POST':
            return HttpResponse('服务执行器尚未接入，不能提交任务。', status=409)
        return render(request, 'portal/operations.html', {
            'form': None, 'jobs': OperationJob.objects.order_by('-id')[:30],
            'actions': (), 'operator_enabled': False})
    form = OperationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        if not settings.OPERATOR_ENABLED:
            form.add_error(None, '服务器执行器尚未接入。本地候选不会执行生产操作。')
        elif throttle('operation', str(request.user.pk), limit=20):
            from .operation_policy import build_command
            action = form.cleaned_data['action']
            build_command(action, {})
            with transaction.atomic():
                job = OperationJob.objects.create(actor=request.user, action=action)
                audit(request.user, 'operation_queued', job.public_id, 'QUEUED')
            messages.success(request, '操作已排队，请查看实际执行结果。')
            return redirect('operations')
        else:
            form.add_error(None, '操作过于频繁，请稍后再试。')
    return render(request, 'portal/operations.html', {'form': form,
        'jobs': OperationJob.objects.order_by('-id')[:30], 'actions': form.fields['action'].choices,
        'operator_enabled': True})


@staff_required
@never_cache
@require_http_methods(['GET', 'POST'])
def client_direct_rules(request):
    """管理员编辑客户端代理／直连候选；不触碰生产路由或设备。"""
    edit_id = request.GET.get('edit') if request.method == 'GET' else request.POST.get('rule_id')
    edit = get_object_or_404(ClientDirectRule, pk=edit_id) if edit_id else None
    form = ClientDirectRuleForm(instance=edit, initial={'enabled': True, 'outbound': 'direct'}
                                if edit is None else {'revision': edit.revision})
    preview = None
    if request.method == 'POST':
        action = request.POST.get('action')
        if not throttle('client-direct-edit', str(request.user.pk), limit=30):
            messages.error(request, '操作过于频繁，请稍后再试。')
            return redirect('client_direct_rules')
        if action == 'preview':
            try:
                preview = preview_rule(ClientDirectRule.objects.all(), request.POST.get('domain', ''))
            except ValidationError:
                messages.error(request, '预览域名格式无效。')
        elif action == 'save':
            form = ClientDirectRuleForm(request.POST, instance=edit)
            if form.is_valid():
                try:
                    with transaction.atomic():
                        if edit:
                            locked = ClientDirectRule.objects.select_for_update().get(pk=edit.pk)
                            if locked.revision != form.cleaned_data['revision']:
                                raise ValidationError('规则已被其他操作更新，请刷新后重试。')
                            for field in ('outbound', 'kind', 'value', 'scope_domain', 'enabled'):
                                setattr(locked, field, form.cleaned_data[field])
                            locked.revision += 1
                            locked.save()
                            saved = locked
                        else:
                            saved = form.save(commit=False)
                            saved.creator = request.user
                            saved.save()
                        audit(request.user, 'client_route_candidate_saved', str(saved.pk), 'PENDING')
                        write_routing_policy(ClientDirectRule.objects.all())
                except (ValidationError, IntegrityError):
                    form.add_error(None, '保存失败：规则冲突或版本过期。')
                else:
                    messages.success(request, '域名规则候选已保存；设备尚未更新。')
                    return redirect('client_direct_rules')
        elif action == 'delete':
            if edit is None or request.POST.get('confirm') != 'yes':
                return HttpResponse('删除目标或确认无效。', status=400)
            try:
                revision = int(request.POST.get('revision', ''))
            except ValueError:
                return HttpResponse('规则版本无效。', status=400)
            with transaction.atomic():
                locked = get_object_or_404(ClientDirectRule.objects.select_for_update(), pk=edit.pk)
                if locked.revision != revision:
                    return HttpResponse('规则已变更，请刷新后重试。', status=409)
                identifier = locked.pk
                locked.delete()
                audit(request.user, 'client_route_candidate_deleted', str(identifier), 'PENDING')
                write_routing_policy(ClientDirectRule.objects.all())
            messages.success(request, '候选规则已删除；已发布的设备配置仍需单独更新。')
            return redirect('client_direct_rules')
        else:
            return HttpResponse('操作不受支持。', status=400)
    rules = list(ClientDirectRule.objects.all())
    _, digest = policy_document(rules)
    publish_status = {}
    try:
        if settings.ROUTING_STATUS_PATH.is_file() and not settings.ROUTING_STATUS_PATH.is_symlink():
            candidate = json.loads(settings.ROUTING_STATUS_PATH.read_text(encoding='utf-8'))
            if isinstance(candidate, dict) and candidate.get('policy_sha256') == policy_document(rules)[1]:
                publish_status = {k: candidate.get(k) for k in ('state', 'version') if k in candidate}
    except (OSError, ValueError, TypeError):
        publish_status = {}
    return render(request, 'portal/client_direct_rules.html', {
        'form': form, 'rules': rules, 'editing': edit, 'preview': preview, 'digest': digest[:12],
        'publish_status': publish_status})


@staff_required
@never_cache
@require_http_methods(['GET', 'HEAD'])
def client_direct_export(request):
    document, digest = source_document(ClientDirectRule.objects.all())
    if not document['rules']:
        return HttpResponse('没有已启用的直连规则。', status=409)
    response = HttpResponse(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':')).encode('utf-8'), content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="megabox-local-direct.json"'
    response['X-Content-SHA256'] = digest
    return response


@staff_required
@never_cache
@require_http_methods(['GET', 'HEAD'])
def client_direct_policy_export(request):
    """导出带正则作用域的审查清单；不直接修改设备。"""
    document, digest = policy_document(ClientDirectRule.objects.all())
    if not document['rules']:
        return HttpResponse('没有已启用的域名规则。', status=409)
    response = HttpResponse(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':')).encode('utf-8'), content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="megabox-client-routing-policy.json"'
    response['X-Content-SHA256'] = digest
    return response


@staff_required
@never_cache
@require_http_methods(['GET', 'HEAD'])
def client_proxy_export(request):
    document, digest = source_document(ClientDirectRule.objects.all(), action='proxy')
    if not document['rules']:
        return HttpResponse('没有已启用的代理规则。', status=409)
    response = HttpResponse(json.dumps(document, ensure_ascii=False, sort_keys=True,
                                       separators=(',', ':')).encode('utf-8'), content_type='application/json')
    response['Content-Disposition'] = 'attachment; filename="megabox-client-proxy.json"'
    response['X-Content-SHA256'] = digest
    return response


@login_required
def guide(request):
    if settings.WORKSPACE_V2:
        return render(request, 'portal/v2_guide.html')
    return render(request, 'portal/guide.html')
