"""每用户不可变产物版本；经校验的整批文件写完后切换单个清单。"""
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import uuid

ARTIFACTS = {
    'windows': ('windows.txt', 'windows'),
    'android': ('android.json', 'android'),
    'v2rayng': ('v2rayng.txt', 'v2rayng'),
    'windows-rules': ('windows-rules.json', 'windows'),
    'v2rayng-routes': ('v2rayng-routes.json', 'v2rayng'),
    'v2rayng-geosite': ('megabox-geosite.dat', 'v2rayng'),
    'v2rayng-geoip': ('megabox-geoip.dat', 'v2rayng'),
}
LIMIT = 16 * 1024 * 1024
VERSION = re.compile(r'[a-f0-9]{32}\Z')


class ArtifactError(ValueError):
    """错误码不包含秘密路径、内容或令牌。"""


def require(value, code):
    if not value:
        raise ArtifactError(code)


def owner_directory(root, owner):
    try:
        owner = str(uuid.UUID(str(owner)))
    except (ValueError, AttributeError, TypeError):
        raise ArtifactError('owner_invalid') from None
    root = Path(root)
    require(not root.is_symlink(), 'root_symlink')
    path = root.resolve() / owner
    require(not path.is_symlink(), 'owner_symlink')
    return path


def _write(path, data):
    with path.open('xb') as stream:
        if os.name == 'posix':
            os.chmod(path, 0o640)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _sync_directory(path):
    if os.name == 'posix':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish(root, owner, payloads, *, expected_version=None):
    """输入必须已经过客户端生成器语义检查；本层只管隔离与完整发布。

    每次要求三种节点载荷齐全。资源文件可选，但不得继承旧版资源混用。
    expected_version=None 仅允许初次发布；后续必须传当前版本，防止覆盖。
    """
    require(type(payloads) is dict and {'windows', 'android', 'v2rayng'} <= payloads.keys()
            and payloads.keys() <= ARTIFACTS.keys(), 'payload_keys_invalid')
    require(all(type(raw) is bytes and 0 < len(raw) <= LIMIT for raw in payloads.values()), 'payload_size_invalid')
    directory = owner_directory(root, owner)
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    releases = directory / 'releases'
    require(not releases.is_symlink(), 'releases_symlink')
    releases.mkdir(exist_ok=True, mode=0o750)
    lock = directory / '.publish-lock'
    try:
        lock.mkdir(mode=0o700 if os.name == 'posix' else 0o777)
    except FileExistsError:
        raise ArtifactError('publication_busy') from None
    temp = directory / ('.manifest-' + secrets.token_hex(16))
    try:
        current = directory / 'current.json'
        require(not current.is_symlink(), 'manifest_symlink')
        previous = _manifest(directory, owner)['version'] if current.exists() else None
        require(previous == expected_version, 'publication_version_conflict')
        version = secrets.token_hex(16)
        target = releases / version
        target.mkdir(mode=0o750)
        manifest = {'schema': 1, 'owner': directory.name, 'version': version, 'files': {}}
        for artifact, raw in payloads.items():
            name = ARTIFACTS[artifact][0]
            _write(target / name, raw)
            manifest['files'][artifact] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        _sync_directory(target)
        _sync_directory(releases)
        _write(temp, json.dumps(manifest, sort_keys=True).encode('utf8'))
        os.replace(temp, current)
        _sync_directory(directory)
        return version
    finally:
        temp.unlink(missing_ok=True)
        lock.rmdir()


def _manifest(directory, owner):
    path = directory / 'current.json'
    require(not path.is_symlink() and path.is_file(), 'manifest_missing')
    with path.open('rb') as stream:
        raw = stream.read(65537)
    require(len(raw) <= 65536, 'manifest_too_large')
    try:
        value = json.loads(raw)
        require(type(value) is dict and value.get('schema') == 1 and value.get('owner') == str(uuid.UUID(str(owner)))
                and type(value.get('version')) is str and VERSION.fullmatch(value['version'])
                and type(value.get('files')) is dict and value['files'].keys() <= ARTIFACTS.keys(), 'manifest_invalid')
    except (ValueError, TypeError):
        raise ArtifactError('manifest_invalid') from None
    return value


def read_artifact(root, owner, artifact):
    """读取同一版本的有界字节并核对摘要；返回值就是将被交付的字节。"""
    require(artifact in ARTIFACTS, 'artifact_unknown')
    directory = owner_directory(root, owner)
    value = _manifest(directory, owner)
    spec = value['files'].get(artifact)
    require(type(spec) is dict and type(spec.get('bytes')) is int and 0 < spec['bytes'] <= LIMIT
            and type(spec.get('sha256')) is str and re.fullmatch('[a-f0-9]{64}', spec['sha256']), 'artifact_spec_invalid')
    release = directory / 'releases' / value['version']
    path = release / ARTIFACTS[artifact][0]
    require(not release.parent.is_symlink() and not release.is_symlink() and not path.is_symlink()
            and path.is_file(), 'artifact_missing')
    with path.open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    require(len(raw) == spec['bytes'] and hashlib.sha256(raw).hexdigest() == spec['sha256'], 'artifact_integrity')
    return raw


def available(root, owner, artifact):
    try:
        read_artifact(root, owner, artifact)
        return True
    except (ArtifactError, OSError):
        return False
