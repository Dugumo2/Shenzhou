"""可信绑定接线；默认没有生产适配器，不接受HTTP或环境传入任意执行对象。"""
from django.conf import settings


def _binding(core):
    bindings = getattr(settings, 'CORE_CONTROL_BINDINGS', {})
    if type(bindings) is not dict:
        raise ValueError('binding_unavailable')
    value = bindings.get(str(core.public_id))
    if value is None or str(value.checker.profile.core_id) != str(core.public_id):
        raise ValueError('binding_unavailable')
    return value


def binding_status(core):
    try:
        return _binding(core).checker.binding_status()
    except Exception:
        return {'available': False, 'reason': 'binding_unavailable'}


def prepare_candidate(core, patch):
    return _binding(core).checker.prepare_candidate(patch)


def _envelope(job, result):
    return {**result, 'job_id': str(job.public_id), 'core_id': str(job.core.public_id),
            'action': job.action, 'fence': job.fence,
            'candidate_sha256': job.candidate.candidate_sha256,
            'binding_fingerprint': job.candidate.binding_fingerprint}


def execute_job(job):
    binding = _binding(job.core)
    if job.action == 'check':
        candidate = job.candidate
        result = binding.checker.check_candidate(candidate.artifact_ref,
            base_sha256=candidate.base_sha256, candidate_sha256=candidate.candidate_sha256,
            binding_fingerprint=candidate.binding_fingerprint, check_fence=job.fence)
        if (result.get('core_id') != str(job.core.public_id)
                or result.get('check_fence') != job.fence
                or result.get('candidate_sha256') != candidate.candidate_sha256
                or result.get('binding_fingerprint') != candidate.binding_fingerprint):
            return _envelope(job, {'status': 'unknown', 'error_code': 'receipt_mismatch'})
        if result.get('status') == 'succeeded':
            result = {**result, 'config_sha256': candidate.candidate_sha256}
        return _envelope(job, result)
    return binding.executor.execute_job(job)


def reconcile_job(job):
    # 检查中断只读取已持久回执，绝不重新运行检查进程。
    if job.action == 'check':
        try:
            result = _binding(job.core).checker.load_check(job.candidate.artifact_ref)
            if (result.get('core_id') != str(job.core.public_id) or result.get('check_fence') != job.fence
                    or result.get('candidate_sha256') != job.candidate.candidate_sha256
                    or result.get('binding_fingerprint') != job.candidate.binding_fingerprint):
                raise ValueError('check_mismatch')
            if result.get('status') == 'succeeded':
                result = {**result, 'config_sha256': job.candidate.candidate_sha256}
            return _envelope(job, result)
        except Exception:
            return _envelope(job, {'status': 'unknown', 'error_code': 'reconciliation_unavailable'})
    return _binding(job.core).executor.reconcile_job(job)
