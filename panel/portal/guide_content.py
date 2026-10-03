"""登录后指南的结构化内容；历史操作参考不充当当前实机验收。"""
from copy import deepcopy
import unicodedata


CONTENT_REVISION = '2026-10-02'
CLIENT_FILTERS = (
    {'id': 'windows', 'label': 'Windows · v2rayN'},
    {'id': 'v2rayng', 'label': 'Android · v2rayNG'},
    {'id': 'android', 'label': 'Android · SFA'},
    {'id': 'router', 'label': '路由器'},
)


def section(title, *, paragraphs=(), steps=(), notes=()):
    return {'title': title, 'paragraphs': list(paragraphs),
            'steps': list(steps), 'notes': list(notes)}


def article(identifier, title, summary, category, sections, *, clients=('all',),
            keywords=(), historical=(), unsupported=False):
    software_specific = clients != ('all',)
    state = 'unsupported' if unsupported else 'not_tested' if software_specific else 'not_applicable'
    return {'id': identifier, 'title': title, 'summary': summary, 'category': category,
            'clients': list(clients), 'keywords': list(keywords), 'sections': sections,
            'verification': {'state': state, 'checked_at': None, 'note':
                '尚无适配交付，请等待明确的软件与资源说明。' if unsupported else
                '以下菜单来自历史版本记录；当前安装版本的菜单、导入与更新仍需实机核对。' if software_specific else
                '这是通用操作说明；具体服务是否就绪以服务详情的实际状态为准。'},
            'versions': {'software': None, 'core': None, 'historical': list(historical)}}


ARTICLES = (
    article('first-import', '第一次使用：从我的服务开始',
        '进入已开通的服务，选择设备、系统和软件，再导入匹配资源。', '开始使用', [
        section('先准备，再导入', steps=(
            '登录后进入“我的服务”，打开管理员已为你开通的那份服务。没有服务时，请联系管理员开通。',
            '在服务详情依次选择设备、系统与实际安装的软件。此处选择软件，不需要选择线路或填写设备名称。',
            '查看该软件的资源状态与导入说明。只有显示可用的资源才用于导入；未准备好的资源请等待管理员处理。',
            '保留此前可用的配置，按软件指南导入对应地址或文件，再在客户端启动、检查实际应用。',
        )),
        section('切换软件不会新开套餐', paragraphs=(
            '同一服务的节点订阅、完整配置和规则资源属于同一份服务。切换软件或重复获取，不应增加额度、续期或清空用量。',
            '客户端里可以使用管理员已授权的节点；具体有哪些节点取决于该服务的授权与交付，不承诺固定数量。',
        ), notes=('面板不能替你开启电脑 TUN、系统代理或手机 VPN。启动与停止要在客户端完成。',)),
    ], keywords=('首次导入', '注册', '开通', '获取配置', '手机', '电脑', '设备', '系统')),
    article('resource-differences', '节点、完整配置、路由和规则有什么区别？',
        '先确认资源类型，避免把路由链接放进节点订阅栏。', '理解资源', [
        section('四种资源分别做什么', paragraphs=(
            '节点订阅提供可连接的节点与凭据。节点能出现，不代表路由、DNS 与规则资源都已设置完成。',
            '完整配置把节点、DNS、路由和引用的规则组合起来。SFA 使用为它生成的完整配置，不能把普通节点列表直接当作完整配置。',
            '路由方案决定匹配顺序和最终动作；域名或 IP 规则资源提供被路由引用的名单。更新名单与改变路由顺序可能需要不同入口。',
            'DNS 决定域名如何解析，路由决定连接去哪里；二者需要配合。不要用全系统 DNS 改动代替客户端适配。',
        )),
        section('代理与直连也有区别', paragraphs=(
            '客户端本地直连使用设备本地网络；服务器直出仍先连接服务器，再从服务器出口访问。二者的身份、DNS 和流量路径不同。',
            '规则模式按已选路由判断；全局模式通常把进入客户端的业务送往当前节点，仍取决于当前节点的实际出口。',
            '要求使用指定出口的服务若出口或其 DNS 失败，应停止该请求，不能自动改用另一出口或本地网络。',
        )),
    ], keywords=('节点订阅', '完整配置', '路由方案', '规则资源', 'DNS', '直连', '全局', '分流')),
    article('update-rules', '规则已保存，怎样让设备用上新内容？',
        '发布、客户端下载、客户端应用是三个独立步骤。', '日常更新', [
        section('按影响范围更新', steps=(
            '先由管理员确认资源已经发布成功。仅保存候选规则，不代表已经发布到下载资源。',
            '在服务详情查看对应软件的资源说明，确认变更影响节点、原生路由、名单资源还是完整配置。',
            '在客户端更新受影响的资源：节点变化更新节点订阅；路由变化更新原生路由；名单变化更新引用的资源；SFA 更新远程完整配置及其规则。',
            '保存成功后重新加载客户端服务或停止后重新启动，再关闭旧连接并重新打开实际应用进行检查。',
        )),
        section('成功与失败怎样判断', paragraphs=(
            '资源发布成功，表示服务器已准备新内容；客户端下载成功，表示设备已取得内容；客户端应用成功，表示重新加载时没有配置错误。三者不能互相代替。',
            '普通内容更新沿用同一稳定链接。只有链接泄露、明确要求重置等情况才需要更换地址。',
            '更新失败时保留最后可用文件与配置，不保存空规则，也不先删除活动配置。',
        ), notes=('订阅更新不会替换客户端程序或核心。升级软件与核心前另行核对兼容性。',)),
    ], keywords=('更新规则', '更新路由', '保存规则', '不生效', '发布', '下载', '应用', '更新订阅')),
    article('windows-v2rayn', 'Windows · v2rayN：导入节点与原生路由',
        '节点订阅与原生路由分别导入；日常路由更新使用客户端入口。', '软件操作', [
        section('添加或更新节点订阅', steps=(
            '在本人服务选择 Windows 与 v2rayN，复制节点订阅资源的地址。保留原来可工作的分组。',
            '历史 v2rayN 7.24.9 的入口为“订阅分组设置”：添加分组，名称可自定，填入节点订阅地址并保存。已有该服务的分组时更新原项。',
            '在订阅菜单执行“更新订阅（通过代理）”，确认没有下载错误，并检查该服务实际授权的节点。',
        )),
        section('原生路由的导入与以后更新', steps=(
            '复制为 v2rayN 生成的“原生路由”资源地址，先在客户端备份当前路由。不要把原生路由地址填进节点订阅栏。',
            '历史 7.24.9 的菜单参考：“设置 → 路由设置 → 预定义规则集列表”，打开准备使用的项目，把地址填入“可选地址（Url）”。',
            '使用“从订阅 Url 中导入规则”。资源明确是完整替换方案时，在“是否追加规则？”中选“否”；未说明替换范围时先向管理员核对。',
            '保存编辑窗与路由设置，回到主窗口“重启服务”，确认选择正确的路由模式。以后更新同一原生路由时继续使用这个入口。',
        ), notes=('原生路由已经包含完整规则时，不再另外运行任务计划或旧规则下载脚本。节点地址或凭据变化才需要更新节点订阅。',)),
        section('系统代理、TUN 与停止', paragraphs=(
            '系统代理和 TUN 控制哪些应用进入客户端；路由模式控制进入后的流量怎样处理。按已有适配启用，不能把 TUN 开关当作已验证的防泄漏保护。',
            '只停止系统代理时使用“清除系统代理”。若 TUN 仍开启，还需关闭原生 TUN；完全退出时使用托盘“退出”。强制结束进程不等于正常退出。',
        )),
    ], clients=('windows',), keywords=('Windows', 'v2rayN', '订阅分组设置', '原生路由', '可选地址', '重启服务', 'TUN', '系统代理'),
        historical=({'software': 'v2rayN 7.24.9', 'core': 'sing-box 1.13.21', 'date': '2026-09-26',
                     'note': '本地历史记录包含节点菜单与原生路由入口；该原生路由首次实机导入当时仍待验。'},)),
    article('android-v2rayng', 'Android · v2rayNG：节点、路由与资源分开导入',
        '使用节点订阅；按交付说明导入小型路由和对应 DAT 资源。', '软件操作', [
        section('导入节点订阅', steps=(
            '先停止 SFA 或其他 VPN 客户端。在本人服务选择 Android 与 v2rayNG，复制对应节点订阅地址。',
            '历史 v2rayNG 2.2.6 的入口为侧栏“订阅分组设置 → 加号”：名称自定，填入地址并保存，返回主页更新该订阅。',
            '确认节点列表属于这份服务。不要导入 Windows 专用策略组或 SFA 完整 JSON。',
        )),
        section('导入路由与名单资源', steps=(
            '如果交付包含 DAT 名单，先备份旧文件与路由。历史 2.2.6 可在“资源文件 → 添加 → 添加链接”登记资源地址。别名必须与路由引用的文件名一致。',
            '在资源页使用“下载文件”，逐项确认资源下载成功且大小非零。这个入口可能批量更新已登记资源，不能用节点订阅更新代替。',
            '为 v2rayNG 交付的小型路由 JSON 使用“路由设置 → 从剪贴板导入规则集”。复制完整文件内容，从 [ 到 ]，不是文件下载地址。',
            '核对引用的资源存在、规则顺序没有被旧配置覆盖。DNS 按这份交付的适配说明填写，再启动连接并确认 Android VPN 权限。',
        ), notes=(
            'DNS 地址必须与当前服务设计一致；不要把历史示例直接当作当前 DNS。只接受 IP 的 VPN DNS 输入框不能填协议格式地址。',
            '下载失败时保留可用文件。修改同名资源地址可能影响旧本地文件，应先停止客户端、备份并确认下载完成；必要时用“添加文件”恢复离线备份。',
        )),
        section('再次更新与使用', paragraphs=(
            '节点更新走订阅分组；DAT 更新走资源页“下载文件”；路由顺序变更重新导入对应路由。更新后停止再启动连接。',
            '自动组若提供，应只包含具有相同出口要求的授权节点。具体入口和协议支持按实际软件版本核对，不假设订阅自动建立分组。',
            '若启用了分应用接管，未被选中的应用不会自动受这份 VPN 规则控制。需要整机使用时按已核对的适配确认范围。',
        )),
    ], clients=('v2rayng',), keywords=('Android', 'v2rayNG', 'DAT', '资源文件', '添加链接', '下载文件', '剪贴板', '分应用', 'DNS'),
        historical=({'software': 'v2rayNG 2.2.6', 'core': None, 'date': '2026-09-22',
                     'note': '历史有单节点与修正 DNS 后的用户反馈；其他节点、原生组、完整路由与切网未由该反馈一并验收。'},)),
    article('android-sfa', 'Android · SFA：添加与更新远程完整配置',
        '使用 sing-box for Android 对应的完整配置，更新后重新加载。', '软件操作', [
        section('首次添加远程配置', steps=(
            '停止 v2rayNG 或其他 VPN 客户端。在本人服务选择 Android 与 sing-box / SFA，复制完整配置资源地址。',
            '历史 SFA 核心 1.14.1 的入口参考：配置页“新增 / 添加配置 / +”，类型选“远程 / Remote”，名称自定，填入地址并保存、更新。',
            '选择这份配置启动，按 Android 提示允许 VPN。配置内显示哪些节点与规则取决于该服务的授权与适配。',
            '更新后停止再启动，让客户端重新加载配置。短测实际应用和国内网页，再观察锁屏、切网与后台表现。',
        )),
        section('以后怎样更新', paragraphs=(
            '在配置页打开原来的远程配置，执行更新。不要把节点列表或 v2rayNG 路由 JSON 粘贴到完整配置入口。',
            '完整配置可以引用独立规则集；按其交付说明检查更新结果。配置下载成功不代表引用的每个规则都已加载成功。',
            '出现闪退、发热或无法联网时，先停止客户端并保留错误信息；切换回已工作的配置前，先确认只有一个 VPN 在接管。',
        )),
        section('停止后仍不能联网', paragraphs=(
            '在 Android 系统设置搜索“VPN”，检查“始终开启 VPN”和“阻止未使用 VPN 的连接”的实际状态。这些系统开关由你在手机操作，网页无法读取或修改。',
            '调整任何附加开关前先了解它的用途；当前指南不把某个开关组合当作已验证的防泄漏保证。',
        )),
    ], clients=('android',), keywords=('SFA', 'sing-box', 'Android', '远程配置', 'Remote', 'VPN', '闪退', '发热'),
        historical=({'software': None, 'core': 'sing-box 1.14.1', 'date': '2026-09-22',
                     'note': '历史记录有远程配置导入流程；应用版本号、当前手机菜单与完整实机表现仍需核对。'},)),
    article('troubleshooting', '连接或更新失败：先定位哪一步出错',
        '按服务状态、下载、加载、实际应用逐步检查，保留可用配置。', '遇到问题', [
        section('先检查这四步', steps=(
            '服务：在“我的服务”检查是否已开通、是否到期或停用。无服务与软件导入失败是不同问题。',
            '下载：检查客户端是否提示下载失败、链接失效或权限错误。不要公开完整订阅地址。',
            '加载：检查配置语法、规则资源是否存在，以及当前选择的路由。更新失败保留最后可用配置，不关闭证书校验。',
            '实际应用：关闭旧连接后重新尝试指定应用。延迟数字、TCP 可连接或一次 IP 查询不能证明所有应用、UDP 和 IPv6 都正常。',
        )),
        section('个别应用不同或网络变化', paragraphs=(
            '确认系统代理、TUN、VPN 与分应用设置是否覆盖该应用。应用自带 DNS、缓存与已有连接也可能影响结果。',
            '指定出口不可用时不要临时改为无条件本地直连。把错误时间、软件与核心版本、报错文字和发生场景发给管理员，帮助区分入口、DNS、规则和应用问题。',
        ), notes=('截图或日志先去除完整订阅地址、节点凭据、账号密码和令牌。无需提供钱包助记词或金融 API 密钥。',)),
    ], keywords=('打不开', '无法连接', '下载失败', '更新失败', '延迟', 'DNS', 'IPv6', 'UDP', '切网', '日志')),
    article('account-service', '账号、服务与密码：分别在哪里处理？',
        '账号可登录不代表已经开通服务；密码与订阅链接是不同凭据。', '账号与用量', [
        section('账号与服务', paragraphs=(
            '邀请码注册后可以登录，但不会自动获得服务、节点或额度。管理员在你的账号下开通后，服务才会出现在“我的服务”。',
            '每份独立服务有各自的额度、到期时间和重置时间；同一服务的多个客户端格式共用这些信息。',
            '需要开通、调整额度或续期时联系管理员，说明具体服务。不要把添加软件当成另开服务。',
        )),
        section('修改密码与账号异常', paragraphs=(
            '通过“账号设置”进入本人密码修改流程，按页面显示的密码规则填写。不要把账号密码粘贴到订阅地址或客户端配置中。',
            '若不能登录，核对账号、密码和账号状态，再联系管理员处理。告知错误时间与提示即可，无需发送密码。',
            '修改登录密码、普通内容更新与重置订阅链接是不同操作；改密码不能作为旧节点身份已经撤销的证据。',
        )),
    ], keywords=('账号', '密码', '注册', '邀请码', '开通', '续期', '额度', '账号设置')),
    article('usage-billing', '查看流量、重置时间与到期时间',
        '看具体服务的套餐数据；未知计量不能理解为零用量。', '账号与用量', [
        section('到服务详情查看', paragraphs=(
            '“我的服务”与服务详情展示总额度、已用、剩余、下次重置和到期。GB 使用十进制：1 GB = 1,000,000,000 字节。',
            '套餐扣减按当时生效的授权倍率计算；原始传输量与倍率扣减量是不同口径。同一连接的中间转发不应再次当作个人用量。',
            '客户端本机流量统计、服务器网卡监测和供应商账单都不能直接替代个人服务的套餐余额。',
        )),
        section('时间与统计质量', paragraphs=(
            '下次流量重置与服务到期是两件事。修改下一次重置时间本身不会清空当前用量，也不会续期。',
            '显示“暂无数据”、计量缺口或等待核算，表示存在未知范围。不能理解为已用 0 GB 或完整剩余；已确认记录也不能冒充完整历史。',
            '查看统计时留意时间范围、最后更新时间与统计状态。不同服务的剩余不宜直接相加为一个共同余额。',
        )),
    ], keywords=('流量', '用量', 'GB', '重置日', '下次重置', '到期', '剩余', '倍率', '暂无数据', '统计')),
    article('subscription-reset', '内容更新与重置订阅链接怎样区分？',
        '普通更新沿用链接；泄露后由管理员处理该服务的链接与身份。', '日常更新', [
        section('普通更新', paragraphs=(
            '管理员发布节点或规则新内容后，客户端按软件指南更新原有资源，再重新加载。普通发布不需要每次换链接。',
        )),
        section('链接泄露或明确要求重置', paragraphs=(
            '把泄露情况告诉管理员，请其处理这份服务的链接与接入身份。新页面未提供操作时，不要寻找不存在的重置按钮。',
            '管理员提供完成结果与新地址后，更新使用这份服务的各设备资源，并检查新连接。保留必要备份，但不要继续公开或转发旧地址。',
            '旧下载地址失效、旧节点不能建立新连接与旧存量连接结束是不同检查项；仅看到新链接不能证明所有旧接入都已经撤销。',
        )),
    ], keywords=('重置链接', '轮换', '泄露', '泄漏', '凭据', '旧链接', '更新内容')),
    article('router-readiness', '路由器：先确认具体型号与适配',
        '路由器暂未交付可用的软件配置，先核对设备和能力。', '软件操作', [
        section('准备设备资料', paragraphs=(
            '先向管理员说明路由器型号、系统和可运行的客户端版本。手机或电脑的资源不能直接视为路由器适配。',
            '在明确资源格式、DNS、IPv6、UDP 和接管范围并完成设备验证前，不提供示例下载地址或通用安装命令。',
            '可以先用已适配的手机或电脑软件；这不会把路由器自动变成已支持设备。',
        )),
    ], clients=('router',), keywords=('路由器', '型号', '固件', '适配'), unsupported=True),
)


def normalized(value):
    """支持全角输入、大小写和多空白，不执行正则或用户代码。"""
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def search_text(value):
    parts = [value['title'], value['summary'], value['category'], *value['keywords']]
    for value_section in value['sections']:
        parts.extend([value_section['title'], *value_section['paragraphs'],
                      *value_section['steps'], *value_section['notes']])
    return normalized(' '.join(parts))


def guide_catalog(query='', client=None):
    terms = normalized(query).split()
    values = [value for value in ARTICLES
              if (client is None or 'all' in value['clients'] or client in value['clients'])
              and all(term in search_text(value) for term in terms)]
    return {'articles': deepcopy(values), 'query': query, 'client': client, 'total': len(values),
            'content_revision': CONTENT_REVISION, 'client_filters': deepcopy(list(CLIENT_FILTERS))}


def client_guide_payload(client_id):
    """保留旧软件指南字段，主 API 可直接委托而无需引用指南 API。"""
    labels = {value['id']: value['label'] for value in CLIENT_FILTERS}
    if client_id not in labels:
        return None
    values = guide_catalog(client=client_id)['articles']
    specific = next(value for value in values if client_id in value['clients'])
    steps = [] if client_id == 'router' else [
        {'title': value_section['title'], 'body': '\n'.join([
            *value_section['paragraphs'], *value_section['steps'], *value_section['notes']])}
        for value_section in specific['sections']
    ]
    return {'client_id': client_id, 'title': labels[client_id] + ' 使用指南',
            'verification': specific['verification']['state'], 'software_version': None, 'core_version': None,
            'steps': steps, 'update_status': {'published': 'unknown', 'downloaded': 'unknown', 'applied': 'unknown'},
            'limitations': [specific['verification']['note'],
                            '发布、下载和客户端应用是三个独立状态；下载成功不代表已生效。'],
            'articles': values, 'versions': deepcopy(specific['versions']), 'content_revision': CONTENT_REVISION}
