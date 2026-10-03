from decimal import Decimal
from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm, UsernameField
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import ClientDirectRule, Membership
from .client_rules import validate_rule


def clean_username(value, *, exclude=None):
    value = value.strip()
    query = User.objects.filter(username__iexact=value)
    if exclude:
        query = query.exclude(pk=exclude)
    if query.exists():
        raise ValidationError('此账号名称已被使用。')
    return value


class LoginForm(AuthenticationForm):
    username = forms.CharField(label='账号', max_length=150)


class RegisterForm(UserCreationForm):
    invitation = forms.CharField(label='邀请码', max_length=100)

    class Meta:
        model = User
        fields = ['username', 'password1', 'password2', 'invitation']

    def clean_username(self):
        return clean_username(self.cleaned_data['username'])


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['username']
        labels = {'username': '账号名称'}

    def clean_username(self):
        return clean_username(self.cleaned_data['username'], exclude=self.instance.pk)


class InvitationForm(forms.Form):
    label = forms.CharField(label='备注', max_length=80, required=False)
    quota_gb = forms.DecimalField(label='套餐额度（GB，上传与下载合计）', min_value=Decimal('0.1'), max_value=100000,
                                 decimal_places=1, initial=100)
    service_days = forms.IntegerField(label='开通后有效天数', min_value=1, max_value=3660, initial=30)
    valid_days = forms.IntegerField(label='邀请码有效天数', min_value=1, max_value=90, initial=7)
    max_uses = forms.IntegerField(label='可注册人数', min_value=1, max_value=30, initial=1)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if settings.WORKSPACE_V2:
            for name in ('quota_gb', 'service_days'):
                self.fields.pop(name)

    def clean(self):
        data = super().clean()
        if settings.WORKSPACE_V2:
            data.update(quota_gb=Decimal(0), service_days=30)
        return data


class AdminUserCreateForm(forms.ModelForm):
    quota_gb = forms.DecimalField(label='分配额度（GB，上传与下载合计）', min_value=Decimal('0.1'),
                                 max_value=100000, decimal_places=1, initial=100)
    service_days = forms.IntegerField(label='开通后有效天数', min_value=1, max_value=3660, initial=30)
    confirm = forms.BooleanField(label='确认创建普通用户；套餐仍需另行开通')

    class Meta:
        model = User
        fields = ['username']
        field_classes = {'username': UsernameField}
        labels = {'username': '账号名称'}
        help_texts = {'username': '用户以后可以在账号设置中修改；不能与已有账号重名。'}
        widgets = {'username': forms.TextInput(attrs={'autocomplete': 'off'})}

    def clean_username(self):
        return clean_username(self.cleaned_data['username'])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if settings.WORKSPACE_V2:
            for name in ('quota_gb', 'service_days'):
                self.fields.pop(name)

    def clean(self):
        data = super().clean()
        if settings.WORKSPACE_V2:
            data.update(quota_gb=Decimal(0), service_days=30)
        return data


class MemberForm(forms.ModelForm):
    quota_gb = forms.DecimalField(label='额度（GB，上传与下载合计）', min_value=Decimal('0.1'),
                                 max_value=100000, decimal_places=1)
    revision = forms.IntegerField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(label='确认保存套餐候选；以服务器应用结果为准')

    class Meta:
        model = Membership
        fields = ['status', 'expires_at']
        labels = {'status': '期望套餐状态', 'expires_at': '到期时间'}
        widgets = {'expires_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M')}

    def clean_expires_at(self):
        expiry = self.cleaned_data.get('expires_at')
        if self.cleaned_data.get('status') == 'active' and (not expiry or expiry <= timezone.now()):
            raise ValidationError('启用套餐需要未来的到期时间。')
        return expiry


class OperationForm(forms.Form):
    action = forms.ChoiceField(label='操作', choices=[('status', '检查服务状态'), ('check', '检查配置'),
        ('backup', '创建备份'), ('backups', '查看备份'), ('core_status', '查看核心版本'),
        ('upstreams', '查看住宅上游')])
    confirm = forms.BooleanField(label='确认提交此操作')


class ClientDirectRuleForm(forms.ModelForm):
    revision = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = ClientDirectRule
        fields = ['outbound', 'kind', 'value', 'scope_domain', 'enabled']
        labels = {'outbound': '命中后走向', 'kind': '匹配方式', 'value': '域名或正则',
                  'scope_domain': '正则限定域名', 'enabled': '启用'}
        help_texts = {'value': '精确：example.com；后缀：example.com；正则：^api[0-9]+\\.example\\.com$',
                      'scope_domain': '仅正则填写，例如 example.com；表达式只能匹配它的子域名。'}
        widgets = {'value': forms.TextInput(attrs={'autocomplete': 'off'}),
                   'scope_domain': forms.TextInput(attrs={'autocomplete': 'off'})}

    def clean(self):
        cleaned = super().clean()
        if 'kind' in cleaned and 'value' in cleaned and 'outbound' in cleaned:
            value, scope = validate_rule(cleaned['kind'], cleaned['value'],
                                         cleaned.get('scope_domain', ''), cleaned['outbound'])
            cleaned['value'], cleaned['scope_domain'] = value, scope
            duplicate = ClientDirectRule.objects.filter(kind=cleaned['kind'], value=value)
            if self.instance.pk:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error('value', '相同匹配方式和域名已存在；请编辑现有规则，避免重复或冲突。')
            # 同一走向下，父后缀已经覆盖子域名时拒绝重复候选；相反走向允许，
            # 由构建器按“代理优先”处理，并可在预览页看到实际命中。
            if not self.errors and cleaned['kind'] in ('exact', 'suffix'):
                for other in ClientDirectRule.objects.exclude(pk=self.instance.pk if self.instance.pk else None):
                    if other.outbound != cleaned['outbound'] or other.kind not in ('exact', 'suffix'):
                        continue
                    candidate = cleaned['value']
                    existing = other.value
                    # 只有后缀才能覆盖其子域；精确父域与子域后缀不相交。
                    existing_covers = other.kind == 'suffix' and (
                        candidate == existing or candidate.endswith('.' + existing))
                    candidate_covers = cleaned['kind'] == 'suffix' and (
                        existing == candidate or existing.endswith('.' + candidate))
                    if existing_covers or candidate_covers:
                        self.add_error('value', '同一走向已有父域或子域规则覆盖它；请编辑现有规则，避免重复。')
                        break
        return cleaned
