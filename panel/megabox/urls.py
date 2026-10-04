from django.conf import settings
from django.urls import include, path
from portal.frontend import workspace, workspace_root, workspace_configured

urlpatterns = [
    path('api/v1/', include('portal.api_urls')),
    path('api/v1/', include('portal.billing_urls')),
    path('app/', workspace, name='workspace'),
    path('app/<path:asset>', workspace, name='workspace_asset'),
]
if workspace_configured():
    urlpatterns.append(path('', workspace_root))
urlpatterns.append(path('', include('portal.urls')))
