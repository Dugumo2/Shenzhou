"""由主应用在/api/v1/下包含；服务ID是稳定公开UUID。"""
from django.urls import path
from . import billing_api

urlpatterns = [
    path('admin/services/<uuid:public_id>/billing', billing_api.billing, name='api_billing'),
    path('admin/services/<uuid:public_id>/billing/preview', billing_api.preview, name='api_billing_preview'),
]
