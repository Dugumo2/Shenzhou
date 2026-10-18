"""固定核心的受限配置切换；未知结果只能核实，不能自动重放。"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


class ReleaseError(Exception):
    """错误码不携带配置、路径或底层异常文本。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise ReleaseError(code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _private(path):
    info = path.lstat()
    _need(not path.is_symlink(), 'unsafe_path')
    _need(os.name == 'posix', 'private_permissions_unverified')
    _need(info.st_uid == os.geteuid() and not stat.S_IMODE(info.st_mode) & 0o077,
          'unsafe_permissions')


class CoreReleaseExecutor:
    """只接受检查器的固定绑定，不解析网页路径、命令或系统服务名。

    control_guard 必须与身份撤销及配置写入共用互斥协议，覆盖整个执行窗口。
    adapter 必须固定 restart/observe 能力；此模块不提供生产系统命令适配器。
    """

    def __init__(self, checker, adapter, *, control_guard=None, fence_guard=None,
                 identity_guard=None, private_path_guard=None):
        self.checker = checker
        self.profile = checker.profile
        self.adapter = adapter
        self.control_guard = control_guard
        self.fence_guard = fence_guard
        self.identity_guard = identity_guard
        self.private_path_guard = private_path_guard or _private
        self.directory = Path(self.profile.artifact_root) / '.release'

    def _path(self, path):
        path = Path(path)
        _need(path.is_absolute(), 'unsafe_path')
        for parent in (path, *path.parents):
            _need(not parent.is_symlink(), 'unsafe_path')
        self.private_path_guard(path)
        return path

    def _read(self, path, limit=8 * 1024 * 1024):
        path = self._path(path)
        _need(path.is_file(), 'unsafe_path')
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            _need(stat.S_ISREG(info.st_mode) and info.st_size <= limit, 'invalid_material')
            data = stream.read(limit + 1)
        _need(len(data) <= limit, 'invalid_material')
        return data

    @staticmethod
    def _sync_directory(path):
        if os.name == 'posix':
            descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _write(self, target, data):
        """临时文件从创建起即0600，并在替换前持久化。"""
        self._path(target.parent)
        _need(not target.is_symlink(), 'unsafe_path')
        temp = target.parent / ('.core-release-' + uuid.uuid4().hex)
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self._path(temp)
            os.replace(temp, target)
            self._sync_directory(target.parent)
        finally:
            if temp.exists():
                temp.unlink()

    def _save(self, name, value):
        self._write(self.directory / name,
                    json.dumps(value, sort_keys=True, separators=(',', ':')).encode('ascii'))

    def _finish(self, binding):
        state = self._load('state.json')
        if state and state.get('pending') == binding['job_id'] and state.get('fence') == binding['fence']:
            self._save('state.json', {'fence': binding['fence'], 'pending': ''})

    @staticmethod
    def _request(job):
        return {'base_sha256': job.candidate.base_sha256, 'artifact_ref': job.candidate.artifact_ref,
                'checked_fence': getattr(job.candidate, 'checked_fence', None),
                'expected_revision': job.expected_revision}

    def _load(self, name, default=None):
        path = self.directory / name
        if not path.exists() and not path.is_symlink():
            return default
        try:
            value = json.loads(self._read(path, 65536))
        except (ValueError, UnicodeError):
            raise ReleaseError('invalid_journal') from None
        _need(type(value) is dict, 'invalid_journal')
        return value

    @contextmanager
    def _lock(self):
        self._path(Path(self.profile.artifact_root))
        # Windows必须先由注入的受信ACL验证器确认父目录，默认在上一步失败关闭。
        self.directory.mkdir(mode=0o700 if os.name == 'posix' else 0o777, exist_ok=True)
        self._path(self.directory)
        path = self.directory / 'executor.lock'
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            self._path(path)
            if os.name == 'posix':
                import fcntl
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ReleaseError('executor_busy') from None
            else:
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b'0')
                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise ReleaseError('executor_busy') from None
            yield
        finally:
            os.close(descriptor)

    def _binding(self, job):
        candidate = job.candidate
        values = {'job_id': str(job.public_id), 'core_id': str(job.core.public_id),
                  'fence': job.fence, 'action': job.action,
                  'candidate_sha256': candidate.candidate_sha256,
                  'binding_fingerprint': candidate.binding_fingerprint}
        _need(re.fullmatch(r'[0-9a-f-]{36}', values['job_id']) is not None, 'invalid_job')
        _need(values['core_id'] == str(self.profile.core_id), 'wrong_core')
        _need(type(job.fence) is int and job.fence > 0, 'invalid_fence')
        _need(job.action in ('apply', 'rollback'), 'unsupported_action')
        _need(all(type(values[key]) is str and re.fullmatch(r'[a-f0-9]{64}', values[key])
                  for key in ('candidate_sha256', 'binding_fingerprint')), 'invalid_job')
        return values

    @staticmethod
    def _receipt(binding, status, code='', config_sha256=''):
        return dict(binding, status=status, error_code=code, config_sha256=config_sha256)

    def _validate(self, job, binding):
        _need(self.control_guard is not None and self.fence_guard is not None
              and self.identity_guard is not None, 'authority_guard_unavailable')
        _need(self.fence_guard(job) is True, 'stale_fence')
        _need(self.checker.verify_binding() == binding['binding_fingerprint'], 'binding_changed')
        _need(self.profile.generator_mode == 'single-json'
              and Path(self.profile.source_path) == Path(self.profile.active_path), 'unsupported_generator')
        artifact = self.checker.load_artifact(job.candidate.artifact_ref)
        base, candidate = artifact['base'], artifact['candidate']
        _need(_sha(base) == job.candidate.base_sha256 and _sha(candidate) == binding['candidate_sha256'],
              'artifact_changed')
        check = self.checker.load_check(job.candidate.artifact_ref)
        _need(check is not None and check.get('status') == 'succeeded', 'check_required')
        for key, expected in (('base_sha256', _sha(base)), ('candidate_sha256', _sha(candidate)),
                              ('binding_fingerprint', binding['binding_fingerprint']),
                              ('identity_generation', self.profile.identity_generation),
                              ('check_fence', getattr(job.candidate, 'checked_fence', None))):
            _need(check.get(key) == expected, 'check_mismatch')
        checked = check.get('check_fence')
        _need(type(checked) is int and 0 < checked < job.fence, 'stale_check')
        expected = _sha(base) if job.action == 'apply' else _sha(candidate)
        target = candidate if job.action == 'apply' else base
        _need(_sha(self._read(Path(self.profile.source_path))) == expected, 'source_changed')
        _need(self.identity_guard(self.profile, target) is True, 'identity_changed')
        return target, expected

    def _observe(self, job, binding, target_hash):
        """文件hash不能证明进程已加载；必须取得相同作业的运行证明。"""
        observed = self.adapter.observe(self.profile, binding['job_id'])
        actual = _sha(self._read(Path(self.profile.active_path)))
        if (type(observed) is dict and observed.get('job_id') == binding['job_id']
                and observed.get('status') == 'succeeded' and observed.get('running') is True
                and observed.get('config_sha256') == target_hash == actual):
            return self._receipt(binding, 'succeeded', config_sha256=actual)
        return self._receipt(binding, 'unknown', 'runtime_unconfirmed')

    def execute_job(self, job):
        """重复作业仅返持久回执；任一副作用异常保留UNKNOWN。"""
        binding = self._binding(job)
        attempted = False
        try:
            with self._lock():
                name = binding['job_id'] + '.json'
                record = self._load(name)
                if record:
                    _need(record.get('binding') == binding and record.get('request') == self._request(job),
                          'job_replay_mismatch')
                    if record['receipt']['status'] == 'succeeded':
                        self._finish(binding)
                    return record['receipt']
                state = self._load('state.json', {'fence': 0, 'pending': ''})
                _need(not state['pending'], 'unresolved_job')
                _need(job.fence > state['fence'], 'stale_fence')
                _need(self.control_guard is not None, 'authority_guard_unavailable')
                with self.control_guard(job):
                    target, previous = self._validate(job, binding)
                    record = {'binding': binding, 'request': self._request(job),
                              'target_sha256': _sha(target), 'previous_sha256': previous,
                              'receipt': self._receipt(binding, 'unknown', 'execution_pending')}
                    # 先保留全核阻断，再写作业回执；任一崩溃都不能绕过未决动作。
                    self._save('state.json', {'fence': job.fence, 'pending': binding['job_id']})
                    attempted = True
                    self._save(name, record)
                    self._write(Path(self.profile.active_path), target)
                    self.adapter.restart(self.profile, binding['job_id'])
                    record['receipt'] = self._observe(job, binding, _sha(target))
                    self._save(name, record)
                    if record['receipt']['status'] == 'succeeded':
                        self._finish(binding)
                    return record['receipt']
        except Exception as exc:
            code = exc.code if isinstance(exc, ReleaseError) else 'execution_unconfirmed' if attempted else 'validation_failed'
            return self._receipt(binding, 'unknown' if attempted else 'failed', code)

    def reconcile_job(self, job):
        """只读核实际文件/运行状态；不写配置、不重启、不自动回退。"""
        binding = self._binding(job)
        try:
            with self._lock():
                name = binding['job_id'] + '.json'
                record = self._load(name)
                _need(record is not None and record.get('binding') == binding
                      and record.get('request') == self._request(job), 'journal_unavailable')
                if record['receipt']['status'] != 'unknown':
                    if record['receipt']['status'] == 'succeeded':
                        self._finish(binding)
                    return record['receipt']
                _need(self.control_guard is not None and self.fence_guard is not None
                      and self.identity_guard is not None, 'authority_guard_unavailable')
                with self.control_guard(job):
                    _need(self.fence_guard(job) is True, 'stale_fence')
                    _need(self.checker.verify_binding() == binding['binding_fingerprint'], 'binding_changed')
                    active = self._read(Path(self.profile.active_path))
                    _need(self.identity_guard(self.profile, active) is True, 'identity_changed')
                    receipt = self._observe(job, binding, record['target_sha256'])
                    record['receipt'] = receipt
                    self._save(name, record)
                    if receipt['status'] == 'succeeded':
                        self._finish(binding)
                    return receipt
        except Exception as exc:
            code = exc.code if isinstance(exc, ReleaseError) else 'reconciliation_unconfirmed'
            return self._receipt(binding, 'unknown', code)
