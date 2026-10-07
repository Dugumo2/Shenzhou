# Django 5.2.17 生成于 2026-10-07；仅增加可空字段与新表，不回填旧账。

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0018_p8_entitlement_binding'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='usageledger',
            name='interval_end',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='usageledger',
            name='interval_start',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='ServiceRateVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('multiplier', models.DecimalField(decimal_places=6, max_digits=12)),
                ('effective_at', models.DateTimeField()),
                ('reason', models.CharField(max_length=240)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('entitlement', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='grant_rates', to='portal.entitlement')),
                ('ingress', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.ingress')),
                ('line', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.line')),
            ],
            options={
                'ordering': ['effective_at', 'pk'],
            },
        ),
        migrations.AddField(
            model_name='usageledger',
            name='grant_rate_version',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='portal.servicerateversion'),
        ),
        migrations.CreateModel(
            name='IngressUsageSample',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('epoch', models.CharField(max_length=80)),
                ('sequence', models.PositiveBigIntegerField()),
                ('upload_bytes', models.PositiveBigIntegerField()),
                ('download_bytes', models.PositiveBigIntegerField()),
                ('observed_at', models.DateTimeField()),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('interval_start', models.DateTimeField(blank=True, null=True)),
                ('fingerprint', models.CharField(max_length=64)),
                ('coverage_verified', models.BooleanField(default=False)),
                ('status', models.CharField(choices=[('baseline', '起始基线'), ('accepted', '区间已入账'), ('gap', '区间缺口'), ('late', '迟到样本')], max_length=16)),
                ('reason_code', models.CharField(blank=True, max_length=40)),
                ('identity_generation', models.PositiveIntegerField()),
                ('server', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.server')),
                ('line', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.line')),
                ('ingress', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.ingress')),
                ('subscription', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.devicesubscription')),
                ('core_instance', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='portal.coreinstance')),
                ('entitlement', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ingress_samples', to='portal.entitlement')),
                ('identity', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='ingress_samples', to='portal.nodeidentity')),
            ],
            options={
                'indexes': [models.Index(fields=['entitlement', 'observed_at'], name='ingress_service_time')],
                'constraints': [models.UniqueConstraint(fields=('identity', 'epoch', 'sequence'), name='unique_ingress_sample')],
            },
        ),
        migrations.AddConstraint(
            model_name='servicerateversion',
            constraint=models.UniqueConstraint(fields=('entitlement', 'line', 'ingress', 'effective_at'), name='unique_service_rate_time'),
        ),
        migrations.AddConstraint(
            model_name='servicerateversion',
            constraint=models.CheckConstraint(condition=models.Q(('multiplier__gt', 0)), name='positive_service_rate'),
        ),
    ]
