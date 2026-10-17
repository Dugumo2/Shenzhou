# B批资源增量结构；不修改旧服务与账本，2026-10-17。

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0020_partial_observed_bridge'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ResourceImportBinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(max_length=80)),
                ('resource_kind', models.CharField(choices=[('server', '服务器'), ('core', '核心'), ('ingress', '入口'), ('egress', '出口'), ('line', '线路')], max_length=12)),
                ('source_id', models.CharField(max_length=120)),
                ('target_pk', models.PositiveBigIntegerField()),
                ('source_revision', models.PositiveIntegerField()),
                ('fingerprint', models.CharField(max_length=64)),
            ],
        ),
        migrations.CreateModel(
            name='ResourceImportReceipt',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source', models.CharField(max_length=80)),
                ('revision', models.PositiveIntegerField()),
                ('fingerprint', models.CharField(max_length=64)),
                ('response', models.JSONField(default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.AddField(
            model_name='coreinstance',
            name='evidence_observed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='coreinstance',
            name='evidence_source',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.AddField(
            model_name='coreinstance',
            name='owner_state',
            field=models.CharField(choices=[('unknown', '未知'), ('observed_only', '仅观察到'), ('verified', '已核验')], default='unknown', max_length=16),
        ),
        migrations.AddField(
            model_name='coreinstance',
            name='service_unit',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='egress',
            name='endpoint_host',
            field=models.CharField(blank=True, max_length=253, null=True),
        ),
        migrations.AddField(
            model_name='egress',
            name='endpoint_port',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='egress',
            name='evidence_observed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='egress',
            name='evidence_source',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.AddField(
            model_name='egress',
            name='external_kind',
            field=models.CharField(blank=True, choices=[('node', '外部节点'), ('subscription', '上游订阅来源')], max_length=12, null=True),
        ),
        migrations.AddField(
            model_name='egress',
            name='ownership',
            field=models.CharField(choices=[('unknown', '未知'), ('self_owned', '自有'), ('external', '外部')], default='unknown', max_length=12),
        ),
        migrations.AddField(
            model_name='egress',
            name='protocol',
            field=models.CharField(blank=True, max_length=24, null=True),
        ),
        migrations.AddField(
            model_name='egress',
            name='purchase_meter_id',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name='egress',
            name='revision',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='ingress',
            name='endpoint_host',
            field=models.CharField(blank=True, max_length=253, null=True),
        ),
        migrations.AddField(
            model_name='ingress',
            name='endpoint_port',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ingress',
            name='evidence_observed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='ingress',
            name='evidence_source',
            field=models.CharField(blank=True, default='', max_length=120),
        ),
        migrations.AddField(
            model_name='ingress',
            name='revision',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name='server',
            name='purchase_meter_id',
            field=models.CharField(blank=True, max_length=64, null=True),
        ),
        migrations.AddConstraint(
            model_name='egress',
            constraint=models.CheckConstraint(condition=models.Q(('endpoint_port__isnull', True), models.Q(('endpoint_port__gte', 1), ('endpoint_port__lte', 65535)), _connector='OR'), name='egress_valid_endpoint_port'),
        ),
        migrations.AddConstraint(
            model_name='egress',
            constraint=models.CheckConstraint(condition=models.Q(('ownership__in', ['unknown', 'self_owned', 'external'])), name='egress_known_ownership'),
        ),
        migrations.AddConstraint(
            model_name='egress',
            constraint=models.CheckConstraint(condition=models.Q(('external_kind__isnull', True), models.Q(('external_kind__in', ['node', 'subscription']), ('ownership', 'external')), _connector='OR'), name='egress_external_kind_compatible'),
        ),
        migrations.AddConstraint(
            model_name='ingress',
            constraint=models.CheckConstraint(condition=models.Q(('endpoint_port__isnull', True), models.Q(('endpoint_port__gte', 1), ('endpoint_port__lte', 65535)), _connector='OR'), name='ingress_valid_endpoint_port'),
        ),
        migrations.AddConstraint(
            model_name='resourceimportbinding',
            constraint=models.UniqueConstraint(fields=('source', 'resource_kind', 'source_id'), name='unique_resource_import_source'),
        ),
        migrations.AddConstraint(
            model_name='resourceimportbinding',
            constraint=models.UniqueConstraint(fields=('resource_kind', 'target_pk'), name='unique_resource_import_target'),
        ),
        migrations.AddConstraint(
            model_name='resourceimportbinding',
            constraint=models.CheckConstraint(condition=models.Q(('source_revision__gte', 1), ('target_pk__gte', 1)), name='resource_import_positive_version'),
        ),
        migrations.AddConstraint(
            model_name='resourceimportbinding',
            constraint=models.CheckConstraint(condition=models.Q(('resource_kind__in', ['server', 'core', 'ingress', 'egress', 'line'])), name='resource_import_known_kind'),
        ),
        migrations.AddConstraint(
            model_name='resourceimportreceipt',
            constraint=models.UniqueConstraint(fields=('source', 'revision'), name='unique_resource_import_receipt'),
        ),
        migrations.AddConstraint(
            model_name='resourceimportreceipt',
            constraint=models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='resource_receipt_positive_version'),
        ),

        migrations.CreateModel(
            name='ResourceCheckJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('idempotency_key', models.CharField(max_length=100)),
                ('request_fingerprint', models.CharField(max_length=64)),
                ('target_kind', models.CharField(max_length=24)),
                ('target_id', models.CharField(max_length=80)),
                ('target_version', models.CharField(max_length=64)),
                ('action', models.CharField(max_length=24)),
                ('state', models.CharField(choices=[('queued', '排队'), ('running', '执行中'), ('completed', '已结束')], default='queued', max_length=24)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('lease_until', models.DateTimeField(blank=True, null=True)),
                ('failure_code', models.CharField(blank=True, default='', max_length=40)),
                ('actor', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
                ('result', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='portal.checkresult')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('actor', 'idempotency_key'), name='unique_resource_check_request'), models.UniqueConstraint(condition=models.Q(('state__in', ['queued', 'running'])), fields=('target_kind', 'target_id'), name='unique_active_resource_check'), models.CheckConstraint(condition=models.Q(('state__in', ['queued', 'running', 'completed'])), name='resource_check_known_state'), models.CheckConstraint(condition=models.Q(('target_kind__in', ['server', 'core', 'ingress', 'egress', 'line'])), name='resource_check_known_target')],
            },
        ),
        migrations.AddField(
            model_name='egress',
            name='core_instance',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='executed_egresses', to='portal.coreinstance'),
        ),
        migrations.AddField(
            model_name='egress',
            name='next_egress',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='previous_egresses', to='portal.egress'),
        ),
        migrations.AddConstraint(
            model_name='egress',
            constraint=models.CheckConstraint(condition=models.Q(('next_egress__isnull', True), models.Q(('pk', models.F('next_egress')), _negated=True), _connector='OR'), name='egress_no_self_loop'),
        ),
    ]
