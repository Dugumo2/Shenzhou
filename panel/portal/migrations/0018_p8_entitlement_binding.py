"""新增显式P8套餐映射；无数据迁移，不改变现有额度、账期或交付身份。"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0017_rule_policies'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='P8EntitlementBinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('state', models.CharField(choices=[('unverified', '未核验'), ('verified', '已核验'), ('revoked', '已撤销')], default='unverified', max_length=12)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('snapshot', models.JSONField(default=dict)),
                ('snapshot_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('evidence_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('verification_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('entitlement', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='p8_entitlement_binding', to='portal.entitlement')),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='p8_entitlement_bindings', to=settings.AUTH_USER_MODEL)),
                ('p8_binding', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='entitlement_binding', to='portal.p8sourcebinding')),
                ('verified_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='verified_p8_entitlements', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'constraints': [models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='p8_entitlement_positive_revision'), models.CheckConstraint(condition=models.Q(('state__in', ['unverified', 'verified', 'revoked'])), name='p8_entitlement_known_state')],
            },
        ),
    ]
