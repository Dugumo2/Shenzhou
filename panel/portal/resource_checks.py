"""固定资源检查队列；浏览器不能指定地址、服务名或命令。"""
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
from django.utils import timezone

from .inventory_service import InventoryError, digest
from .inventory_queries import load_check_results
from .models import CheckResult, ResourceCheckJob
from .observation_ingest import ObservationError, _target, check_projection, target_fingerprint

ACTIVE = ('queued', 'running')
SOURCE = 'fixed-resource-worker-v1'
TIMEOUT_SECONDS = 5
MIN_INTERVAL_SECONDS = 60
QUEUE_TTL_SECONDS = 120
LEASE_SECONDS = 15
NOT_TESTED = ['authentication', 'dns', 'udp', 'egress', 'whole_path']


def _need(value, code, message, status=422):
    if not value:
        raise InventoryError(code, message, status)


def _staff(actor):
    current = get_user_model().objects.filter(pk=getattr(actor, 'pk', None), is_active=True, is_staff=True).first()
    _need(current is not None, 'permission_denied', '此操作仅限当前有效管理员。', 403)
    return current


def _resolve(kind, identifier):
    try:
        return _target(kind, identifier)
    except ObservationError:
        raise InventoryError('invalid_target', '检查目标不存在或格式无效。', 404) from None


def _version(kind, target):
    try:
        return target_fingerprint(kind, target)
    except ObservationError:
        raise InventoryError('target_unavailable', '目标关系无效或超出检查范围，请先核对资源资料。', 409) from None


def _validate_policy(value):
    _need(type(value) is dict and value.keys() == {'version', 'actions'} and value['version'] == 1
          and type(value['actions']) is list and len(value['actions']) <= 100, 'invalid_policy', '固定检查目录无效。', 503)
    seen = set()
    for entry in value['actions']:
        common = {'target_kind', 'target_id', 'target_version', 'action'}
        _need(type(entry) is dict and common <= entry.keys(), 'invalid_policy', '固定检查目录无效。', 503)
        kind, action = entry['target_kind'], entry['action']
        _need(all(type(entry[k]) is str for k in common)
              and re.fullmatch(r'[a-f0-9]{64}', entry['target_version']), 'invalid_policy', '固定检查目录无效。', 503)
        if action == 'service_status':
            valid = (kind in ('server', 'core') and entry.keys() == common | {'unit'}
                     and type(entry['unit']) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,99}\.service', entry['unit']))
        elif action == 'tcp':
            valid = kind in ('ingress', 'egress') and entry.keys() == common | {'ip', 'port', 'allow_loopback'}
            try:
                address = ipaddress.ip_address(entry.get('ip', ''))
                valid = (valid and '%' not in entry['ip'] and type(entry['port']) is int and 1 <= entry['port'] <= 65535
                         and type(entry['allow_loopback']) is bool and not address.is_multicast
                         and not getattr(address, 'ipv4_mapped', None)
                         and (address.is_global or (address.is_loopback and entry['allow_loopback'])))
            except (ValueError, TypeError):
                valid = False
        else:
            valid = False
        key = (kind, entry['target_id'], action)
        _need(valid and key not in seen, 'invalid_policy', '固定检查目录无效。', 503)
        seen.add(key)
    return value['actions']


def load_policy():
    """生产目录与各级父目录必须由 root 持有且不可被其他用户写入。"""
    name = os.environ.get('PANEL_RESOURCE_CHECKS_POLICY', '')
    if not name:
        return []
    path = Path(name)
    try:
        _need(path.is_absolute() and not path.is_symlink(), 'unsafe_policy', '检查目录权限不符合要求。', 503)
        if settings.PANEL_LIVE:
            _need(os.name == 'posix', 'unsafe_policy', '生产检查目录只支持已审核的 Linux 部署。', 503)
            for candidate in (path, *path.parents):
                info = candidate.lstat()
                _need(not stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                      'unsafe_policy', '检查目录权限不符合要求。', 503)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'r', encoding='utf-8') as stream:
            raw = stream.read(65537)
        _need(len(raw) <= 65536, 'invalid_policy', '固定检查目录过大。', 503)
        return _validate_policy(json.loads(raw))
    except (OSError, ValueError, TypeError):
        raise InventoryError('policy_unavailable', '固定检查目录不可用。', 503) from None


def _spec(kind, identifier, action, version):
    entry = next((e for e in load_policy() if (e['target_kind'], e['target_id'], e['action']) == (kind, identifier, action)), None)
    _need(entry is not None, 'action_not_allowed', '此目标尚未开放该固定检查。', 403)
    _need(entry['target_version'] == version, 'target_version_changed', '目标或批准版本已变更，请刷新资料。', 409)
    return entry


def actions_for_targets(targets):
    """targets 为已获授权详情范围内的 (kind, ORM对象)；只读取目录和数据库。"""
    try:
        policy = load_policy()
    except InventoryError:
        return []
    if not policy:
        return []
    result = []
    for kind, target in targets:
        identifier = str(target.pk if kind in ('ingress', 'egress') else target.public_id)
        entries = [entry for entry in policy if (entry['target_kind'], entry['target_id']) == (kind, identifier)]
        if not entries:
            continue
        try:
            version = _version(kind, target)
        except InventoryError:
            continue
        for entry in entries:
            if (entry['target_kind'], entry['target_id'], entry['target_version']) != (kind, identifier, version):
                continue
            result.append({'target_kind': kind, 'target_id': identifier, 'target_version': version,
                           'action': entry['action'], 'label': target.name + (' · 服务状态' if entry['action'] == 'service_status' else ' · TCP连接'),
                           'timeout_seconds': TIMEOUT_SECONDS, 'min_interval_seconds': MIN_INTERVAL_SECONDS,
                           'scope': '仅本机固定服务状态' if entry['action'] == 'service_status' else '仅固定端点TCP连接；不发送业务数据'})
    return result


def job_projection(job, *, replayed=False):
    results = load_check_results(CheckResult.objects.filter(pk=job.result_id)) if job.result_id else []
    return {'id': str(job.public_id), 'state': job.state, 'target_kind': job.target_kind,
            'target_id': job.target_id, 'target_version': job.target_version, 'action': job.action,
            'created_at': job.created_at.isoformat(), 'started_at': job.started_at.isoformat() if job.started_at else None,
            'finished_at': job.finished_at.isoformat() if job.finished_at else None,
            'failure_code': job.failure_code, 'replayed': replayed,
            'lease_until': job.lease_until.isoformat() if job.lease_until else None,
            'result': check_projection(results[0]) if results else None, 'not_tested': NOT_TESTED}


def get_job(actor, identifier):
    _staff(actor)
    try:
        job = ResourceCheckJob.objects.select_related('result').filter(public_id=identifier).first()
    except (ValueError, TypeError, ValidationError):
        job = None
    _need(job is not None, 'not_found', '检查任务不存在。', 404)
    return job_projection(job)


def recover_expired(now=None):
    now = now or timezone.now()
    return ResourceCheckJob.objects.filter(state__in=ACTIVE, lease_until__lte=now).update(
        state='completed', finished_at=now, failure_code='worker_deadline_exceeded')


def enqueue_check(actor, payload):
    current = _staff(actor)
    fields = {'target_kind', 'target_id', 'target_version', 'action', 'idempotency_key'}
    _need(type(payload) is dict and payload.keys() == fields and all(type(v) is str for v in payload.values()),
          'invalid_fields', '仅接受固定检查目标、版本、动作和请求标识。')
    _need(re.fullmatch(r'[A-Za-z0-9_-]{8,100}', payload['idempotency_key'])
          and re.fullmatch(r'[a-f0-9]{64}', payload['target_version']), 'invalid_request', '请求标识或目标版本无效。')
    fingerprint = digest(payload)
    try:
        with transaction.atomic():
            current = _staff(actor)
            previous = ResourceCheckJob.objects.filter(actor=current, idempotency_key=payload['idempotency_key']).first()
            if previous:
                _need(previous.request_fingerprint == fingerprint, 'idempotency_conflict', '此请求标识已用于不同检查。', 409)
                return job_projection(previous, replayed=True)
            kind, identifier = payload['target_kind'], payload['target_id']
            target = _resolve(kind, identifier)
            _need(_version(kind, target) == payload['target_version'], 'target_version_changed', '目标版本已变更，请刷新资料。', 409)
            _spec(kind, identifier, payload['action'], payload['target_version'])
            now = timezone.now()
            recover_expired(now)
            _need(not ResourceCheckJob.objects.filter(target_kind=kind, target_id=identifier, state__in=ACTIVE).exists(),
                  'check_in_progress', '此目标已有检查进行中。', 409)
            _need(not ResourceCheckJob.objects.filter(target_kind=kind, target_id=identifier,
                      created_at__gt=now - timedelta(seconds=MIN_INTERVAL_SECONDS)).exists(),
                  'check_rate_limited', '此目标每60秒最多发起一次检查。', 429)
            job = ResourceCheckJob.objects.create(actor=current, request_fingerprint=fingerprint,
                lease_until=now + timedelta(seconds=QUEUE_TTL_SECONDS), **payload)
            return job_projection(job)
    except (IntegrityError, OperationalError):
        raise InventoryError('check_conflict', '检查请求正在更新，请保留请求标识重试。', 409) from None


def execute_fixed(entry):
    """固定参数、无 shell、无 DNS；返回码和异常转为有限错误词汇。"""
    start = time.monotonic()
    stage = 'collect' if entry['action'] == 'service_status' else 'connect'
    try:
        if entry['action'] == 'service_status':
            process = subprocess.run(['/usr/bin/systemctl', 'is-active', '--quiet', entry['unit']],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=TIMEOUT_SECONDS, check=False, env={'PATH': '/usr/bin:/bin', 'LANG': 'C'})
            result, code = ('pass', '') if process.returncode == 0 else ('fail', 'collection_failed')
        else:
            address = ipaddress.ip_address(entry['ip'])
            family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(TIMEOUT_SECONDS)
                connection.connect((str(address), entry['port']))
            result, code = 'pass', ''
    except (subprocess.TimeoutExpired, TimeoutError):
        result, code = 'timeout', 'timeout'
    except ConnectionRefusedError:
        result, code = 'fail', 'connection_refused'
    except OSError:
        result, code = 'fail', 'collection_failed' if stage == 'collect' else 'unreachable'
    return {'result': result, 'latency_ms': round((time.monotonic() - start) * 1000, 3),
            'error_stage': '' if result == 'pass' else stage, 'error_code': code}


def _job_context(job):
    _staff(job.actor)
    target = _resolve(job.target_kind, job.target_id)
    _need(_version(job.target_kind, target) == job.target_version, 'target_version_changed', '目标版本已变更。', 409)
    entry = _spec(job.target_kind, job.target_id, job.action, job.target_version)
    return target, entry


def run_next():
    """一次消费一个任务；IO外移出事务，不阻塞面板与计量写入。"""
    _need(not (os.name == 'posix' and os.geteuid() == 0), 'root_worker_forbidden', '检查worker必须由非root用户运行。', 503)
    # 空闲轮询只读；这不是领取锁，进入事务后仍重新判断并领取。
    if not ResourceCheckJob.objects.filter(Q(state='queued') | Q(state='running', lease_until__lte=timezone.now())).exists():
        return None
    with transaction.atomic():
        recover_expired()
        job = ResourceCheckJob.objects.select_for_update().filter(state='queued').order_by('created_at', 'pk').first()
        if job is None:
            return None
        job.state, job.started_at = 'running', timezone.now()
        job.lease_until = job.started_at + timedelta(seconds=LEASE_SECONDS)
        job.save(update_fields=['state', 'started_at', 'lease_until'])
    failure, outcome = '', None
    try:
        _, entry = _job_context(job)
        outcome = execute_fixed(entry)
    except InventoryError as exc:
        failure = exc.code
    with transaction.atomic():
        job = ResourceCheckJob.objects.select_for_update().get(pk=job.pk)
        if job.state != 'running':
            return job_projection(job)
        if timezone.now() >= job.lease_until:
            failure = 'worker_deadline_exceeded'
        if not failure:
            try:
                target, current_entry = _job_context(job)
                _need(current_entry == entry, 'action_policy_changed', '固定检查目录在执行期间发生变化。', 409)
            except InventoryError as exc:
                failure = exc.code
        if not failure:
            observed = timezone.now()
            job.result = CheckResult.objects.create(**{job.target_kind: target}, source=SOURCE,
                event_id=str(job.public_id), check_kind=job.action, target_fingerprint=job.target_version,
                observed_at=observed, expires_at=observed + timedelta(seconds=120), **outcome)
        job.state, job.finished_at, job.failure_code = 'completed', timezone.now(), failure
        job.save(update_fields=['result', 'state', 'finished_at', 'failure_code'])
        return job_projection(job)
