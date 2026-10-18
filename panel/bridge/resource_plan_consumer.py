"""受信采购计划消费；同一旧累计上投影，文件成功落盘后才持久确认版本。"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

from . import resource_usage_exporter as exporter


def require(condition, code='unsafe_purchase_projection'):
    if not condition:
        raise ValueError(code)


def encoded(document):
    return (json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def file_digest(document):
    return hashlib.sha256(encoded(document)).hexdigest()


def _database_boundary():
    """只接受现场已核实的固定 SQLite 目录，不能以 root 新建或接管数据库。"""
    from django.conf import settings
    require(os.name == 'posix' and os.geteuid() == 0 and os.getuid() == 0, 'database_owner_required')
    require(threading.current_thread() is threading.main_thread() and threading.active_count() == 1, 'database_owner_single_thread_required')
    require(set(settings.DATABASES) == {'default'} and not settings.DATABASE_ROUTERS, 'database_configuration_refused')
    database = settings.DATABASES['default']
    require(database['ENGINE'] == 'django.db.backends.sqlite3' and str(database['NAME']) == '/var/lib/megabox-panel/panel.sqlite3', 'database_configuration_refused')
    path = Path('/var/lib/megabox-panel/panel.sqlite3')
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'unsafe_database_path')
    directory = path.parent.stat()
    require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == directory.st_gid == 998
            and stat.S_IMODE(directory.st_mode) == 0o700, 'unsafe_database_directory')
    for candidate in (path, *(Path(str(path)+suffix) for suffix in ('-journal', '-wal', '-shm'))):
        require(not candidate.is_symlink(), 'unsafe_database_path')
        if candidate == path or candidate.exists():
            info = candidate.stat()
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == info.st_gid == 998
                    and not info.st_mode & 0o022 and info.st_mode & 0o600 == 0o600, 'unsafe_database_file')


def run_as_database_owner(callback):
    """固定 UID/GID 998 的数据库作用域；两侧关闭连接，异常仍恢复 root。"""
    from django.conf import settings
    from django.db import connections
    require(callable(callback), 'invalid_database_callback')
    test_root = getattr(settings, 'RESOURCE_PLAN_PROJECTION_TEST_ROOT', None)
    if test_root is not None:
        require(not settings.PANEL_LIVE, 'database_test_override_refused')
        ProjectionStore(test_root)
        # 隔离测试不改变操作系统身份，但保留实际连接边界。
        connections.close_all()
        try:
            return callback()
        finally:
            connections.close_all()
    _database_boundary()
    previous_gid, previous_groups = os.getegid(), os.getgroups()
    connections.close_all()
    try:
        os.setgroups([998])
        os.setegid(998)
        os.seteuid(998)
        require(os.geteuid() == os.getegid() == 998, 'database_identity_switch_failed')
        return callback()
    finally:
        try:
            # SQLite 提交、回滚与 journal 清理都必须仍由数据库所有者完成。
            connections.close_all()
        finally:
            try:
                os.seteuid(0)
            finally:
                try:
                    os.setegid(previous_gid)
                finally:
                    os.setgroups(previous_groups)
        require(os.geteuid() == 0 and os.getegid() == previous_gid, 'database_identity_restore_failed')


def load_confirmed_consumptions():
    """在数据库作用域中物化全部有限回执字段，不把惰性 QuerySet 带回 root。"""
    from portal.resource_plan_models import ResourcePlanConsumption
    result = {}
    for value in ResourcePlanConsumption.objects.values('version__public_id', 'status', 'error_code',
                                                        'plan_fingerprint', 'effective_plan_fingerprint'):
        key = str(value.pop('version__public_id'))
        result[key] = value
    return result


class ProjectionStore:
    """生产固定目录；测试目录只能由受信 Python 设置提供，CLI 不接受路径。"""
    def __init__(self, test_root=None):
        self.testing = test_root is not None
        if self.testing:
            from django.conf import settings
            root = Path(test_root)
            require(not settings.PANEL_LIVE and root.is_absolute() and root.name.startswith('purchase-projection-'))
            require(root != exporter.ROOT and (root/'.isolated-purchase-test').is_file())
            self.root, self.plans, self.source = root, root/'plans.json', root/'source.json'
        else:
            require(os.name == 'posix' and os.geteuid() == 0)
            self.root, self.plans, self.source = exporter.ROOT, exporter.PLANS, exporter.SOURCE
        require(not any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (self.root, *self.root.parents)))
        info = self.root.stat()
        require(stat.S_ISDIR(info.st_mode))
        if not self.testing:
            require(info.st_uid == 0 and info.st_gid == 998 and stat.S_IMODE(info.st_mode) == 0o750)
        self.state, self.view, self.journal = (self.root/name for name in ('cycle-state.json', 'usage.json', 'purchase-journal.json'))
        self.authority = self.root/'plan-authority.json'

    def read(self, path, *, optional=False):
        require(not any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)() for p in (path, *path.parents)))
        if optional and not path.exists():
            return None
        if not self.testing and path not in (self.view, self.authority):
            return exporter.protected_json(path)
        info = path.stat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and 0 < info.st_size <= 131072)
        if os.name == 'posix':
            require(not info.st_mode & 0o027 and (self.testing or info.st_uid == 0))
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(131073)
            after = os.fstat(stream.fileno())
        require((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) ==
                (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) ==
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))
        require(len(raw) == info.st_size)
        return json.loads(raw, object_pairs_hook=exporter.unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_json')))

    def write(self, path, document):
        require(path in (self.state, self.view, self.journal, self.authority))
        raw = encoded(document)
        require(len(raw) <= (65536 if path == self.view else 131072))
        if not self.testing:
            shared = path in (self.view, self.authority)
            return exporter.atomic_json(path, document, 0o640 if shared else 0o600, 998 if shared else 0)
        require(not path.is_symlink())
        if path.exists():
            self.read(path)
        fd, name = tempfile.mkstemp(prefix='.'+path.name, dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(name, path)
            self.sync()
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return hashlib.sha256(raw).hexdigest()

    def sync(self):
        if os.name == 'posix':
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @contextmanager
    def lock(self):
        path = self.root/'.export.lock'
        require(not path.is_symlink())
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
            if not self.testing:
                require(info.st_uid == 0 and not info.st_mode & 0o077)
            if os.name == 'posix':
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                if info.st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            yield
        finally:
            os.close(fd)


def _validate_versions(plans, overrides):
    from portal.resource_usage import _digest, _plan
    require(type(overrides) is dict and set(overrides) == {'schema_version', 'entries'} and overrides['schema_version'] == 1)
    sources = {p['id']: p for p in plans}
    require(len(sources) == len(plans))
    for p in plans:
        _plan(p)
    entries = {}
    for entry in overrides['entries']:
        meter = entry['meter_id']
        require(meter not in entries and meter in sources, 'purchase_source_changed')
        require(entry['source_plan'] == sources[meter] and entry['source_fingerprint'] == _digest(sources[meter]), 'purchase_source_changed')
        previous, revision = sources[meter], 1
        for version in entry['versions']:
            revision += 1
            require(version['revision'] == revision and version['before'] == previous, 'purchase_version_out_of_order')
            _plan(version['plan'])
            field = {'quota': 'quota_bytes', 'expiry': 'expires_on', 'cycle': 'cycle'}.get(version['operation'])
            require(field is not None and (previous['source'] == 'residential' or field == 'expires_on'))
            require({k for k in previous if previous[k] != version['plan'][k]} == {field}, 'purchase_version_changed')
            previous = version['plan']
        require(entry['revision'] == revision, 'purchase_version_out_of_order')
        entries[meter] = entry
    return entries


def _publish(store, journal, record):
    """journal 保留到 DB ACK；故障后只重放固定输出，不重新累计或更改回执。"""
    for path, key in ((store.state, 'state'), (store.view, 'view')):
        current = store.read(path, optional=True)
        require(file_digest(current) in (journal['before_'+key], file_digest(journal[key])), 'purchase_recovery_conflict')
    store.write(store.state, journal['state'])
    store.write(store.view, journal['view'])
    if journal['receipt'] is not None:
        record(journal['receipt'])
    store.journal.unlink()
    store.sync()


def _changed_plan(effective, version):
    """只应用本操作的字段，不能复活已被拒绝的其他 desired 字段。"""
    result = deepcopy(effective)
    field = {'quota': 'quota_bytes', 'expiry': 'expires_on', 'cycle': 'cycle'}[version['operation']]
    result[field] = deepcopy(version['plan'][field])
    return result


def consume_once(store, *, now, export_overrides, record, read_confirmed=load_confirmed_consumptions):
    from portal.provider_usage import read_provider_usage
    from portal.resource_usage import _digest, _iso, _time, _plan, project_resource_usage, transition_resource_plan
    with store.lock():
        document = store.read(store.plans)
        require(type(document) is dict and set(document) == {'schema_version', 'plans'} and document['schema_version'] == 1)
        plans = document['plans']
        require(type(plans) is list and 0 < len(plans) <= 16)
        for plan in plans:
            _plan(plan)
        require(len({p['id'] for p in plans}) == len(plans))
        snapshot = read_provider_usage(store.source, now=now)
        require(snapshot.get('state') == 'available', 'purchase_source_unavailable')
        # 面板只读有限权威镜像，不能为了预览开放 root 私有原计划文件。
        store.write(store.authority, {'schema_version': 1, 'observed_at': _iso(now), 'plans': plans})
        entries = _validate_versions(plans, export_overrides())
        source_hash = file_digest(document)
        pending = store.read(store.journal, optional=True)
        if pending:
            require(set(pending) == {'schema_version', 'source_hash', 'state', 'view', 'before_state', 'before_view', 'receipt'}
                    and pending['schema_version'] == 1 and pending['source_hash'] == source_hash, 'purchase_recovery_conflict')
            receipt = pending['receipt']
            if receipt:
                matches = [(e, v) for e in entries.values() for v in e['versions'] if v['id'] == receipt['version_id']]
                require(len(matches) == 1, 'purchase_recovery_conflict')
                entry, version = matches[0]
                require(receipt['revision'] == version['revision'] and receipt['source_fingerprint'] == entry['source_fingerprint']
                        and receipt['plan_fingerprint'] == _digest(version['plan'])
                        and receipt['effective_plan_fingerprint'] == _digest(pending['state']['purchase_tracking'][entry['meter_id']]['plan'])
                        and receipt['state_before_sha256'] == pending['before_state']
                        and receipt['state_after_sha256'] == file_digest(pending['state']), 'purchase_recovery_conflict')
            _publish(store, pending, record)
        previous = store.read(store.state, optional=True)
        tracking = deepcopy((previous or {}).get('purchase_tracking', {}))
        require(type(tracking) is dict and set(tracking) <= set(entries), 'purchase_source_changed')
        # 文件回退不能越过数据库已确认版本；丢失 journal 的半次发布也不能猜测成功。
        confirmed = read_confirmed()
        for meter, entry in entries.items():
            applied_revision = tracking.get(meter, {}).get('revision', 1)
            for version in entry['versions']:
                receipt = confirmed.get(version['id'])
                require((receipt is not None) == (version['revision'] <= applied_revision), 'purchase_state_receipt_conflict')
                if receipt:
                    require(receipt['plan_fingerprint'] == _digest(version['plan']), 'purchase_state_receipt_conflict')
        effective = deepcopy(plans)
        for plan in effective:
            item = tracking.get(plan['id'])
            if item:
                entry = entries[plan['id']]
                require(set(item) == {'revision', 'source_fingerprint', 'plan', 'history'} and item['source_fingerprint'] == entry['source_fingerprint'], 'purchase_state_conflict')
                expected = deepcopy(entry['source_plan'])
                require(type(item['revision']) is int and 2 <= item['revision'] <= entry['revision'], 'purchase_state_conflict')
                for version in entry['versions']:
                    if version['revision'] > item['revision']:
                        break
                    receipt = confirmed[version['id']]
                    require(receipt['status'] in ('consumed', 'rejected'), 'purchase_state_receipt_conflict')
                    if receipt['status'] == 'consumed':
                        expected = _changed_plan(expected, version)
                    else:
                        require(version['operation'] == 'cycle' and receipt['error_code'] == 'cycle_reset_expired', 'purchase_state_receipt_conflict')
                    require(receipt['effective_plan_fingerprint'] == _digest(expected), 'purchase_state_receipt_conflict')
                require(expected == item['plan'], 'purchase_state_conflict')
                plan.update(deepcopy(expected))
        pure = deepcopy(previous)
        if pure is not None:
            pure.pop('purchase_tracking', None)
        state, view = project_resource_usage(snapshot, effective, pure, now=now)

        def commit(next_state, next_view, receipt=None):
            nonlocal previous
            if tracking:
                next_state['purchase_tracking'] = deepcopy(tracking)
            journal = {'schema_version': 1, 'source_hash': source_hash, 'state': next_state, 'view': next_view,
                       'before_state': file_digest(previous), 'before_view': file_digest(store.read(store.view, optional=True)), 'receipt': receipt}
            if receipt:
                receipt.update(state_before_sha256=journal['before_state'], state_after_sha256=file_digest(next_state))
            require(file_digest(store.read(store.plans)) == source_hash, 'purchase_source_changed')
            # 再验证当前 owner/来源；并发新增版本可以等下轮，撤权和来源漂移必须阻断发布。
            fresh = _validate_versions(plans, export_overrides())
            require(all(key in fresh and fresh[key]['source_fingerprint'] == entry['source_fingerprint']
                        and fresh[key]['versions'][:len(entry['versions'])] == entry['versions']
                        for key, entry in entries.items()), 'purchase_source_changed')
            require(len(encoded(next_state)) <= 131072 and len(encoded(next_view)) <= 65536, 'purchase_projection_too_large')
            store.write(store.journal, journal)
            _publish(store, journal, record)
            previous = deepcopy(next_state)

        commit(state, view)
        consumed, rejected = 0, 0
        for position, plan in enumerate(effective):
            entry = entries.get(plan['id'])
            if not entry:
                continue
            revision = tracking.get(plan['id'], {}).get('revision', 1)
            for version in entry['versions']:
                if version['revision'] <= revision:
                    continue
                require(version['revision'] == revision+1, 'purchase_state_conflict')
                require(_time(version['created_at']) <= now, 'purchase_clock_conflict')
                pure = deepcopy(previous)
                pure.pop('purchase_tracking', None)
                history = deepcopy(tracking.get(plan['id'], {}).get('history', []))
                expired = version['operation'] == 'cycle' and _time(version['plan']['cycle']['anchor']) <= now
                if version['operation'] == 'cycle' and not expired:
                    history.append({'version_id': version['id'], 'plan': deepcopy(plan), 'state': deepcopy(pure['meters'][plan['id']]),
                                    'cycle': deepcopy(view['meters'][position]['cycle'])})
                desired_effective = deepcopy(plan) if expired else _changed_plan(plan, version)
                transitioned = transition_resource_plan(pure, plan, desired_effective, now=now)
                plan.update(desired_effective)
                state, view = project_resource_usage(snapshot, effective, transitioned, now=now)
                revision = version['revision']
                tracking[plan['id']] = {'revision': revision, 'source_fingerprint': entry['source_fingerprint'], 'plan': deepcopy(plan), 'history': history}
                receipt = {'version_id': version['id'], 'revision': revision, 'source_fingerprint': entry['source_fingerprint'],
                           'plan_fingerprint': _digest(version['plan']), 'effective_plan_fingerprint': _digest(plan),
                           'state_before_sha256': '', 'state_after_sha256': '', 'consumed_at': _iso(now),
                           'status': 'rejected' if expired else 'consumed', 'error_code': 'cycle_reset_expired' if expired else ''}
                commit(state, view, receipt)
                if expired:
                    rejected += 1
                else:
                    consumed += 1
        return {'status': 'PASS', 'consumed': consumed, 'rejected': rejected, 'meters': len(view['meters']), 'view_sha256': file_digest(view)}
