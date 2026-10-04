"""规则组合版本及本地编译候选；不表示已发布或客户端已应用。"""
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class RulePolicy(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=100)
    revision = models.PositiveIntegerField(default=1)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_rule_policies')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at', 'id']
        constraints = [models.CheckConstraint(condition=models.Q(revision__gte=1), name='rule_policy_positive_revision')]


class ImmutableRuleRecord(models.Model):
    """应用层仅追加；读取时另核摘要，避免把损坏正文当可信版本。"""
    def save(self, *args, **kwargs):
        if not self._state.adding or (self.pk is not None and type(self).objects.filter(pk=self.pk).exists()):
            raise ValidationError('已保存记录不可改写，请创建新版本。')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('历史记录不可删除。')

    class Meta:
        abstract = True


class RulePolicyVersion(ImmutableRuleRecord):
    policy = models.ForeignKey('portal.RulePolicy', on_delete=models.PROTECT, related_name='versions')
    revision = models.PositiveIntegerField()
    document = models.JSONField()
    sha256 = models.CharField(max_length=64)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_policy_versions')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-revision']
        constraints = [
            models.UniqueConstraint(fields=['policy', 'revision'], name='unique_rule_policy_version'),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='policy_version_positive'),
        ]


class RulePolicyBinding(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    policy = models.ForeignKey('portal.RulePolicy', on_delete=models.PROTECT, related_name='bindings')
    service_public_id = models.UUIDField()
    service_source = models.CharField(max_length=32)
    revision = models.PositiveIntegerField(default=1)
    # 此栅栏只管理本地编译候选；生产发布器的锁与当前版本仍须现场核验。
    fence = models.PositiveBigIntegerField(default=0)
    publisher = models.CharField(max_length=48, default='p8_local_candidate')
    current_candidate = models.ForeignKey('portal.RulePolicyCandidate', null=True, blank=True,
                                          on_delete=models.PROTECT, related_name='+')
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_policy_bindings')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['service_public_id', 'service_source'], name='unique_service_rule_policy'),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name='policy_binding_positive'),
        ]


class RulePolicyCandidate(ImmutableRuleRecord):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    binding = models.ForeignKey('portal.RulePolicyBinding', on_delete=models.PROTECT, related_name='candidates')
    version = models.ForeignKey('portal.RulePolicyVersion', on_delete=models.PROTECT, related_name='candidates')
    fence = models.PositiveBigIntegerField()
    manifest = models.JSONField()
    artifact = models.JSONField()
    sha256 = models.CharField(max_length=64)
    creator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='created_policy_candidates')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        constraints = [
            models.UniqueConstraint(fields=['binding', 'fence'], name='unique_policy_candidate_fence'),
            models.CheckConstraint(condition=models.Q(fence__gte=1), name='policy_candidate_positive_fence'),
        ]
