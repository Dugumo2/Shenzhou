"""按已核服务归属读取资源摘要；请求内复用快照，不触发任何采集。"""
import hashlib
import json

from django.conf import settings


class ServiceUsageSource:
    """只为当前管理员本人提供已绑定来源；不缓存跨请求的权限或时效。"""

    def __init__(self, viewer):
        self.viewer = viewer
        self._snapshot = None

    def read(self, record, kind, item):
        # 服务投影已核验来源关联；复用其结论，避免每个入口重复一套鉴权。
        if (kind != 'p8' or item['state'] == 'mapping_required'
                or self.viewer is None or not self.viewer.is_authenticated
                or not self.viewer.is_active or not self.viewer.is_staff
                or record.owner_id != self.viewer.pk):
            return None
        source = getattr(settings, 'PROVIDER_USAGE_SOURCE', {})
        if (not isinstance(source, dict) or not source.get('path')
                or record.source_instance != source.get('source_instance')
                or record.source_id != source.get('source_id')):
            return None
        if self._snapshot is None:
            from .resource_usage_view import read_resource_usage_view
            self._snapshot = read_resource_usage_view(source['path'])
        # 只标识公开服务及已核来源代际，不包含文件路径、令牌或快照内容。
        # 故障时仍返回同一代际，前端才可安全保留该来源上次成功的图形。
        identity = ['p8-resource-usage-v1', str(record.public_id), record.source_instance,
                    record.source_id, record.verification_sha256]
        source_key = hashlib.sha256(json.dumps(identity, ensure_ascii=True,
                                              separators=(',', ':')).encode('utf-8')).hexdigest()
        return {**self._snapshot, 'source_key': source_key}
