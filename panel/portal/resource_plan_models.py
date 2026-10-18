"""采购计划的版本与消费证据；不承载个人套餐或流量账本。"""
import uuid

from django.conf import settings
from django.db import models


class ResourcePlanBinding(models.Model):
    meter_id = models.SlugField(max_length=48, unique=True)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    target_kind = models.CharField(max_length=12)
    target_id = models.CharField(max_length=64)
    purchase_binding_key = models.CharField(max_length=64, unique=True)
    source_plan = models.JSONField()
    source_fingerprint = models.CharField(max_length=64)
    revision = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['target_kind', 'target_id'], name='unique_purchase_plan_target')]


class ResourcePlanVersion(models.Model):
    """保存只表示待消费；实际生效必须有对应消费回执。"""
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    binding = models.ForeignKey(ResourcePlanBinding, on_delete=models.PROTECT, related_name='versions')
    revision = models.PositiveIntegerField()
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=100)
    request_fingerprint = models.CharField(max_length=64)
    source_fingerprint = models.CharField(max_length=64)
    operation = models.CharField(max_length=12)
    changes = models.JSONField()
    before = models.JSONField()
    plan = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['binding', 'revision'], name='unique_purchase_plan_version'),
            models.UniqueConstraint(fields=['actor', 'idempotency_key'], name='unique_purchase_plan_request'),
        ]


class ResourcePlanConsumption(models.Model):
    """仅受信定时器回传，HTTP保存不能自行创建消费成功。"""
    version = models.OneToOneField(ResourcePlanVersion, on_delete=models.PROTECT, related_name='consumption')
    receipt_fingerprint = models.CharField(max_length=64)
    plan_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=12, default='consumed', choices=[('consumed', '已消费'), ('rejected', '已拒绝')])
    error_code = models.CharField(max_length=40, blank=True, default='')
    effective_plan_fingerprint = models.CharField(max_length=64)
    state_before_sha256 = models.CharField(max_length=64)
    state_after_sha256 = models.CharField(max_length=64)
    consumed_at = models.DateTimeField()
    recorded_at = models.DateTimeField(auto_now_add=True)
