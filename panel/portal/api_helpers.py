"""服务查询的安全只读投影；不创建账期、交付或接入身份。"""
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from .metering_quality import assess_metering
from .legacy_binding import legacy_service_memberships, verified_legacy_bindings
from .models import Entitlement, LegacyServiceBinding


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
    return legacy_service_memberships()


def visible_legacy_services():
    """仅显式核验的独立旧服务可与权益并列；坏绑定不得退回未绑定路径。"""
    return legacy_services().annotate(
        has_binding=Exists(LegacyServiceBinding.objects.filter(membership_id=OuterRef('pk'))),
        independent_verified=Exists(verified_legacy_bindings().filter(
            membership_id=OuterRef('pk'), entitlement__isnull=True)),
        has_entitlement=Exists(Entitlement.objects.filter(user_id=OuterRef('user_id'))),
    ).filter(Q(has_binding=False, has_entitlement=False) | Q(independent_verified=True))


def compatibility_for(user_id, has_entitlement):
    members = legacy_services().filter(user_id=user_id)
    has_target = has_entitlement or Entitlement.objects.filter(user_id=user_id).exists()
    unresolved_members = members.exclude(pk__in=verified_legacy_bindings().values('membership_id'))
    if not has_target:
        unresolved_members = unresolved_members.filter(legacy_binding__isnull=False)
    unresolved = unresolved_members.exists()
    return {'state': 'mapping_required' if unresolved else 'clear',
            'message': '部分服务资料尚需管理员核对；暂不合并或增加额度。' if unresolved else None}


def service_queryset():
    return Entitlement.objects.select_related('user')


def service_facts(record, source_type, now):
    """只从现有记录核对状态依据；业务筛选与页面投影复用同一份事实。"""
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
        is_applied = (record.state == 'active' and record.applied_revision > 0
                      and record.applied_revision == record.revision)
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
    return {'quota': quota, 'used': used, 'raw': raw, 'observed': observed, 'quality': quality,
            'quota_state': quota_state, 'state': state, 'enabled': enabled, 'is_applied': is_applied,
            'application': application, 'next_reset': next_reset, 'expires': expires,
            'remaining': remaining}


def service_business_status(record, source_type, facts, now):
    """业务主状态不能仅由应用回执决定；未知用量不证明耗尽或有效。"""
    state, expires = facts['state'], facts['expires']
    if not record.user.is_active:
        return 'account_disabled', '账号已停用', '账号已停用，请联系管理员。'
    if state in ('disabled', 'suspended', 'disabling', 'suspension_pending', 'enforcement_pending'):
        return state, STATE_LABELS[state], '服务已暂停或正在停用，请联系管理员。'
    if not facts['enabled']:
        return 'disabled', '已停用', '服务已停用，请联系管理员。'
    if expires and expires <= now:
        return 'expired', '已到期', '服务已到期，请联系管理员续期。'
    if facts['remaining'] == 0:
        return 'exhausted', '本期额度已用完', '本期额度已用完，请等待下次重置与恢复核验。'
    if facts['quality'] == 'gap':
        return 'metering_gap', '用量待核算', '计量或账期存在缺口，请等待管理员核对。'
    if source_type == 'membership':
        return ('verification_required', '待核验',
                '此服务的资源尚未迁入当前交付入口，请联系管理员核对。')
    if state == 'simulated':
        return 'simulated', STATE_LABELS[state], '当前仅完成隔离验证，尚未提供可用交付。'
    if not facts['is_applied']:
        business_state = state if state in STATE_LABELS and state != 'active' else 'pending'
        label = '等待应用' if state == 'active' else STATE_LABELS.get(state, '等待应用')
        return business_state, label, '服务尚未完成应用，请等待管理员核对。'
    if expires is None:
        return 'verification_required', '有效期待核验', '服务有效期尚未确认，请联系管理员核对。'
    if facts['quota_state'] != 'applied':
        return 'verification_required', '额度待核验', '服务额度尚未完成应用核验，请联系管理员。'
    if facts['quality'] != 'measured':
        return ('metering_' + facts['quality'], '用量待核算',
                '统计已过期或尚无可靠计量证据，请等待管理员核对。')
    # 这里只确认登记状态和已持久样本，不能作为核心限额、撤销或真实交付验收。
    return 'active', STATE_LABELS['active'], None


def matching_service_ids(query, source_type, requested_state, now):
    """分块核对派生状态，只保留索引主键；不物化所有用户或全部账本。"""
    matches = []
    for record in query.select_related('user').iterator(chunk_size=100):
        facts = service_facts(record, source_type, now)
        if requested_state == 'metering_gap':
            # 缺口筛选是计量维度，已到期服务也可能同时存在缺口。
            match = facts['quality'] == 'gap'
        else:
            match = service_business_status(record, source_type, facts, now)[0] == requested_state
        if match:
            matches.append(record.pk)
    return matches


def project_service(record, source_type, *, now=None, detail=False, administrator=False):
    now = now or timezone.now()
    facts = service_facts(record, source_type, now)
    business_state, status_label, message = service_business_status(record, source_type, facts, now)
    quota, used, raw = facts['quota'], facts['used'], facts['raw']
    remaining, quality = facts['remaining'], facts['quality']
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
              'quota_bytes': byte_string(quota), 'quota_state': facts['quota_state'],
              'used_bytes': byte_string(used), 'raw_bytes': byte_string(raw), 'remaining_bytes': byte_string(remaining),
              'next_reset_at': iso(facts['next_reset']), 'expires_at': iso(facts['expires']),
              'state': facts['state'], 'enabled': facts['enabled'],
              'business_state': business_state, 'status_label': status_label,
              'usage': {'quality': quality, 'updated_at': iso(facts['observed']), 'message': usage_messages[quality]},
              'application': facts['application'], 'delivery': delivery,
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
