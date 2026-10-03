"""同源会话和只读服务 API；写能力由独立工作包接入。"""
from django.urls import path

from . import api
from . import admin_api, guide_api
from . import rule_api, usage_api, candidate_delivery


app_name = 'api'
urlpatterns = [
    path('catalog/guides', guide_api.guides, name='guides'),
    path('admin/overview', admin_api.overview, name='admin_overview'),
    path('admin/users', admin_api.users, name='admin_users'),
    path('admin/users/<str:user_id>', admin_api.user_detail, name='admin_user_detail'),
    path('session', api.session, name='session'),
    path('login', api.login, name='login'),
    path('logout', api.logout, name='logout'),
    path('me/services', api.my_services, name='my_services'),
    path('me/services/<str:public_id>', api.my_service_detail, name='my_service_detail'),
    path('me/services/<str:public_id>/usage', usage_api.service_usage, name='service_usage'),
    path('me/services/<str:public_id>/delivery', candidate_delivery.delivery, name='candidate_delivery'),
    path('me/services/<str:public_id>/delivery/<str:client_id>/resources/<str:resource>', candidate_delivery.download_resource, name='candidate_resource'),
    path('catalog/clients', api.clients, name='clients'),
    path('catalog/clients/<str:client_id>/guide', api.client_guide, name='client_guide'),
    path('admin/services', api.admin_services, name='admin_services'),
    path('admin/rules', rule_api.rules, name='admin_rules'),
    path('admin/rules/preview', rule_api.preview, name='rule_preview'),
    path('admin/rules/<int:rule_id>', rule_api.rule_detail, name='rule_detail'),
]
