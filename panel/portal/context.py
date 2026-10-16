import hashlib
from functools import lru_cache
from pathlib import Path
from django.conf import settings
from .frontend import workspace_configured

@lru_cache(maxsize=8)
def legacy_asset_version(root):
    """不可变发行目录的公开资源内容版本，避开浏览器/CDN缓存的旧失败响应。"""
    digest = hashlib.sha256()
    for name in ('portal.css', 'v2.css', 'portal.js', 'v2.js'):
        digest.update(name.encode())
        digest.update((Path(root) / 'portal' / 'static' / 'portal' / name).read_bytes())
    return digest.hexdigest()[:16]

def environment(request):
    return {'is_live': settings.PANEL_LIVE, 'site_brand': settings.SITE_BRAND,
            'legacy_asset_version': legacy_asset_version(str(settings.BASE_DIR)),
            'workspace_v2': settings.WORKSPACE_V2,
            'frontend_workspace': workspace_configured(),
            'monitor_url': settings.MONITOR_URL if request.user.is_authenticated and request.user.is_staff else ''}
