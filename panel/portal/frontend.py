"""隔离候选的同源前端入口；生产静态资源由部署服务器负责。"""
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.views.decorators.http import require_safe


@require_safe
def workspace(request, asset=''):
    if not settings.FRONTEND_ENABLED or settings.PANEL_LIVE:
        raise Http404
    root = (settings.BASE_DIR / 'frontend' / 'dist').resolve()
    path = (root / (asset or 'index.html')).resolve()
    # 仅提供构建目录，既不提供源码，也不允许路径逃逸。
    if not path.is_relative_to(root) or not path.is_file() or path.suffix not in {'.html', '.js', '.css', '.svg', '.png', '.woff2'}:
        raise Http404
    return FileResponse(path.open('rb'))


@require_safe
def workspace_root(request):
    return HttpResponseRedirect('/app/')
