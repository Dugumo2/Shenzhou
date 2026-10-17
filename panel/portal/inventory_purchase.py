"""资源采购显示沿用本人来源授权，不按显示名绑定或直接读取全局快照。"""
import hashlib
from copy import deepcopy

from .service_projection import p8_records, valid_p8
from .service_usage_source import ServiceUsageSource


class InventoryPurchases:
    def __init__(self, viewer):
        self.viewer = viewer
        self.sources = None

    def _load(self):
        if self.sources is not None:
            return
        self.sources = []
        if not self.viewer.is_authenticated or not self.viewer.is_active or not self.viewer.is_staff:
            return
        source = ServiceUsageSource(self.viewer)
        for record in p8_records().filter(owner=self.viewer):
            if valid_p8(record):
                value = source.read(record, 'p8', {'state': 'verified'})
                if isinstance(value, dict) and value.get('source_key'):
                    self.sources.append(value)

    def for_resource(self, meter_id, scope):
        empty = {'purchase_meter': None, 'purchase_binding_key': None, 'purchase_meter_state': 'unbound'}
        if not meter_id:
            return empty
        self._load()
        if len(self.sources) != 1:
            return {**empty, 'purchase_meter_state': 'forbidden'}
        source = self.sources[0]
        key = hashlib.sha256((source['source_key'] + ':' + scope + ':' + meter_id).encode()).hexdigest()
        matches = [item for item in source.get('meters', [])
                   if item.get('id') == meter_id and item.get('scope') == scope]
        if len(matches) != 1:
            return {**empty, 'purchase_binding_key': key, 'purchase_meter_state': 'unavailable'}
        # 只向管理员资源视图投影既有公开字段，诊断原件保持后台。
        meter = deepcopy(matches[0])
        meter['details'] = {}
        return {'purchase_meter': meter, 'purchase_binding_key': key, 'purchase_meter_state': 'ready'}
