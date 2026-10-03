from django.conf import settings

def environment(request):
    return {'is_live': settings.PANEL_LIVE, 'site_brand': settings.SITE_BRAND,
            'workspace_v2': settings.WORKSPACE_V2,
            'frontend_workspace': settings.FRONTEND_ENABLED and not settings.PANEL_LIVE,
            'monitor_url': settings.MONITOR_URL if request.user.is_authenticated and request.user.is_staff else ''}
