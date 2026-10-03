"""只新增本地来源及不可变候选版本，不迁入或发布真实规则。"""
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('portal', '0013_legacy_service_binding'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RuleSource',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('public_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('name', models.CharField(max_length=100)),
                ('revision', models.PositiveIntegerField(default=1)),
                ('state', models.CharField(choices=[('candidate_unbound', '候选未绑定')],
                                           default='candidate_unbound', max_length=24)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('creator', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name='created_rule_sources', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-updated_at', 'id'], 'constraints': [
                models.CheckConstraint(condition=models.Q(revision__gte=1), name='rule_source_positive_revision'),
                models.CheckConstraint(condition=models.Q(state='candidate_unbound'), name='rule_source_candidate_state'),
            ]},
        ),
        migrations.CreateModel(
            name='RuleSourceVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('revision', models.PositiveIntegerField()),
                ('document', models.JSONField()),
                ('sha256', models.CharField(max_length=64)),
                ('count', models.PositiveIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('creator', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                              related_name='created_rule_source_versions', to=settings.AUTH_USER_MODEL)),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                             related_name='versions', to='portal.rulesource')),
            ],
            options={'ordering': ['-revision'], 'constraints': [
                models.UniqueConstraint(fields=['source', 'revision'], name='unique_rule_source_version'),
                models.CheckConstraint(condition=models.Q(revision__gte=1), name='rule_source_version_positive'),
                models.CheckConstraint(condition=models.Q(count__lte=500), name='rule_source_version_count_limit'),
            ]},
        ),
    ]
