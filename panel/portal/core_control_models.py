"""核心候选、固定动作队列与实例栅栏；不保存完整配置或凭据。"""
import uuid

from django.conf import settings
from django.db import models


class CoreConfigCandidate(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    core = models.ForeignKey('portal.CoreInstance', on_delete=models.PROTECT, related_name='config_candidates')
    base_revision = models.PositiveIntegerField()
    patch = models.JSONField()
    previous_level = models.CharField(max_length=16, blank=True)
    base_sha256 = models.CharField(max_length=64)
    candidate_sha256 = models.CharField(max_length=64)
    binding_fingerprint = models.CharField(max_length=64)
    artifact_ref = models.CharField(max_length=160)
    state = models.CharField(max_length=24, default='prepared')
    checked_fence = models.PositiveBigIntegerField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    idempotency_key = models.CharField(max_length=80)
    request_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['creator', 'idempotency_key'], name='unique_core_candidate_request'),
            models.CheckConstraint(condition=models.Q(base_revision__gte=1), name='core_candidate_positive_version'),
            models.CheckConstraint(condition=models.Q(state__in=['prepared', 'checked', 'check_failed', 'applied', 'rolled_back']),
                                   name='core_candidate_valid_state'),
        ]


class CoreControlState(models.Model):
    core = models.OneToOneField('portal.CoreInstance', on_delete=models.PROTECT, related_name='control_state')
    fence = models.PositiveBigIntegerField(default=0)
    last_applied_candidate = models.ForeignKey('portal.CoreConfigCandidate', null=True, blank=True,
                                               on_delete=models.PROTECT, related_name='+')


class CoreControlJob(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    core = models.ForeignKey('portal.CoreInstance', on_delete=models.PROTECT, related_name='control_jobs')
    candidate = models.ForeignKey('portal.CoreConfigCandidate', on_delete=models.PROTECT, related_name='jobs')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    action = models.CharField(max_length=12)
    status = models.CharField(max_length=16, default='queued')
    expected_revision = models.PositiveIntegerField()
    fence = models.PositiveBigIntegerField()
    idempotency_key = models.CharField(max_length=80)
    request_sha256 = models.CharField(max_length=64)
    result = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['actor', 'idempotency_key'], name='unique_core_control_request'),
            models.UniqueConstraint(fields=['core'], condition=models.Q(status__in=['queued', 'running', 'unknown']),
                                    name='unique_core_active_job'),
            models.CheckConstraint(condition=models.Q(expected_revision__gte=1, fence__gte=1), name='core_job_positive_version'),
            models.CheckConstraint(condition=models.Q(action__in=['check', 'apply', 'rollback']), name='core_job_valid_action'),
            models.CheckConstraint(condition=models.Q(status__in=['queued', 'running', 'unknown', 'succeeded', 'failed']),
                                   name='core_job_valid_status'),
        ]
        indexes = [models.Index(fields=['status', 'created_at'], name='core_control_queue')]
