# Django 5.2.17 生成并经人工补充唯一回执约束，2026-10-04。

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0015_p8_source_binding'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='InventoryMutationReceipt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('key', models.CharField(max_length=100)),
                ('fingerprint', models.CharField(max_length=64)),
                ('response', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
            options={'constraints': [models.UniqueConstraint(fields=('actor', 'key'), name='unique_inventory_mutation_key')]},
        ),
        migrations.AddField(
            model_name='line',
            name='notes',
            field=models.TextField(blank=True, default='', max_length=1000),
        ),
        migrations.AddField(
            model_name='line',
            name='revision',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='server',
            name='notes',
            field=models.TextField(blank=True, default='', max_length=1000),
        ),
        migrations.AddField(
            model_name='server',
            name='revision',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.CreateModel(
            name='CoreInstance',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('instance_key', models.SlugField(max_length=80)),
                ('name', models.CharField(max_length=100)),
                ('core_type', models.CharField(choices=[('xray', 'Xray'), ('sing_box', 'sing-box'), ('other', '其他')], max_length=24)),
                ('registered_version', models.CharField(blank=True, max_length=80, null=True)),
                ('configuration_owner', models.CharField(max_length=100)),
                ('configuration_version', models.CharField(blank=True, default='', max_length=80)),
                ('notes', models.TextField(blank=True, default='', max_length=1000)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('ingresses', models.ManyToManyField(blank=True, related_name='core_instances', to='portal.ingress')),
                ('server', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='cores', to='portal.server')),
            ],
        ),
        migrations.CreateModel(
            name='CheckResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(max_length=80)),
                ('event_id', models.CharField(max_length=100)),
                ('check_kind', models.CharField(max_length=24)),
                ('observed_at', models.DateTimeField()),
                ('expires_at', models.DateTimeField()),
                ('target_fingerprint', models.CharField(max_length=64)),
                ('result', models.CharField(choices=[('pass', '通过'), ('fail', '失败'), ('timeout', '超时'), ('unknown', '未知')], max_length=12)),
                ('latency_ms', models.FloatField(blank=True, null=True)),
                ('error_stage', models.CharField(blank=True, default='', max_length=24)),
                ('error_code', models.CharField(blank=True, default='', max_length=40)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('egress', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='checks', to='portal.egress')),
                ('ingress', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='checks', to='portal.ingress')),
                ('line', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='checks', to='portal.line')),
                ('server', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='checks', to='portal.server')),
                ('core', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='checks', to='portal.coreinstance')),
            ],
        ),
        migrations.CreateModel(
            name='ServerObservation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(max_length=80)),
                ('event_id', models.CharField(max_length=100)),
                ('observed_at', models.DateTimeField()),
                ('expires_at', models.DateTimeField()),
                ('target_fingerprint', models.CharField(max_length=64)),
                ('metrics', models.JSONField(default=dict)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('server', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='observations', to='portal.server')),
            ],
        ),
        migrations.AddConstraint(
            model_name='coreinstance',
            constraint=models.UniqueConstraint(fields=('server', 'instance_key'), name='unique_core_instance_key'),
        ),
        migrations.AddIndex(
            model_name='checkresult',
            index=models.Index(fields=['line', '-observed_at'], name='line_check_latest'),
        ),
        migrations.AddIndex(
            model_name='checkresult',
            index=models.Index(fields=['server', '-observed_at'], name='server_check_latest'),
        ),
        migrations.AddConstraint(
            model_name='checkresult',
            constraint=models.UniqueConstraint(fields=('source', 'event_id'), name='unique_inventory_check_event'),
        ),
        migrations.AddConstraint(
            model_name='checkresult',
            constraint=models.CheckConstraint(condition=models.Q(('expires_at__gt', models.F('observed_at'))), name='inventory_check_validity'),
        ),
        migrations.AddConstraint(
            model_name='checkresult',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('core__isnull', True), ('egress__isnull', True), ('ingress__isnull', True), ('line__isnull', True), ('server__isnull', False)), models.Q(('core__isnull', True), ('egress__isnull', True), ('ingress__isnull', True), ('line__isnull', False), ('server__isnull', True)), models.Q(('core__isnull', False), ('egress__isnull', True), ('ingress__isnull', True), ('line__isnull', True), ('server__isnull', True)), models.Q(('core__isnull', True), ('egress__isnull', True), ('ingress__isnull', False), ('line__isnull', True), ('server__isnull', True)), models.Q(('core__isnull', True), ('egress__isnull', False), ('ingress__isnull', True), ('line__isnull', True), ('server__isnull', True)), _connector='OR'), name='inventory_check_one_target'),
        ),
        migrations.AddIndex(
            model_name='serverobservation',
            index=models.Index(fields=['server', '-observed_at'], name='server_observation_latest'),
        ),
        migrations.AddConstraint(
            model_name='serverobservation',
            constraint=models.UniqueConstraint(fields=('source', 'event_id'), name='unique_server_observation_event'),
        ),
        migrations.AddConstraint(
            model_name='serverobservation',
            constraint=models.CheckConstraint(condition=models.Q(('expires_at__gt', models.F('observed_at'))), name='server_observation_validity'),
        ),
    ]
