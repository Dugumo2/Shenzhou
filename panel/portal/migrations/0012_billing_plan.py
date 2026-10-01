# Django 5.2.17 于2026-10-01生成；只建计划表，不改旧账本。

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0011_identity_scope_reuse'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='BillingPlan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('next_reset_at', models.DateTimeField()),
                ('anchor_day', models.PositiveSmallIntegerField()),
                ('hour', models.PositiveSmallIntegerField()),
                ('minute', models.PositiveSmallIntegerField()),
                ('revision', models.PositiveIntegerField(default=1)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('cycle', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='active_plans', to='portal.billingcycle')),
                ('entitlement', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='billing_plan', to='portal.entitlement')),
                ('updated_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [models.CheckConstraint(condition=models.Q(('anchor_day__gte', 1), ('anchor_day__lte', 31)), name='billing_anchor_day'), models.CheckConstraint(condition=models.Q(('hour__lte', 23), ('minute__lte', 59)), name='billing_anchor_time')],
            },
        ),
        migrations.CreateModel(
            name='BillingPlanRevision',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('revision', models.PositiveIntegerField()),
                ('expected_revision', models.PositiveIntegerField()),
                ('idempotency_key', models.CharField(max_length=128)),
                ('request_digest', models.CharField(max_length=64)),
                ('before', models.JSONField()),
                ('after', models.JSONField()),
                ('result', models.JSONField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('entitlement', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='billing_revisions', to='portal.entitlement')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('entitlement', 'revision'), name='unique_billing_plan_revision'), models.UniqueConstraint(fields=('entitlement', 'actor', 'idempotency_key'), name='unique_billing_plan_request')],
            },
        ),
    ]
