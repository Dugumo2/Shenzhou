"""仅登录后可读的指南目录，不创建服务、任务或交付资源。"""
from .api import endpoint, error, success
from .guide_content import CLIENT_FILTERS, guide_catalog


@endpoint()
def guides(request):
    query = request.GET.get('q', '').strip()
    client = request.GET.get('client', '').strip()
    if len(query) > 200 or client not in ('', 'all', *(value['id'] for value in CLIENT_FILTERS)):
        return error('invalid_filter', '搜索内容过长或软件筛选无效。', 422)
    return success(guide_catalog(query=query, client=None if client in ('', 'all') else client))
