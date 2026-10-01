"""用户开通事务协调器；先检查产物，失败后只停用本次用户。

本模块不主动加载生产路径。部署必须注入受限适配器，并在单个 root 工作者
中调用。日志只存 UUID、动作和阶段，不存凭据或命令输出。进程意外退出后
先执行 recover；不能因为网页任务消失就忽略可能已启用的账号。
"""
import json
import os
from pathlib import Path
import secrets
import uuid

from .identity import build_candidate


class LifecycleError(ValueError):
    """仅返回固定错误码。"""


def _uuid(value):
    try:
        value = uuid.UUID(str(value))
        if not value.int:
            raise ValueError()
        return str(value)
    except (ValueError, TypeError, AttributeError):
        raise LifecycleError('identifier_invalid') from None


def _checkpoint(row, job=None):
    """只接受固定的无秘密检查点字段，读取与写入使用同一约束。"""
    try:
        if (type(row) is not dict or set(row) != {'job', 'owner', 'action', 'state'}
                or _uuid(row['job']) != row['job'] or _uuid(row['owner']) != row['owner']
                or (job is not None and row['job'] != job)
                or row['action'] not in ('provision', 'enable', 'suspend')
                or row['state'] not in ('applying', 'core_applied', 'complete',
                                        'compensating', 'compensated', 'recovery_required')):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise LifecycleError('journal_invalid') from None
    return dict(row)


def _unique_fields(pairs):
    row = {}
    for key, value in pairs:
        if key in row:
            raise ValueError()
        row[key] = value
    return row


def _owned(authority, owner):
    """确认整个所属命名空间，额外或重复账户不能被八个预期 ID 掩盖。"""
    prefix = 'pnl-' + uuid.UUID(owner).hex + '-'
    accounts = [account for account in authority['accounts'] if account['id'].startswith(prefix)]
    result = {account['id']: account for account in accounts}
    if len(result) != len(accounts):
        raise LifecycleError('owner_account_duplicate')
    return result


class Journal:
    """受限目录内的持久检查点，原子替换且同步文件与目录。"""

    def __init__(self, directory):
        self.directory = Path(directory)
        if self.directory.is_symlink():
            raise LifecycleError('journal_path_invalid')
        self.directory.mkdir(parents=True, exist_ok=True,
                             mode=0o700 if os.name == 'posix' else 0o777)

    def read(self, job):
        path = self.directory / (_uuid(job) + '.json')
        if path.is_symlink():
            raise LifecycleError('journal_path_invalid')
        if not path.exists():
            return None
        try:
            with path.open('rb') as stream:
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError()
            result = json.loads(raw, object_pairs_hook=_unique_fields)
            return _checkpoint(result, _uuid(job))
        except (ValueError, TypeError, KeyError):
            raise LifecycleError('journal_invalid') from None

    def write(self, row):
        row = _checkpoint(row)
        path = self.directory / (_uuid(row['job']) + '.json')
        if path.is_symlink():
            raise LifecycleError('journal_path_invalid')
        temp = self.directory / ('.checkpoint-' + secrets.token_hex(12))
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(json.dumps(row, sort_keys=True).encode('ascii'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
            if os.name == 'posix':
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temp.unlink(missing_ok=True)

    def pending(self):
        # 损坏、非规范名称或消失的 JSON 检查点阻止新开通；临时文件未提交。
        rows = []
        for path in sorted(self.directory.glob('*.json')):
            if _uuid(path.stem) != path.stem:
                raise LifecycleError('journal_invalid')
            row = self.read(path.stem)
            if row is None:
                raise LifecycleError('journal_invalid')
            rows.append(row)
        return [row for row in rows if row['state'] not in ('complete', 'compensated')]


class Lifecycle:
    """backend 复用现有管理器；artifact_store 为已校验产物提供原子发布。

    artifact_store.publish(owner,payloads) 必须检查该用户当前版本，再完整发布。
    外层工作者负责跨进程串行化；本类不提供锁，CAS 不能代替整个事务的互斥。
    complete 是该任务的历史结果，不是后来其他任务执行后的用户现状；网站
    写入 applied 前仍须检查任务版本和当前许可，不能用旧任务重放覆盖新状态。
    额度/到期等业务许可必须在外层再次检查，不能把本类作为公网接口。
    recovery_required 必须停止新任务；终态检查点写入不确定时，先恢复检查点
    再重试同一任务，不能把异常直接当作配置未应用。
    """

    def __init__(self, backend, artifact_store, journal):
        self.backend, self.artifacts, self.journal = backend, artifact_store, journal

    def _state(self, row, state):
        changed = dict(row, state=state)
        self.journal.write(changed)
        row.update(changed)

    def _suspend(self, row):
        # 之前的 applying/core_applied 检查点已存在；更新失败不能阻止实际停用。
        try:
            self._state(row, 'compensating')
        except Exception:
            pass
        try:
            current, digest = self.backend.snapshot()
            owned = _owned(current, row['owner'])
            expected = set()
            if owned:
                candidate, _ = build_candidate(current, row['owner'], 'suspend')
                self.backend.validate(candidate)
                if candidate != current:
                    self.backend.publish(candidate, expected_hash=digest)
                expected = set(_owned(candidate, row['owner']))
            # 即便初次快照没有该用户，也复查一次，避免直接写出虚假的已补偿。
            confirmed, _ = self.backend.snapshot()
            observed = _owned(confirmed, row['owner'])
            if expected != observed.keys() or any(account['enabled'] for account in observed.values()):
                raise LifecycleError('suspension_not_confirmed')
            self._state(row, 'compensated')
        except Exception:
            try:
                self._state(row, 'recovery_required')
            except Exception:
                # 已有未完成检查点仍需人工/下次启动恢复；不暴露底层错误正文。
                pass
            raise LifecycleError('recovery_required') from None
        return {'job': row['job'], 'state': 'compensated'}

    def recover(self):
        return [self._suspend(row) for row in self.journal.pending()]

    def execute(self, job, owner, action):
        job, owner = _uuid(job), _uuid(owner)
        if action not in ('provision', 'enable', 'suspend'):
            raise LifecycleError('action_invalid')
        previous = self.journal.read(job)
        if previous:
            if previous['owner'] != owner or previous['action'] != action:
                raise LifecycleError('job_reused')
            if previous['state'] in ('complete', 'compensated'):
                return {'job': job, 'state': previous['state']}
            return self._suspend(previous)
        if self.journal.pending():
            raise LifecycleError('recovery_required')
        try:
            current, digest = self.backend.snapshot()
            candidate, account_ids = build_candidate(current, owner, action)
            self.backend.validate(candidate)
            # 生成器必须用本人八身份，先检查全部客户端；暂停无需导出停用凭据。
            payloads = self.backend.render(candidate, account_ids) if action != 'suspend' else None
        except Exception:
            raise LifecycleError('candidate_preparation_failed') from None
        row = {'job': job, 'owner': owner, 'action': action, 'state': 'applying'}
        try:
            self.journal.write(row)
        except Exception:
            raise LifecycleError('checkpoint_write_failed') from None
        try:
            if candidate != current:
                self.backend.publish(candidate, expected_hash=digest)
            confirmed, _ = self.backend.snapshot()
            wanted = _owned(candidate, owner)
            actual = _owned(confirmed, owner)
            if wanted != actual:
                raise LifecycleError('application_not_confirmed')
            self._state(row, 'core_applied')
            if payloads is not None:
                self.artifacts.publish(owner, payloads)
                confirmed, _ = self.backend.snapshot()
                if wanted != _owned(confirmed, owner):
                    raise LifecycleError('application_not_confirmed')
        except Exception:
            self._suspend(row)
            raise LifecycleError('application_failed_user_suspended') from None
        try:
            self._state(row, 'complete')
        except Exception:
            # replace 后的 fsync 也可能失败：此时 complete 可能已经可见。
            # 发布实际已完成，不能再停用核心却留下无法改写的 complete。
            # 按崩溃恢复处理：旧检查点会补偿，新检查点则保持完成结果。
            raise LifecycleError('recovery_required') from None
        return {'job': job, 'state': 'complete'}
