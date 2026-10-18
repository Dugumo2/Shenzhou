"""每机独立的先导身份；样本沿用 ServerObservation，不建立第二份账。"""
import uuid

from django.db import models


class AgentRegistration(models.Model):
    machine_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    server = models.OneToOneField('portal.Server', on_delete=models.PROTECT, related_name='agent_registration')
    identity_epoch = models.PositiveIntegerField(default=1)
    token_digest = models.CharField(max_length=64)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(identity_epoch__gte=1),
                                               name='agent_identity_epoch_positive')]
