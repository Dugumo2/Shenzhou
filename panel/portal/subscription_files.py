"""独立订阅产物读取；发布器提供摘要，拒绝串用旧用户共享目录。"""
import hashlib
import json
import re
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from .delivery import resolve_subscription

FILENAMES = {'windows': ('subscription.txt', 'text/plain; charset=utf-8'),
             'v2rayng': ('subscription.txt', 'text/plain; charset=utf-8'),
             'android': ('subscription.json', 'application/json')}


def _read(sub, resource='subscription'):
    root = Path(settings.ARTIFACT_ROOT).absolute()
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise ValidationError('产物根目录不可重定向')
    root = root.resolve()
    directory = root / 'subscriptions' / str(sub.public_id) / str(sub.generation)
    try:
        filename, content_type = FILENAMES[sub.client]
        manifest_path = directory / ('current.json' if (directory / 'current.json').exists() else 'manifest.json')
        for candidate in (root / 'subscriptions', directory.parent, directory, manifest_path):
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
                raise ValueError
        if manifest_path.stat().st_size > 32768:
            raise ValueError
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if (manifest.get('subscription') != str(sub.public_id) or manifest.get('generation') != sub.generation
                or manifest.get('client') != sub.client or manifest.get('evidence_scope') != 'production'):
            raise ValueError
        expected_hash, expected_size = manifest.get('sha256'), None
        if manifest.get('schema') == 2:
            release = manifest['release']
            item = manifest['resources'][resource]
            filename = item['filename']
            if not re.fullmatch(r'[a-f0-9]{32}', release) or not re.fullmatch(r'[a-z][a-z0-9.-]{0,90}', filename):
                raise ValueError
            directory = directory / 'releases' / release
            expected_hash, expected_size = item['sha256'], item['bytes']
            content_type = item['content_type']
            if content_type not in {'application/json', 'application/octet-stream', 'text/plain; charset=utf-8'}:
                raise ValueError
            for candidate in (directory.parent, directory):
                if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
                    raise ValueError
        elif resource != 'subscription':
            raise ValueError
        path = directory / filename
        # 所有层级均禁止符号链接跳转；路径永不由请求直接指定。
        for candidate in (root / 'subscriptions', directory.parent, directory, manifest_path, path):
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(root):
                raise ValueError
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError
        content = path.read_bytes()
        if (not content or hashlib.sha256(content).hexdigest() != expected_hash
                or (expected_size is not None and len(content) != expected_size)):
            raise ValueError
        return content, content_type
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValidationError('此订阅产物尚未完成发布核验')


def available(sub, resource='subscription'):
    try:
        _read(sub, resource)
        return True
    except ValidationError:
        return False


def download_artifact(token, resource='subscription'):
    sub = resolve_subscription(token)
    content, content_type = _read(sub, resource)
    record = sub.entitlement
    now = timezone.now()
    cycle = record.cycles.get(starts_at__lte=now, ends_at__gt=now)
    # 入口双向合计作为 download 字段呈现，不声称单独下载流量。
    userinfo = f'upload=0; download={cycle.used_bytes}; total={record.quota_bytes}; expire={int(record.expires_at.timestamp())}'
    return {'content': content, 'content_type': content_type, 'userinfo': userinfo}
