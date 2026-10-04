"""新增规则组合、不可变版本与本地编译候选；不迁入或发布真实资源。"""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('portal', '0016_inventory_observations'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RulePolicy',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('name', models.CharField(max_length=100)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('creator', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='created_rule_policies', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-updated_at', 'id'],
            },
        ),
        migrations.CreateModel(
            name='RulePolicyBinding',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('service_public_id', models.UUIDField()),
                ('service_source', models.CharField(max_length=32)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('fence', models.PositiveBigIntegerField(default=0)),
                ('publisher', models.CharField(default='p8_local_candidate', max_length=48)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('creator', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='created_policy_bindings', to=settings.AUTH_USER_MODEL)),
                ('policy', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bindings', to='portal.rulepolicy')),
            ],
        ),
        migrations.CreateModel(
            name='RulePolicyCandidate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('fence', models.PositiveBigIntegerField()),
                ('manifest', models.JSONField()),
                ('artifact', models.JSONField()),
                ('sha256', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('binding', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='candidates', to='portal.rulepolicybinding')),
                ('creator', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='created_policy_candidates', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.AddField(
            model_name='rulepolicybinding',
            name='current_candidate',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='portal.rulepolicycandidate'),
        ),
        migrations.CreateModel(
            name='RulePolicyVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('revision', models.PositiveIntegerField()),
                ('document', models.JSONField()),
                ('sha256', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('creator', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='created_policy_versions', to=settings.AUTH_USER_MODEL)),
                ('policy', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='versions', to='portal.rulepolicy')),
            ],
            options={
                'ordering': ['-revision'],
            },
        ),
        migrations.AddField(
            model_name='rulepolicycandidate',
            name='version',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='candidates', to='portal.rulepolicyversion'),
        ),
        migrations.AddConstraint(
            model_name='rulepolicy',
            constraint=models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='rule_policy_positive_revision'),
        ),
        migrations.AddConstraint(
            model_name='rulepolicybinding',
            constraint=models.UniqueConstraint(fields=('service_public_id', 'service_source'), name='unique_service_rule_policy'),
        ),
        migrations.AddConstraint(
            model_name='rulepolicybinding',
            constraint=models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='policy_binding_positive'),
        ),
        migrations.AddConstraint(
            model_name='rulepolicyversion',
            constraint=models.UniqueConstraint(fields=('policy', 'revision'), name='unique_rule_policy_version'),
        ),
        migrations.AddConstraint(
            model_name='rulepolicyversion',
            constraint=models.CheckConstraint(condition=models.Q(('revision__gte', 1)), name='policy_version_positive'),
        ),
        migrations.AddConstraint(
            model_name='rulepolicycandidate',
            constraint=models.UniqueConstraint(fields=('binding', 'fence'), name='unique_policy_candidate_fence'),
        ),
        migrations.AddConstraint(
            model_name='rulepolicycandidate',
            constraint=models.CheckConstraint(condition=models.Q(('fence__gte', 1)), name='policy_candidate_positive_fence'),
        ),
    ]
