"""隔离候选的同源前端入口；生产静态资源由部署服务器负责。"""
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.views.decorators.http import require_safe


def workspace_configured():
    """旧账号/注册流程返回同一工作区；生产仍必须显式配置构建版本。"""
    return settings.FRONTEND_ENABLED and (not settings.PANEL_LIVE or bool(
        getattr(settings, 'FRONTEND_RELEASE_ROOT', '') and getattr(settings, 'FRONTEND_MANIFEST_SHA256', '')))


@require_safe
def workspace(request, asset=''):
    if not settings.FRONTEND_ENABLED:
        raise Http404
    if settings.PANEL_LIVE:
        from .frontend_release import release_asset
        return release_asset(request, asset)
    root = (settings.BASE_DIR / 'frontend' / 'dist').resolve()
    path = (root / (asset or 'index.html')).resolve()
    # 仅提供构建目录，既不提供源码，也不允许路径逃逸。
    if not path.is_relative_to(root) or not path.is_file() or path.suffix not in {'.html', '.js', '.css', '.svg', '.png', '.woff2'}:
        raise Http404
    return FileResponse(path.open('rb'))


@require_safe
def workspace_root(request):
    return HttpResponseRedirect('/app/')
