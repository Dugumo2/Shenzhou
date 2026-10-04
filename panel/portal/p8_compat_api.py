"""本人 P8 已有资源读取：只复用原地址，不创建下载凭据或套餐。"""

from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from .api import endpoint, error, success
from .p8_compat import P8SourceError, _source_config, current_binding, load_p8_source


CLIENT_RESOURCES = {
    'android': ('android',),
    'v2rayng': ('v2rayng', 'v2rayng-routes', 'v2rayng-geosite', 'v2rayng-geoip'),
    'windows': ('windows', 'windows-routing'),
}
LABELS = {'android': '完整配置', 'v2rayng': '节点订阅', 'v2rayng-routes': '路由资源',
          'v2rayng-geosite': '域名资源', 'v2rayng-geoip': 'IP 资源',
          'windows': '节点订阅', 'windows-routing': '原生路由资源'}


@endpoint()
@sensitive_variables()
def delivery(request, public_id):
    clients = request.GET.getlist('client_adapter')
    if len(clients) != 1 or clients[0] not in (*CLIENT_RESOURCES, 'router'):
        return error('invalid_filter', '请选择支持的软件。', 422)
    client = clients[0]
    # 数据库一致快照使同次查询的owner/核验者/源绑定使用同一代状态。
    with transaction.atomic():
        binding = current_binding(request.user, public_id)
        if binding is None:
            return error('not_found', '服务不存在或不可访问。', 404)
        result = {'service_id': str(binding.public_id), 'client_id': client, 'state': 'blocked',
                  'message': '资源尚未完成核验，请联系管理员。', 'resources': [],
                  'runtime_acceptance': 'not_tested'}
        if client == 'router':
            result.update(state='unsupported', message='路由器适配尚未验收。')
            return success(result)
        config = _source_config(binding)
        declared = config.get('resources') if config is not None else None
        # 未核格式在消费受限文件之前关闭；Windows素材包不冒充原生路由。
        if (type(declared) is not dict or any(type(declared.get(key)) is not dict
                or declared[key].get('verified') is not True for key in CLIENT_RESOURCES[client])):
            return success(result)
        try:
            source = load_p8_source(config, binding.links_sha256, binding.evidence_sha256)
        except P8SourceError:
            return success(result)
        resources = [source.resource(key) for key in CLIENT_RESOURCES[client]]
        if not all(resources):
            return success(result)
        result.update(state='available', message='可复制资源链接；客户端导入和连接仍需实际核验。', resources=[
            {'key': item.kind, 'label': LABELS[item.kind], 'download_url': item.url,
             'verification': 'http_format_verified'} for item in resources])
        response = success(result)
        response['Cache-Control'] = 'no-store, private'
        response['Referrer-Policy'] = 'no-referrer'
        return response
