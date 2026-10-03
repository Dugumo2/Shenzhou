"""同源会话和只读服务 API；写能力由独立工作包接入。"""
from django.urls import path

from . import api
from . import admin_api, guide_api


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
    path('catalog/clients', api.clients, name='clients'),
    path('catalog/clients/<str:client_id>/guide', api.client_guide, name='client_guide'),
    path('admin/services', api.admin_services, name='admin_services'),
    path('admin/rules', api.admin_rules, name='admin_rules'),
]
