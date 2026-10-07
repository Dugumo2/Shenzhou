"""同源会话和只读服务 API；写能力由独立工作包接入。"""
from django.urls import path

from . import api
from . import admin_api, guide_api
from . import rule_api, usage_api, candidate_delivery
from . import inventory_api, rule_source_api
from . import p8_compat_api
from . import observation_api
from . import rule_policy_api


app_name = 'api'
urlpatterns = [
    path('catalog/guides', guide_api.guides, name='guides'),
    path('admin/overview', admin_api.overview, name='admin_overview'),
    path('admin/resource-usage', admin_api.resource_usage, name='admin_resource_usage'),
    path('admin/observed-site', observation_api.observed_site, name='observed_site'),
    path('admin/users', admin_api.users, name='admin_users'),
    path('admin/users/<str:user_id>', admin_api.user_detail, name='admin_user_detail'),
    path('session', api.session, name='session'),
    path('login', api.login, name='login'),
    path('logout', api.logout, name='logout'),
    path('me/services', api.my_services, name='my_services'),
    path('me/services/<str:public_id>', api.my_service_detail, name='my_service_detail'),
    path('me/services/<str:public_id>/usage', usage_api.service_usage, name='service_usage'),
    path('me/services/<str:public_id>/p8-delivery', p8_compat_api.delivery, name='p8_delivery'),
    path('me/services/<str:public_id>/delivery', candidate_delivery.delivery, name='candidate_delivery'),
    path('me/services/<str:public_id>/delivery/<str:client_id>/resources/<str:resource>', candidate_delivery.download_resource, name='candidate_resource'),
    path('catalog/clients', api.clients, name='clients'),
    path('catalog/clients/<str:client_id>/guide', api.client_guide, name='client_guide'),
    path('admin/services', api.admin_services, name='admin_services'),
    path('admin/rules', rule_api.rules, name='admin_rules'),
    path('admin/rules/preview', rule_api.preview, name='rule_preview'),
    path('admin/rules/<int:rule_id>', rule_api.rule_detail, name='rule_detail'),
    path('admin/rule-sources', rule_source_api.sources, name='rule_sources'),
    path('admin/rule-sources/preview', rule_source_api.preview, name='rule_source_preview'),
    path('admin/rule-sources/commit', rule_source_api.commit, name='rule_source_commit'),
    path('admin/rule-sources/<str:source_id>', rule_source_api.source_detail, name='rule_source_detail'),
    path('admin/rule-policies', rule_policy_api.collection, name='rule_policies'),
    path('admin/rule-policies/options', rule_policy_api.options, name='rule_policy_options'),
    path('admin/rule-policies/<uuid:policy_id>', rule_policy_api.detail, name='rule_policy_detail'),
    path('admin/rule-policies/<uuid:policy_id>/preview', rule_policy_api.preview, name='rule_policy_preview'),
    path('admin/rule-policies/<uuid:policy_id>/bind', rule_policy_api.bind, name='rule_policy_bind'),
    path('admin/rule-policies/<uuid:policy_id>/compile', rule_policy_api.compile, name='rule_policy_compile'),
    path('admin/rule-policies/<uuid:policy_id>/candidates/<uuid:candidate_id>', rule_policy_api.candidate, name='rule_policy_candidate'),
    path('admin/servers', inventory_api.servers, name='inventory_servers'),
    path('admin/servers/<str:public_id>', inventory_api.server_detail, name='inventory_server_detail'),
    path('admin/lines', inventory_api.lines, name='inventory_lines'),
    path('admin/lines/<str:public_id>', inventory_api.line_detail, name='inventory_line_detail'),
]
