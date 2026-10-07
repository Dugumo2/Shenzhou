"""网站身份、订阅权限和服务器应用状态分别记录。"""
import uuid
from datetime import timedelta
from django.conf import settings
from django.db import models
from django.utils import timezone


class Membership(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=16, default='pending', choices=[
        ('pending', '待开通'), ('active', '启用'), ('suspended', '暂停')])
    quota_bytes = models.PositiveBigIntegerField(default=0)
    service_days = models.PositiveIntegerField(default=30)
    used_bytes = models.PositiveBigIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    provisioning_state = models.CharField(max_length=24, default='not_provisioned')
    usage_state = models.CharField(max_length=24, default='not_connected')
    usage_updated_at = models.DateTimeField(null=True, blank=True)
    last_applied_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def eligible(self):
        now = timezone.now()
        fresh = self.usage_updated_at is not None and now - timedelta(minutes=3) <= self.usage_updated_at <= now + timedelta(seconds=30)
        return (fresh and self.user.is_active and self.status == 'active' and self.provisioning_state == 'applied'
                and self.usage_state == 'measured' and self.quota_bytes > self.used_bytes
                and self.expires_at is not None and self.expires_at > now)


class Invitation(models.Model):
    code_hash = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=80, blank=True)
    expires_at = models.DateTimeField()
    max_uses = models.PositiveIntegerField(default=1)
    uses = models.PositiveIntegerField(default=0)
    revoked = models.BooleanField(default=False)
    quota_bytes = models.PositiveBigIntegerField()
    service_days = models.PositiveIntegerField(default=30)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)


class SubscriptionGrant(models.Model):
    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name='grants')
    device = models.CharField(max_length=16, choices=[('windows', 'Windows / v2rayN'),
                              ('android', 'Android / sing-box'), ('v2rayng', 'Android / v2rayNG')])
    version = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['membership', 'device'], name='unique_device_grant')]


class AuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=40)
    subject = models.CharField(max_length=80, blank=True)
    result = models.CharField(max_length=24)
    created_at = models.DateTimeField(auto_now_add=True)


class OperationJob(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=20, default='queued')
    result_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class RateBucket(models.Model):
    key = models.CharField(max_length=64, unique=True)
    started_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)


class ClientDirectRule(models.Model):
    """面板内的客户端域名路由候选；服务器和设备生效状态不由此行推断。"""
    outbound = models.CharField(max_length=8, choices=[('proxy', '当前代理'), ('direct', '本机直连')], default='direct')
    kind = models.CharField(max_length=8, choices=[('exact', '精确域名'), ('suffix', '域名及子域名'), ('regex', '限定域名正则')])
    value = models.CharField(max_length=253)
    scope_domain = models.CharField(max_length=253, blank=True)
    enabled = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['kind', 'value'], name='unique_client_direct_rule')]
        ordering = ['id']


# 第二阶段实体与旧 Membership 独立，迁移不会自动发放或轮换线上身份。
class Server(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=100)
    notes = models.TextField(blank=True, default='', max_length=1000)
    revision = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    adapter = models.CharField(max_length=40, default='unconfigured')
    last_seen_at = models.DateTimeField(null=True, blank=True)
    deployment_lease_id = models.UUIDField(null=True, blank=True)
    deployment_lease_until = models.DateTimeField(null=True, blank=True)
    deployment_fence = models.PositiveBigIntegerField(default=0)

    def __str__(self):
        return self.name


class Ingress(models.Model):
    server = models.ForeignKey(Server, on_delete=models.PROTECT, related_name='ingresses')
    name = models.CharField(max_length=100)
    protocol = models.CharField(max_length=24, choices=[('reality', 'Reality'), ('ws', 'WebSocket'),
        ('hy2', 'Hysteria 2'), ('anytls', 'AnyTLS')])
    enabled = models.BooleanField(default=True)
    # 此处只保存受限配置的引用；不保存私钥、节点密码或订阅 URI。
    config_ref = models.CharField(max_length=120, blank=True)


class Egress(models.Model):
    server = models.ForeignKey(Server, on_delete=models.PROTECT, related_name='egresses')
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=24, choices=[('direct', '服务器出站'), ('upstream', '上游链路')])
    config_ref = models.CharField(max_length=120, blank=True)
    fail_closed = models.BooleanField(default=True)


class Line(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=100)
    notes = models.TextField(blank=True, default='', max_length=1000)
    revision = models.PositiveIntegerField(default=1)
    ingress = models.ForeignKey(Ingress, on_delete=models.PROTECT, related_name='lines')
    additional_ingresses = models.ManyToManyField(Ingress, blank=True, related_name='additional_lines')
    egress = models.ForeignKey(Egress, on_delete=models.PROTECT, related_name='lines')
    enabled = models.BooleanField(default=True)

    def __str__(self):
        return self.name

    def all_ingresses(self):
        return [self.ingress] + list(self.additional_ingresses.exclude(pk=self.ingress_id).order_by('pk'))


class CoreInstance(models.Model):
    """核心登记，不承担安装、配置发布或运行控制。"""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    server = models.ForeignKey(Server, on_delete=models.PROTECT, related_name='cores')
    instance_key = models.SlugField(max_length=80)
    name = models.CharField(max_length=100)
    core_type = models.CharField(max_length=24, choices=[('xray', 'Xray'), ('sing_box', 'sing-box'), ('other', '其他')])
    registered_version = models.CharField(max_length=80, null=True, blank=True)
    configuration_owner = models.CharField(max_length=100)
    configuration_version = models.CharField(max_length=80, blank=True, default='')
    notes = models.TextField(blank=True, default='', max_length=1000)
    revision = models.PositiveIntegerField(default=1)
    ingresses = models.ManyToManyField(Ingress, blank=True, related_name='core_instances')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['server', 'instance_key'], name='unique_core_instance_key')]


class InventoryMutationReceipt(models.Model):
    """以数据库唯一键保护资料修改重试，不复用审计事件猜测成功。"""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.CharField(max_length=100)
    fingerprint = models.CharField(max_length=64)
    response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['actor', 'key'], name='unique_inventory_mutation_key')]


class ServerObservation(models.Model):
    """受信采集器产生的不可覆盖样本；数值缺失保留 null。"""
    server = models.ForeignKey(Server, on_delete=models.PROTECT, related_name='observations')
    source = models.CharField(max_length=80)
    event_id = models.CharField(max_length=100)
    observed_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    target_fingerprint = models.CharField(max_length=64)
    metrics = models.JSONField(default=dict)
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'event_id'], name='unique_server_observation_event'),
                       models.CheckConstraint(condition=models.Q(expires_at__gt=models.F('observed_at')), name='server_observation_validity')]
        indexes = [models.Index(fields=['server', '-observed_at'], name='server_observation_latest')]


class CheckResult(models.Model):
    """固定目标的独立检测回执；单端点成功不提升整条线路状态。"""
    server = models.ForeignKey(Server, null=True, blank=True, on_delete=models.PROTECT, related_name='checks')
    line = models.ForeignKey(Line, null=True, blank=True, on_delete=models.PROTECT, related_name='checks')
    core = models.ForeignKey(CoreInstance, null=True, blank=True, on_delete=models.PROTECT, related_name='checks')
    ingress = models.ForeignKey(Ingress, null=True, blank=True, on_delete=models.PROTECT, related_name='checks')
    egress = models.ForeignKey(Egress, null=True, blank=True, on_delete=models.PROTECT, related_name='checks')
    source = models.CharField(max_length=80)
    event_id = models.CharField(max_length=100)
    check_kind = models.CharField(max_length=24)
    observed_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    target_fingerprint = models.CharField(max_length=64)
    result = models.CharField(max_length=12, choices=[('pass', '通过'), ('fail', '失败'), ('timeout', '超时'), ('unknown', '未知')])
    latency_ms = models.FloatField(null=True, blank=True)
    error_stage = models.CharField(max_length=24, blank=True, default='')
    error_code = models.CharField(max_length=40, blank=True, default='')
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'event_id'], name='unique_inventory_check_event'),
                       models.CheckConstraint(condition=models.Q(expires_at__gt=models.F('observed_at')), name='inventory_check_validity'),
                       models.CheckConstraint(condition=(
                           models.Q(server__isnull=False, line__isnull=True, core__isnull=True, ingress__isnull=True, egress__isnull=True)
                           | models.Q(server__isnull=True, line__isnull=False, core__isnull=True, ingress__isnull=True, egress__isnull=True)
                           | models.Q(server__isnull=True, line__isnull=True, core__isnull=False, ingress__isnull=True, egress__isnull=True)
                           | models.Q(server__isnull=True, line__isnull=True, core__isnull=True, ingress__isnull=False, egress__isnull=True)
                           | models.Q(server__isnull=True, line__isnull=True, core__isnull=True, ingress__isnull=True, egress__isnull=False)
                       ), name='inventory_check_one_target')]
        indexes = [models.Index(fields=['line', '-observed_at'], name='line_check_latest'),
                   models.Index(fields=['server', '-observed_at'], name='server_check_latest')]


class Entitlement(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='entitlement')
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    lines = models.ManyToManyField(Line, related_name='entitlements')
    quota_bytes = models.PositiveBigIntegerField()
    service_name = models.CharField(max_length=100, default='', blank=True)
    reset_day = models.PositiveSmallIntegerField(default=None, null=True, blank=True)
    reset_hour = models.PositiveSmallIntegerField(null=True, blank=True)
    reset_minute = models.PositiveSmallIntegerField(null=True, blank=True)
    service_months = models.PositiveSmallIntegerField(default=1)
    enabled = models.BooleanField(default=True)
    requested_expires_at = models.DateTimeField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=1)
    applied_revision = models.PositiveIntegerField(default=0)
    applied_snapshot = models.JSONField(default=dict)
    state = models.CharField(max_length=24, default='pending')
    usage_updated_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    suspension_reason = models.CharField(max_length=32, blank=True)
    metering_gap = models.BooleanField(default=False)
    enforcement_epoch = models.PositiveIntegerField(default=0)


class BillingCycle(models.Model):
    entitlement = models.ForeignKey(Entitlement, on_delete=models.PROTECT, related_name='cycles')
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    used_bytes = models.PositiveBigIntegerField(default=0)
    # 旧账本原始累计未核验，迁移时保持未知，不重新计算旧扣费。
    raw_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    weighted_remainder = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['entitlement', 'starts_at'], name='unique_billing_cycle'),
            models.CheckConstraint(condition=models.Q(ends_at__gt=models.F('starts_at')), name='positive_billing_period')]


class BillingPlan(models.Model):
    """独立账期计划；版本不参与核心配置或身份执行。"""
    entitlement = models.OneToOneField(Entitlement, on_delete=models.PROTECT, related_name='billing_plan')
    cycle = models.ForeignKey(BillingCycle, on_delete=models.PROTECT, related_name='active_plans')
    next_reset_at = models.DateTimeField()
    anchor_day = models.PositiveSmallIntegerField()
    hour = models.PositiveSmallIntegerField()
    minute = models.PositiveSmallIntegerField()
    revision = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(anchor_day__gte=1, anchor_day__lte=31), name='billing_anchor_day'),
            models.CheckConstraint(condition=models.Q(hour__lte=23, minute__lte=59), name='billing_anchor_time'),
        ]


class BillingPlanRevision(models.Model):
    """计划修改与安全重放结果；不存预览令牌，不改历史用量。"""
    entitlement = models.ForeignKey(Entitlement, on_delete=models.PROTECT, related_name='billing_revisions')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    expected_revision = models.PositiveIntegerField()
    idempotency_key = models.CharField(max_length=128)
    request_digest = models.CharField(max_length=64)
    before = models.JSONField()
    after = models.JSONField()
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['entitlement', 'revision'], name='unique_billing_plan_revision'),
            models.UniqueConstraint(fields=['entitlement', 'actor', 'idempotency_key'], name='unique_billing_plan_request'),
        ]


class DeviceSubscription(models.Model):
    CLIENTS = [('windows', 'Windows / v2rayN'), ('v2rayng', 'Android / v2rayNG'), ('android', 'Android / SFA')]
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    entitlement = models.ForeignKey(Entitlement, on_delete=models.PROTECT, related_name='subscriptions')
    name = models.CharField(max_length=80)
    client = models.CharField(max_length=16, choices=CLIENTS)
    # 逻辑交付范围不随线路集合变化；旧独立设备订阅保持空值。
    delivery_scope = models.CharField(max_length=40, blank=True, default='')
    lines = models.ManyToManyField(Line)
    generation = models.PositiveIntegerField(default=1)
    applied_generation = models.PositiveIntegerField(default=0)
    token_version = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=24, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['entitlement', 'client', 'delivery_scope'],
            condition=~models.Q(delivery_scope='') & ~models.Q(state__in=['disabled', 'disabling']),
            name='unique_active_service_delivery')]


class ServiceDeliveryReset(models.Model):
    """整服务重置的不可变操作清单，重放不能扩张到后来新增的交付。"""
    entitlement = models.ForeignKey(Entitlement, on_delete=models.PROTECT, related_name='delivery_resets')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.CharField(max_length=64)
    subscription_ids = models.JSONField(default=list)
    job_ids = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['entitlement', 'key'], name='unique_service_reset_request')]


class NodeIdentity(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    subscription = models.ForeignKey(DeviceSubscription, on_delete=models.PROTECT, related_name='identities')
    line = models.ForeignKey(Line, on_delete=models.PROTECT)
    ingress = models.ForeignKey(Ingress, on_delete=models.PROTECT)
    generation = models.PositiveIntegerField()
    credential_ref = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    state = models.CharField(max_length=24, default='candidate')
    # 一旦设置不撤销；发布器必须先应用撤销记录，禁止回滚复活旧身份。
    revoked_at = models.DateTimeField(null=True, blank=True)

    def save(self, *args, **kwargs):
        if self.ingress_id is None:
            self.ingress_id = self.line.ingress_id
        super().save(*args, **kwargs)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['subscription', 'line', 'ingress', 'generation'],
            condition=models.Q(revoked_at__isnull=True), name='unique_identity_generation')]


class UsageStream(models.Model):
    identity = models.ForeignKey(NodeIdentity, on_delete=models.PROTECT, related_name='streams')
    epoch = models.CharField(max_length=80)
    sequence = models.PositiveBigIntegerField(default=0)
    upload_bytes = models.PositiveBigIntegerField(default=0)
    download_bytes = models.PositiveBigIntegerField(default=0)
    retired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['identity', 'epoch'], name='unique_usage_epoch')]


class UsageLedger(models.Model):
    cycle = models.ForeignKey(BillingCycle, on_delete=models.PROTECT, related_name='ledger')
    stream = models.ForeignKey(UsageStream, on_delete=models.PROTECT)
    sequence = models.PositiveBigIntegerField()
    upload_delta = models.PositiveBigIntegerField()
    download_delta = models.PositiveBigIntegerField()
    rate_version = models.ForeignKey('LineRateVersion', null=True, blank=True, on_delete=models.PROTECT)
    grant_rate_version = models.ForeignKey('ServiceRateVersion', null=True, blank=True, on_delete=models.PROTECT)
    # 旧流水不回填发生区间；新采样有边界证据时才填写。
    interval_start = models.DateTimeField(null=True, blank=True)
    interval_end = models.DateTimeField(null=True, blank=True)
    weighted_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    quality = models.CharField(max_length=24, default='legacy_unknown')
    observed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['stream', 'sequence'], name='unique_usage_sample')]


class DeploymentJob(models.Model):
    """事务任务箱：与权益或订阅变更同事务写入，可续租重领，不依赖内存队列。"""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    entitlement = models.ForeignKey(Entitlement, on_delete=models.PROTECT, related_name='jobs')
    subscription = models.ForeignKey(DeviceSubscription, null=True, blank=True, on_delete=models.PROTECT, related_name='jobs')
    kind = models.CharField(max_length=24)
    revision = models.PositiveIntegerField()
    idempotency_key = models.CharField(max_length=64, unique=True)
    request_fingerprint = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    state = models.CharField(max_length=24, default='queued')
    attempts = models.PositiveIntegerField(default=0)
    lease_id = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    result_code = models.CharField(max_length=80, blank=True)
    receipt = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)


class LineRateVersion(models.Model):
    """只追加的倍率版本；不为迁入的真实线路虚构倍率。"""
    line = models.ForeignKey(Line, on_delete=models.PROTECT, related_name='rate_versions')
    multiplier = models.DecimalField(max_digits=12, decimal_places=6)
    effective_at = models.DateTimeField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['effective_at', 'pk']
        constraints = [models.UniqueConstraint(fields=['line', 'effective_at'], name='unique_line_rate_time'),
            models.CheckConstraint(condition=models.Q(multiplier__gt=0), name='positive_line_rate')]


class ServiceRateVersion(models.Model):
    """服务在指定线路入口的授权倍率；旧全局线路倍率保持独立。"""
    entitlement = models.ForeignKey(Entitlement, on_delete=models.PROTECT, related_name='grant_rates')
    line = models.ForeignKey(Line, on_delete=models.PROTECT)
    ingress = models.ForeignKey(Ingress, on_delete=models.PROTECT)
    multiplier = models.DecimalField(max_digits=12, decimal_places=6)
    effective_at = models.DateTimeField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            from django.core.exceptions import ValidationError
            raise ValidationError('授权倍率版本不可覆盖；请追加新版本')
        super().save(*args, **kwargs)

    class Meta:
        ordering = ['effective_at', 'pk']
        constraints = [
            models.UniqueConstraint(fields=['entitlement', 'line', 'ingress', 'effective_at'], name='unique_service_rate_time'),
            models.CheckConstraint(condition=models.Q(multiplier__gt=0), name='positive_service_rate'),
        ]


class IngressUsageSample(models.Model):
    """入口累计样本和未计费原因持久保存；基线、未知与实测零分开。"""
    identity = models.ForeignKey(NodeIdentity, on_delete=models.PROTECT, related_name='ingress_samples')
    entitlement = models.ForeignKey(Entitlement, on_delete=models.PROTECT, related_name='ingress_samples')
    # 冻结非秘密归属锚，防止可变资料把旧累计量套到新入口或服务。
    server = models.ForeignKey(Server, on_delete=models.PROTECT)
    line = models.ForeignKey(Line, on_delete=models.PROTECT)
    ingress = models.ForeignKey(Ingress, on_delete=models.PROTECT)
    subscription = models.ForeignKey(DeviceSubscription, on_delete=models.PROTECT)
    identity_generation = models.PositiveIntegerField()
    core_instance = models.ForeignKey(CoreInstance, on_delete=models.PROTECT)
    epoch = models.CharField(max_length=80)
    sequence = models.PositiveBigIntegerField()
    upload_bytes = models.PositiveBigIntegerField()
    download_bytes = models.PositiveBigIntegerField()
    observed_at = models.DateTimeField()
    received_at = models.DateTimeField(auto_now_add=True)
    interval_start = models.DateTimeField(null=True, blank=True)
    fingerprint = models.CharField(max_length=64)
    coverage_verified = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=[
        ('baseline', '起始基线'), ('accepted', '区间已入账'), ('gap', '区间缺口'), ('late', '迟到样本')])
    reason_code = models.CharField(max_length=40, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['identity', 'epoch', 'sequence'], name='unique_ingress_sample')]
        indexes = [models.Index(fields=['entitlement', 'observed_at'], name='ingress_service_time')]


class CapacityPool(models.Model):
    """独立供应商流量包或规划额度，与用户套餐完全分账。"""
    name = models.CharField(max_length=100)
    owner = models.CharField(max_length=120, blank=True)
    server = models.ForeignKey(Server, null=True, blank=True, on_delete=models.PROTECT)
    provider = models.CharField(max_length=100, blank=True)
    capacity_mode = models.CharField(max_length=12, default='unknown', choices=[
        ('unknown', '未知'), ('limited', '有限容量'), ('unlimited', '不限量')])
    planned_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    original_unit = models.CharField(max_length=20, default='GB')
    accounting_basis = models.CharField(max_length=120, blank=True)
    source_priority = models.JSONField(default=list)
    stale_after_seconds = models.PositiveIntegerField(default=21600)
    safety_bytes = models.PositiveBigIntegerField(default=0)
    warning_percent = models.PositiveSmallIntegerField(default=70)
    critical_percent = models.PositiveSmallIntegerField(default=85)
    urgent_percent = models.PositiveSmallIntegerField(default=95)
    overage_policy = models.CharField(max_length=200, blank=True)
    enabled = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(period_end__gt=models.F('period_start')),
                                               name='positive_capacity_period')]


class LineCapacity(models.Model):
    line = models.ForeignKey(Line, on_delete=models.PROTECT, related_name='capacity_links')
    pool = models.ForeignKey(CapacityPool, on_delete=models.PROTECT, related_name='line_links')
    consumption_factor = models.DecimalField(max_digits=12, decimal_places=6, default=1)
    topology_verified = models.BooleanField(default=False)
    note = models.CharField(max_length=240, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['line', 'pool'], name='unique_line_capacity'),
            models.CheckConstraint(condition=models.Q(consumption_factor__gt=0), name='positive_capacity_factor')]


class CapacitySample(models.Model):
    pool = models.ForeignKey(CapacityPool, on_delete=models.PROTECT, related_name='samples')
    source = models.CharField(max_length=12, choices=[('api', '供应商API'), ('agent', '探针观测'), ('manual', '人工校准')])
    source_key = models.CharField(max_length=120)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    observed_at = models.DateTimeField()
    used_bytes = models.PositiveBigIntegerField()
    reported_capacity_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    quality = models.CharField(max_length=24)
    accounting_basis = models.CharField(max_length=120)
    reason = models.CharField(max_length=240, blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['pool', 'source', 'source_key'], name='unique_capacity_sample')]


class CapacityAdjustment(models.Model):
    pool = models.ForeignKey(CapacityPool, on_delete=models.PROTECT, related_name='adjustments')
    added_bytes = models.PositiveBigIntegerField()
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    reason = models.CharField(max_length=240)
    request_key = models.CharField(max_length=120)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['pool', 'request_key'], name='unique_capacity_adjustment')]


class CapacityAlert(models.Model):
    pool = models.ForeignKey(CapacityPool, on_delete=models.PROTECT, related_name='alerts')
    code = models.CharField(max_length=40)
    severity = models.CharField(max_length=12)
    message = models.CharField(max_length=300)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['pool', 'code'], name='unique_capacity_alert')]


class LegacyServiceBinding(models.Model):
    """旧会员归属的显式核验；不储存额度、凭据或交付正文。"""
    membership = models.OneToOneField(Membership, on_delete=models.PROTECT, related_name='legacy_binding')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='legacy_service_bindings')
    entitlement = models.ForeignKey(Entitlement, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name='legacy_bindings')
    evidence_sha256 = models.CharField(max_length=64)
    revision = models.PositiveIntegerField(default=1)
    membership_revision = models.PositiveIntegerField()
    state = models.CharField(max_length=12, default='unverified', choices=[
        ('unverified', '未核验'), ('verified', '已核验'), ('revoked', '已撤销')])
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.PROTECT, related_name='verified_legacy_bindings')
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        import re
        from django.core.exceptions import ValidationError
        errors = {}
        if not re.fullmatch(r'[a-f0-9]{64}', self.evidence_sha256 or ''):
            errors['evidence_sha256'] = '核验依据必须是64位小写SHA256摘要。'
        if self.membership_id:
            member = self.membership
            if self.owner_id != member.user_id:
                errors['owner'] = '绑定归属必须与旧会员一致。'
            if not (member.quota_bytes > 0 or member.grants.exists() or member.status != 'pending'
                    or member.expires_at is not None):
                errors['membership'] = '待开通的注册占位不是服务。'
            if self.state == 'verified' and self.membership_revision != member.revision:
                errors['membership_revision'] = '旧会员来源已变化，请重新核验。'
        if self.entitlement_id and self.entitlement.user_id != self.owner_id:
            errors['entitlement'] = '目标权益必须属于同一账号。'
        if self.state == 'verified' and (self.verified_at is None or self.verified_by_id is None
                or not self.verified_by.is_active or not self.verified_by.is_staff):
            errors['state'] = '有效绑定必须有管理员及核验时间。'
        if errors:
            raise ValidationError(errors)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='legacy_binding_positive_revision'),
            models.CheckConstraint(condition=models.Q(membership_revision__gte=1), name='legacy_binding_source_revision'),
            models.CheckConstraint(condition=models.Q(state__in=['unverified', 'verified', 'revoked']),
                                   name='legacy_binding_known_state'),
        ]


class RuleSource(models.Model):
    """文件来源的稳定标识；与自建规则、组合方案和发布状态分开。"""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=100)
    revision = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=24, default='candidate_unbound',
                             choices=[('candidate_unbound', '候选未绑定')])
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name='created_rule_sources')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', 'id']
        constraints = [
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='rule_source_positive_revision'),
            models.CheckConstraint(condition=models.Q(state='candidate_unbound'), name='rule_source_candidate_state'),
        ]


class RuleSourceVersion(models.Model):
    """仅追加的规范化文件版本；更新来源时保留已保存正文。"""
    source = models.ForeignKey(RuleSource, on_delete=models.PROTECT, related_name='versions')
    revision = models.PositiveIntegerField()
    document = models.JSONField()
    sha256 = models.CharField(max_length=64)
    count = models.PositiveIntegerField()
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                related_name='created_rule_source_versions')
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        if not self._state.adding or (self.pk is not None and type(self).objects.filter(pk=self.pk).exists()):
            raise ValidationError('已保存来源版本不可改写，请新增版本。')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.core.exceptions import ValidationError
        raise ValidationError('来源历史版本不可删除。')

    class Meta:
        ordering = ['-revision']
        constraints = [
            models.UniqueConstraint(fields=['source', 'revision'], name='unique_rule_source_version'),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='rule_source_version_positive'),
            models.CheckConstraint(condition=models.Q(count__lte=500), name='rule_source_version_count_limit'),
        ]


class P8SourceBinding(models.Model):
    """独立 P8 来源的指定本人绑定；没有套餐、节点凭据或秘密链接。"""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    source_instance = models.CharField(max_length=96)
    source_id = models.CharField(max_length=96)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                              related_name='p8_source_bindings')
    legacy_membership = models.OneToOneField(Membership, null=True, blank=True,
                                             on_delete=models.PROTECT, related_name='p8_binding')
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name='verified_p8_bindings')
    enabled = models.BooleanField(default=False)
    state = models.CharField(max_length=12, default='unverified', choices=[
        ('unverified', '未核验'), ('verified', '已核验'), ('revoked', '已撤销')])
    revision = models.PositiveIntegerField(default=1)
    verified_at = models.DateTimeField(null=True, blank=True)
    evidence_sha256 = models.CharField(max_length=64)
    links_sha256 = models.CharField(max_length=64)
    verification_sha256 = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        from django.core.exceptions import ValidationError
        from django.utils import timezone
        from .p8_compat import DIGEST, SOURCE_ID, binding_verification_sha256
        errors = {}
        for name in ('source_instance', 'source_id'):
            if not SOURCE_ID.fullmatch(getattr(self, name) or ''):
                errors[name] = '来源标识必须为明确的字母数字、点、横线或下划线。'
        for name in ('evidence_sha256', 'links_sha256'):
            if not DIGEST.fullmatch(getattr(self, name) or ''):
                errors[name] = '必须提供64位小写SHA256摘要。'
        if self.state == 'verified' and (self.verified_at is None or self.verified_at > timezone.now()
                or self.verified_by_id is None or not self.verified_by.is_active or not self.verified_by.is_staff):
            errors['state'] = '已核验绑定需要当前有效管理员和真实核验时间。'
        if self.state == 'verified' and self.verification_sha256 != binding_verification_sha256(self):
            errors['verification_sha256'] = '归属或来源已变化，必须重新显式核验当前绑定。'
        if self.legacy_membership_id:
            from .p8_entitlement import current_entitlement
            mapped_target = current_entitlement(self) if self.pk else None
            if (self.legacy_membership.user_id != self.owner_id
                    or LegacyServiceBinding.objects.filter(membership_id=self.legacy_membership_id).exists()
                    or Entitlement.objects.filter(user_id=self.owner_id).exclude(
                        pk=mapped_target.pk if mapped_target is not None else None).exists()):
                errors['legacy_membership'] = '会员归属冲突或已有其他权益映射，请先人工核对。'
        if errors:
            raise ValidationError(errors)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['source_instance', 'source_id'], name='unique_p8_source_binding'),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='p8_binding_positive_revision'),
            models.CheckConstraint(condition=models.Q(state__in=['unverified', 'verified', 'revoked']),
                                   name='p8_binding_known_state'),
        ]


class P8EntitlementBinding(models.Model):
    """显式关联原交付与既有套餐；不保存凭据、不创建额度或账期。"""
    p8_binding = models.OneToOneField(P8SourceBinding, on_delete=models.PROTECT,
                                     related_name='entitlement_binding')
    entitlement = models.OneToOneField(Entitlement, on_delete=models.PROTECT,
                                      related_name='p8_entitlement_binding')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                              related_name='p8_entitlement_bindings')
    state = models.CharField(max_length=12, default='unverified', choices=[
        ('unverified', '未核验'), ('verified', '已核验'), ('revoked', '已撤销')])
    revision = models.PositiveIntegerField(default=1)
    snapshot = models.JSONField(default=dict)
    snapshot_sha256 = models.CharField(max_length=64, blank=True, default='')
    evidence_sha256 = models.CharField(max_length=64, blank=True, default='')
    verification_sha256 = models.CharField(max_length=64, blank=True, default='')
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name='verified_p8_entitlements')
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        from django.core.exceptions import ValidationError
        from .p8_entitlement import valid_mapping
        if self.state == 'verified' and not valid_mapping(self):
            raise ValidationError('套餐映射尚未核验或归属证据已经变化。')

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='p8_entitlement_positive_revision'),
            models.CheckConstraint(condition=models.Q(state__in=['unverified', 'verified', 'revoked']),
                                   name='p8_entitlement_known_state'),
        ]

# 规则组合使用独立模块；与资源观测迁移分离，统一由portal注册。
from .rule_policy_models import RulePolicy, RulePolicyVersion, RulePolicyBinding, RulePolicyCandidate
