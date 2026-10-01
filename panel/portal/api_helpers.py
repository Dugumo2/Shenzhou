"""服务查询的安全只读投影；不创建账期、交付或接入身份。"""
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .metering_quality import assess_metering
from .models import Entitlement, Membership, SubscriptionGrant


CLIENTS = (
    {'id': 'windows', 'name': 'v2rayN', 'device': 'computer', 'os': 'Windows',
     'resources': [{'kind': 'nodes', 'label': '节点订阅'}, {'kind': 'routing', 'label': '原生路由资源'}]},
    {'id': 'v2rayng', 'name': 'v2rayNG', 'device': 'phone', 'os': 'Android',
     'resources': [{'kind': 'nodes', 'label': '节点订阅'}, {'kind': 'routing', 'label': '路由资源'},
                   {'kind': 'geosite', 'label': '域名资源'}, {'kind': 'geoip', 'label': 'IP 资源'}]},
    {'id': 'android', 'name': 'sing-box / SFA', 'device': 'phone', 'os': 'Android',
     'resources': [{'kind': 'configuration', 'label': '完整配置'}, {'kind': 'rules', 'label': '规则集'}]},
    {'id': 'router', 'name': '路由器', 'device': 'router', 'os': None, 'resources': []},
)

STATE_LABELS = {'pending': '等待开通', 'active': '已应用', 'simulated': '隔离验证，未上线',
                'disabled': '已停用', 'suspended': '已暂停', 'enforcement_pending': '等待服务器停用',
                'suspension_pending': '等待服务器暂停', 'failed': '应用失败', 'blocked': '等待条件满足',
                'resuming': '等待恢复', 'disabling': '正在停用', 'resetting': '正在重置'}


def iso(value):
    return value.isoformat() if value is not None else None


def byte_string(value):
    """以十进制字符串保留大整数精度；无可靠证据的数值保持未知。"""
    return str(value) if type(value) is int and 0 <= value <= 2**63 - 1 else None


def legacy_services():
    """邀请码注册的零额度待开通占位记录不是服务。"""
    return Membership.objects.annotate(has_grants=Exists(
        SubscriptionGrant.objects.filter(membership_id=OuterRef('pk')))).filter(
            Q(quota_bytes__gt=0) | Q(has_grants=True) | ~Q(status='pending') | Q(expires_at__isnull=False))


def compatibility_for(user_id, has_entitlement):
    unresolved = has_entitlement and legacy_services().filter(user_id=user_id).exists()
    return {'state': 'mapping_required' if unresolved else 'clear',
            'message': '部分服务资料尚需管理员核对；暂不合并或增加额度。' if unresolved else None}


def service_queryset():
    return Entitlement.objects.select_related('user')


def project_service(record, source_type, *, now=None, detail=False, administrator=False):
    now = now or timezone.now()
    gap = False
    cycle = None
    if source_type == 'entitlement':
        # 直接查询已保存的边界，不能调用兼做建期的 current_cycle。
        cycles = list(record.cycles.filter(starts_at__lte=now, ends_at__gt=now).order_by('-starts_at')[:2])
        cycle = cycles[0] if len(cycles) == 1 else None
        gap = record.metering_gap or len(cycles) > 1
        observed = record.usage_updated_at
        metering = assess_metering(record, cycle, now)
        gap = gap or metering['quality'] == 'gap'
        quality = 'gap' if gap else 'measured' if metering['quality'] == 'fresh' else metering['quality']
        used = cycle.used_bytes if metering['usable'] and not gap else None
        raw = cycle.raw_bytes if metering['usable'] and not gap else None
        applied_quota = record.applied_snapshot.get('quota_bytes') if record.applied_revision else None
        quota = applied_quota if byte_string(applied_quota) is not None else record.quota_bytes
        quota_state = 'applied' if byte_string(applied_quota) is not None else 'configured'
        state, enabled = record.state, record.enabled
        is_applied = record.state == 'active' and record.applied_revision == record.revision
        application = {'state': 'isolated' if state == 'simulated' else 'applied' if is_applied else
                       'failed' if state == 'failed' else 'blocked' if state == 'blocked' else 'waiting',
                       'desired_revision': record.revision, 'applied_revision': record.applied_revision}
        next_reset = cycle.ends_at if cycle else None
    else:
        # 旧链尚未完成历史计量归属核验，不借用另一条链的账期或用量。
        quota, used, raw, observed, quality = record.quota_bytes, None, None, record.usage_updated_at, 'unknown'
        quota_state, state, enabled = 'configured', record.status, record.status != 'suspended'
        is_applied = record.provisioning_state == 'applied'
        application = {'state': 'verification_required', 'desired_revision': record.revision,
                       'applied_revision': None}
        next_reset = None
    expires = record.expires_at
    remaining = (max(0, quota - used) if quality == 'measured' and quota_state == 'applied'
                 and used is not None and byte_string(quota) is not None else None)
    if not record.user.is_active:
        message = '账号已停用，请联系管理员。'
    elif not enabled or state in ('disabled', 'suspended', 'disabling', 'suspension_pending', 'enforcement_pending'):
        message = '服务已暂停或正在停用，请联系管理员。'
    elif expires and expires <= now:
        message = '服务已到期，请联系管理员续期。'
    elif gap:
        message = '计量存在缺口，请等待管理员核对。'
    elif source_type == 'membership':
        message = '此服务的资源尚未迁入当前交付入口，请联系管理员核对。'
    elif state == 'simulated':
        message = '当前仅完成隔离验证，尚未提供可用交付。'
    elif not is_applied or expires is None:
        message = '服务尚未完成应用，请等待管理员开通。'
    elif remaining == 0:
        message = '本期额度已用完，请等待下次重置与恢复核验。'
    else:
        message = None
    blocked = message is not None
    has_resources = (record.subscriptions.exists() if source_type == 'entitlement' else record.grants.exists())
    delivery = {'state': 'blocked' if blocked else 'verification_required' if has_resources else 'not_prepared',
                'message': message or ('已有资源尚需核验此入口的交付权限，请联系管理员。' if has_resources
                                        else '交付资源尚未准备好，请联系管理员。'), 'download_url': None}
    usage_messages = {'measured': '当前账期已取得计量样本；实际核心完整能力仍须单独验收。',
                      'stale': '显示上次已记录用量；统计已过期，剩余待核算。',
                      'gap': '计量或账期存在缺口，当前用量和剩余待核算。',
                      'unknown': '暂无可靠统计，当前用量和剩余待核算。'}
    result = {'id': str(record.public_id), 'source_type': source_type, 'name': '神舟云',
              'quota_bytes': byte_string(quota), 'quota_state': quota_state,
              'used_bytes': byte_string(used), 'raw_bytes': byte_string(raw), 'remaining_bytes': byte_string(remaining),
              'next_reset_at': iso(next_reset), 'expires_at': iso(expires), 'state': state, 'enabled': enabled,
              'status_label': '已到期' if expires and expires <= now else STATE_LABELS.get(state, '待核验'),
              'usage': {'quality': quality, 'updated_at': iso(observed), 'message': usage_messages[quality]},
              'application': application, 'delivery': delivery,
              'actions': {'billing': administrator and source_type == 'entitlement', 'quota': False, 'renew': False,
                          'grants': False, 'enable': False, 'reset': False}}
    if detail:
        result['compatibility'] = compatibility_for(record.user_id, source_type == 'entitlement')
        result['clients'] = [{'id': client['id'], 'delivery': dict(delivery)} for client in CLIENTS]
        result['clients'][-1]['delivery'] = {'state': 'blocked', 'message': '路由器适配尚未验收。', 'download_url': None}
    return result


def client_catalog():
    return [dict(client, verification='unsupported' if client['id'] == 'router' else 'not_tested',
                 software_version=None, core_version=None,
                 guide_url='/api/v1/catalog/clients/' + client['id'] + '/guide',
                 reason='尚无已验收的路由器适配。' if client['id'] == 'router'
                     else '此入口尚未完成实际软件版本、菜单和导入更新流程核验。') for client in CLIENTS]
