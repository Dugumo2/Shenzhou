"""只增加旧会员核验映射表，不迁移真实资产或修改账本。"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('portal', '0012_billing_plan'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='LegacyServiceBinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('evidence_sha256', models.CharField(max_length=64)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('membership_revision', models.PositiveIntegerField()),
                ('state', models.CharField(choices=[('unverified', '未核验'), ('verified', '已核验'),
                                                  ('revoked', '已撤销')], default='unverified', max_length=12)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('membership', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                                                    related_name='legacy_binding', to='portal.membership')),
                ('owner', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                           related_name='legacy_service_bindings', to=settings.AUTH_USER_MODEL)),
                ('entitlement', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                                 related_name='legacy_bindings', to='portal.entitlement')),
                ('verified_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                                                  related_name='verified_legacy_bindings', to=settings.AUTH_USER_MODEL)),
            ],
            options={'constraints': [
                models.CheckConstraint(condition=models.Q(revision__gte=1), name='legacy_binding_positive_revision'),
                models.CheckConstraint(condition=models.Q(membership_revision__gte=1), name='legacy_binding_source_revision'),
                models.CheckConstraint(condition=models.Q(state__in=['unverified', 'verified', 'revoked']),
                                       name='legacy_binding_known_state'),
            ]},
        ),
    ]
