"""仅本机双门禁的合成资源交付；不消费真实凭据，不写接入身份。"""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.cache import never_cache

from bridge.client_delivery import DeliveryError, _uri, encoded, publish
from .api import endpoint, error, json_body, success
from .client_rules import policy_document, source_document, validate_rule
from .models import ClientDirectRule, Entitlement


CLIENTS = frozenset({'windows', 'v2rayng', 'android'})
NAMESPACE = 'candidate-demo-v1'
LIMIT = 16 * 1024 * 1024
NOTICE = '本机合成演示资源，节点不可连接。仅验证获取和下载，不能作为真实代理或客户端自动更新订阅。'
LABELS = {'subscription': '演示完整配置', 'routing': '候选原生路由',
          'rules-direct': '候选直连规则集', 'rules-proxy': '候选代理规则集', 'policy': '候选规则来源'}


class CandidateError(Exception):
    """错误只保留公开分类，避免把本地路径或产物内容返回浏览器。"""


def _enabled():
    return settings.PANEL_LIVE is False and getattr(settings, 'CANDIDATE_DEMO_DATA', False) is True


def _service(actor, public_id):
    try:
        value = uuid.UUID(str(public_id))
    except (ValueError, TypeError, AttributeError):
        return None
    return Entitlement.objects.filter(user=actor, public_id=value).first()


def _blocked(record):
    now = timezone.now()
    if not record.enabled or record.state not in {'active', 'simulated'}:
        return '服务尚未开通或已停用，不能获取资源。'
    if record.applied_revision != record.revision or record.metering_gap:
        return '服务应用或计量存在待核验项，不能获取资源。'
    if record.expires_at is None or record.expires_at <= now:
        return '服务有效期尚未确认或已经到期。'
    cycle = record.cycles.filter(starts_at__lte=now, ends_at__gt=now).first()
    if cycle is None:
        return '当前账期尚未核验，不能获取资源。'
    if record.quota_bytes <= 0 or cycle.used_bytes >= record.quota_bytes:
        return '服务额度已经耗尽，不能获取资源。'
    return ''


def _checked(path):
    path = Path(path).absolute()
    if any(item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction())
           for item in (path, *path.parents)):
        raise CandidateError('unsafe_path')
    return path


def _root(client_id=None):
    root = _checked(Path(settings.ARTIFACT_ROOT) / NAMESPACE)
    return _checked(root / client_id) if client_id is not None else root


def _directory(service_id, client_id):
    return _checked(_root(client_id) / 'subscriptions' / str(service_id) / '1')


def _read(path, limit=LIMIT):
    path = _checked(path)
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise CandidateError('artifact_invalid')
    content = path.read_bytes()
    if not 0 < len(content) <= limit:
        raise CandidateError('artifact_invalid')
    return content


def _policy_rows(policy):
    if (type(policy) is not dict or set(policy) != {'schema_version', 'rules'}
            or policy['schema_version'] != 2 or type(policy['rules']) is not list
            or len(policy['rules']) > 200):
        raise CandidateError('policy_invalid')
    rows, seen = [], set()
    for entry in policy['rules']:
        if type(entry) is not dict or set(entry) != {'action', 'kind', 'value', 'scope'}:
            raise CandidateError('policy_invalid')
        value, scope = validate_rule(entry['kind'], entry['value'], entry['scope'], entry['action'])
        if value != entry['value'] or scope != entry['scope'] or (entry['kind'], value) in seen:
            raise CandidateError('policy_invalid')
        seen.add((entry['kind'], value))
        rows.append(SimpleNamespace(enabled=True, outbound=entry['action'], kind=entry['kind'],
                                    value=value, scope_domain=scope))
    canonical, _ = policy_document(rows)
    if canonical != policy:
        raise CandidateError('policy_invalid')
    return rows


def _render(service_id, client_id, policy):
    """只编译当前候选自建规则；不臆造尚未接入的CN/媒体基础来源。"""
    rows = _policy_rows(policy)
    direct, _ = source_document(rows, 'direct')
    proxy, _ = source_document(rows, 'proxy')
    fake_id = str(uuid.uuid5(uuid.UUID(str(service_id)), NAMESPACE + ':identity'))
    node = {'type': 'vless', 'tag': 'PROXY', 'server': 'demo.invalid', 'server_port': 443,
            'uuid': fake_id, 'tls': {'enabled': True, 'server_name': 'demo.invalid'}}
    resources = {}

    def add(key, filename, content, content_type='application/json'):
        resources[key] = {'filename': filename, 'content': content, 'content_type': content_type}

    add('policy', 'candidate-policy.json', encoded(policy))
    add('rules-direct', 'candidate-direct.json', encoded(direct))
    add('rules-proxy', 'candidate-proxy.json', encoded(proxy))
    if client_id == 'android':
        rules = [dict(rule, outbound=outbound) for document, outbound in ((proxy, 'PROXY'), (direct, 'DIRECT'))
                 for rule in document['rules']]
        # 演示配置没有TUN、监听、自动测速或远程规则拉取，不应用系统网络。
        config = {'log': {'disabled': True}, 'inbounds': [],
                  'outbounds': [node, {'type': 'direct', 'tag': 'DIRECT'}],
                  'route': {'rules': rules, 'final': 'PROXY'}}
        add('subscription', 'demo-configuration.json', encoded(config))
    else:
        add('subscription', 'demo-subscription.txt', (_uri(node, client_id) + '\n').encode(),
            'text/plain; charset=utf-8')
        native = []
        for index, row in enumerate(sorted(rows, key=lambda item: (item.outbound != 'proxy', item.kind, item.value))):
            domain = {'exact': 'full:', 'suffix': 'domain:', 'regex': 'regexp:'}[row.kind] + row.value
            if client_id == 'windows':
                native.append({'Id': 'candidate-' + str(index), 'Remarks': '候选自建规则', 'Enabled': True,
                               'OutboundTag': row.outbound, 'Domain': [domain]})
            else:
                native.append({'remarks': '候选自建规则', 'enabled': True, 'locked': False,
                               'outboundTag': row.outbound, 'domain': [domain]})
        if client_id == 'windows':
            native.append({'Id': 'candidate-final', 'Remarks': '其他跟随当前代理', 'Enabled': True,
                           'OutboundTag': 'proxy', 'Port': '0-65535'})
        else:
            native.append({'remarks': '其他跟随当前代理', 'enabled': True, 'locked': False,
                           'outboundTag': 'proxy', 'port': '0-65535'})
        add('routing', 'candidate-routing.json', encoded(native))
    return {'client': client_id, 'evidence_scope': 'isolated', 'runtime_acceptance': 'NOT TESTED',
            'identities': [{'identity_id': 1, 'line_id': 1, 'node_tag': 'PROXY'}],
            'policy_sha256': sha256(encoded(policy)).hexdigest(), 'resources': resources}


def _load(record, client_id):
    """固定路径、完整摘要及重新合成比对；不接受任意路径或真实节点资产。"""
    directory = _directory(record.public_id, client_id)
    current = _checked(directory / 'current.json')
    if not current.exists():
        return None
    manifest = json.loads(_read(current, 32768))
    if (type(manifest) is not dict or manifest.get('schema') != 2
            or manifest.get('subscription') != str(record.public_id) or manifest.get('generation') != 1
            or manifest.get('client') != client_id or manifest.get('evidence_scope') != 'isolated'
            or manifest.get('runtime_acceptance') != 'NOT TESTED'
            or not re.fullmatch(r'[a-f0-9]{32}', str(manifest.get('release', '')))):
        raise CandidateError('manifest_invalid')
    release = _checked(directory / 'releases' / manifest['release'])
    values = manifest.get('resources')
    if type(values) is not dict or not 1 <= len(values) <= 6 or 'policy' not in values:
        raise CandidateError('manifest_invalid')
    policy_item = values['policy']
    if type(policy_item) is not dict or policy_item.get('filename') != 'candidate-policy.json':
        raise CandidateError('manifest_invalid')
    policy = json.loads(_read(release / 'candidate-policy.json', 256 * 1024))
    expected = _render(record.public_id, client_id, policy)
    if (set(values) != set(expected['resources']) or manifest.get('identities') != expected['identities']
            or manifest.get('policy_sha256') != expected['policy_sha256'] or manifest.get('primary') != 'subscription'):
        raise CandidateError('manifest_invalid')
    contents = {}
    for key, item in expected['resources'].items():
        raw = _read(release / item['filename'])
        safe = {'filename': item['filename'], 'bytes': len(raw), 'sha256': sha256(raw).hexdigest(),
                'content_type': item['content_type']}
        if values[key] != safe or raw != item['content']:
            raise CandidateError('artifact_invalid')
        contents[key] = raw
    return manifest, contents


def _current_policy():
    rules = list(ClientDirectRule.objects.order_by('pk'))
    if len(rules) > 200:
        raise CandidateError('too_many_rules')
    policy, _ = policy_document(rules)
    _policy_rows(policy)
    return policy


def _payload(record, client_id, asset=None, *, message='', update_error=''):
    manifest = asset[0] if asset else None
    resources = []
    if manifest:
        for key, item in manifest['resources'].items():
            url = f'/api/v1/me/services/{record.public_id}/delivery/{client_id}/resources/{key}'
            label = '演示节点订阅' if key == 'subscription' and client_id != 'android' else LABELS[key]
            resources.append({'key': key, 'label': label, 'download_url': url, 'filename': item['filename'],
                              'content_type': item['content_type'], 'bytes': item['bytes'], 'sha256': item['sha256']})
    primary = next((item['download_url'] for item in resources if item['key'] == 'subscription'), None)
    return {'service_id': str(record.public_id), 'service_revision': record.revision, 'client_id': client_id,
            'mode': 'synthetic', 'runtime_acceptance': 'NOT TESTED', 'state': 'ready' if manifest else 'not_prepared',
            'message': message or NOTICE, 'download_url': primary, 'resources': resources,
            'policy_sha256': manifest['policy_sha256'] if manifest else None, 'updates_available': False,
            'update_error': update_error, 'rule_source': 'current_candidate_custom_rules',
            'unsupported_resources': ['未接入的基础规则来源', '客户端DNS配置'] +
                                     ([] if client_id == 'android' else ['二进制Geo资源'])}


@contextmanager
def _obtain_lock(record):
    directory = _checked(_root() / 'requests' / str(record.public_id))
    directory.mkdir(parents=True, exist_ok=True)
    lock = _checked(directory / '.obtain-lock')
    try:
        lock.mkdir()
    except FileExistsError:
        raise CandidateError('publication_busy') from None
    try:
        yield directory
    finally:
        lock.rmdir()


def _obtain(record, actor, client_id, key):
    request_digest = sha256(encoded({'client_adapter': client_id, 'expected_revision': record.revision})).hexdigest()
    with _obtain_lock(record) as receipts:
        receipt_name = sha256(f'{actor.pk}:{key}'.encode()).hexdigest() + '.json'
        receipt = _checked(receipts / receipt_name)
        replay = receipt.exists()
        if replay and json.loads(_read(receipt, 1024)) != {'request_digest': request_digest}:
            raise CandidateError('idempotency_conflict')
        previous = _load(record, client_id)
        if replay:
            if previous is None:
                raise CandidateError('idempotency_asset_missing')
            # 已成功的同键重放只读现行产物；规则变更另发一次明确获取请求。
            return previous
        rendered = _render(record.public_id, client_id, _current_policy())
        if previous is None or previous[0]['policy_sha256'] != rendered['policy_sha256']:
            publish(_root(client_id), record.public_id, 1, rendered,
                    expected_release=previous[0]['release'] if previous else None)
        asset = _load(record, client_id)
        if not receipt.exists():
            temporary = _checked(receipts / ('.request-' + uuid.uuid4().hex))
            try:
                with temporary.open('xb') as handle:
                    handle.write(encoded({'request_digest': request_digest}))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, receipt)
            finally:
                temporary.unlink(missing_ok=True)
        return asset


@never_cache
@endpoint(methods=('GET', 'POST'))
def delivery(request, public_id):
    if not _enabled():
        return error('not_found', '此环境没有启用合成资源交付。', 404)
    record = _service(request.user, public_id)
    if record is None:
        return error('not_found', '服务不存在或不可访问。', 404)
    if request.method == 'POST':
        body, failure = json_body(request, {'client_adapter', 'expected_revision', 'idempotency_key'})
        if failure:
            return failure
        client_id = body['client_adapter']
    else:
        if set(request.GET) != {'client_adapter'} or len(request.GET.getlist('client_adapter')) != 1:
            return error('invalid_fields', '请选择一种软件。', 422)
        client_id = request.GET.get('client_adapter')
    if type(client_id) is not str or client_id not in CLIENTS:
        return error('client_not_supported', '此软件尚未支持资源编译。', 422)
    blocked = _blocked(record)
    if blocked:
        payload = _payload(record, client_id, message=blocked)
        payload['state'] = 'blocked'
        return error('delivery_blocked', blocked, 409) if request.method == 'POST' else success(payload)
    try:
        if request.method == 'POST':
            if type(body['expected_revision']) is not int or body['expected_revision'] != record.revision:
                return error('revision_conflict', '服务版本已经改变，请刷新后重试。', 409)
            key = body['idempotency_key']
            if type(key) is not str or re.fullmatch(r'[A-Za-z0-9_-]{8,128}', key) is None:
                return error('invalid_fields', '操作标识无效。', 422)
            return success(_payload(record, client_id, _obtain(record, request.user, client_id, key)))
        asset = _load(record, client_id)
        try:
            current = _render(record.public_id, client_id, _current_policy())
        except (ValidationError, CandidateError, ValueError, TypeError, KeyError):
            payload = _payload(record, client_id, asset,
                               update_error='当前候选规则无法编译，保留上一份已经校验的演示资源。')
            if not asset:
                payload['state'] = 'blocked'
                payload['message'] = '当前候选规则无法编译，尚无可下载资源。'
            return success(payload)
        payload = _payload(record, client_id, asset)
        payload['updates_available'] = bool(asset and asset[0]['policy_sha256'] != current['policy_sha256'])
        return success(payload)
    except CandidateError as exc:
        if str(exc) == 'idempotency_conflict':
            return error('idempotency_conflict', '同一操作标识不能用于不同的软件或服务版本。', 409)
        return error('candidate_unavailable', '演示资源未通过校验或正忙，请稍后重试；已发布产物保持原样。', 503)
    except ValidationError:
        return error('rules_not_supported', '当前候选规则未通过编译核验，已发布产物保持原样。', 422)
    except (DeliveryError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return error('candidate_unavailable', '演示资源暂时无法完成交付，已发布产物保持原样。', 503)


@never_cache
@endpoint(methods=('GET', 'HEAD'))
def download_resource(request, public_id, client_id, resource):
    if not _enabled():
        return error('not_found', '资源不存在或不可访问。', 404)
    record = _service(request.user, public_id)
    if record is None or client_id not in CLIENTS or _blocked(record):
        return error('not_found', '资源不存在或不可访问。', 404)
    try:
        asset = _load(record, client_id)
        if asset is None or resource not in asset[1]:
            return error('not_found', '资源尚未准备好。', 404)
        manifest, contents = asset
        item = manifest['resources'][resource]
        response = HttpResponse(b'' if request.method == 'HEAD' else contents[resource], content_type=item['content_type'])
        response['Content-Disposition'] = 'attachment; filename="' + item['filename'] + '"'
        response['Content-Length'] = str(item['bytes'])
        response['X-Content-Type-Options'] = 'nosniff'
        response['X-Shenzhou-Resource-Mode'] = 'synthetic'
        return response
    except (CandidateError, DeliveryError, ValidationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        return error('not_found', '资源未通过发布校验。', 404)
