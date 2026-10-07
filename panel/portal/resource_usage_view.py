"""只读已发布的统一资源用量；读取请求不创建周期、不触发采集。"""
from datetime import datetime, timezone
import json
import re
from .provider_usage import _private_read, _unique

METER_FIELDS = {'id','label','scope','source_kind','quality','quota_bytes','used_bytes','remaining_bytes',
                'upload_bytes','download_bytes','observed_at','expires_at','cycle','expires_on','alerts','details'}
BYTE_FIELDS = {'quota_bytes','used_bytes','remaining_bytes','upload_bytes','download_bytes'}


def require(value):
    if not value:
        raise ValueError('invalid_usage_view')


def instant(value, nullable=True):
    if nullable and value is None:
        return None
    require(type(value) is str and len(value) <= 40)
    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
    require(parsed.tzinfo is not None)
    return parsed


def read_resource_usage_view(path=None, *, now=None):
    now = now or datetime.now(timezone.utc)
    fallback = {'schema_version':2,'generated_at':None,'meters':[],
                'error':{'code':'usage_unavailable','message':'统计暂不可用，请查看采集状态或稍后刷新。'}}
    if not path:
        return fallback
    try:
        data = json.loads(_private_read(path),object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        require(type(data) is dict and set(data)=={'schema_version','generated_at','meters'}
                and type(data['schema_version']) is int and data['schema_version']==2
                and type(data['meters']) is list and 0<len(data['meters'])<=32)
        if instant(data['generated_at'],False)>now:
            fallback['error']={'code':'usage_clock_error','message':'统计时间异常，请检查采集时钟。'}
            return fallback
        identifiers=set()
        for meter in data['meters']:
            require(type(meter) is dict and set(meter)==METER_FIELDS)
            require(type(meter['id']) is str and re.fullmatch(r'[a-z0-9_-]{1,64}',meter['id'])
                    and meter['id'] not in identifiers)
            identifiers.add(meter['id'])
            require(type(meter['label']) is str and 0<len(meter['label'])<=160
                    and meter['scope'] in ('external_node','node','server','route','service')
                    and meter['source_kind'] in ('estimate','official')
                    and meter['quality'] in ('current','stale','error','gap','missing'))
            for key in BYTE_FIELDS:
                value=meter[key]
                require(value is None or type(value)is str and re.fullmatch(r'0|[1-9][0-9]{0,29}',value))
            observed=instant(meter['observed_at']);expires=instant(meter['expires_at'])
            require(observed is None or observed<=now or meter['quality']!='current')
            require(type(meter['cycle'])is dict and set(meter['cycle'])=={'kind','starts_at','ends_at','next_reset_at'})
            require(meter['cycle']['kind'] in ('fixed_days','provider','none','calendar_month','manual'))
            for key in ('starts_at','ends_at','next_reset_at'):
                instant(meter['cycle'][key])
            if meter['expires_on'] is not None:
                require(type(meter['expires_on'])is str and re.fullmatch(r'\d{4}-\d{2}-\d{2}',meter['expires_on']))
                datetime.strptime(meter['expires_on'],'%Y-%m-%d')
            require(type(meter['alerts'])is list and len(meter['alerts'])<=32 and type(meter['details'])is dict)
            for alert in meter['alerts']:
                require(type(alert)is dict and set(alert)=={'code','severity','message'}
                        and type(alert['code'])is str and re.fullmatch(r'[a-z0-9_]{1,80}',alert['code'])
                        and alert['severity'] in ('info','warning','error')
                        and type(alert['message'])is str and len(alert['message'])<=300)
            # 发布停止后，读取也按来源时间降级，不能将旧数字一直标为当前。
            if (expires is not None and expires<=now) or (meter['quality']=='current' and expires is None):
                if meter['quality']=='current':
                    meter['quality']='stale'
                if not any(a['code']=='source_stale' for a in meter['alerts']):
                    meter['alerts'].append({'code':'source_stale','severity':'warning','message':'统计更新延迟，当前显示上次记录。'})
        return data
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError,OverflowError):
        return fallback
