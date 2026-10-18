"""管理员有限配置控制；受理、检查和真实应用分别记录。"""
import hashlib
import json
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .core_control_models import CoreConfigCandidate, CoreControlJob, CoreControlState
from .models import CoreInstance

ACTIVE = ('queued', 'running', 'unknown')
LEVELS = ('debug', 'info', 'warning', 'error', 'none')
HASH = re.compile(r'[a-f0-9]{64}')
ERROR_CODES = frozenset('''unsafe_path private_permissions_unverified unsafe_permissions invalid_material
invalid_journal executor_busy invalid_job wrong_core invalid_fence unsupported_action authority_guard_unavailable
stale_fence binding_changed unsupported_generator artifact_changed check_required check_mismatch stale_check
source_changed identity_changed runtime_unconfirmed job_replay_mismatch unresolved_job execution_unconfirmed
validation_failed journal_unavailable reconciliation_unconfirmed invalid_receipt receipt_mismatch executor_interrupted
reconciliation_unavailable revision_changed_during_execution fence_changed_during_execution job_expired
revision_conflict permission_revoked control_disabled fence_conflict check_failed executor_failed'''.split())


class CoreControlError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def _need(condition, code, message, status=409):
    if not condition:
        raise CoreControlError(code, message, status)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _staff(actor):
    _need(get_user_model().objects.filter(pk=getattr(actor, 'pk', None), is_active=True, is_staff=True).exists(),
          'permission_denied', '此操作仅限有效管理员。', 403)


def _write(actor):
    _staff(actor)
    # 同一管理员的幂等键跨核心也唯一，先锁身份行再锁核心，保持固定锁顺序。
    _need(get_user_model().objects.select_for_update().filter(pk=actor.pk, is_active=True, is_staff=True).first()
          is not None, 'permission_denied', '此操作仅限有效管理员。', 403)
    _need(getattr(settings, 'CORE_CONTROL_WRITE_ENABLED', False) is True, 'control_disabled', '核心配置写入尚未开放。', 403)


def _identifier(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise CoreControlError('not_found', '核心或任务不存在。', 404) from None


def _core(identifier, lock=False):
    query = CoreInstance.objects.select_related('server')
    if lock:
        query = query.select_for_update()
    core = query.filter(public_id=_identifier(identifier)).first()
    _need(core is not None, 'not_found', '核心不存在。', 404)
    return core


def _executor():
    from . import core_executor
    return core_executor


def _binding(core):
    if core.owner_state != 'verified':
        return {'available': False, 'reason': 'ownership_unverified'}
    if not core.server.enabled:
        return {'available': False, 'reason': 'server_disabled'}
    try:
        binding = _executor().binding_status(core)
        return {'available': binding.get('available') is True,
                'reason': '' if binding.get('available') is True else 'binding_unavailable'}
    except Exception:
        return {'available': False, 'reason': 'binding_unavailable'}


def _ready(core):
    binding = _binding(core)
    _need(binding['available'], 'binding_unavailable', '此核心尚未核验独占配置所有权或固定执行绑定。')


def _revision(core, revision):
    _need(type(revision) is int and revision > 0, 'invalid_revision', '请提供有效的配置修订号。', 422)
    _need(core.revision == revision, 'revision_conflict', '核心资料或配置已经变化，请重新读取。')


def _request(body, keys):
    _need(type(body) is dict and set(body) == set(keys), 'invalid_fields', '请求包含不支持的字段或缺少字段。', 422)
    key = body['idempotency_key']
    _need(type(key) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{8,80}', key),
          'invalid_idempotency_key', '请提供有效的幂等请求标识。', 422)
    _need(type(body['expected_revision']) is int and body['expected_revision'] > 0,
          'invalid_revision', '请提供有效的配置修订号。', 422)


def _iso(value):
    return value.isoformat() if value else None


def _safe_hash(value):
    return value if type(value) is str and HASH.fullmatch(value) else None


def _candidate_context(core):
    state = CoreControlState.objects.filter(core=core).first()
    return {'writable': getattr(settings, 'CORE_CONTROL_WRITE_ENABLED', False) is True
            and _binding(core)['available'] and not core.control_jobs.filter(status__in=ACTIVE).exists(),
            'revision': core.revision, 'fence': state.fence if state else 0,
            'current_id': state.last_applied_candidate_id if state else None}


def candidate_projection(candidate, context=None):
    context = context if context is not None else _candidate_context(candidate.core)
    patch = candidate.patch
    level = patch.get('log', {}).get('level') if type(patch) is dict and type(patch.get('log')) is dict else None
    level = level if level in LEVELS else None
    current = context['writable'] and context['revision'] == candidate.base_revision
    capabilities = {'check': current and candidate.state in ('prepared', 'checked', 'check_failed'),
                    'apply': current and candidate.state == 'checked' and candidate.checked_at is not None
                    and candidate.checked_fence is not None and 0 < candidate.checked_fence <= context['fence'],
                    'rollback': context['writable'] and candidate.state == 'applied'
                    and context['current_id'] == candidate.pk and context['revision'] > candidate.base_revision}
    return {'id': str(candidate.public_id), 'core_id': str(candidate.core.public_id),
            'base_revision': candidate.base_revision, 'state': candidate.state,
            'patch': {'log': {'level': level}},
            'difference': {'field': 'log.level', 'before': candidate.previous_level if candidate.previous_level in LEVELS else None,
                           'after': level},
            'base_sha256': _safe_hash(candidate.base_sha256), 'candidate_sha256': _safe_hash(candidate.candidate_sha256),
            'created_at': _iso(candidate.created_at), 'checked_at': _iso(candidate.checked_at),
            'capabilities': capabilities,
            'impact': {'scope': 'single_core', 'may_restart': True,
                       'message': '仅更改此核心日志级别；应用或回滚可能重启该实例并中断其现有连接。'}}


def job_projection(job):
    return {'id': str(job.public_id), 'core_id': str(job.core.public_id),
            'candidate_id': str(job.candidate.public_id), 'action': job.action, 'status': job.status,
            'expected_revision': job.expected_revision, 'fence': job.fence,
            'result': _safe_result(job.result) if job.result else {},
            'error_code': job.error_code if job.error_code in ERROR_CODES else ('executor_failed' if job.error_code else ''),
            'created_at': _iso(job.created_at),
            'started_at': _iso(job.started_at), 'finished_at': _iso(job.finished_at)}


def control_detail(actor, core_id):
    _staff(actor)
    core = _core(core_id)
    binding = _binding(core)
    busy = core.control_jobs.filter(status__in=ACTIVE).exists()
    enabled = getattr(settings, 'CORE_CONTROL_WRITE_ENABLED', False) is True
    candidates = list(core.config_candidates.order_by('-created_at', '-pk')[:20])
    jobs = list(core.control_jobs.select_related('candidate').order_by('-created_at', '-pk')[:20])
    state = CoreControlState.objects.select_related('last_applied_candidate').filter(core=core).first()
    current = state.last_applied_candidate if state else None
    context = {'writable': enabled and binding['available'] and not busy, 'revision': core.revision,
               'fence': state.fence if state else 0, 'current_id': state.last_applied_candidate_id if state else None}
    return {'core_id': str(core.public_id), 'revision': core.revision,
            'configuration_version': core.configuration_version, 'owner_state': core.owner_state,
            'binding': binding, 'busy': busy, 'write_enabled': enabled,
            'current_applied_candidate_id': str(current.public_id) if current else None,
            'capabilities': {'create_candidate': enabled and binding['available'] and not busy,
                             'supported_fields': ['log.level'], 'log_levels': list(LEVELS)},
            'candidates': [candidate_projection(item, context) for item in candidates],
            'jobs': [job_projection(item) for item in jobs],
            'limitations': ['首批仅支持日志级别；协议、Mux和核心升级尚未开放。',
                            '检查通过只证明固定核心接受候选配置，不证明连接、UDP、DNS或出口路径通过。']}


@transaction.atomic
def create_candidate(actor, core_id, body):
    _write(actor)
    _request(body, ('patch', 'expected_revision', 'idempotency_key'))
    patch = body['patch']
    _need(type(patch) is dict and set(patch) == {'log'} and type(patch['log']) is dict
          and set(patch['log']) == {'level'} and type(patch['log']['level']) is str
          and patch['log']['level'] in LEVELS, 'unsupported_patch', '首批只接受日志级别变更。', 422)
    core = _core(core_id, lock=True)
    fingerprint = _digest({'core_id': str(core.public_id), **body})
    existing = CoreConfigCandidate.objects.filter(creator=actor, idempotency_key=body['idempotency_key']).first()
    if existing:
        _need(existing.request_sha256 == fingerprint, 'idempotency_conflict', '此请求标识已用于其他变更。')
        return {'candidate': candidate_projection(existing), 'replayed': True}
    _revision(core, body['expected_revision'])
    _ready(core)
    _need(not core.control_jobs.filter(status__in=ACTIVE).exists(), 'core_busy', '该核心存在未完成或结果未知的任务。')
    try:
        prepared = _executor().prepare_candidate(core, patch)
    except Exception:
        raise CoreControlError('prepare_failed', '无法生成受限候选，请核对执行绑定与当前配置。') from None
    _need(type(prepared) is dict and all(type(prepared.get(key)) is str and HASH.fullmatch(prepared[key])
          for key in ('base_sha256', 'candidate_sha256', 'binding_fingerprint'))
          and type(prepared.get('artifact_ref')) is str and re.fullmatch(r'[a-f0-9]{32}', prepared['artifact_ref']),
          'invalid_artifact', '候选生成未返回有效的受限产物。')
    previous = prepared.get('previous_level', '')
    _need(previous in ('', *LEVELS), 'invalid_artifact', '原日志级别无法安全表示。')
    candidate = CoreConfigCandidate.objects.create(core=core, base_revision=core.revision, patch=patch,
        previous_level=previous, creator=actor, idempotency_key=body['idempotency_key'], request_sha256=fingerprint,
        **{key: prepared[key] for key in ('base_sha256', 'candidate_sha256', 'binding_fingerprint', 'artifact_ref')})
    return {'candidate': candidate_projection(candidate), 'replayed': False}


@transaction.atomic
def enqueue_action(actor, core_id, candidate_id, action, body):
    _write(actor)
    _request(body, ('expected_revision', 'idempotency_key'))
    _need(action in ('check', 'apply', 'rollback'), 'unsupported_action', '不支持此核心动作。', 422)
    core = _core(core_id, lock=True)
    candidate = core.config_candidates.filter(public_id=_identifier(candidate_id)).first()
    _need(candidate is not None, 'not_found', '候选不存在或不属于此核心。', 404)
    fingerprint = _digest({'core_id': str(core.public_id), 'candidate_id': str(candidate.public_id), 'action': action, **body})
    existing = CoreControlJob.objects.filter(actor=actor, idempotency_key=body['idempotency_key']).first()
    if existing:
        _need(existing.request_sha256 == fingerprint, 'idempotency_conflict', '此请求标识已用于其他动作。')
        return {'job': job_projection(existing), 'replayed': True}
    _revision(core, body['expected_revision'])
    _ready(core)
    _need(not core.control_jobs.filter(status__in=ACTIVE).exists(), 'core_busy', '该核心存在未完成或结果未知的任务。')
    state, _ = CoreControlState.objects.get_or_create(core=core)
    if action == 'rollback':
        _need(state.last_applied_candidate_id == candidate.pk and candidate.state == 'applied',
              'rollback_unavailable', '只能回滚当前已应用的候选。')
    else:
        _need(candidate.base_revision == core.revision, 'candidate_stale', '候选基于旧修订，请重新生成。')
        if action == 'apply':
            _need(candidate.state == 'checked' and candidate.checked_fence is not None
                  and 0 < candidate.checked_fence <= state.fence and candidate.checked_at is not None,
                  'check_required', '请先完成此候选的真实配置检查。')
    claimed = CoreControlState.objects.filter(pk=state.pk, fence=state.fence).update(fence=F('fence') + 1)
    _need(claimed == 1, 'core_busy', '此核心已有并发操作，请重新读取。')
    job = CoreControlJob.objects.create(core=core, candidate=candidate, actor=actor, action=action,
        expected_revision=core.revision, fence=state.fence + 1, idempotency_key=body['idempotency_key'],
        request_sha256=fingerprint, expires_at=timezone.now() + timedelta(minutes=10))
    return {'job': job_projection(job), 'replayed': False}


def get_job(actor, job_id, core_id=None):
    _staff(actor)
    job = CoreControlJob.objects.select_related('core', 'candidate').filter(public_id=_identifier(job_id)).first()
    _need(job is not None, 'not_found', '任务不存在。', 404)
    if core_id is not None:
        _need(job.core.public_id == _identifier(core_id), 'not_found', '任务不存在或不属于此核心。', 404)
    return {'job': job_projection(job)}


def _safe_result(result):
    """执行器输出再次白名单投影；任意stdout、路径和异常均不能进API。"""
    if type(result) is not dict or result.get('status') not in ('succeeded', 'failed', 'unknown'):
        return {'status': 'unknown', 'error_code': 'invalid_receipt'}
    safe = {'status': result['status']}
    if result.get('error_code'):
        code = result['error_code']
        safe['error_code'] = code if type(code) is str and code in ERROR_CODES else 'executor_failed'
    for key in ('job_id', 'core_id'):
        value = result.get(key)
        if type(value) is str and re.fullmatch(r'[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}', value):
            safe[key] = value
    if result.get('action') in ('check', 'apply', 'rollback'):
        safe['action'] = result['action']
    if type(result.get('core_version')) is str and re.fullmatch(r'v?\d+(?:\.\d+){1,3}(?:-[A-Za-z0-9.]{1,20})?', result['core_version']):
        safe['core_version'] = result['core_version']
    for key in ('config_sha256', 'candidate_sha256', 'binding_fingerprint'):
        if type(result.get(key)) is str and HASH.fullmatch(result[key]):
            safe[key] = result[key]
    if type(result.get('fence')) is int and result['fence'] > 0:
        safe['fence'] = result['fence']
    return safe


def _receipt(job, result):
    """回执须绑定同一实例、动作、候选和栅栏，不能凭成功字样改数据库。"""
    result = _safe_result(result)
    if result['status'] == 'unknown':
        return result
    expected = {'job_id': str(job.public_id), 'core_id': str(job.core.public_id),
                'action': job.action, 'fence': job.fence,
                'candidate_sha256': job.candidate.candidate_sha256,
                'binding_fingerprint': job.candidate.binding_fingerprint}
    expected_hash = job.candidate.base_sha256 if job.action == 'rollback' else job.candidate.candidate_sha256
    if any(result.get(key) != value for key, value in expected.items()) or (
            result['status'] == 'succeeded' and result.get('config_sha256') != expected_hash):
        return {'status': 'unknown', 'error_code': 'receipt_mismatch'}
    return result


def _finish_job(job_id, result):
    with transaction.atomic():
        job = CoreControlJob.objects.select_related('core', 'candidate').get(public_id=job_id)
        core = _core(job.core.public_id, lock=True)
        job.refresh_from_db()
        if job.status not in ('running', 'unknown'):
            return job_projection(job)
        result = _receipt(job, result)
        if core.revision != job.expected_revision:
            result = {'status': 'unknown', 'error_code': 'revision_changed_during_execution'}
        elif not CoreControlState.objects.filter(core=core, fence=job.fence).exists():
            result = {'status': 'unknown', 'error_code': 'fence_changed_during_execution'}
        # 先以原状态抢占终态，再更新候选/核心；整个事务一起提交。
        claimed = CoreControlJob.objects.filter(pk=job.pk, status=job.status).update(
            status=result['status'], result=result, error_code=result.get('error_code', ''),
            finished_at=timezone.now() if result['status'] != 'unknown' else None)
        if not claimed:
            job.refresh_from_db()
            return job_projection(job)
        candidate = CoreConfigCandidate.objects.get(pk=job.candidate_id)
        if result['status'] == 'succeeded':
            if job.action == 'check':
                candidate.state, candidate.checked_fence, candidate.checked_at = 'checked', job.fence, timezone.now()
            else:
                core.revision += 1
                core.configuration_version = result['config_sha256']
                core.save(update_fields=['revision', 'configuration_version'])
                candidate.state = 'applied' if job.action == 'apply' else 'rolled_back'
                CoreControlState.objects.filter(core=core, fence=job.fence).update(
                    last_applied_candidate=candidate if job.action == 'apply' else None)
            candidate.save(update_fields=['state', 'checked_fence', 'checked_at'])
        elif job.action == 'check':
            candidate.state, candidate.checked_fence, candidate.checked_at = 'check_failed', None, None
            candidate.save(update_fields=['state', 'checked_fence', 'checked_at'])
    job.refresh_from_db()
    return job_projection(job)


def run_job(job_id):
    """固定worker入口；已领取的任务永不自动重做。"""
    identifier = _identifier(job_id)
    with transaction.atomic():
        job = CoreControlJob.objects.select_related('core', 'candidate', 'actor').filter(public_id=identifier).first()
        _need(job is not None, 'not_found', '任务不存在。', 404)
        if job.status != 'queued':
            return job_projection(job)
        core = _core(job.core.public_id, lock=True)
        error_code = ''
        if job.expires_at <= timezone.now():
            error_code = 'job_expired'
        elif core.revision != job.expected_revision:
            error_code = 'revision_conflict'
        elif not get_user_model().objects.filter(pk=job.actor_id, is_active=True, is_staff=True).exists():
            error_code = 'permission_revoked'
        elif getattr(settings, 'CORE_CONTROL_WRITE_ENABLED', False) is not True or not _binding(core)['available']:
            error_code = 'control_disabled'
        elif not CoreControlState.objects.filter(core=core, fence=job.fence).exists():
            error_code = 'fence_conflict'
        if error_code:
            CoreControlJob.objects.filter(pk=job.pk, status='queued').update(status='failed', error_code=error_code,
                result={'status': 'failed', 'error_code': error_code}, finished_at=timezone.now())
            job.refresh_from_db()
            return job_projection(job)
        if not CoreControlJob.objects.filter(pk=job.pk, status='queued').update(status='running', started_at=timezone.now()):
            job.refresh_from_db()
            return job_projection(job)
    job.refresh_from_db()
    try:
        result = _executor().execute_job(job)
    except Exception:
        result = {'status': 'unknown', 'error_code': 'executor_interrupted'}
    return _finish_job(identifier, result)


def reconcile_job(job_id):
    """只读核对持久执行回执；任何情况下都不重新执行或自动回滚。"""
    identifier = _identifier(job_id)
    job = CoreControlJob.objects.select_related('core', 'candidate').filter(public_id=identifier).first()
    _need(job is not None, 'not_found', '任务不存在。', 404)
    if job.status not in ('running', 'unknown'):
        return job_projection(job)
    try:
        result = _executor().reconcile_job(job)
    except Exception:
        result = {'status': 'unknown', 'error_code': 'reconciliation_unavailable'}
    return _finish_job(identifier, result)


def run_next_job():
    job = CoreControlJob.objects.filter(status='queued').order_by('created_at', 'pk').first()
    return run_job(job.public_id) if job else None
