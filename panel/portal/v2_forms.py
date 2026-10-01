"""行舟工作区表单；提交时仍由业务服务复核授权与线路。"""
import uuid
from decimal import Decimal
from django import forms
from django.contrib.auth.models import User
from .models import DeviceSubscription, Line, CapacityPool, Server


class RequestForm(forms.Form):
    idempotency_key = forms.UUIDField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault('idempotency_key', uuid.uuid4())


class AllocationForm(RequestForm):
    user = forms.ModelChoiceField(label='用户', queryset=User.objects.none())
    lines = forms.ModelMultipleChoiceField(label='允许使用的线路', queryset=Line.objects.none(),
                                           widget=forms.CheckboxSelectMultiple)
    quota_gb = forms.DecimalField(label='每月流量（GB）', min_value=Decimal('0.1'),
                                 max_value=100000, decimal_places=1, initial=100)
    reset_day = forms.IntegerField(label='每月重置日', min_value=1, max_value=31, initial=1,
                                  help_text='短月自动取月末，下个月仍按原日期计算。')
    service_months = forms.IntegerField(label='开通后有效月数', min_value=1, max_value=120, initial=1)
    expires_at = forms.DateTimeField(label='手动到期时间', required=False,
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
        help_text='选填。未填写时从实际开通成功计算自然月；保存不会清零已用流量。')
    enabled = forms.BooleanField(label='启用套餐', required=False, initial=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['user'].queryset = User.objects.order_by('username')
        self.fields['lines'].queryset = Line.objects.filter(enabled=True).select_related('ingress__server')


class SubscriptionForm(RequestForm):
    name = forms.CharField(label='订阅名称', max_length=80, help_text='例如：我的手机。各订阅可以独立重置。')
    client = forms.ChoiceField(label='客户端', choices=DeviceSubscription.CLIENTS)
    lines = forms.ModelMultipleChoiceField(label='使用线路', queryset=Line.objects.none(),
                                           widget=forms.CheckboxSelectMultiple)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['lines'].queryset = Line.objects.filter(entitlements__user=user, enabled=True)


class SubscriptionActionForm(RequestForm):
    idempotency_key = forms.CharField(min_length=8, max_length=128, widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=[('refresh', '更新内容'), ('reset', '重置订阅链接'), ('disable', '停用')])
    revision = forms.IntegerField(min_value=1)
    confirm = forms.BooleanField(required=False)

    def clean(self):
        data = super().clean()
        if data.get('action') in ('reset', 'disable') and not data.get('confirm'):
            raise forms.ValidationError('请确认此订阅的影响范围。')
        return data


class RenameResourceForm(forms.Form):
    action = forms.ChoiceField(choices=[('rename_server', '修改服务器名称'), ('rename_line', '修改线路名称')])
    public_id = forms.UUIDField()
    name = forms.CharField(max_length=100, strip=True)


class ServiceAllocationForm(RequestForm):
    service_name = forms.CharField(label='服务名称', max_length=100, required=False,
        help_text='可留空，默认使用线路名称。')
    lines = forms.ModelMultipleChoiceField(label='包含线路', queryset=Line.objects.none(), widget=forms.CheckboxSelectMultiple)
    quota_gb = forms.DecimalField(label='套餐月流量（GB）', min_value=Decimal('0.1'), max_value=100000,
        decimal_places=1, initial=100, help_text='所有线路共用这一额度，按各线路倍率扣减。')
    validity_mode = forms.ChoiceField(label='有效期方式', choices=[('months', '按时长开通'), ('date', '指定到期日期')])
    service_months = forms.IntegerField(label='有效月数', min_value=1, max_value=120, initial=1, required=False)
    expires_at = forms.DateTimeField(label='到期日期', required=False,
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'))
    reset_mode = forms.ChoiceField(label='流量重置方式', choices=[('activation', '每月按开通日和时间'), ('custom', '指定每月重置时间')])
    reset_day = forms.IntegerField(label='每月日期', min_value=1, max_value=31, required=False)
    reset_time = forms.TimeField(label='重置时间', required=False, widget=forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'))
    enabled = forms.BooleanField(label='启用服务', required=False, initial=True)
    operation = forms.ChoiceField(label='本次操作', choices=[('save', '保存设置'), ('renew', '续期')], initial='save')
    revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0, initial=0)

    def __init__(self, *args, billing_managed=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['lines'].queryset = Line.objects.filter(enabled=True).select_related('ingress__server')
        if billing_managed:
            for name in ('reset_mode', 'reset_day', 'reset_time'):
                self.fields[name].disabled = True
                self.fields[name].help_text = '重置计划已独立管理，请使用服务的重置时间操作。'

    def clean(self):
        data = super().clean()
        if data.get('validity_mode') == 'months':
            if data.get('service_months') is None:
                self.add_error('service_months', '请选择有效月数。')
            data['expires_at'] = None
        else:
            from django.utils import timezone
            if not data.get('expires_at') or data['expires_at'] <= timezone.now():
                self.add_error('expires_at', '请选择未来的到期日期。')
            data['service_months'] = data.get('service_months') or 1
        if data.get('reset_mode') == 'custom':
            if not data.get('reset_day'):
                self.add_error('reset_day', '请选择每月重置日期。')
            if data.get('reset_time') is None:
                self.add_error('reset_time', '请选择重置时间。')
        else:
            data['reset_day'], data['reset_time'] = None, None
        return data


class ServiceDeliveryForm(RequestForm):
    client = forms.ChoiceField(choices=DeviceSubscription.CLIENTS)
    scope = forms.CharField(max_length=40, initial='all')


class ServiceResetForm(RequestForm):
    action = forms.ChoiceField(choices=[('reset', '重置订阅链接')])
    revision = forms.IntegerField(min_value=1)
    confirm = forms.BooleanField(label='确认重置此服务的全部订阅链接')


class ServiceFilterForm(forms.Form):
    q = forms.CharField(label='用户或服务', max_length=120, required=False)
    line = forms.ModelChoiceField(label='包含线路', queryset=Line.objects.none(), required=False, empty_label='全部线路')
    state = forms.ChoiceField(label='服务状态', required=False, choices=[('', '全部状态'), ('active', '有效'),
        ('pending', '等待开通'), ('simulated', '隔离验证'), ('suspended', '暂停'), ('expired', '已到期'),
        ('expiring', '7天内到期'), ('exhausted', '额度耗尽'), ('disabled', '停用')])
    client = forms.ChoiceField(label='已生成格式', required=False, choices=[('', '全部格式')] + DeviceSubscription.CLIENTS)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['line'].queryset = Line.objects.all().order_by('name')


class LineRateForm(forms.Form):
    multiplier = forms.DecimalField(label='扣费倍率', max_digits=12, decimal_places=6,
        min_value=Decimal('0.000001'), max_value=Decimal('999999'), help_text='未知倍率不默认当作 1 倍。')
    effective_at = forms.DateTimeField(label='生效时间',
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'))
    reason = forms.CharField(label='调整说明', max_length=240)


def local_datetime():
    return forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')


class CapacityPoolForm(forms.Form):
    pool = forms.ModelChoiceField(label='编辑已有资源（留空新建）', queryset=CapacityPool.objects.all(), required=False)
    name = forms.CharField(label='资源名称', max_length=100)
    owner = forms.CharField(label='归属', max_length=100, required=False)
    server = forms.ModelChoiceField(label='关联服务器', queryset=Server.objects.all(), required=False)
    provider = forms.CharField(label='供应商', max_length=100, required=False)
    capacity_mode = forms.ChoiceField(label='容量类型', choices=[('unknown', '未知'), ('limited', '有限容量'), ('unlimited', '无限')])
    planned_gb = forms.DecimalField(label='规划容量（GB）', required=False, min_value=0, max_value=1000000000, decimal_places=3)
    original_unit = forms.ChoiceField(label='供应商原始单位', choices=[('GB', 'GB（十进制）'), ('GiB', 'GiB（二进制）'), ('TB', 'TB（十进制）'), ('TiB', 'TiB（二进制）'), ('bytes', '字节')],
        initial='GB', help_text='记录来源单位。上方容量始终填写换算后的十进制 GB，例如 1 GiB = 1.073741824 GB。')
    period_start = forms.DateTimeField(label='账期开始', widget=local_datetime())
    period_end = forms.DateTimeField(label='账期结束', widget=local_datetime())
    accounting_basis = forms.CharField(label='供应商计量口径', max_length=120, help_text='例如上传、下载、双向；保持供应商真实口径。')
    source_priority = forms.ChoiceField(label='来源优先顺序', choices=[('api,manual,agent', '供应商 API → 人工校准 → Agent'), ('api,agent,manual', '供应商 API → Agent → 人工校准'), ('manual,api,agent', '人工校准 → 供应商 API → Agent')])
    stale_after_seconds = forms.IntegerField(label='超过多少秒未更新视为陈旧', min_value=60, max_value=2592000, initial=21600)
    safety_gb = forms.DecimalField(label='预留安全容量（GB）', min_value=0, max_value=1000000000, decimal_places=3, initial=0)
    warning_percent = forms.IntegerField(label='提醒阈值（%）', min_value=1, max_value=100, initial=70)
    critical_percent = forms.IntegerField(label='警告阈值（%）', min_value=1, max_value=100, initial=85)
    urgent_percent = forms.IntegerField(label='紧张阈值（%）', min_value=1, max_value=100, initial=95)
    overage_policy = forms.CharField(label='供应商超量处理方式', max_length=200, required=False)
    enabled = forms.BooleanField(label='纳入容量概览', required=False, initial=True)

    def clean(self):
        data = super().clean()
        if data.get('capacity_mode') == 'limited' and data.get('planned_gb') is None:
            self.add_error('planned_gb', '有限容量必须填写数值；不清楚时请选择未知。')
        if data.get('period_start') and data.get('period_end') and data['period_end'] <= data['period_start']:
            self.add_error('period_end', '账期结束必须晚于开始。')
        return data


class CapacitySampleForm(RequestForm):
    pool = forms.ModelChoiceField(label='资源', queryset=CapacityPool.objects.all())
    used_gb = forms.DecimalField(label='本账期累计已用（GB）', min_value=0, max_value=1000000000, decimal_places=3)
    observed_at = forms.DateTimeField(label='校准时间', widget=local_datetime())
    accounting_basis = forms.CharField(label='计量口径', max_length=120)
    reason = forms.CharField(label='人工校准说明', max_length=240)


class CapacityAdditionForm(RequestForm):
    pool = forms.ModelChoiceField(label='资源', queryset=CapacityPool.objects.all())
    added_gb = forms.DecimalField(label='追加容量（GB）', min_value=Decimal('0.001'), max_value=1000000000, decimal_places=3)
    reason = forms.CharField(label='追加说明', max_length=240)


class LineCapacityForm(forms.Form):
    pool = forms.ModelChoiceField(label='资源', queryset=CapacityPool.objects.all())
    line = forms.ModelChoiceField(label='使用线路', queryset=Line.objects.all())
    consumption_factor = forms.DecimalField(label='资源消耗系数', min_value=Decimal('0.000001'), max_value=999999, decimal_places=6,
        help_text='每单位入口实际流量对应的资源消耗，区别于用户扣费倍率。')
    topology_verified = forms.BooleanField(label='已核验资源关系与计量口径', required=False)
    note = forms.CharField(label='关联说明', max_length=240, required=False)


class UserFilterForm(forms.Form):
    q = forms.CharField(label='用户名', max_length=120, required=False)
    state = forms.ChoiceField(label='账号服务', required=False, choices=[('', '全部账号'),
        ('none', '无服务'), ('assigned', '已分配服务'), ('disabled', '账号已禁用')])
