"""业务服务：邀请码原子消费、账户隔离和受限订阅交付。"""
import hashlib
import json
import os
import re
import secrets
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac
from .models import AuditEvent, Invitation, Membership, RateBucket, SubscriptionGrant
from bridge.artifacts import available

DEVICES = {'windows': 'Windows / v2rayN', 'android': 'Android / sing-box', 'v2rayng': 'Android / v2rayNG'}
ARTIFACTS = {'windows': 'windows.txt', 'android': 'android.json', 'v2rayng': 'v2rayng.txt'}
LEGACY_LABELS = {'windows': 'Windows / v2rayN 节点', 'android': 'Android / sing-box 配置',
                 'v2rayng': 'Android / v2rayNG 节点', 'windows-rules': 'Windows 路由资源',
                 'v2rayng-routes': 'v2rayNG 路由资源', 'v2rayng-geosite': 'v2rayNG 域名资源',
                 'v2rayng-geoip': 'v2rayNG IP 资源'}
LEGACY_PATH = re.compile(r'/s/[A-Za-z0-9_-]{43}/([a-z0-9-]+)\Z')
LEGACY_ARTIFACTS = {
    'windows': 'windows', 'android': 'android', 'v2rayng': 'v2rayng',
    'windows-rules': 'windows-rules', 'v2rayng-routes': 'v2rayng-routes',
    'v2rayng-geosite': 'v2rayng-geosite', 'v2rayng-geoip': 'v2rayng-geoip',
}


def existing_admin_links():
    """只向管理员展示已有 P8 自用订阅；不把它冒充独立用户套餐。"""
    path = settings.LEGACY_LINKS_PATH
    try:
        if path is None or path.is_symlink() or not path.is_file() or path.stat().st_size > 8192:
            return []
        if os.name == 'posix' and path.stat().st_mode & 0o007:
            return []
        value = json.loads(path.read_text(encoding='utf-8'))
        if type(value) is not dict or not value.keys() <= LEGACY_LABELS.keys():
            return []
        links = []
        for device, url in value.items():
            if type(url) is not str or len(url) > 300:
                return []
            parsed = urlsplit(url)
            matched = LEGACY_PATH.fullmatch(parsed.path)
            if (parsed.scheme != 'https' or parsed.hostname != 'sub.relay.example.invalid' or parsed.port != 2053
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or not matched or matched[1] != LEGACY_ARTIFACTS[device]):
                return []
            links.append({'label': LEGACY_LABELS[device], 'url': url, 'kind': '已有订阅'})
            if device == 'windows-rules':
                links.append({'label': 'Windows / v2rayN 原生路由（从订阅 Url 导入）',
                              'url': url[:-len('windows-rules')] + 'windows-routing',
                              'kind': '原生路由链接'})
        return links
    except (OSError, ValueError, UnicodeError):
        return []


def audit(actor, action, subject='', result='OK'):
    AuditEvent.objects.create(actor=actor, action=action, subject=str(subject)[:80], result=result)


def invite_hash(code):
    return hashlib.sha256(code.encode('utf-8')).hexdigest()


def throttle(scope, identity, limit=10, seconds=900):
    # 不记录用户名、IP 或密码正文；只对固定时间窗口内的尝试计数。
    key = salted_hmac('rate-bucket', scope + ':' + identity).hexdigest()
    now = timezone.now()
    with transaction.atomic():
        row, _ = RateBucket.objects.select_for_update().get_or_create(key=key, defaults={'started_at': now})
        if row.started_at + timedelta(seconds=seconds) < now:
            row.started_at, row.attempts = now, 0
        if row.attempts >= limit:
            return False
        row.attempts += 1
        row.save(update_fields=['started_at', 'attempts'])
    return True


def issue_invitation(actor, values):
    code = secrets.token_urlsafe(24)
    with transaction.atomic():
        record = Invitation.objects.create(code_hash=invite_hash(code), label=values['label'], creator=actor,
            quota_bytes=int(values['quota_gb'] * 1_000_000_000), service_days=values['service_days'],
            expires_at=timezone.now() + timedelta(days=values['valid_days']), max_uses=values['max_uses'])
        audit(actor, 'invite_created', record.pk)
    return code


def register(form):
    with transaction.atomic():
        invitation = Invitation.objects.select_for_update().filter(code_hash=invite_hash(form.cleaned_data['invitation'])).first()
        now = timezone.now()
        if not invitation or invitation.revoked or invitation.expires_at <= now or invitation.uses >= invitation.max_uses:
            raise ValidationError('邀请码无效、已过期或已用完。')
        # 表单校验与真正保存之间再次在写事务中检查，不允许不同大小写抢占账号。
        from .forms import clean_username
        clean_username(form.cleaned_data['username'])
        user = form.save(commit=False)
        user.is_staff, user.is_superuser = False, False
        user.save()
        member = Membership.objects.create(user=user,
            quota_bytes=0 if settings.WORKSPACE_V2 else invitation.quota_bytes,
            service_days=invitation.service_days)
        # 待开通时不提前消耗套餐有效期，执行器开通成功后计算到期日。
        if not settings.WORKSPACE_V2:
            for device in DEVICES:
                SubscriptionGrant.objects.create(membership=member, device=device)
        invitation.uses += 1
        invitation.save(update_fields=['uses'])
        audit(user, 'registered', user.pk)
    return user


def token_for(grant):
    return signing.Signer(salt='megabox-subscription-v1').sign_object(
        {'member': str(grant.membership.public_id), 'device': grant.device, 'version': grant.version})


def grant_from_token(token, device):
    if device not in DEVICES or len(token) > 512:
        raise ValidationError('订阅不可用。')
    try:
        value = signing.Signer(salt='megabox-subscription-v1').unsign_object(token)
        if value['device'] != device:
            raise ValueError()
        grant = SubscriptionGrant.objects.select_related('membership__user').get(
            membership__public_id=value['member'], device=device, version=value['version'], enabled=True)
    except (signing.BadSignature, ValueError, KeyError, TypeError, SubscriptionGrant.DoesNotExist):
        raise ValidationError('订阅不可用。')
    if not grant.membership.eligible:
        raise ValidationError('订阅不可用。')
    return grant


def artifact_path(grant):
    # 兼容页面的存在判断；不再返回可以被换写的文件路径。
    return True if available(settings.ARTIFACT_ROOT, grant.membership.public_id, grant.device) else None
